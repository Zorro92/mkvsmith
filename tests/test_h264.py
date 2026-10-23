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

from mkvsmith import mkv
from mkvsmith.h264 import FrameScan, ScanType, classify_scan, stream_scan_type
from tests.synth_h264 import BitWriter as _BitWriter
from tests.synth_h264 import nal as _nal
from tests.synth_h264 import pic_timing as _pic_timing
from tests.synth_h264 import pps as _pps
from tests.synth_h264 import slice_header as _slice
from tests.synth_h264 import sps as _sps


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

    monkeypatch.setattr("mkvsmith.mkvread.video_scan_type", fake_scan)
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

    monkeypatch.setattr("mkvsmith.mkvread.video_scan_type", fake_scan)
    monkeypatch.setattr(mkv.shutil, "which", missing)

    assert mkv._apply_interlace_flags(Path("out.mkv")) is False


def test_sps_vui_with_both_nal_and_vcl_hrd_preserves_pic_struct() -> None:
    from mkvsmith.h264 import FrameAnalyzer, _parse_sps

    w = _BitWriter().u(8, 77).u(16, 0x401E).ue(0)  # Main profile, sps_id 0
    w.ue(0).ue(0).ue(2).ue(1).u(1, 0).ue(44).ue(14)
    w.u(1, 0).u(1, 1)  # frame_mbs_only=0, mbaff=1
    w.u(1, 1).u(1, 0).u(1, 1)  # direct_8x8, no crop, vui_parameters_present=1
    w.u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0)
    for _ in range(2):  # nal_hrd_parameters_present=1 and vcl_hrd_parameters_present=1
        w.u(1, 1)
        w.ue(0).u(8, 0).ue(0).ue(0).u(1, 0)
        w.u(5, 23).u(5, 23).u(5, 23).u(5, 24)
    w.u(1, 0)  # low_delay_hrd_flag
    w.u(1, 1).u(1, 0)  # pic_struct_present_flag=1, bitstream_restriction=0
    sps_nal = _nal(0x67, w.rbsp())

    _, sps = _parse_sps(sps_nal)
    assert sps.pic_struct_present is True
    assert sps.cpb_dpb_delays_present is True

    analyzer = FrameAnalyzer([sps_nal], [_pps()])
    assert analyzer.frame(
        [b"", _nal(0x06, b"\xff\xff"), _slice(field=False)]
    ) == FrameScan(True, None)
