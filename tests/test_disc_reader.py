"""Tests for low-level ISO extraction process handling."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import disc_reader
from models import UserPrompts


class _FakeStdout:
    def __init__(self, chunks: list[bytes] | BaseException):
        self.chunks = chunks

    def read(self, _size: int) -> bytes:
        if isinstance(self.chunks, BaseException):
            raise self.chunks
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class _FakePopen:
    def __init__(self, stdout: _FakeStdout | None):
        self.stdout = stdout
        self.pid = 12345
        self.killed = False
        self.waited = False
        self.temp_files: list[Path] = []

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> None:
        if not self.killed:
            raise AssertionError("process was not killed before wait")
        self.waited = True


def prepare_extraction(
    monkeypatch: Any, tmp_path: Path, stdout: _FakeStdout | None
) -> _FakePopen:
    process = _FakePopen(stdout)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        disc_reader, "_get_safe_7z_path", lambda source, symlinks=None: (source, None)
    )
    monkeypatch.setattr(
        disc_reader.subprocess, "Popen", lambda *_args, **_kwargs: process
    )
    return process


def test_partial_extraction_stops_process_and_removes_temp_on_read_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    process = prepare_extraction(
        monkeypatch, tmp_path, _FakeStdout(OSError("pipe failed"))
    )

    result = disc_reader._extract_partial_7z(
        tmp_path / "movie.iso", "clip.m2ts", temp_files=process.temp_files
    )

    assert result is None
    assert process.killed is True
    assert process.waited is True
    assert list(tmp_path.iterdir()) == []
    assert len(process.temp_files) == 1
    assert not process.temp_files[0].exists()


def test_partial_extraction_stops_process_when_stdout_is_missing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    process = prepare_extraction(monkeypatch, tmp_path, None)

    result = disc_reader._extract_partial_7z(
        tmp_path / "movie.iso", "clip.m2ts", temp_files=process.temp_files
    )

    assert result is None
    assert process.killed is True
    assert process.waited is True
    assert list(tmp_path.iterdir()) == []
    assert len(process.temp_files) == 1
    assert not process.temp_files[0].exists()


def test_partial_extraction_reads_bounded_prefix(
    monkeypatch: Any, tmp_path: Path
) -> None:
    first = b"a" * (1024 * 1024)
    second = b"b" * (1024 * 1024)
    process = prepare_extraction(monkeypatch, tmp_path, _FakeStdout([first, second]))

    result = disc_reader._extract_partial_7z(
        tmp_path / "movie.iso",
        "clip.m2ts",
        size_mb=1,
        temp_files=process.temp_files,
    )

    assert result is not None
    assert result.read_bytes() == first
    assert process.killed is True
    assert process.waited is True
    assert process.temp_files == [result]


def prepare_direct_mount(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    mountpoint: Path,
    confirmed: bool = True,
):
    calls: list[tuple[Path, Path]] = []

    def make_mountpoint(*_args, **_kwargs):
        mountpoint.mkdir()
        return str(mountpoint)

    def run_direct_mount(iso_path: Path, candidate: Path):
        calls.append((iso_path, candidate))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        disc_reader, "_confirm_direct_mount", lambda _iso_path, *a, **k: confirmed
    )
    monkeypatch.setattr(disc_reader, "_IS_LINUX", True)
    monkeypatch.setattr(disc_reader.tempfile, "mkdtemp", make_mountpoint)
    monkeypatch.setattr(disc_reader, "_run_direct_mount", run_direct_mount)
    return calls


def test_direct_mount_success_registers_injected_mountpoint(
    monkeypatch: Any, tmp_path: Path
) -> None:
    iso_path = tmp_path / "movie.iso"
    mountpoint = tmp_path / "mount"
    direct_mounts: list[Path] = []
    calls = prepare_direct_mount(
        monkeypatch, tmp_path, mountpoint=mountpoint, confirmed=True
    )

    result = disc_reader._try_direct_mount(iso_path, direct_mounts=direct_mounts)

    assert result == mountpoint
    assert direct_mounts == [mountpoint]
    assert calls == [(iso_path, mountpoint)]


def test_direct_mount_decline_does_not_run_sudo(
    monkeypatch: Any, tmp_path: Path
) -> None:
    mountpoint = tmp_path / "mount"
    prepare_direct_mount(monkeypatch, tmp_path, mountpoint=mountpoint, confirmed=False)

    result = disc_reader._try_direct_mount(tmp_path / "movie.iso", direct_mounts=[])

    assert result is None
    assert not mountpoint.exists()


def test_direct_mount_failure_removes_empty_mountpoint(
    monkeypatch: Any, tmp_path: Path
) -> None:
    iso_path = tmp_path / "movie.iso"
    mountpoint = tmp_path / "mount"
    prepare_direct_mount(monkeypatch, tmp_path, mountpoint=mountpoint)
    monkeypatch.setattr(
        disc_reader,
        "_run_direct_mount",
        lambda _iso_path, _mountpoint: SimpleNamespace(
            returncode=1, stdout="", stderr="mount failed"
        ),
    )
    direct_mounts: list[Path] = []

    result = disc_reader._try_direct_mount(iso_path, direct_mounts=direct_mounts)

    assert result is None
    assert not mountpoint.exists()
    assert direct_mounts == []


def test_direct_mount_exception_removes_empty_mountpoint(
    monkeypatch: Any, tmp_path: Path
) -> None:
    iso_path = tmp_path / "movie.iso"
    mountpoint = tmp_path / "mount"
    prepare_direct_mount(monkeypatch, tmp_path, mountpoint=mountpoint)

    def fail_mount(_iso_path: Path, _mountpoint: Path):
        raise subprocess.TimeoutExpired(cmd="sudo mount", timeout=60)

    monkeypatch.setattr(disc_reader, "_run_direct_mount", fail_mount)
    direct_mounts: list[Path] = []

    result = disc_reader._try_direct_mount(iso_path, direct_mounts=direct_mounts)

    assert result is None
    assert not mountpoint.exists()
    assert direct_mounts == []


def test_direct_mount_skipped_on_non_linux_platforms(
    monkeypatch: Any, tmp_path: Path
) -> None:
    iso_path = tmp_path / "movie.iso"
    mountpoint = tmp_path / "mount"
    prepare_direct_mount(monkeypatch, tmp_path, mountpoint=mountpoint)
    monkeypatch.setattr(disc_reader, "_IS_LINUX", False)

    result = disc_reader._try_direct_mount(iso_path, direct_mounts=[])

    assert result is None
    assert not mountpoint.exists()


def test_confirm_direct_mount_uses_injected_prompts(tmp_path: Path) -> None:
    iso = tmp_path / "movie.iso"
    assert (
        disc_reader._confirm_direct_mount(
            iso, UserPrompts(confirm=lambda _message: True)
        )
        is True
    )
    assert (
        disc_reader._confirm_direct_mount(
            iso, UserPrompts(confirm=lambda _message: False)
        )
        is False
    )


def test_try_direct_mount_uses_injected_prompts_without_stdin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The scan path answers via hooks instead of reading stdin."""
    iso_path = tmp_path / "movie.iso"
    mountpoint = tmp_path / "mount"

    def make_mountpoint(*_args: Any, **_kwargs: Any) -> str:
        mountpoint.mkdir()
        return str(mountpoint)

    ran: list[tuple[Path, Path]] = []

    def run_direct_mount(iso: Path, candidate: Path) -> SimpleNamespace:
        ran.append((iso, candidate))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(disc_reader, "_IS_LINUX", True)
    monkeypatch.setattr(disc_reader.tempfile, "mkdtemp", make_mountpoint)
    monkeypatch.setattr(disc_reader, "_run_direct_mount", run_direct_mount)
    monkeypatch.setattr(
        builtins, "input", lambda _p: (_ for _ in ()).throw(AssertionError())
    )

    result = disc_reader._try_direct_mount(
        iso_path,
        direct_mounts=[],
        prompts=UserPrompts(confirm=lambda _message: True),
    )

    assert result == mountpoint
    assert ran == [(iso_path, mountpoint)]


def test_parse_7z_listing_returns_all_regular_files() -> None:
    stdout = "\n".join(
        [
            "Path = /archive.iso",
            "Type = Udf",
            "Path = BDMV",
            "Folder = +",
            "Path = BDMV/STREAM/001.M2TS",
            "Folder = -",
            "Size = 123",
            "Path = BDMV/BACKUP/BDJO/00000.bdjo",
            "Folder = -",
            "Size = 999",
            "Path = BDMV/PLAYLIST/00800.mpls",
            "Folder = -",
            "Size = not-a-number",
            "Path = VIDEO_TS/VTS_01_0.IFO",
            "Folder = -",
            "Size = 456",
        ]
    )

    paths, sizes = disc_reader._parse_7z_listing(stdout)

    assert paths == [
        "BDMV/STREAM/001.M2TS",
        "BDMV/BACKUP/BDJO/00000.bdjo",
        "BDMV/PLAYLIST/00800.mpls",
        "VIDEO_TS/VTS_01_0.IFO",
    ]
    assert sizes == {
        "BDMV/STREAM/001.M2TS": 123,
        "BDMV/BACKUP/BDJO/00000.bdjo": 999,
        "VIDEO_TS/VTS_01_0.IFO": 456,
    }


def test_is_iso_media_path_matches_compatible_scanning_subset() -> None:
    accepted = (
        "BDMV/STREAM/movie.m2ts",
        "BDMV/PLAYLIST/movie.mpls",
        "BDMV/CLIPINF/movie.clpi",
        "VIDEO_TS/VTS_01_0.IFO",
        "BDMV/META/DL/bdmt_en.xml",
    )
    rejected = (
        "BDMV/index.bdmv",
        "BDMV/BACKUP/BDJO/movie.bdjo",
        "outside/movie.m2ts",
    )

    assert all(disc_reader._is_iso_media_path(path) for path in accepted)
    assert not any(disc_reader._is_iso_media_path(path) for path in rejected)


def test_run_7z_listing_uses_expected_arguments(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(disc_reader.subprocess, "run", run)

    result = disc_reader._run_7z_listing(Path("safe.iso"))

    assert result.returncode == 0
    assert calls == [
        (
            ["7z", "l", "-slt", "safe.iso"],
            {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 120,
            },
        )
    ]


def test_copy_bounded_stdout_stops_after_limit_chunk() -> None:
    first = b"a" * (1024 * 1024)
    second = b"b" * (1024 * 1024)
    stdout = _FakeStdout([first, second])
    output = bytearray()

    class _Output:
        def write(self, data: bytes) -> None:
            output.extend(data)

    disc_reader._copy_bounded_stdout(stdout, _Output(), len(first))

    assert bytes(output) == first
    assert stdout.chunks == [second]


def test_start_7z_pipe_uses_expected_arguments(monkeypatch: Any) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return object.__new__(_FakePopen)

    monkeypatch.setattr(disc_reader.subprocess, "Popen", popen)

    process = disc_reader._start_7z_pipe(Path("safe.iso"), "BDMV/STREAM/1.m2ts")

    assert isinstance(process, _FakePopen)
    assert calls == [
        (
            ["7z", "e", "-so", "safe.iso", "BDMV/STREAM/1.m2ts"],
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


def test_directory_source_type_detects_disc_structures_and_media(
    tmp_path: Path,
) -> None:
    def source(name: str) -> Path:
        return tmp_path / name

    (source("dvd") / "VIDEO_TS").mkdir(parents=True)
    (source("bluray-upper") / "BDMV").mkdir(parents=True)
    (source("bluray-lower") / "bdmv").mkdir(parents=True)
    raw_bluray = source("raw-bluray") / "STREAM"
    raw_bluray.mkdir(parents=True)
    (raw_bluray / "movie.m2ts").write_bytes(b"m2ts")
    raw_dvd = source("raw-dvd") / "VIDEO"
    raw_dvd.mkdir(parents=True)
    (raw_dvd / "movie.vob").write_bytes(b"vob")
    iso_dir = source("iso-dir") / "nested"
    iso_dir.mkdir(parents=True)
    (iso_dir / "movie.iso").write_bytes(b"iso")
    source("empty").mkdir()

    assert (
        disc_reader._directory_source_type(source("dvd")) == disc_reader.SourceType.DVD
    )
    assert (
        disc_reader._directory_source_type(source("bluray-upper"))
        == disc_reader.SourceType.BLURAY
    )
    assert (
        disc_reader._directory_source_type(source("bluray-lower"))
        == disc_reader.SourceType.BLURAY
    )
    assert (
        disc_reader._directory_source_type(source("raw-bluray"))
        == disc_reader.SourceType.BLURAY_RAW
    )
    assert (
        disc_reader._directory_source_type(source("raw-dvd"))
        == disc_reader.SourceType.DVD_RAW
    )
    assert (
        disc_reader._directory_source_type(source("iso-dir"))
        == disc_reader.SourceType.ISO_UNKNOWN
    )
    assert disc_reader._directory_source_type(source("empty")) is None


def test_detect_source_type_prefers_directory_layout_over_media(
    tmp_path: Path,
) -> None:
    source = tmp_path / "disc"
    video_ts = source / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    stream = source / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "movie.m2ts").write_bytes(b"m2ts")

    assert disc_reader.detect_source_type(source) == disc_reader.SourceType.DVD


def test_file_source_type_matches_iso_and_video_extensions(
    tmp_path: Path,
) -> None:
    for extension in disc_reader._VIDEO_FILE_EXTENSIONS:
        video = tmp_path / f"movie{extension}"
        video.write_bytes(b"video")
        assert disc_reader._file_source_type(video) == disc_reader.SourceType.VIDEO_FILE

    iso = tmp_path / "MOVIE.ISO"
    iso.write_bytes(b"iso")
    assert disc_reader._file_source_type(iso) == disc_reader.SourceType.ISO_UNKNOWN
    text_file = tmp_path / "movie.txt"
    text_file.write_bytes(b"text")
    assert disc_reader._file_source_type(text_file) is None


def test_detect_source_type_matches_files_devices_and_unknown(
    tmp_path: Path,
) -> None:
    video = tmp_path / "movie.MKV"
    video.write_bytes(b"video")
    assert disc_reader.detect_source_type(video) == disc_reader.SourceType.VIDEO_FILE
    assert (
        disc_reader.detect_source_type(Path("missing.txt"))
        == disc_reader.SourceType.UNKNOWN
    )

    if os.name == "posix":
        # Path("/dev/...") normalises to backslashes on Windows, where such
        # nodes do not exist; assert the POSIX spelling only where it is real.
        assert (
            disc_reader.detect_source_type(Path("/dev/nonexistent-optical"))
            == disc_reader.SourceType.DEVICE
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX /dev block-device nodes")
def test_is_device_path_matches_posix_device_nodes() -> None:
    assert disc_reader._is_device_path(Path("/dev/sr0"))
    assert disc_reader._is_device_path(Path("/dev/disk4"))


def test_is_device_path_matches_windows_drive_paths() -> None:
    # Windows bare drive letters.
    assert disc_reader._is_device_path(Path("Q:"))
    assert disc_reader._is_device_path(Path("Z:/"))
    assert disc_reader._is_device_path(Path("q:\\"))
    # Regular files, directories, and near-misses are not devices.
    assert not disc_reader._is_device_path(Path("movie.iso"))
    assert not disc_reader._is_device_path(Path("Q:/VIDEO_TS"))
    assert not disc_reader._is_device_path(Path("EQ:"))


def test_detect_source_type_matches_windows_drive_paths() -> None:
    assert disc_reader.detect_source_type(Path("Q:")) == disc_reader.SourceType.DEVICE
    assert disc_reader.detect_source_type(Path("Z:/")) == disc_reader.SourceType.DEVICE


def _isolated_link_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point per-process link state at tmp_path (no global leakage)."""
    import tempfile

    monkeypatch.setattr(disc_reader, "_SAFE_LINK_DIR", None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_safe_7z_path_leaves_clean_names_alone(tmp_path: Path) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"fake")

    assert disc_reader._get_safe_7z_path(iso) == (iso, None)
    assert list(tmp_path.iterdir()) == [iso]


def test_safe_7z_path_links_inside_private_temp_not_media_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_link_dir(monkeypatch, tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    iso = media / "Movie (2020).iso"
    iso.write_bytes(b"fake")
    registry: list[Path] = []

    target, link = disc_reader._get_safe_7z_path(iso, registry)

    assert link is not None
    assert target == link
    assert link.parent != media
    assert link.resolve() == iso.resolve()
    # The media folder sees no stray symlink (folder watchers stay quiet).
    assert list(media.iterdir()) == [iso]
    assert registry == [link]


def test_safe_7z_path_registers_globally_without_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import models

    _isolated_link_dir(monkeypatch, tmp_path)
    state = models.RuntimeState()
    monkeypatch.setattr(disc_reader, "RUNTIME_STATE", state)
    iso = tmp_path / "Movie (2020).iso"
    iso.write_bytes(b"fake")

    _, link = disc_reader._get_safe_7z_path(iso)

    assert link is not None
    assert state.cleanup.symlinks == [link]


def test_safe_7z_path_uniquifies_colliding_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_link_dir(monkeypatch, tmp_path)
    first = tmp_path / "a" / "Movie (2020).iso"
    second = tmp_path / "b" / "Movie (2020).iso"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    target_a, link_a = disc_reader._get_safe_7z_path(first, [])
    target_b, link_b = disc_reader._get_safe_7z_path(second, [])

    assert link_a is not None and link_b is not None
    assert target_a != target_b
    assert link_a.resolve() == first.resolve()
    assert link_b.resolve() == second.resolve()


def test_safe_7z_path_reuses_link_for_same_iso(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_link_dir(monkeypatch, tmp_path)
    iso = tmp_path / "Movie (2020).iso"
    iso.write_bytes(b"fake")

    assert disc_reader._get_safe_7z_path(iso, []) == disc_reader._get_safe_7z_path(
        iso, []
    )


def test_safe_7z_path_falls_back_when_no_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tempfile

    monkeypatch.setattr(disc_reader, "_SAFE_LINK_DIR", None)
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    iso = tmp_path / "Movie (2020).iso"
    iso.write_bytes(b"fake")

    assert disc_reader._get_safe_7z_path(iso, []) == (iso, None)
