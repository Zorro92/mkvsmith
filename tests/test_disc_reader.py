"""Tests for source-type detection and disc-image probing helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mkvsmith import disc_reader


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


def _write_sector16(path: Path, ident: bytes) -> None:
    data = bytearray(17 * 2048)
    data[16 * 2048] = 1
    data[16 * 2048 + 1 : 16 * 2048 + 6] = ident
    path.write_bytes(data)


@pytest.mark.parametrize("ident", [b"CD001", b"BEA01", b"NSR02", b"NSR03", b"TEA01"])
def test_probe_accepts_iso9660_and_udf_images(tmp_path: Path, ident: bytes) -> None:
    # Blu-ray images are often UDF-only (BEA01 at sector 16, no ISO9660
    # bridge); the probe must accept them, not just CD001 discs.
    iso = tmp_path / "disc.iso"
    _write_sector16(iso, ident)

    assert disc_reader._probe_has_disc_image_fs(iso) is True


def test_probe_rejects_garbage_and_missing_files(tmp_path: Path) -> None:
    iso = tmp_path / "disc.iso"
    _write_sector16(iso, b"XXXXX")

    assert disc_reader._probe_has_disc_image_fs(iso) is False
    assert disc_reader._probe_has_disc_image_fs(tmp_path / "absent.iso") is False
