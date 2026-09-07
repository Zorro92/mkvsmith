"""Tests for graceful-shutdown temp-file cleanup (SIGINT/SIGTERM).

The signal handlers themselves terminate the process, so these tests cover
the helpers they delegate to: the active-muxer/output registries populated by
MKVCreator, and the kill/cleanup functions the handlers call.
"""

# The helpers under test are private (underscore-prefixed) internals;
# accessing them from tests is intentional.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import models
import pytest

from models import (
    RuntimeState,
    _kill_active_muxers,
    cleanup_temp_dirs,
    finish_progress_line,
    register_active_muxer,
    register_active_output,
    set_progress_active,
    unregister_active_muxer,
    unregister_active_output,
)


@pytest.fixture(autouse=True)
def isolated_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> RuntimeState:
    """Run each test against a fresh injected runtime state."""
    runtime_state = RuntimeState()
    monkeypatch.setattr(models, "RUNTIME_STATE", runtime_state)
    return runtime_state


def test_cleanup_temp_dirs_removes_tracked_files_and_dirs(tmp_path: Path) -> None:
    d = tmp_path / "mkv_scan_x"
    d.mkdir()
    (d / "clip.clpi").write_bytes(b"\x00" * 8)
    f = tmp_path / "partial.tmp"
    f.write_bytes(b"data")

    models.RUNTIME_STATE.cleanup.temp_dirs.append(d)
    models.RUNTIME_STATE.cleanup.temp_files.append(f)
    cleanup_temp_dirs()

    assert not d.exists()
    assert not f.exists()


def test_cleanup_temp_dirs_removes_symlinks_only(tmp_path: Path) -> None:
    target = tmp_path / "target.iso"
    target.write_bytes(b"iso")
    link = tmp_path / "safe.iso"
    link.symlink_to(target)

    models.RUNTIME_STATE.cleanup.symlinks.append(link)
    cleanup_temp_dirs()

    assert not link.exists()
    assert target.exists()  # only the symlink is removed, not its target


def test_cleanup_temp_dirs_ignores_missing_paths() -> None:
    models.RUNTIME_STATE.cleanup.temp_dirs.append(Path("/nonexistent/mkv_scan"))
    models.RUNTIME_STATE.cleanup.temp_files.append(Path("/nonexistent/partial.tmp"))
    cleanup_temp_dirs()  # must not raise


def test_kill_active_muxers_kills_child_and_removes_partial_output(
    tmp_path: Path,
) -> None:
    out = tmp_path / "movie_t01.mkv"
    out.write_bytes(b"partial mux data")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        register_active_muxer(child.pid)
        register_active_output(out)
        _kill_active_muxers()

        assert child.wait(timeout=10) == -signal.SIGKILL
        assert not out.exists()
        assert models.RUNTIME_STATE.active_processes.muxer_pgids == []
        assert models.RUNTIME_STATE.active_processes.output_files == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_register_unregister_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "movie_t01.mkv"
    register_active_muxer(42)
    register_active_output(out)

    unregister_active_muxer(42)
    unregister_active_output(out)
    assert models.RUNTIME_STATE.active_processes.muxer_pgids == []
    assert models.RUNTIME_STATE.active_processes.output_files == []

    # Unregistering an unknown entry is a no-op.
    unregister_active_muxer(42)
    unregister_active_output(out)


def test_finish_progress_line_terminates_active_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_progress_active(True)
    finish_progress_line()
    assert capsys.readouterr().err == "\n"

    # Not active: no newline is written.
    finish_progress_line()
    assert capsys.readouterr().err == ""


# Script run in a subprocess to exercise the real SIGINT handler: it registers
# a fake muxer child (own session, like mkvmerge) plus a partial output and a
# temp file, shows a carriage-return progress line, then SIGINTs itself. The
# handler must kill the child, delete the tracked files, finish the progress
# line, and let the process die from SIGINT.
_SIGINT_CHILD_SCRIPT = r"""
import os, signal, subprocess, sys, time
from pathlib import Path

import models

g = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
out = Path(sys.argv[1])
out.write_bytes(b"partial mux data")
tmp = Path(sys.argv[2])
tmp.write_bytes(b"temp")
Path(sys.argv[3]).write_text(str(g.pid))
models.register_active_muxer(g.pid)
models.register_active_output(out)
models.RUNTIME_STATE.cleanup.temp_files.append(tmp)
models.set_progress_active(True)
sys.stderr.write("\rMuxing 42%")
sys.stderr.flush()
os.kill(os.getpid(), signal.SIGINT)
time.sleep(10)  # reached only if the handler failed to terminate us
"""


def _proc_gone_or_zombie(pid: int) -> bool:
    """True if *pid* no longer exists or is a zombie about to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    # Field 3 of /proc/<pid>/stat is the process state character.
    state = stat.split()[2]
    return state == "Z"


def test_sigint_handler_kills_muxer_and_cleans_up(tmp_path: Path) -> None:
    if not Path("/proc").exists():
        pytest.skip("/proc not available; process-state check is Linux-only")
    out = tmp_path / "movie_t01.mkv"
    tmp = tmp_path / "partial.tmp"
    pidfile = tmp_path / "muxer.pid"

    proc = subprocess.run(
        [sys.executable, "-c", _SIGINT_CHILD_SCRIPT, str(out), str(tmp), str(pidfile)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    gpid = int(pidfile.read_text())
    try:
        # The handler re-raises SIGINT after cleanup, so the process must die
        # from the signal rather than exiting normally.
        assert proc.returncode == -signal.SIGINT
        assert not out.exists()
        assert not tmp.exists()
        # The handler finished the carriage-return progress line.
        assert proc.stderr.endswith("\n")

        # The fake muxer runs in its own session; only the handler's killpg
        # can have stopped it. Poll briefly for the kernel/init to reap the
        # zombie (a surviving process stays in state 'S').
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _proc_gone_or_zombie(gpid):
            time.sleep(0.05)
        assert _proc_gone_or_zombie(gpid), "muxer child survived SIGINT"
    finally:
        if not _proc_gone_or_zombie(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass


def test_cleanup_temp_dirs_removes_nested_contents(tmp_path: Path) -> None:
    directory = tmp_path / "mkv_scan_nested"
    nested = directory / "BDMV" / "STREAM"
    nested.mkdir(parents=True)
    (nested / "clip.m2ts").write_bytes(b"video")

    models.RUNTIME_STATE.cleanup.temp_dirs.append(directory)
    (directory / "partial.tmp").write_bytes(b"data")
    models.RUNTIME_STATE.cleanup.temp_files.append(directory / "partial.tmp")
    cleanup_temp_dirs()

    assert not directory.exists()


def test_cleanup_failed_unmount_preserves_mountpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        models.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=1)
        ),
    )
    models.RUNTIME_STATE.cleanup.direct_mounts.append(mountpoint)

    cleanup_temp_dirs(interrupt=True)

    assert commands == [["sudo", "-n", "umount", str(mountpoint)]]
    assert mountpoint.exists()


def test_cleanup_successful_interrupt_unmount_removes_mountpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        models.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )
    models.RUNTIME_STATE.cleanup.direct_mounts.append(mountpoint)

    cleanup_temp_dirs(interrupt=True)

    assert commands == [["sudo", "-n", "umount", str(mountpoint)]]
    assert not mountpoint.exists()
