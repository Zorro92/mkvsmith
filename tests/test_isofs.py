"""Regression tests for the native ISO reader (isofs) and its disc_reader wiring.

Fixtures are sparse sector captures of real images made with
``scripts/capture_iso_fixture.py``: only the sectors the readers touch are
stored, alongside the listing and content hashes the reader produced when it
was cross-checked against 7z on the full image.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import disc_reader
import isofs
from conftest import FIXTURES_DIR

MONSTER_HIGH = "monster_high_udf250.isofix.gz"
TREASURE_PLANET = "treasure_planet_dvd.isofix.gz"


def _build_image(name: str, dest: Path) -> dict[str, Any]:
    """Rebuild a sparse image from fixture *name*; return its JSON header.

    Disc-derived fixtures are not committed (see docs/DEVELOPMENT.md "Disc fixtures"
    section), so tests that need one skip on a fresh clone.
    """
    if not (FIXTURES_DIR / name).exists():
        pytest.skip(
            f"disc fixture {name} not present; capture it locally (see docs/DEVELOPMENT.md)"
        )
    with gzip.open(FIXTURES_DIR / name, "rb") as fixture:
        header: dict[str, Any] = json.loads(fixture.readline())
        with dest.open("wb") as out:
            out.truncate(header["size"])
            while record := fixture.read(4 + isofs.SECTOR):
                out.seek(int.from_bytes(record[:4], "little") * isofs.SECTOR)
                out.write(record[4:])
    return header


def _listing(entries: list[isofs.IsoEntry]) -> list[list[object]]:
    return [
        [e.path, e.size, e.modified.isoformat() if e.modified else None]
        for e in entries
    ]


@pytest.fixture
def monster_high(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "monster high.iso"
    return path, _build_image(MONSTER_HIGH, path)


@pytest.fixture
def treasure_planet(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "Treasure Planet (USA).iso"
    return path, _build_image(TREASURE_PLANET, path)


# =============================================================================
# isofs
# =============================================================================


def test_udf250_metadata_partition_listing(
    monster_high: tuple[Path, dict[str, Any]],
) -> None:
    path, header = monster_high
    with isofs.IsoImage(path) as image:
        assert image.filesystem == "udf"
        assert image.label == "MONSTER_HIGH_WELCOME_TO_MONST_BD"
        assert _listing(image.files()) == header["udf"]["files"]
    assert header["iso9660"] is None  # UDF-only: why pycdlib cannot open it


@pytest.mark.parametrize("fixture_name", ["monster_high", "treasure_planet"])
def test_member_content_matches_capture(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    path, header = request.getfixturevalue(fixture_name)
    with isofs.IsoImage(path) as image:
        for member, (length, digest) in header["content"].items():
            data = image.read(image.entries[member], 0, length)
            assert hashlib.sha256(data).hexdigest() == digest, member


def test_large_m2ts_is_one_contiguous_run(
    monster_high: tuple[Path, dict[str, Any]],
) -> None:
    path, _ = monster_high
    with isofs.IsoImage(path) as image:
        entry = image.entries["BDMV/STREAM/00800.m2ts"]
    # 22.4 GB over 21 UDF extents, physically adjacent and merged into one run.
    assert entry.size == 22_393_516_032
    assert len(entry.runs) == 1


def test_dvd_udf_and_iso9660_trees(
    treasure_planet: tuple[Path, dict[str, Any]],
) -> None:
    path, header = treasure_planet
    with isofs.IsoImage(path) as image:
        assert image.filesystem == "udf"
        assert _listing(image.files()) == header["udf"]["files"]
        iso_entries = isofs._Iso9660Reader(image._file).files()
    assert _listing(iso_entries) == header["iso9660"]["files"]
    udf_sizes = {row[0]: row[1] for row in header["udf"]["files"]}
    assert {e.path: e.size for e in iso_entries} == udf_sizes


def test_falls_back_to_iso9660_without_udf(
    treasure_planet: tuple[Path, dict[str, Any]],
) -> None:
    path, header = treasure_planet
    with path.open("r+b") as f:
        for sector in range(16, 32):
            f.seek(sector * isofs.SECTOR + 1)
            if f.read(5) in (b"NSR02", b"NSR03"):
                f.seek(sector * isofs.SECTOR)
                f.write(bytes(isofs.SECTOR))
    with isofs.IsoImage(path) as image:
        assert image.filesystem == "iso9660"
        assert _listing(image.files()) == header["iso9660"]["files"]


def test_unreadable_image_raises(tmp_path: Path) -> None:
    path = tmp_path / "junk.iso"
    path.write_bytes(bytes(300 * isofs.SECTOR))
    with pytest.raises(isofs.IsoImageError, match="UDF.*ISO9660"):
        isofs.IsoImage(path)


def test_copy_to_honours_limit(
    monster_high: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, header = monster_high
    _, digest = header["content"]["BDMV/STREAM/00800.m2ts"]
    dest = tmp_path / "prefix.m2ts"
    with isofs.IsoImage(path) as image:
        image.copy_to(image.entries["BDMV/STREAM/00800.m2ts"], dest, limit=4096)
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == digest


# =============================================================================
# disc_reader wiring
# =============================================================================


def test_list_iso_files_uses_native_reader(
    monster_high: tuple[Path, dict[str, Any]],
) -> None:
    path, header = monster_high
    paths, sizes = disc_reader._list_iso_files(path)
    assert paths == [row[0] for row in header["udf"]["files"]]
    assert sizes == {row[0]: row[1] for row in header["udf"]["files"]}


def test_unreadable_iso_reports_error_and_returns_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "junk.iso"
    path.write_bytes(bytes(300 * isofs.SECTOR))

    assert disc_reader._list_iso_files(path) == ([], {})
    assert disc_reader._list_iso_file_metadata(path) == []
    assert disc_reader._extract_iso_files(path, ["X"], tmp_path / "out") == []
    assert disc_reader._extract_iso_prefix(path, "X") is None
    assert "Could not read ISO image junk.iso" in capsys.readouterr().out


def test_metadata_timestamps_are_timezone_aware(
    monster_high: tuple[Path, dict[str, Any]],
) -> None:
    path, header = monster_high
    expected = {row[0]: row[2] for row in header["udf"]["files"]}
    for entry in disc_reader._list_iso_file_metadata(path):
        assert entry.modified is not None
        assert entry.modified.utcoffset() is not None
        assert entry.modified.isoformat() == expected[entry.path]


def test_extract_iso_files_flattens_and_skips_missing(
    treasure_planet: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, header = treasure_planet
    out = tmp_path / "out"
    extracted = disc_reader._extract_iso_files(
        path, ["VIDEO_TS/VIDEO_TS.IFO", "VIDEO_TS/NOPE.IFO"], out
    )
    assert extracted == [out / "VIDEO_TS.IFO"]
    _, digest = header["content"]["VIDEO_TS/VIDEO_TS.IFO"]
    assert hashlib.sha256(extracted[0].read_bytes()).hexdigest() == digest


def test_stopping_an_extraction_removes_what_was_extracted(
    treasure_planet: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    path, _header = treasure_planet
    out = tmp_path / "out"
    chunks: list[None] = []

    class Stopped(Exception):
        pass

    def before_chunk() -> None:
        # The IFO is one chunk; stop partway into the VOB.
        if len(chunks) == 3:
            raise Stopped
        chunks.append(None)

    with pytest.raises(Stopped):
        disc_reader._extract_iso_files(
            path,
            ["VIDEO_TS/VIDEO_TS.IFO", "VIDEO_TS/VTS_01_1.VOB"],
            out,
            before_chunk,
        )

    assert list(out.iterdir()) == []


def test_extract_iso_prefix_registers_temp_file(
    treasure_planet: tuple[Path, dict[str, Any]],
) -> None:
    path, header = treasure_planet
    temp_files: list[Path] = []
    result = disc_reader._extract_iso_prefix(
        path, "VIDEO_TS/VTS_01_1.VOB", 1, temp_files=temp_files
    )
    try:
        assert result is not None and temp_files == [result]
        _, digest = header["content"]["VIDEO_TS/VTS_01_1.VOB"]
        assert hashlib.sha256(result.read_bytes()[:4096]).hexdigest() == digest
        assert result.stat().st_size == 1024 * 1024
    finally:
        for temp in temp_files:
            temp.unlink(missing_ok=True)


# =============================================================================
# Opt-in cross-check against 7z on real images
# =============================================================================
# 7z is only a test-time reference here; mkvsmith itself never runs it.


def _parse_7z_slt(listing: str) -> dict[str, int]:
    """``{path: size}`` for the regular files in ``7z l -slt`` output."""
    files: dict[str, int] = {}
    block: dict[str, str] = {}
    for line in [*listing.splitlines(), ""]:
        if " = " in line:
            key, value = line.split(" = ", 1)
            block[key] = value
        elif not line and block:
            if block.get("Folder") == "-" and "Size" in block:
                files[block["Path"].lstrip("/")] = int(block["Size"])
            block = {}
    return files


_REAL_ISOS = [
    Path(p) for p in os.environ.get("MKVSMITH_TEST_ISOS", "").split(os.pathsep) if p
]


@pytest.mark.skipif(
    not _REAL_ISOS or shutil.which("7z") is None,
    reason="set MKVSMITH_TEST_ISOS to real ISO paths (and install 7z) to run",
)
@pytest.mark.parametrize("iso", _REAL_ISOS, ids=lambda p: p.name)
def test_real_iso_matches_7z(iso: Path, tmp_path: Path) -> None:
    link = tmp_path / "image.iso"  # 7z mishandles some characters in names
    link.symlink_to(iso)
    listing = subprocess.run(
        ["7z", "l", "-slt", str(link)], capture_output=True, text=True, check=True
    ).stdout
    with isofs.IsoImage(iso) as image:
        assert {e.path: e.size for e in image.files()} == _parse_7z_slt(listing)
        for entry in image.files():
            if entry.size > 2_000_000:
                continue
            extracted = subprocess.run(
                ["7z", "e", "-so", str(link), entry.path],
                capture_output=True,
                check=True,
            ).stdout
            assert image.read(entry) == extracted, entry.path
