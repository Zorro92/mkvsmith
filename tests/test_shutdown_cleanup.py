"""Tests for graceful-shutdown temp-file cleanup (SIGINT/SIGTERM/SIGHUP).

The signal handlers themselves terminate the process, so these tests cover
the helpers they delegate to: the active-muxer/output registries populated by
MKVCreator, and the kill/cleanup functions the handlers call.
"""

# The helpers under test are private (underscore-prefixed) internals;
# accessing them from tests is intentional.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from mkvsmith import models
import pytest

from mkvsmith.models import (
    RuntimeState,
    _SIGKILL,
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

        if os.name == "posix":
            # POSIX Popen.wait() reports death-by-signal as a negative code.
            assert child.wait(timeout=10) == -_SIGKILL
        else:
            # Windows os.kill(pid, 9) terminates via TerminateProcess, which
            # reports the passed value back as the (positive) exit code.
            assert child.wait(timeout=10) == _SIGKILL
        assert not out.exists()
        assert models.RUNTIME_STATE.active_processes.muxer_pgids == []
        assert models.RUNTIME_STATE.active_processes.output_files == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX-only")
def test_kill_process_group_falls_back_to_current_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killpg_calls: list[tuple[int, int]] = []

    def killpg(process_id: int, sig: int) -> None:
        killpg_calls.append((process_id, sig))
        if len(killpg_calls) == 1:
            # First attempt fails (e.g. PID was reused / permission error).
            raise OSError(process_id)

    monkeypatch.setattr(models.os, "killpg", killpg)

    def fake_getpgid(_pid: int) -> int:
        return 432

    monkeypatch.setattr(models.os, "getpgid", fake_getpgid)

    models._kill_process_group(123)

    assert killpg_calls == [
        (123, _SIGKILL),
        (432, _SIGKILL),
    ]


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

from mkvsmith import models

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
os.kill(os.getpid(), getattr(signal, sys.argv[4]))
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


@pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_signal_handler_kills_muxer_and_cleans_up(tmp_path: Path, signame: str) -> None:
    if not Path("/proc").exists():
        pytest.skip("/proc not available; process-state check is Linux-only")
    signum = getattr(signal, signame)
    out = tmp_path / "movie_t01.mkv"
    tmp = tmp_path / "partial.tmp"
    pidfile = tmp_path / "muxer.pid"

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _SIGINT_CHILD_SCRIPT,
            str(out),
            str(tmp),
            str(pidfile),
            signame,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    gpid = int(pidfile.read_text())
    try:
        # The handler re-raises the signal after cleanup, so the process must
        # die from it rather than exiting normally.
        assert proc.returncode == -signum
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
        assert _proc_gone_or_zombie(gpid), f"muxer child survived {signame}"
    finally:
        if not _proc_gone_or_zombie(gpid):
            try:
                # _SIGKILL rather than signal.SIGKILL: the Windows signal
                # module does not define SIGKILL (this test skips there, but
                # type checking still resolves the reference).
                os.kill(gpid, _SIGKILL)
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


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on Windows")
def test_sighup_left_ignored_under_nohup() -> None:
    # nohup ignores SIGHUP before exec; importing models must not undo that,
    # or a detached rip would die when its terminal closes.
    script = (
        "import signal; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "from mkvsmith import models; "
        "print(signal.getsignal(signal.SIGHUP) == signal.SIG_IGN)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.stdout.strip() == "True", proc.stderr


def test_cleanup_keeps_temp_dir_holding_a_live_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = tmp_path / "mkvsmith-tmp-1-x"
    mountpoint = session / "mkv_mount_y"
    mountpoint.mkdir(parents=True)

    def is_mountpoint(path: str | Path) -> bool:
        return Path(path) == mountpoint

    monkeypatch.setattr(models.os.path, "ismount", is_mountpoint)
    models.RUNTIME_STATE.cleanup.temp_dirs.append(session)

    cleanup_temp_dirs()

    assert mountpoint.exists()


# =============================================================================
# Session temp dirs and the stale-session sweep
# =============================================================================


def _make_session(base: Path, owner: Mapping[str, object] | None) -> Path:
    directory = base / f"{models.SESSION_DIR_PREFIX}{owner and owner.get('pid')}-abc"
    (directory / "mkv_mux_q").mkdir(parents=True)
    (directory / "mkv_mux_q" / "00800.m2ts").write_bytes(b"x" * 1000)
    if owner is not None:
        (directory / models._SESSION_MARKER).write_text(json.dumps(owner))
    return directory


def _exited_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_session_dir_is_marked_cached_and_registered(tmp_path: Path) -> None:
    cleanup = models.RUNTIME_STATE.cleanup
    session = cleanup.session_dir(tmp_path)
    assert session.parent == tmp_path
    assert session.name.startswith(f"{models.SESSION_DIR_PREFIX}{os.getpid()}-")
    owner = json.loads((session / models._SESSION_MARKER).read_text())
    assert owner["pid"] == os.getpid()
    assert owner["host"] == socket.gethostname()
    assert cleanup.session_dir(tmp_path) == session
    assert cleanup.temp_dirs == [session]


posix_only = pytest.mark.skipif(os.name != "posix", reason="sweep is POSIX-only")


@posix_only
def test_sweep_removes_session_of_exited_owner(tmp_path: Path) -> None:
    host = socket.gethostname()
    stale = _make_session(tmp_path, {"pid": _exited_pid(), "host": host, "start": None})

    marker_size = (stale / models._SESSION_MARKER).stat().st_size

    removed = models.sweep_stale_session_dirs([tmp_path])

    # The reported size covers the 1000-byte clip plus the owner marker.
    assert removed == [(stale, 1000 + marker_size)]
    assert not stale.exists()


@posix_only
def test_sweep_removes_session_whose_pid_was_reused(tmp_path: Path) -> None:
    if models._process_start_token(os.getpid()) is None:
        pytest.skip("process start time unavailable (no /proc)")
    owner = {"pid": os.getpid(), "host": socket.gethostname(), "start": "1"}
    reused = _make_session(tmp_path, owner)

    removed = models.sweep_stale_session_dirs([tmp_path])
    assert [path for path, _size in removed] == [reused]
    assert not reused.exists()


@posix_only
def test_sweep_keeps_live_foreign_unmarked_and_mounted_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = socket.gethostname()
    me = models.create_session_dir(tmp_path)
    other_host = _make_session(
        tmp_path / "a", {"pid": _exited_pid(), "host": "elsewhere", "start": None}
    )
    unmarked = _make_session(tmp_path / "b", None)
    mounted = _make_session(
        tmp_path / "c", {"pid": _exited_pid(), "host": host, "start": None}
    )

    def is_mountpoint(path: str | Path) -> bool:
        return Path(path) == mounted / "mkv_mux_q"

    monkeypatch.setattr(models.os.path, "ismount", is_mountpoint)

    bases = [tmp_path, tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    assert models.sweep_stale_session_dirs(bases) == []
    for directory in (me, other_host, unmarked, mounted):
        assert directory.exists()


@posix_only
def test_sweep_ignores_unrelated_dirs_and_missing_bases(tmp_path: Path) -> None:
    unrelated = tmp_path / "mkv_mux_legacy"
    unrelated.mkdir()
    assert models.sweep_stale_session_dirs([tmp_path, tmp_path / "missing"]) == []
    assert unrelated.exists()
