"""Seamless-connection handling: MPLS connection_condition, M2TS tail scans,
the track-mode trim plan, and the muxer's staging of trimmed clip copies.

The ``*_tail_headers.m2ts`` fixtures are header-only carvings of the last
2 MiB of Monsters University clips 00875/00876 (see
``scripts/carve_m2ts_tail_fixture.py``): real PES timestamps and
stream_id_extension layout for video 0x1011, TrueHD 0x1100 (with its AC-3
core), AC-3 0x1101 and E-AC-3 0x1103, with all essence zeroed.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from mkvsmith import mkv
from mkvsmith.bluray import MplsPlayItem, _parse_mpls, has_seamless_connections
from mkvsmith.m2ts import (
    AudioTail,
    ClipTail,
    null_packets_from,
    plan_seamless_trim,
    scan_clip_tail,
)
from mkvsmith.mkv import MappedStream
from mkvsmith.models import Stream, StreamType, Title

AUDIO_PIDS = {0x1100, 0x1101, 0x1103}

# Disc-derived fixtures are not committed (see test_parser_fixtures.py and
# docs/DEVELOPMENT.md "Disc fixtures" section); these tests skip on a fresh clone.
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
needs_fixtures = pytest.mark.skipif(
    not all(
        (_FIXTURES_DIR / name).exists()
        for name in ("00800.mpls", "00875_tail_headers.m2ts", "00876_tail_headers.m2ts")
    ),
    reason="disc fixtures not present; capture them locally (see docs/DEVELOPMENT.md)",
)


def _tail(fixtures_dir: Path, clip: str) -> ClipTail:
    return scan_clip_tail(fixtures_dir / f"{clip}_tail_headers.m2ts", AUDIO_PIDS)


# --- MPLS connection_condition ----------------------------------------------


@needs_fixtures
def test_mpls_connection_conditions(fixtures_dir: Path) -> None:
    info = _parse_mpls(fixtures_dir / "00800.mpls")
    assert info is not None
    conditions = [item["connection_condition"] for item in info["play_items"]]
    assert len(conditions) == 132
    assert conditions[0] == 1
    assert set(conditions[1:]) == {5}
    assert has_seamless_connections(info["play_items"])


def test_has_seamless_connections_ignores_first_item() -> None:
    def item(clip: str, condition: int) -> MplsPlayItem:
        return {
            "clip": clip,
            "duration": 1.0,
            "in_time": 0,
            "out_time": 45000,
            "connection_condition": condition,
        }

    # The first PlayItem's condition describes no join.
    assert not has_seamless_connections([item("A", 5), item("B", 1)])
    assert has_seamless_connections([item("A", 1), item("B", 6)])


# --- tail scan ----------------------------------------------------------------


@needs_fixtures
def test_scan_clip_tail_real_clip(fixtures_dir: Path) -> None:
    tail = _tail(fixtures_dir, "00875")
    # 00875 OUT time is 144.116333 s; the last 23.976p frame ends there.
    assert tail.video_end == 12970470
    truehd = tail.audio[0x1100]
    # The TrueHD access units (0.833 ms), not the 32 ms embedded AC-3 core.
    assert truehd.frame_duration == 75
    assert truehd.end - tail.video_end == 15  # +0.17 ms
    ac3 = tail.audio[0x1101]
    assert ac3.frame_duration == 2880
    assert ac3.end - tail.video_end == 1290  # +14.3 ms overhang
    eac3 = tail.audio[0x1103]
    # Core + dependent frames share a PTS and collapse to one frame each.
    assert len({pts for pts, _ in eac3.frames}) == len(eac3.frames)
    assert eac3.frame_duration == 2880


# --- trim plan ----------------------------------------------------------------


def _synthetic(video_end: int, audio_end: int, duration: int) -> ClipTail:
    # File order follows PTS order, as in a real mux.
    frames = [(audio_end - duration * (5 - k), 1000 * k) for k in range(5)]
    return ClipTail(video_end=video_end, audio={0x1101: AudioTail(frames, duration)})


def test_plan_keeps_drift_within_half_a_frame() -> None:
    # Every clip's audio overhangs the video by 14 ms (1260 ticks).
    tails = [_synthetic(90_000, 90_000 + 1260, 2880) for _ in range(40)]
    plan = plan_seamless_trim(tails, {0x1101})
    assert plan is not None
    drift = 0
    for tail, cuts in zip(tails[:-1], plan):
        audio = tail.audio[0x1101]
        kept = [
            pts for pts, off in audio.frames if 0x1101 not in cuts or off < cuts[0x1101]
        ]
        drift += max(kept) + audio.frame_duration - 90_000
        assert abs(drift) <= 2880 // 2
    assert plan[-1] == {}  # nothing follows the last clip
    assert sum(1 for cuts in plan if cuts) > 0


def test_plan_rejects_audio_shorter_than_video() -> None:
    # Audio ends 0.6 s before the video in every clip (cannot add frames).
    tails = [_synthetic(90_000, 90_000 - 54_000, 2880) for _ in range(3)]
    assert plan_seamless_trim(tails, {0x1101}) is None


def test_plan_rejects_missing_track() -> None:
    tails = [_synthetic(90_000, 90_000, 2880), ClipTail(video_end=90_000)]
    tails.append(_synthetic(90_000, 90_000, 2880))
    assert plan_seamless_trim(tails, {0x1101}) is None


@needs_fixtures
def test_plan_on_real_clips_drops_the_overhanging_ac3_frame(fixtures_dir: Path) -> None:
    # 00875 then 00876 overhang the AC-3 by 14.3 + 5.2 ms: past half a frame
    # after the second join, so 00876's last AC-3 frame is dropped.
    tails = [_tail(fixtures_dir, c) for c in ("00875", "00876", "00875")]
    plan = plan_seamless_trim(tails, AUDIO_PIDS)
    assert plan is not None
    assert 0x1101 not in plan[0]
    ac3 = tails[1].audio[0x1101]
    _last_pts, last_offset = max(ac3.frames)
    assert plan[1][0x1101] == last_offset
    assert 0x1100 not in plan[0]  # TrueHD overhang is far below half a frame


# --- nulling ------------------------------------------------------------------


@needs_fixtures
def test_null_packets_from_drops_only_the_cut_pid(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    clip = tmp_path / "00876.m2ts"
    shutil.copyfile(fixtures_dir / "00876_tail_headers.m2ts", clip)
    before = scan_clip_tail(clip, AUDIO_PIDS)
    last_pts, last_offset = max(before.audio[0x1101].frames)
    size = clip.stat().st_size

    assert null_packets_from(clip, {0x1101: last_offset}) >= 1

    after = scan_clip_tail(clip, AUDIO_PIDS)
    assert clip.stat().st_size == size
    assert max(after.audio[0x1101].frames)[0] == last_pts - 2880
    assert after.video_end == before.video_end
    assert after.audio[0x1100].end == before.audio[0x1100].end
    assert after.audio[0x1103].end == before.audio[0x1103].end


# --- muxer staging ------------------------------------------------------------


def _mapped(pids: list[int]) -> list[MappedStream]:
    entries: list[MappedStream] = [
        {
            "input_id": 0,
            "type": "video",
            "stream": Stream(0, StreamType.VIDEO, "h264", "und", pid=0x1011),
            "ident_channels": None,
        }
    ]
    for index, pid in enumerate(pids, start=1):
        entries.append(
            {
                "input_id": index,
                "type": "audio",
                "stream": Stream(index, StreamType.AUDIO, "ac3", "eng", pid=pid),
                "ident_channels": 2,
            }
        )
    return entries


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_clips(fixtures_dir: Path, tmp_path: Path) -> list[Path]:
    source = tmp_path / "source"
    source.mkdir()
    clips: list[Path] = []
    for n, clip in enumerate(("00875", "00876", "00875")):
        path = source / f"{n:05d}.m2ts"
        shutil.copyfile(fixtures_dir / f"{clip}_tail_headers.m2ts", path)
        clips.append(path)
    return clips


@needs_fixtures
def test_prepare_seamless_append_copies_unowned_clips(
    fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def temp_base(*_args: object, **_kwargs: object) -> Path:
        return tmp_path

    monkeypatch.setattr("mkvsmith.disc_reader.temp_base_for_title", temp_base)
    clips = _staged_clips(fixtures_dir, tmp_path)
    digests = [_digest(p) for p in clips]
    title = Title(0, clips[0], "T", 10.0)
    title.seamless_connections = True
    cleanup: list[Path] = []
    temp_dirs: list[Path] = []

    result = mkv._prepare_seamless_append(
        title, clips, _mapped([0x1100, 0x1101]), set(), cleanup, temp_dirs
    )

    assert result is not None
    assert result[0] == clips[0] and result[2] == clips[2]  # untrimmed
    assert result[1] != clips[1] and result[1].parent == temp_dirs[0]
    assert cleanup == [result[1]]
    assert [_digest(p) for p in clips] == digests  # sources untouched
    assert _digest(result[1]) != digests[1]


@needs_fixtures
def test_prepare_seamless_append_trims_owned_clips_in_place(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    clips = _staged_clips(fixtures_dir, tmp_path)
    before = _digest(clips[1])
    title = Title(0, clips[0], "T", 10.0)
    title.seamless_connections = True
    cleanup: list[Path] = []
    temp_dirs: list[Path] = []

    result = mkv._prepare_seamless_append(
        title, clips, _mapped([0x1101]), set(clips), cleanup, temp_dirs
    )

    assert result == clips
    assert cleanup == [] and temp_dirs == []
    assert _digest(clips[1]) != before


@needs_fixtures
def test_prepare_seamless_append_skips_non_seamless_and_shared_pids(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    clips = _staged_clips(fixtures_dir, tmp_path)
    title = Title(0, clips[0], "T", 10.0)
    assert (
        mkv._prepare_seamless_append(title, clips, _mapped([0x1101]), set(), [], [])
        is None
    )
    title.seamless_connections = True
    # TrueHD + its AC-3 core both muxed from PID 0x1100: not trimmable.
    shared = _mapped([0x1100, 0x1100])
    assert mkv._prepare_seamless_append(title, clips, shared, set(), [], []) is None


def test_append_input_files_track_mode_for_trimmed_bluray_clips() -> None:
    cmd: list[str] = ["mkvmerge"]
    mkv._append_input_files(cmd, [Path("a.m2ts"), Path("b.m2ts")], [], track_mode=True)
    assert cmd == ["mkvmerge", "--append-mode", "track", "a.m2ts", "+", "b.m2ts"]


def test_clone_or_copy_preserves_content(tmp_path: Path) -> None:
    src = tmp_path / "clip.m2ts"
    src.write_bytes(bytes(range(256)) * 1024)
    dst = tmp_path / "copy.m2ts"

    mkv._clone_or_copy(src, dst)

    assert dst.read_bytes() == src.read_bytes()
    assert src.read_bytes() == bytes(range(256)) * 1024
