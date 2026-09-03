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
from collections.abc import Iterator
from pathlib import Path

import pytest

from models import (
    _ACTIVE_MUXER_PGIDS,
    _ACTIVE_OUTPUT_FILES,
    _SYMLINK_CLEANUP,
    _TEMP_DIRS,
    _TEMP_FILES,
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
def reset_cleanup_state() -> Iterator[None]:
    """Isolate tests from the shared global cleanup registries."""
    _ACTIVE_MUXER_PGIDS.clear()
    _ACTIVE_OUTPUT_FILES.clear()
    _SYMLINK_CLEANUP.clear()
    _TEMP_DIRS.clear()
    _TEMP_FILES.clear()
    set_progress_active(False)
    yield
    _ACTIVE_MUXER_PGIDS.clear()
    _ACTIVE_OUTPUT_FILES.clear()
    _SYMLINK_CLEANUP.clear()
    _TEMP_DIRS.clear()
    _TEMP_FILES.clear()
    set_progress_active(False)


def test_cleanup_temp_dirs_removes_tracked_files_and_dirs(tmp_path: Path) -> None:
    d = tmp_path / "mkv_scan_x"
    d.mkdir()
    (d / "clip.clpi").write_bytes(b"\x00" * 8)
    f = tmp_path / "partial.tmp"
    f.write_bytes(b"data")

    _TEMP_DIRS.append(d)
    _TEMP_FILES.append(f)
    cleanup_temp_dirs()

    assert not d.exists()
    assert not f.exists()


def test_cleanup_temp_dirs_removes_symlinks_only(tmp_path: Path) -> None:
    target = tmp_path / "target.iso"
    target.write_bytes(b"iso")
    link = tmp_path / "safe.iso"
    link.symlink_to(target)

    _SYMLINK_CLEANUP.append(link)
    cleanup_temp_dirs()

    assert not link.exists()
    assert target.exists()  # only the symlink is removed, not its target


def test_cleanup_temp_dirs_ignores_missing_paths() -> None:
    _TEMP_DIRS.append(Path("/nonexistent/mkv_scan"))
    _TEMP_FILES.append(Path("/nonexistent/partial.tmp"))
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
        assert _ACTIVE_MUXER_PGIDS == []
        assert _ACTIVE_OUTPUT_FILES == []
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
    assert _ACTIVE_MUXER_PGIDS == []
    assert _ACTIVE_OUTPUT_FILES == []

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
models._TEMP_FILES.append(tmp)
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
