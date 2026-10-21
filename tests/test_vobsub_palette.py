"""Tests for DVD subpicture display-control command parsing."""

from __future__ import annotations

from mkvsmith.vobsub import _extract_spu_palette, _parse_spu_control_state


_FIRST_CONTROL_SEQUENCE = bytes.fromhex(
    "00000a0c01030231040ff0050002cf00223e06000604e9ff"
)
_FINAL_CONTROL_SEQUENCE = bytes.fromhex("00930a0c02ff")
_FIRST_CONTROL_OFFSET = 4
_FINAL_CONTROL_OFFSET = 0x0A0C


def _documented_reference_spu() -> bytes:
    spu = bytearray(_FINAL_CONTROL_OFFSET + len(_FINAL_CONTROL_SEQUENCE))
    spu[0:2] = len(spu).to_bytes(2, "big")
    spu[2:4] = _FIRST_CONTROL_OFFSET.to_bytes(2, "big")
    start = _FIRST_CONTROL_OFFSET
    spu[start : start + len(_FIRST_CONTROL_SEQUENCE)] = _FIRST_CONTROL_SEQUENCE
    start = _FINAL_CONTROL_OFFSET
    spu[start : start + len(_FINAL_CONTROL_SEQUENCE)] = _FINAL_CONTROL_SEQUENCE
    return bytes(spu)


def test_extract_spu_palette_follows_display_control_chains() -> None:
    palette = _extract_spu_palette(_documented_reference_spu())

    assert palette == [
        (0, 0, 0),
        (0xFE, 0xFE, 0xFE),
        (0, 0, 0),
        (0, 0, 0),
        *((0x82, 0x82, 0x82),) * 12,
    ]


def test_extract_spu_palette_prefers_authoritative_clut() -> None:
    clut = [(index, 0, 0) for index in range(16)]

    assert _extract_spu_palette(_documented_reference_spu(), clut) == clut


def test_extract_spu_palette_skips_change_color_command_payload() -> None:
    spu = bytearray(16)
    spu[0:2] = len(spu).to_bytes(2, "big")
    spu[2:4] = (4).to_bytes(2, "big")
    # CHG_COLCON (07) with 2 data bytes: its size word counts itself, so
    # 0004 (mpucoder spu.html; docs/formats/subpictures.md).
    spu[4:] = bytes.fromhex("00000004070004aabb031234040ff0ff")

    palette = _extract_spu_palette(bytes(spu))

    assert palette is not None
    assert palette[:4] == [
        (0, 0, 0),
        (0xFE, 0xFE, 0xFE),
        (0, 0, 0),
        (0, 0, 0),
    ]


def test_extract_spu_palette_rejects_truncated_control_offset() -> None:
    assert _extract_spu_palette(b"\x00\x10\x0a\x0c") is None


def _spu(size: int, fill: int) -> bytes:
    """An SPU of *size* bytes: SPDSZ, a control table at 4, then filler."""
    return size.to_bytes(2, "big") + (4).to_bytes(2, "big") + bytes([fill]) * (size - 4)


def test_spus_are_complete_at_their_declared_size() -> None:
    from mkvsmith.vobsub import _SpuAccumulator

    first, second = _spu(10, 1), _spu(8, 2)
    accumulator = _SpuAccumulator()

    # The first SPU arrives in two packets, the second (same PTS, as when a
    # display and its erase share one) right after; padding trails it.
    assert accumulator.add(0x20, 9000, first[:6]) is None
    assert accumulator.add(0x20, 0, first[6:]) == (9000, first)
    assert accumulator.add(0x20, 9000, second + b"\xff\xff") == (9000, second)
    assert accumulator.flush() == {}


def test_an_spu_without_its_size_ends_at_a_new_pts() -> None:
    from mkvsmith.vobsub import _SpuAccumulator

    hd_dvd = b"\x00\x00" + b"\x00\x00\x00\x0c" + bytes(6)  # SPDSZ 0: HD layout
    accumulator = _SpuAccumulator()

    assert accumulator.add(0x20, 9000, hd_dvd) is None
    assert accumulator.add(0x20, 18000, _spu(8, 2)) == (9000, hd_dvd)


def test_a_link_too_near_the_end_stops_the_control_parse() -> None:
    # The second sequence's link points 2 bytes before the end: reading its
    # 4-byte header used to raise IndexError.
    spu = bytearray(14)
    spu[0:2] = len(spu).to_bytes(2, "big")
    spu[2:4] = (4).to_bytes(2, "big")
    spu[4:10] = bytes.fromhex("0000000c03ff")  # date, next = 12, SET_COLOR
    spu[10:14] = bytes.fromhex("12ff0000")

    assert _parse_spu_control_state(bytes(spu)) is not None
