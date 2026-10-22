"""HD DVD subpictures: decoding EVO units and writing them as PGS."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR, load_fixture
from mkvsmith import mkv
from mkvsmith.hdspu import (
    HdSubpicture,
    _pgs_line,
    decode_subpicture,
    scan_evo_subpictures,
    write_sup,
)
from mkvsmith.models import Stream, StreamType, Title

# 90 kHz ticks per SP_DCSQ_STM unit.
_DELAY_UNIT = 1024


def _pgs_pixels(rle: bytes, width: int) -> list[bytes]:
    """Decode PGS run-length data back to lines of palette indices."""
    lines: list[bytes] = []
    line = bytearray()
    pos = 0
    while pos < len(rle):
        byte = rle[pos]
        pos += 1
        if byte:
            line.append(byte)
            continue
        flags = rle[pos]
        pos += 1
        if flags == 0:
            assert len(line) == width
            lines.append(bytes(line))
            line = bytearray()
            continue
        length = flags & 0x3F
        if flags & 0x40:
            length = (length << 8) | rle[pos]
            pos += 1
        colour = 0
        if flags & 0x80:
            colour = rle[pos]
            pos += 1
        line += bytes([colour]) * length
    assert not line
    return lines


def _segments(data: bytes) -> list[tuple[int, int, bytes]]:
    """(type, PTS, payload) for each segment of a .sup stream."""
    segments: list[tuple[int, int, bytes]] = []
    pos = 0
    while pos < len(data):
        assert data[pos : pos + 2] == b"PG"
        pts = int.from_bytes(data[pos + 2 : pos + 6], "big")
        kind = data[pos + 10]
        size = int.from_bytes(data[pos + 11 : pos + 13], "big")
        segments.append((kind, pts, data[pos + 13 : pos + 13 + size]))
        pos += 13 + size
    return segments


def _hd_unit(
    pixels: list[list[int]],
    *,
    x: int = 100,
    y: int = 900,
    stop: int | None = 50,
    forced: bool = False,
) -> bytes:
    """Build an HD subpicture unit with every pixel coded on its own."""
    height = len(pixels)
    width = len(pixels[0])

    def field(rows: list[list[int]]) -> bytes:
        out = bytearray()
        for row in rows:
            bits = "".join(f"01{colour:08b}" for colour in row)
            bits += "0" * (-len(bits) % 8)
            out += int(bits, 2).to_bytes(len(bits) // 8, "big")
        return bytes(out)

    top = field(pixels[0::2])
    bottom = field(pixels[1::2])
    top_offset = 10
    bottom_offset = top_offset + len(top)
    control = bottom_offset + len(bottom)
    palette = bytes(value for entry in range(256) for value in (entry, 128, 128))
    contrast = bytes(0xFF - entry for entry in range(256))
    x2, y2 = x + width - 1, y + height - 1
    area = bytes(
        [
            x >> 4,
            ((x & 0xF) << 4) | (x2 >> 8),
            x2 & 0xFF,
            y >> 4,
            ((y & 0xF) << 4) | (y2 >> 8),
            y2 & 0xFF,
        ]
    )
    second = control + 6 + 1 + 1 + 768 + 1 + 256 + 1 + 6 + 1 + 8 + 1
    first_sequence = (
        (0).to_bytes(2, "big")
        + (second if stop is not None else control).to_bytes(4, "big")
        + bytes([0x00 if forced else 0x01])
        + b"\x83"
        + palette
        + b"\x84"
        + contrast
        + b"\x85"
        + area
        + b"\x86"
        + top_offset.to_bytes(4, "big")
        + bottom_offset.to_bytes(4, "big")
        + b"\xff"
    )
    tail = b""
    if stop is not None:
        tail = stop.to_bytes(2, "big") + second.to_bytes(4, "big") + b"\x02\xff"
    body = top + bottom + first_sequence + tail
    size = 10 + len(body)
    return b"\0\0" + size.to_bytes(4, "big") + control.to_bytes(4, "big") + body


def _pts_bytes(pts: int) -> bytes:
    return bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ]
    )


def _pack(stream_id: int, payload: bytes, pts: int | None) -> bytes:
    """One 2048-byte pack holding one PES packet, padded with a 0xBE one."""
    header = (b"\x80\x80\x05" + _pts_bytes(pts)) if pts is not None else b"\x80\x00\x00"
    pes = b"\0\0\1" + bytes([stream_id])
    pes += (len(header) + len(payload)).to_bytes(2, "big") + header + payload
    pack = b"\0\0\1\xba" + b"\x44\0\4\0\4\1\1\x89\xc3" + b"\xf8"
    used = len(pack) + len(pes)
    assert used + 6 <= 2048
    padding = 2048 - used - 6
    return pack + pes + b"\0\0\1\xbe" + padding.to_bytes(2, "big") + b"\xff" * padding


def _evo(
    tmp_path: Path, name: str, first_pts: int, units: list[tuple[int, int, bytes]]
) -> Path:
    """An EVO with video at *first_pts* and each (sub-stream, PTS, unit)
    split over two private stream 1 packets."""
    data = _pack(0xFD, b"\0" * 16, first_pts)
    for sub, pts, unit in units:
        half = len(unit) // 2
        data += _pack(0xBD, bytes([sub]) + unit[:half], pts)
        data += _pack(0xBD, bytes([sub]) + unit[half:], None)
    data += _pack(0xFD, b"\0" * 16, first_pts + 90000)
    path = tmp_path / name
    path.write_bytes(data)
    return path


_REAL_UNIT = "hddvd_subpicture_8bit.bin"


@pytest.mark.skipif(
    not (FIXTURES_DIR / _REAL_UNIT).exists(), reason="real-disc fixture missing"
)
def test_real_disc_unit_decodes_like_the_reference_decoder() -> None:
    # A retail disc's unit; the pixel hash comes from an independent
    # bit-by-bit decoder of FFmpeg's 8-bit run codes.
    decoded = decode_subpicture(load_fixture(_REAL_UNIT))
    assert decoded is not None
    assert (decoded.x, decoded.y, decoded.width, decoded.height) == (322, 878, 412, 62)
    assert decoded.start_delay == 0
    assert decoded.stop_delay == 170 * _DELAY_UNIT
    assert not decoded.forced
    pixels = _pgs_pixels(decoded.rle, decoded.width)
    assert len(pixels) == decoded.height
    assert (
        hashlib.sha256(b"".join(pixels)).hexdigest()
        == "c1e44688790ef237f4a3ab1236f0a131149d6f2596ef4be1ff9484b693e392a4"
    )
    assert hashlib.sha256(decoded.palette).hexdigest()[:16] == "029eb421a939f2ee"
    # Entry 0 is the transparent key, the text entries are opaque.
    assert list(decoded.alpha[:4]) == [0, 255, 255, 255]


def test_dvd_layout_and_truncated_units_are_refused() -> None:
    assert decode_subpicture(b"\x00\x20\x00\x04" + b"\0" * 28) is None
    unit = _hd_unit([[1, 2], [3, 4]])
    assert decode_subpicture(unit[:12]) is None


def test_forced_start_and_interlaced_fields() -> None:
    rows = [[1, 1, 2], [3, 3, 3], [5, 0, 0]]
    decoded = decode_subpicture(_hd_unit(rows, forced=True, stop=None))
    assert decoded is not None
    assert decoded.forced
    assert decoded.stop_delay is None
    assert (decoded.x, decoded.y, decoded.width, decoded.height) == (100, 900, 3, 3)
    assert _pgs_pixels(decoded.rle, 3) == [bytes(row) for row in rows]


def test_pgs_line_uses_every_run_form() -> None:
    runs = [(0, 5), (0, 0), (7, 1), (7, 2), (9, 3), (0, 300), (4, 70)]
    line = _pgs_line([run for run in runs if run[1]])
    assert _pgs_pixels(line, 381) == [
        bytes([0] * 5 + [7] * 3 + [9] * 3 + [0] * 300 + [4] * 70)
    ]


def test_scan_times_units_on_the_appended_timeline(tmp_path: Path) -> None:
    # Each test EVO has video PTS at its start and one second later.
    unit = _hd_unit([[1, 2], [3, 4]], stop=50)
    first = _evo(tmp_path, "a.evo", 1_000_000, [(0x20, 1_000_000 + 90000, unit)])
    # The second clip's clock starts elsewhere: it follows the first
    # clip's video span plus one step (its last GOP).
    second = _evo(
        tmp_path,
        "b.evo",
        5000,
        [(0x20, 5000 + 45000, unit), (0x21, 5000 + 45000, unit)],
    )
    found, failed = scan_evo_subpictures([first, second], {0x20})
    assert failed == 0
    assert list(found) == [0x20]
    starts = [(sub.start, sub.end) for sub in found[0x20]]
    assert starts == [
        (90000, 90000 + 50 * _DELAY_UNIT),
        (225000, 225000 + 50 * _DELAY_UNIT),
    ]


def test_scan_follows_the_clock_of_seamless_clips(tmp_path: Path) -> None:
    # The second clip continues the first one's clock 1.33 s after its
    # first video PTS: that is where it starts, whatever the XPL says.
    unit = _hd_unit([[1, 2], [3, 4]], stop=None)
    first = _evo(tmp_path, "a.evo", 1_000_000, [])
    second = _evo(tmp_path, "b.evo", 1_120_000, [(0x20, 1_120_000 + 45000, unit)])
    found, _ = scan_evo_subpictures([first, second], {0x20})
    (sub,) = found[0x20]
    assert sub.start == 120000 + 45000
    assert sub.end == sub.start + 5 * 90000


def test_scan_counts_units_it_cannot_decode(tmp_path: Path) -> None:
    broken = b"\0\0\0\0\0\x10\0\0\0\x0a" + b"\xff" * 6
    path = _evo(tmp_path, "a.evo", 0, [(0x20, 0, broken)])
    found, failed = scan_evo_subpictures([path], {0x20})
    assert found == {}
    assert failed == 1


def _subpicture(start: int, end: int, *, forced: bool = False) -> HdSubpicture:
    palette = bytes(value for entry in range(256) for value in (entry, 100, 150))
    return HdSubpicture(
        start=start,
        end=end,
        x=10,
        y=20,
        width=2,
        height=1,
        rle=_pgs_line([(5, 2)]),
        palette=palette,
        alpha=bytes(range(256)),
        forced=forced,
    )


def test_write_sup_shows_and_clears_each_subpicture(tmp_path: Path) -> None:
    path = tmp_path / "track.sup"
    # The first runs into the second, which replaces it uncleared.
    write_sup(
        [_subpicture(1000, 9000, forced=True), _subpicture(5000, 7000)],
        path,
        (1920, 1080),
    )
    segments = _segments(path.read_bytes())
    assert [(kind, pts) for kind, pts, _ in segments] == [
        (0x16, 1000),
        (0x17, 1000),
        (0x14, 1000),
        (0x15, 1000),
        (0x80, 1000),
        (0x16, 5000),
        (0x17, 5000),
        (0x14, 5000),
        (0x15, 5000),
        (0x80, 5000),
        (0x16, 7000),
        (0x17, 7000),
        (0x80, 7000),
    ]
    show = segments[0][2]
    assert show[:4] == (1920).to_bytes(2, "big") + (1080).to_bytes(2, "big")
    assert show[7] == 0x80  # epoch start
    assert show[10] == 1  # one object
    assert show[14] == 0x40  # forced
    assert show[15:19] == (10).to_bytes(2, "big") + (20).to_bytes(2, "big")
    assert segments[5][2][14] == 0  # not forced
    assert segments[10][2][10] == 0  # cleared
    palette = segments[2][2]
    assert len(palette) == 2 + 256 * 5
    assert palette[2 + 5 * 7 : 2 + 5 * 8] == bytes([7, 7, 100, 150, 7])
    obj = segments[3][2]
    assert obj[3] == 0xC0  # first and last fragment
    assert int.from_bytes(obj[4:7], "big") == len(obj) - 7
    assert _pgs_pixels(obj[11:], 2) == [bytes([5, 5])]


def test_write_sup_splits_large_objects(tmp_path: Path) -> None:
    width = 4000
    rows = 40
    rle = b"".join(
        _pgs_line([(1 + (x % 200), 1) for x in range(width)]) for _ in range(rows)
    )
    big = HdSubpicture(0, 90000, 0, 0, width, rows, rle, bytes(768), bytes(256), False)
    path = tmp_path / "big.sup"
    write_sup([big], path, (1920, 1080))
    objects = [p for kind, _, p in _segments(path.read_bytes()) if kind == 0x15]
    assert len(objects) > 1
    assert [o[3] for o in objects] == [0x80] + [0] * (len(objects) - 2) + [0x40]
    joined = objects[0][4:] + b"".join(o[4:] for o in objects[1:])
    assert int.from_bytes(joined[:3], "big") == len(rle) + 4
    assert joined[7:] == rle


def _hddvd_title(tmp_path: Path, inputs: list[Path]) -> Title:
    title = Title(
        index=0,
        source_file=inputs[0],
        name="Title 1",
        duration_seconds=30.0,
        hddvd_title_number=1,
    )
    title.append_clips = inputs[1:]
    title.clip_durations = [10.0, 20.0][: len(inputs)]
    return title


def _subtitle(sub_id: int, language: str, *, forced: bool = False) -> Stream:
    return Stream(
        index=sub_id,
        stream_type=StreamType.SUBTITLE,
        codec="hdmv_pgs_subtitle",
        language=language,
        is_forced=forced,
        type_index=sub_id - 0x20,
        sub_id=sub_id,
    )


def test_hddvd_subtitles_become_one_sup_per_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mkv.RUNTIME_STATE.config, "temp_dir", tmp_path)
    unit = _hd_unit([[1, 2], [3, 4]])
    evo = _evo(tmp_path, "a.evo", 0, [(0x20, 9000, unit), (0x22, 9000, unit)])
    english = _subtitle(0x20, "en")
    french = _subtitle(0x21, "fr")
    spanish = _subtitle(0x22, "es", forced=True)
    mapped: list[mkv.MappedStream] = [
        {"input_id": -1, "type": "subtitles", "stream": stream, "ident_channels": None}
        for stream in (english, french, spanish)
    ]
    temp_files: list[Path] = []
    warnings: list[str] = []
    monkeypatch.setattr(mkv, "log_warn", warnings.append)

    converted = mkv._extract_hddvd_pgs_subtitles(
        _hddvd_title(tmp_path, [evo]), mapped, [evo], temp_files
    )

    # French has no subpictures in the EVO, so it's left out.
    assert [stream for stream, _ in converted] == [english, spanish]
    assert [path for _, path in converted] == temp_files
    assert all(path.read_bytes()[:2] == b"PG" for path in temp_files)
    assert len(warnings) == 1 and "fr" in warnings[0]

    cleanup: list[Path] = []
    cmd = mkv._build_mkvmerge_command(
        _hddvd_title(tmp_path, [evo]),
        tmp_path / "out.mkv",
        [evo],
        [],
        [],
        None,
        [],
        None,
        [],
        [],
        None,
        cleanup,
        temp_files,
        pgs_subtitles=converted,
    )
    english_sup, spanish_sup = (str(path) for _, path in converted)
    assert cmd[-1] == spanish_sup
    english_options = cmd[cmd.index(str(evo)) + 1 : cmd.index(english_sup)]
    spanish_options = cmd[cmd.index(english_sup) + 1 : -1]
    assert english_options[:4] == ["--language", "0:en", "--default-track", "0:no"]
    assert "--forced-track" not in english_options
    assert spanish_options[:6] == [
        "--language",
        "0:es",
        "--default-track",
        "0:no",
        "--forced-track",
        "0:yes",
    ]
    # SOURCE_ID tags carry each track's own sub-stream.
    english_tags = Path(english_options[english_options.index("--tags") + 1][2:])
    assert "0120BD" in english_tags.read_text()
    assert {Path(english_sup), Path(spanish_sup)} <= set(cleanup)
