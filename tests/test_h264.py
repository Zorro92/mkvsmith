"""Tests for H.264 scan-type detection (progressive / interlaced + order).

Streams are synthesised bit by bit, mirroring the real cases checked against
ffprobe on Blu-ray discs: soft-telecined film (Sgt. Frog S1), MBAFF with
picture timing (S2-S7), field pictures without timing (Face/Off extras),
and MBAFF ordered only by picture order count (Pinocchio extras).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import mkv
from h264 import FrameScan, ScanType, classify_scan, stream_scan_type


class _BitWriter:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def u(self, n: int, value: int) -> _BitWriter:
        self.bits += [(value >> (n - 1 - i)) & 1 for i in range(n)]
        return self

    def ue(self, value: int) -> _BitWriter:
        code = value + 1
        length = code.bit_length()
        return self.u(length - 1, 0).u(length, code)

    def se(self, value: int) -> _BitWriter:
        return self.ue(2 * value - 1 if value > 0 else -2 * value)

    def rbsp(self) -> bytes:
        bits = [*self.bits, 1]  # rbsp_stop_one_bit
        bits += [0] * (-len(bits) % 8)
        return bytes(
            int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)
        )


def _nal(header: int, rbsp: bytes) -> bytes:
    """A NAL unit with emulation prevention applied."""
    out = bytearray([header])
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _sps(*, frame_mbs_only: bool, mbaff: bool = False, pic_struct: bool) -> bytes:
    w = _BitWriter().u(8, 77).u(16, 0x401E).ue(0)  # Main profile, sps_id 0
    w.ue(0)  # log2_max_frame_num_minus4 -> 4-bit frame_num
    w.ue(0).ue(2)  # poc type 0, 6-bit pic_order_cnt_lsb
    w.ue(1).u(1, 0).ue(44).ue(14)  # refs, gaps, size
    w.u(1, int(frame_mbs_only))
    if not frame_mbs_only:
        w.u(1, int(mbaff))
    w.u(1, 1).u(1, 0)  # direct_8x8_inference, no cropping
    w.u(1, 1)  # vui_parameters_present_flag
    w.u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0)  # no AR/overscan/signal/chroma/timing
    w.u(1, 0).u(1, 0)  # no NAL / VCL HRD
    w.u(1, int(pic_struct)).u(1, 0)  # pic_struct_present, no bitstream_restriction
    return _nal(0x67, w.rbsp())


def _pps(*, bottom_poc: bool = False) -> bytes:
    w = _BitWriter().ue(0).ue(0).u(1, 0).u(1, int(bottom_poc))
    w.ue(0).ue(0).ue(0).u(1, 0).u(2, 0).se(0).se(0).se(0).u(1, 0).u(1, 0).u(1, 0)
    return _nal(0x68, w.rbsp())


def _slice(
    *, field: bool | None = None, bottom: bool = False, delta_bottom: int | None = None
) -> bytes:
    """Non-IDR I-slice header; field=None for a frame_mbs_only stream."""
    w = _BitWriter().ue(0).ue(7).ue(0).u(4, 1)  # first_mb, I, pps 0, frame_num
    if field is not None:
        w.u(1, int(field))
        if field:
            w.u(1, int(bottom))
    w.u(6, 4)  # pic_order_cnt_lsb
    if delta_bottom is not None:
        w.se(delta_bottom)
    w.ue(0)  # (rest of the header; not parsed)
    return _nal(0x41, w.rbsp())


def _pic_timing(pic_struct: int, ct_type: int | None = None) -> bytes:
    w = _BitWriter().u(4, pic_struct)
    if ct_type is None:
        w.u(1, 0)  # clock_timestamp_flag
    else:
        w.u(1, 1).u(2, ct_type).u(1, 0).u(5, 0).u(1, 1).u(1, 0).u(1, 0).u(8, 0)
        w.u(6, 0).u(6, 0).u(5, 0)
    payload = w.rbsp()
    return _nal(0x06, bytes([1, len(payload)]) + payload + b"\x80")


def _frames(n: int, make: Callable[[int], list[bytes]]) -> list[list[bytes]]:
    return [make(i) for i in range(n)]


def test_soft_telecine_film_is_progressive() -> None:
    # Progressive frames with a 3:2 pulldown pic_struct cycle (Sgt. Frog S1).
    sps = _sps(frame_mbs_only=False, mbaff=False, pic_struct=True)
    cycle = [4, 6, 3, 5]
    frames = _frames(40, lambda i: [_pic_timing(cycle[i % 4]), _slice(field=False)])

    assert stream_scan_type([sps], [_pps()], frames) == ScanType.PROGRESSIVE


@pytest.mark.parametrize(
    ("pic_struct", "expected"),
    [(3, ScanType.INTERLACED_TFF), (4, ScanType.INTERLACED_BFF)],
)
def test_mbaff_with_picture_timing(pic_struct: int, expected: ScanType) -> None:
    sps = _sps(frame_mbs_only=False, mbaff=True, pic_struct=True)
    frames = _frames(40, lambda i: [_pic_timing(pic_struct), _slice(field=False)])

    assert stream_scan_type([sps], [_pps()], frames) == expected


def test_clock_timestamp_type_overrides_coding() -> None:
    # Frame-coded (not MBAFF) but timing SEI says the frames are interlaced.
    sps = _sps(frame_mbs_only=False, mbaff=False, pic_struct=True)
    frames = _frames(40, lambda i: [_pic_timing(3, ct_type=1), _slice(field=False)])

    assert stream_scan_type([sps], [_pps()], frames) == ScanType.INTERLACED_TFF


def test_field_pictures_without_timing_pair_up() -> None:
    # Each field its own block, top first (Face/Off 1080i extras).
    sps = _sps(frame_mbs_only=False, pic_struct=False)
    frames = _frames(80, lambda i: [_slice(field=True, bottom=bool(i % 2))])

    assert stream_scan_type([sps], [_pps()], frames) == ScanType.INTERLACED_TFF


@pytest.mark.parametrize(
    ("delta", "expected"),
    [(1, ScanType.INTERLACED_TFF), (-1, ScanType.INTERLACED_BFF), (0, None)],
)
def test_mbaff_order_from_picture_order_count(
    delta: int, expected: ScanType | None
) -> None:
    # No picture timing: bottom POC - top POC decides (Pinocchio extras).
    sps = _sps(frame_mbs_only=False, mbaff=True, pic_struct=False)
    frames = _frames(40, lambda i: [_slice(field=False, delta_bottom=delta)])

    assert stream_scan_type([sps], [_pps(bottom_poc=True)], frames) == expected


def test_frame_only_stream_is_progressive() -> None:
    sps = _sps(frame_mbs_only=True, pic_struct=False)
    frames = _frames(40, lambda i: [_slice()])

    assert stream_scan_type([sps], [_pps()], frames) == ScanType.PROGRESSIVE


def test_mixed_or_short_samples_are_undecided() -> None:
    mixed = [FrameScan(i % 2 == 0, True) for i in range(40)]
    assert classify_scan(mixed) is None
    both_orders = [FrameScan(True, i % 2 == 0) for i in range(40)]
    assert classify_scan(both_orders) is None
    assert classify_scan([FrameScan(True, True)] * 5) is None


def test_unparseable_stream_is_undecided() -> None:
    assert stream_scan_type([b"\x67\xff"], [], [[b"\x41\x00"]]) is None


# =============================================================================
# Writing the flags after a mux
# =============================================================================


@pytest.mark.parametrize(
    ("scan", "field_order"),
    [
        (ScanType.INTERLACED_TFF, "1"),
        (ScanType.INTERLACED_BFF, "6"),
        (ScanType.PROGRESSIVE, None),
        (None, None),
    ],
)
def test_interlace_flags_written_only_for_interlaced_video(
    monkeypatch: pytest.MonkeyPatch, scan: ScanType | None, field_order: str | None
) -> None:
    commands: list[list[str]] = []

    def fake_scan(_path: Path) -> ScanType | None:
        return scan

    def fake_run(cmd: list[str]) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(returncode=0)

    def found(_name: str) -> str:
        return "/usr/bin/mkvpropedit"

    monkeypatch.setattr("mkvread.video_scan_type", fake_scan)
    monkeypatch.setattr(mkv.shutil, "which", found)
    monkeypatch.setattr(mkv, "_run_mkvtoolnix", fake_run)

    written = mkv._apply_interlace_flags(Path("out.mkv"))

    if field_order is None:
        assert not written and commands == []
    else:
        assert written
        assert commands == [
            [
                "mkvpropedit",
                "out.mkv",
                "--edit",
                "track:v1",
                "--set",
                "interlaced=1",
                "--set",
                f"field-order={field_order}",
            ]
        ]


def test_interlace_flags_skipped_without_mkvpropedit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_scan(_path: Path) -> ScanType:
        return ScanType.INTERLACED_TFF

    def missing(_name: str) -> None:
        return None

    monkeypatch.setattr("mkvread.video_scan_type", fake_scan)
    monkeypatch.setattr(mkv.shutil, "which", missing)

    assert mkv._apply_interlace_flags(Path("out.mkv")) is False
