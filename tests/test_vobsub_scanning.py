"""Tests for DVD VOB subpicture packet assembly."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import vobsub
from vobsub import (
    _build_pes_entry,
    _dvd_main_content_range,
    _encode_pts,
    _PtsTimelineRebaser,
    _scan_vob_subpictures,
    _SpuAccumulator,
)


def test_spu_accumulator_joins_continuations_and_splits_on_pts() -> None:
    accumulator = _SpuAccumulator()

    assert accumulator.add(0x20, 100, b"first-") is None
    assert accumulator.add(0x20, 0, b"second") is None
    assert accumulator.add(0x20, 200, b"next") == (100, b"first-second")
    assert accumulator.flush() == {0x20: [(200, b"next")]}
    assert accumulator.flush() == {}


def test_pts_timeline_rebaser_snaps_clock_resets() -> None:
    """Non-seamless cells restart the STC; resets re-base onto the
    timeline end (mirroring mkvmerge's appended-segment handling)."""
    rebaser = _PtsTimelineRebaser(tolerance=5 * 90000)
    second = 90000

    # First cell: the raw clock runs 10s..12s.
    assert rebaser.rebase(10 * second) == 10 * second
    assert rebaser.rebase(12 * second) == 12 * second

    # Cell boundary: the clock restarts at 1s and snaps to the timeline
    # end (12s); the cell then continues from there.
    assert rebaser.rebase(1 * second) == 12 * second
    assert rebaser.rebase(3 * second) == 14 * second

    # Small backward jitter within a cell is not a reset.
    assert rebaser.rebase(int(2.5 * second)) == int(13.5 * second)


def _video_pes(pts: int) -> bytes:
    """Minimal video PES packet carrying one PTS (flags: PTS present)."""
    return (
        b"\x00\x00\x01\xe0"
        + struct.pack(">H", 10)
        + b"\x80\x80\x05"
        + _encode_pts(pts)
        + b"\x00" * 5
    )


def test_scan_vob_subpictures_rebases_cell_clock_resets(
    tmp_path: Path,
) -> None:
    """Subtitles past a non-seamless cell boundary keep a continuous
    timeline instead of collapsing onto the first cell's ~minutes clock.

    Treasure Planet (2002, R1 DVD9) resets its clock at nearly every one
    of 42 cell runs; without re-basing, its subtitle tracks ended at
    00:07:16 in a 95-minute movie. The video PES stream drives the shared
    timeline state (dense, true file order); subpictures map through it
    as the walk reaches them.
    """
    second = 90000
    packets: list[bytearray | bytes] = []
    packets += _build_pes_entry(b"cell-one-first", 10 * second, 0x20)
    packets.append(_video_pes(int(10.5 * second)))
    packets += _build_pes_entry(b"cell-one-last", 11 * second, 0x20)
    packets.append(_video_pes(int(11.5 * second)))
    # Cell boundary: the raw clock restarts at 2s.
    packets += _build_pes_entry(b"cell-two-first", 2 * second, 0x20)
    packets.append(_video_pes(int(2.5 * second)))
    packets += _build_pes_entry(b"cell-two-last", 4 * second, 0x20)
    packets.append(_video_pes(int(4.5 * second)))
    vob = tmp_path / "reset.vob"
    vob.write_bytes(b"".join(packets))

    result = _scan_vob_subpictures([vob], max_bytes=vob.stat().st_size)

    assert [pts for pts, _data in result[0x20]] == [
        10 * second,  # cell one
        11 * second,  # cell one
        # Cell two's first subpicture snaps to the video timeline at the
        # boundary (11.5s); its last maps 4s + 9.5s offset.
        int(11.5 * second),
        int(13.5 * second),
    ]


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


# --- PTS-scan main-content range -----------------------------------------------


def _pts_stream(*segments: tuple[int, int, int]) -> list[tuple[int, int]]:
    """(pts, byte_pos) samples from (start_pts, dur_ticks, byte_pos) cells.

    Each cell contributes a start and end sample; consecutive cells start
    at a fresh ~0.3 s clock so the segment builder sees a reset between
    them.
    """
    samples: list[tuple[int, int]] = []
    for start_pts, dur, position in segments:
        samples.append((start_pts, position))
        samples.append((start_pts + dur, position + 4096))
    return samples


@pytest.fixture
def pts_range_scan(monkeypatch: pytest.MonkeyPatch):
    """Patch the VOB PTS scan and pack snapping for range unit tests."""

    def install(samples: list[tuple[int, int]]) -> None:
        monkeypatch.setattr(vobsub, "_scan_vob_pts", lambda _inputs: samples)
        monkeypatch.setattr(vobsub, "_snap_to_pack", lambda _inputs, pos, _total: pos)

    return install


def test_main_content_range_keeps_multi_epoch_movies(
    tmp_path: Path, pts_range_scan
) -> None:
    """Treasure Planet-style: 6 clock-reset epochs of comparable length.

    The old "longest segment wins" rule truncated such movies to a single
    ~7-minute epoch; mutually comparable epochs must all be kept."""
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (1024 * 1024))
    pts_range_scan(
        _pts_stream(
            *[
                (int(0.3 * second), duration * second, 100_000 * index)
                for index, duration in enumerate((400, 360, 420, 380, 340, 390))
            ]
        )
    )

    assert _dvd_main_content_range([vob]) is None


def test_main_content_range_trims_leadin_junk(tmp_path: Path, pts_range_scan) -> None:
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (1024 * 1024))
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 10 * second, 0),  # FBI warning
            (int(0.3 * second), 5700 * second, 100_000),  # the movie
        )
    )

    assert _dvd_main_content_range([vob]) == (100_000, 1024 * 1024)


def test_main_content_range_trims_both_edges(tmp_path: Path, pts_range_scan) -> None:
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (10 * 1024 * 1024))
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 10 * second, 0),
            (int(0.3 * second), 5700 * second, 100_000),
            (int(0.3 * second), 30 * second, 9_000_000),  # trailing menu
        )
    )

    assert _dvd_main_content_range([vob]) == (100_000, 9_000_000)


def test_main_content_range_skips_trailing_edge_when_scan_capped(
    tmp_path: Path, pts_range_scan
) -> None:
    """The PTS scan is capped at 512 MB; a scan that stopped early leaves
    the last segment partially measured, so the trailing edge is kept."""
    second = 90000
    vob = tmp_path / "movie.vob"
    size = vobsub._PTS_SCAN_MAX_BYTES + 1024 * 1024
    with vob.open("wb") as handle:
        handle.seek(size - 1)
        handle.write(b"\x00")
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 10 * second, 0),
            (int(0.3 * second), 5700 * second, 100_000),
            (int(0.3 * second), 60 * second, 400_000_000),  # partial epoch
        )
    )

    assert _dvd_main_content_range([vob]) == (100_000, size)


def test_main_content_range_keeps_equal_episodes(
    tmp_path: Path, pts_range_scan
) -> None:
    """Two equal-length episodes sharing a VOB are both substantial; the
    old rule trimmed to one, the edge rule keeps both (no trim)."""
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (1024 * 1024))
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 1320 * second, 0),
            (int(0.3 * second), 1320 * second, 400_000_000),
        )
    )

    assert _dvd_main_content_range([vob]) is None


def test_main_content_range_single_run_needs_no_trim(
    tmp_path: Path, pts_range_scan
) -> None:
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (1024 * 1024))
    pts_range_scan(_pts_stream((int(0.3 * second), 5700 * second, 0)))

    assert _dvd_main_content_range([vob]) is None


def test_main_content_range_trims_small_trailer(tmp_path: Path, pts_range_scan) -> None:
    """A short trailing menu is trimmed even when the movie dominates —
    the old 98%-of-total rule skipped this trim."""
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (10 * 1024 * 1024))
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 5700 * second, 0),
            (int(0.3 * second), 30 * second, 9_000_000),
        )
    )

    assert _dvd_main_content_range([vob]) == (0, 9_000_000)


def test_main_content_range_refuses_to_drop_most_content(
    tmp_path: Path, pts_range_scan
) -> None:
    """A pathological scan (one long cell plus many junk-sized ones) must
    not trim away most of the content."""
    second = 90000
    vob = tmp_path / "movie.vob"
    vob.write_bytes(b"\x00" * (1024 * 1024))
    pts_range_scan(
        _pts_stream(
            (int(0.3 * second), 100 * second, 0),
            *[
                (int(0.3 * second), 39 * second, 200_000 * index)
                for index in range(1, 6)
            ],
        )
    )

    assert _dvd_main_content_range([vob]) is None


def _ps_pes(stream_id: int, payload_len: int = 64) -> bytes:
    return (
        b"\x00\x00\x01"
        + bytes([stream_id])
        + payload_len.to_bytes(2, "big")
        + bytes(payload_len)
    )


def test_scan_evo_video_stream_id_reads_pack_id(tmp_path: Path) -> None:
    evo = tmp_path / "FEATURE_1.EVO"
    evo.write_bytes(
        b"\x00\x00\x01\xba"
        + bytes(10)
        + _ps_pes(0xE2)
        + _ps_pes(0xBD, 32)
        + _ps_pes(0xE2)
    )
    assert vobsub._scan_evo_video_stream_id([evo]) == 0xE2


def test_scan_evo_video_stream_id_empty_without_video(tmp_path: Path) -> None:
    evo = tmp_path / "EMPTY.EVO"
    evo.write_bytes(b"\x00\x00\x01\xba" + bytes(10) + _ps_pes(0xBD, 32))
    assert vobsub._scan_evo_video_stream_id([evo]) is None
    assert vobsub._scan_evo_video_stream_id([tmp_path / "missing.EVO"]) is None
