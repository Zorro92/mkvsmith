"""The built-in ISO/UDF reader on synthetic disc images, so it runs everywhere.

The images come from tests/synth_iso.py (ECMA-167, OSTA UDF 1.02 / 2.50,
ECMA-119), not from the reader; they mirror the real-image tests in
test_isofs.py: a Blu-ray-style UDF 2.50 image with a metadata partition, and
a DVD-style UDF 1.02 + ISO 9660 bridge image.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mkvsmith import disc_reader, isofs
from tests.synth_iso import build_image

MODIFIED = datetime(2016, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=-8)))


def _pattern(size: int, seed: int) -> bytes:
    """Distinct, non-repeating-looking content for each member."""
    return bytes((n * 31 + seed * 7 + n // 251) & 0xFF for n in range(size))


BD_FILES: dict[str, bytes | int] = {
    "BDMV/index.bdmv": _pattern(300, 1),
    "BDMV/MovieObject.bdmv": _pattern(1500, 2),
    "BDMV/PLAYLIST/00800.mpls": _pattern(4200, 3),
    "BDMV/CLIPINF/00800.clpi": _pattern(900, 4),
    "BDMV/STREAM/00800.m2ts": _pattern(5 * 1024 * 1024 + 192 * 7, 5),
}
DVD_FILES: dict[str, bytes | int] = {
    "VIDEO_TS/VIDEO_TS.IFO": _pattern(12_288, 6),
    "VIDEO_TS/VTS_01_0.IFO": _pattern(30_720, 7),
    "VIDEO_TS/VTS_01_1.VOB": _pattern(3 * 1024 * 1024, 8),
    "AUDIO_TS/.keep": b"",
}


@pytest.fixture
def bd_image(tmp_path: Path) -> Path:
    path = tmp_path / "blu-ray.iso"
    # 1 MiB extents: the 5 MiB clip is recorded as 6 adjacent extents.
    path.write_bytes(
        build_image(
            BD_FILES,
            udf="2.50",
            iso9660=False,
            label="A_FILM_BD",
            modified=MODIFIED,
            max_extent=1024 * 1024,
        )
    )
    return path


@pytest.fixture
def dvd_image(tmp_path: Path) -> Path:
    path = tmp_path / "dvd.iso"
    path.write_bytes(
        build_image(
            DVD_FILES, udf="1.02", iso9660=True, label="A_FILM", modified=MODIFIED
        )
    )
    return path


def _content(files: dict[str, bytes | int], path: str) -> bytes:
    content = files[path]
    assert isinstance(content, bytes)
    return content


def _sizes(files: dict[str, bytes | int]) -> dict[str, int]:
    return {
        path: content if isinstance(content, int) else len(content)
        for path, content in files.items()
    }


def test_udf250_metadata_partition_listing(bd_image: Path) -> None:
    with isofs.IsoImage(bd_image) as image:
        assert image.filesystem == "udf"
        assert image.label == "A_FILM_BD"
        entries = image.files()
    assert {e.path: e.size for e in entries} == _sizes(BD_FILES)
    assert [e.path for e in entries] == sorted(BD_FILES)


@pytest.mark.parametrize(
    ("fixture", "files"), [("bd_image", BD_FILES), ("dvd_image", DVD_FILES)]
)
def test_member_content(
    fixture: str, files: dict[str, bytes | int], request: pytest.FixtureRequest
) -> None:
    path: Path = request.getfixturevalue(fixture)
    with isofs.IsoImage(path) as image:
        for member, content in files.items():
            assert image.read(image.entries[member]) == content, member
            assert image.read(image.entries[member], 100, 50) == bytes(content)[100:150]


def test_a_file_in_adjacent_extents_is_one_run(bd_image: Path) -> None:
    with isofs.IsoImage(bd_image) as image:
        entry = image.entries["BDMV/STREAM/00800.m2ts"]
    assert entry.size == len(_content(BD_FILES, "BDMV/STREAM/00800.m2ts"))
    assert len(entry.runs) == 1


def test_dvd_udf_and_iso9660_trees_agree(dvd_image: Path) -> None:
    with isofs.IsoImage(dvd_image) as image:
        assert image.filesystem == "udf"
        udf = {e.path: e.size for e in image.files()}
        iso = {e.path: e.size for e in isofs._Iso9660Reader(image._file).files()}
    assert udf == iso == _sizes(DVD_FILES)


def test_falls_back_to_iso9660_without_udf(dvd_image: Path) -> None:
    with dvd_image.open("r+b") as f:
        for sector in range(16, 32):
            f.seek(sector * isofs.SECTOR + 1)
            if f.read(5) in (b"NSR02", b"NSR03"):
                f.seek(sector * isofs.SECTOR)
                f.write(bytes(isofs.SECTOR))
    with isofs.IsoImage(dvd_image) as image:
        assert image.filesystem == "iso9660"
        assert image.label == "A_FILM"
        assert {e.path: e.size for e in image.files()} == _sizes(DVD_FILES)


def test_copy_to_honours_limit(bd_image: Path, tmp_path: Path) -> None:
    dest = tmp_path / "prefix.m2ts"
    with isofs.IsoImage(bd_image) as image:
        image.copy_to(image.entries["BDMV/STREAM/00800.m2ts"], dest, limit=4096)
    assert dest.read_bytes() == _content(BD_FILES, "BDMV/STREAM/00800.m2ts")[:4096]


def test_list_iso_files_uses_native_reader(bd_image: Path) -> None:
    paths, sizes = disc_reader._list_iso_files(bd_image)
    assert paths == sorted(BD_FILES)
    assert sizes == _sizes(BD_FILES)


def test_metadata_timestamps_are_timezone_aware(bd_image: Path) -> None:
    entries = disc_reader._list_iso_file_metadata(bd_image)
    assert len(entries) == len(BD_FILES)
    for entry in entries:
        assert entry.modified == MODIFIED
        assert (
            entry.modified is not None
            and entry.modified.utcoffset() == MODIFIED.utcoffset()
        )


def test_extract_iso_files_flattens_and_skips_missing(
    dvd_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    extracted = disc_reader._extract_iso_files(
        dvd_image, ["VIDEO_TS/VIDEO_TS.IFO", "VIDEO_TS/NOPE.IFO"], out
    )
    assert extracted == [out / "VIDEO_TS.IFO"]
    assert extracted[0].read_bytes() == DVD_FILES["VIDEO_TS/VIDEO_TS.IFO"]


def test_stopping_an_extraction_removes_what_was_extracted(
    dvd_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    chunks: list[None] = []

    class Stopped(Exception):
        pass

    def before_chunk() -> None:
        # The IFO is one chunk; stop on the VOB's.
        if len(chunks) == 1:
            raise Stopped
        chunks.append(None)

    with pytest.raises(Stopped):
        disc_reader._extract_iso_files(
            dvd_image,
            ["VIDEO_TS/VIDEO_TS.IFO", "VIDEO_TS/VTS_01_1.VOB"],
            out,
            before_chunk,
        )

    assert list(out.iterdir()) == []


def test_extract_iso_prefix_registers_temp_file(dvd_image: Path) -> None:
    temp_files: list[Path] = []
    result = disc_reader._extract_iso_prefix(
        dvd_image, "VIDEO_TS/VTS_01_1.VOB", 1, temp_files=temp_files
    )
    try:
        assert result is not None and temp_files == [result]
        vob = _content(DVD_FILES, "VIDEO_TS/VTS_01_1.VOB")
        assert result.read_bytes() == vob[: 1024 * 1024]
    finally:
        for temp in temp_files:
            temp.unlink(missing_ok=True)
