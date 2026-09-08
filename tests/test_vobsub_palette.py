"""Tests for DVD subpicture display-control command parsing."""

from __future__ import annotations

from vobsub import _extract_spu_palette


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


def test_extract_spu_palette_rejects_truncated_control_offset() -> None:
    assert _extract_spu_palette(b"\x00\x10\x0a\x0c") is None
