"""VC-1 frame times from EVOs, and rewriting an MKV's block times with them."""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import mkv
from mkvsmith.mkvtimes import is_vc1_track, read_track_times, write_block_times
from mkvsmith.vc1time import (
    FrameTimingError,
    _parse_frame_header,
    _parse_sequence_header,
    _presentation_order,
    correct_vc1_frame_times,
    retime_vc1_join_frames,
    evo_frame_times,
)
from tests.synth_vc1 import FIELD as _FIELD
from tests.synth_vc1 import GOP as _GOP
from tests.synth_vc1 import BitWriter as _BitWriter
from tests.synth_vc1 import frame_header as _frame_header
from tests.synth_vc1 import pack as _pack
from tests.synth_vc1 import pes as _pes
from tests.synth_vc1 import sequence_header as _sequence_header
from tests.synth_vc1 import write_evo as _write_evo


def test_sequence_header_frame_rate_and_flags() -> None:
    seq = _parse_sequence_header(_sequence_header())
    assert seq is not None
    assert seq.interlace and seq.pulldown and not seq.progressive_segmented
    assert seq.field_ticks == pytest.approx(_FIELD)
    no_rate = _parse_sequence_header(_sequence_header(frame_rate=False))
    assert no_rate is not None and no_rate.field_ticks is None


def test_frame_header_types_and_fields() -> None:
    seq = _parse_sequence_header(_sequence_header())
    assert seq is not None
    for kind, shown_at_once in (
        ("I", False),
        ("P", False),
        ("B", True),
        ("BI", True),
        ("S", False),
    ):
        for rff in (0, 1):
            frame = _parse_frame_header(_frame_header(kind, rff), seq)
            assert frame.shown_at_once is shown_at_once
            assert frame.fields == 2 + rff


def test_progressive_pulldown_repeats_whole_frames() -> None:
    seq = _parse_sequence_header(_sequence_header(interlace=0))
    assert seq is not None
    header = _BitWriter().put(6, 3).put(1, 2).to_bytes(16)  # I, RPTFRM 1
    assert _parse_frame_header(header, seq).fields == 4


def test_presentation_order_holds_anchors_until_the_next() -> None:
    seq = _parse_sequence_header(_sequence_header())
    assert seq is not None
    frames = [_parse_frame_header(_frame_header(kind, 0), seq) for kind, _ in _GOP]
    for frame, (kind, _rff) in zip(frames, _GOP):
        frame.pts = ord(kind[0])
    shown = "".join(chr(frame.pts or 0) for frame in _presentation_order(frames))
    assert shown == "IBBPBBP"


def test_evo_frame_times_follow_the_cadence(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    expected = _write_evo(evo, gops=4)
    times = evo_frame_times(evo)
    assert times is not None
    assert times.first_pts == 900_000
    assert times.times == pytest.approx(expected, abs=1)


def test_evo_frame_times_count_a_short_last_frame(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    expected = _write_evo(evo, gops=2)
    # A skipped frame is 5 bytes; at the end of the EVO nothing follows it.
    skipped = b"\x00\x00\x01\x0d" + _frame_header("S", 0)[:1]
    with evo.open("ab") as handle:
        handle.write(_pack(_pes(skipped, None)))
    times = evo_frame_times(evo)
    assert times is not None
    assert len(times.times) == len(expected) + 1


def test_evo_frame_times_refuse_a_cadence_off_the_pts(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    _write_evo(evo, gops=4)
    data = bytearray(evo.read_bytes())
    # Clear one frame's RFF: the next GOP's PTS no longer matches.
    marker = b"\x00\x00\x01\x0d" + _frame_header("P", 1)
    at = data.find(marker)
    data[at + 4 : at + 20] = _frame_header("P", 0)
    evo.write_bytes(bytes(data))
    assert evo_frame_times(evo) is None


# Matroska builder: just the elements mkvtimes reads.


def _size(value: int) -> bytes:
    return bytes([0x01]) + value.to_bytes(7, "big")  # 8-byte size, any value


def _element(element_id: int, payload: bytes) -> bytes:
    raw_id = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
    return raw_id + _size(len(payload)) + payload


def _uint(element_id: int, value: int, width: int = 4) -> bytes:
    return _element(element_id, value.to_bytes(width, "big"))


def _vc1_private() -> bytes:
    return bytes(16) + b"WVC1" + bytes(20)


def _write_mkv(path: Path, block_times: list[int], cluster_every: int = 8) -> None:
    """VC-1 track 1 with one SimpleBlock per time and a cue per cluster."""
    tracks = _element(
        0x1654AE6B,
        _element(
            0xAE,
            _uint(0xD7, 1, 1)
            + _element(0x86, b"V_MS/VFW/FOURCC")
            + _element(0x63A2, _vc1_private()),
        )
        + _element(
            0xAE, _uint(0xD7, 2, 1) + _element(0x86, b"A_AC3") + _element(0x63A2, b"")
        ),
    )
    info = _element(0x1549A966, _uint(0x2AD7B1, 1_000_000))
    head = info + tracks
    clusters = b""
    cue_points = b""
    for start in range(0, len(block_times), cluster_every):
        chunk = block_times[start : start + cluster_every]
        cluster_time = chunk[0]
        body = _uint(0xE7, cluster_time)
        first_block = len(body)
        for time in chunk:
            relative = (time - cluster_time).to_bytes(2, "big", signed=True)
            body += _element(0xA3, b"\x81" + relative + b"\x80" + b"frame")
            body += _element(0xA3, b"\x82" + relative + b"\x80" + b"audio")
        cue_points += _element(
            0xBB,
            _uint(0xB3, chunk[0])
            + _element(
                0xB7,
                _uint(0xF7, 1, 1)
                + _uint(0xF1, len(head) + len(clusters), 8)
                + _uint(0xF0, first_block),
            ),
        )
        clusters += _element(0x1F43B675, body)
    segment = head + clusters + _element(0x1C53BB6B, cue_points)
    ebml = _element(0x1A45DFA3, _element(0x4282, b"matroska"))
    path.write_bytes(ebml + _element(0x18538067, segment))


def test_read_track_times_finds_the_vc1_track(tmp_path: Path) -> None:
    path = tmp_path / "a.mkv"
    _write_mkv(path, [0, 33, 83, 117, 167, 200, 250, 284, 334, 367])
    times = read_track_times(path, is_vc1_track)
    assert times is not None
    assert times.track == 1 and times.timestamp_scale == 1_000_000
    assert [block.time for block in times.blocks][:4] == [0, 33, 83, 117]
    assert [cue.time for cue in times.cues] == [0, 334]
    assert all(cue.block is not None for cue in times.cues)


def test_write_block_times_rewrites_blocks_and_cues(tmp_path: Path) -> None:
    path = tmp_path / "a.mkv"
    _write_mkv(path, [0, 33, 33, 117, 167, 200, 250, 284, 384, 367])
    size = path.stat().st_size
    times = read_track_times(path, is_vc1_track)
    assert times is not None
    new = [10, 43, 93, 127, 177, 210, 260, 294, 344, 377]
    assert write_block_times(path, times, new) == 10
    assert path.stat().st_size == size
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    assert [block.time for block in after.blocks] == new
    assert [cue.time for cue in after.cues] == [10, 344]


def test_write_block_times_refuses_a_time_outside_the_cluster(tmp_path: Path) -> None:
    path = tmp_path / "a.mkv"
    _write_mkv(path, [0, 33, 67])
    times = read_track_times(path, is_vc1_track)
    assert times is not None
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_block_times(path, times, [0, 33, 40_000])
    assert path.read_bytes() == before


def test_correct_vc1_frame_times_fixes_mkvmerges_chapter_glitch(
    tmp_path: Path,
) -> None:
    evo = tmp_path / "a.evo"
    expected = _write_evo(evo, gops=6)
    right = [round(tick / 90) for tick in expected]
    # mkvmerge's times: the first frame at its PTS (40 ms in), the rest
    # 150 ms late, and two frames after a GOP start given the time before
    # them (duplicates, then a hole).
    muxed = [40] + [time + 190 for time in right[1:]]
    muxed[8] = muxed[7]
    muxed[9] = muxed[8]
    path = tmp_path / "a.mkv"
    _write_mkv(path, muxed)
    assert correct_vc1_frame_times(path, [evo]) is not None
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    # Each block carries the time of the frame shown before it.
    assert [block.time for block in after.blocks] == [
        time + 40 for time in right[:1] + right[:-1]
    ]


def test_correct_vc1_frame_times_lines_up_appended_files(tmp_path: Path) -> None:
    first, second = tmp_path / "a.evo", tmp_path / "b.evo"
    times_a = [round(t / 90) for t in _write_evo(first, gops=3)]
    times_b = [round(t / 90) for t in _write_evo(second, gops=3, first_pts=5_000)]
    # mkvmerge starts the second file right after the first.
    muxed = times_a + [time + 1000 for time in times_b]
    path = tmp_path / "a.mkv"
    _write_mkv(path, muxed)
    correct_vc1_frame_times(path, [first, second])
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    assert [block.time for block in after.blocks] == muxed[:1] + muxed[:-1]


def test_correct_vc1_frame_times_puts_a_clock_join_on_the_clock(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "a.evo", tmp_path / "b.evo"
    times_a = [round(t / 90) for t in _write_evo(first, gops=3)]
    # The second EVO carries on the first's clock, 3 GOPs of 17 fields on.
    end = round(3 * sum(2 + rff for _kind, rff in _GOP) * _FIELD)
    times_b = [
        round((end + t) / 90)
        for t in _write_evo(second, gops=3, first_pts=900_000 + end)
    ]
    # mkvmerge runs late and gives the join's frame time to the block
    # before it.
    muxed = [0] + [time + 40 for time in times_a[1:-1]] + times_b[:1]
    muxed += [time + 40 for time in times_b]
    path = tmp_path / "a.mkv"
    _write_mkv(path, muxed)
    correct_vc1_frame_times(path, [first, second])
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    right = times_a + times_b
    assert [block.time for block in after.blocks] == right[:1] + right[:-1]


def test_correct_vc1_frame_times_takes_a_stretch_mkvmerge_runs_early(
    tmp_path: Path,
) -> None:
    evo = tmp_path / "a.evo"
    right = [round(t / 90) for t in _write_evo(evo, gops=4)]
    # 4 of 28 frames two frames early, as where a stream drops its B frames.
    muxed = [0] + [time + 40 for time in right[1:]]
    for index in range(20, 24):
        muxed[index] -= 67
    path = tmp_path / "a.mkv"
    _write_mkv(path, muxed)
    assert correct_vc1_frame_times(path, [evo]) is not None
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    assert [block.time for block in after.blocks] == right[:1] + right[:-1]


def test_correct_vc1_frame_times_refuses_a_first_frame_off_the_rest(
    tmp_path: Path,
) -> None:
    evo = tmp_path / "a.evo"
    right = [round(t / 90) for t in _write_evo(evo, gops=3)]
    # The first block 2 s before every other frame: not the EVO's first.
    muxed = [0] + [time + 2000 for time in right[1:]]
    path = tmp_path / "a.mkv"
    _write_mkv(path, muxed)
    before = path.read_bytes()
    with pytest.raises(FrameTimingError):
        correct_vc1_frame_times(path, [evo])
    assert path.read_bytes() == before


def test_correct_vc1_frame_times_refuses_a_frame_count_mismatch(
    tmp_path: Path,
) -> None:
    evo = tmp_path / "a.evo"
    right = [round(t / 90) for t in _write_evo(evo, gops=2)]
    path = tmp_path / "a.mkv"
    _write_mkv(path, right[:-1])
    before = path.read_bytes()
    with pytest.raises(FrameTimingError):
        correct_vc1_frame_times(path, [evo])
    assert path.read_bytes() == before


def test_correct_vc1_frame_times_skips_a_file_without_vc1(tmp_path: Path) -> None:
    path = tmp_path / "a.mkv"
    _write_mkv(path, [0, 33])
    data = path.read_bytes().replace(b"WVC1", b"H264")
    path.write_bytes(data)
    assert correct_vc1_frame_times(path, [tmp_path / "missing.evo"]) is None


def test_retime_vc1_join_frames_shows_the_frame_before_a_join(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.mkv"
    # Each file starts with two blocks at one time; at the join the first
    # carries the previous clip's last frame, so the frame before is held.
    _write_mkv(path, [0, 0, 42, 83, 125, 208, 208, 250, 292])
    assert retime_vc1_join_frames(path) == 1
    after = read_track_times(path, is_vc1_track)
    assert after is not None
    assert [block.time for block in after.blocks] == [
        0,
        0,
        42,
        83,
        125,
        166,
        208,
        250,
        292,
    ]


def test_mux_failure_to_correct_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(mkv, "log_warn", warnings.append)

    def fail(_path: Path, _inputs: object) -> int:
        raise FrameTimingError("12 frames in the source, 11 in the MKV")

    monkeypatch.setattr(mkv, "correct_vc1_frame_times", fail)
    mkv._correct_vc1_frame_times(tmp_path / "a.mkv", [tmp_path / "a.evo"])
    assert warnings and "VC-1" in warnings[0]


def test_mux_failure_to_retime_joins_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(mkv, "log_warn", warnings.append)

    def fail(_path: Path) -> int:
        raise ValueError("block time outside its cluster's range")

    monkeypatch.setattr(mkv, "retime_vc1_join_frames", fail)
    mkv._retime_vc1_join_frames(tmp_path / "a.mkv")
    assert warnings and "VC-1" in warnings[0]
