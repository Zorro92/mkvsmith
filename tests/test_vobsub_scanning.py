"""Tests for DVD VOB subpicture packet assembly."""

from __future__ import annotations

from pathlib import Path

from vobsub import _build_pes_entry, _scan_vob_subpictures, _SpuAccumulator


def test_spu_accumulator_joins_continuations_and_splits_on_pts() -> None:
    accumulator = _SpuAccumulator()

    assert accumulator.add(0x20, 100, b"first-") is None
    assert accumulator.add(0x20, 0, b"second") is None
    assert accumulator.add(0x20, 200, b"next") == (100, b"first-second")
    assert accumulator.flush() == {0x20: [(200, b"next")]}
    assert accumulator.flush() == {}


def test_scan_vob_subpictures_joins_generated_continuation_packets(
    tmp_path: Path,
) -> None:
    payload = b"subpicture-payload" * 512
    vob = tmp_path / "continuation.vob"
    vob.write_bytes(b"".join(_build_pes_entry(payload, 12345, 0x20)))

    result = _scan_vob_subpictures([vob], max_bytes=vob.stat().st_size)

    assert result == {0x20: [(12345, payload)]}


def test_scan_vob_subpictures_uses_raw_spu_fallback(tmp_path: Path) -> None:
    spu = b"\x00\x0a\x00\x04\x00\x08abcd"
    vob = tmp_path / "raw-spu.vob"
    vob.write_bytes(spu)

    result = _scan_vob_subpictures([vob])

    assert result == {0: [(0, spu)]}
