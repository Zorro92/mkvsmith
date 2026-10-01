"""Tests for episodes packed back to back into one Blu-ray playlist.

Synthetic chapter layouts cover the detection rules; the real Sgt. Frog
playlists (``sgtfrog_s1d1_00000.mpls``, ``sgtfrog_s2d1_00003.mpls``) guard
them end to end. Those fixtures are not committed; the tests skip without
them (see the README "Disc fixtures" section).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import cli
import mkv
from bluray import _parse_mpls
from models import Config, PackedSegment, RuntimeState, Stream, StreamType, Title
from packed_episodes import (
    detect_packed_episodes,
    expand_packed_titles,
    packed_episode_count,
    split_packed_title,
)

_FIXTURES = Path(__file__).parent / "fixtures"

# One episode's segments: opening, part A, part B, ending, preview (seconds).
_EPISODE = [90.0, 600.0, 610.0, 90.0, 31.0]


def _layout(segments: Sequence[float]) -> tuple[list[float], float]:
    """Chapter starts and total duration for back-to-back segment lengths."""
    marks: list[float] = []
    elapsed = 0.0
    for length in segments:
        marks.append(elapsed)
        elapsed += length
    return marks, elapsed


def _starts(segments: list[PackedSegment]) -> list[float]:
    return [s.start for s in segments]


def _needs(*names: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not all((_FIXTURES / name).exists() for name in names),
        reason="disc fixtures not present; capture them locally (see README)",
    )


# =============================================================================
# Detection
# =============================================================================


def test_detects_back_to_back_episodes_with_a_cold_open() -> None:
    marks, duration = _layout([47.0, *_EPISODE * 5])
    episode = sum(_EPISODE)

    segments = detect_packed_episodes(marks, duration)

    assert [s.episode for s in segments] == [1, 2, 3, 4, 5]
    # Episode 1 keeps its cold open; later ones start at their opening song.
    assert _starts(segments) == [0.0, *(47.0 + n * episode for n in range(1, 5))]
    assert segments[-1].end == duration


def test_intro_before_the_opening_song_opens_the_next_episode() -> None:
    marks, duration = _layout([30.0, *_EPISODE] * 4)
    cycle = 30.0 + sum(_EPISODE)

    segments = detect_packed_episodes(marks, duration)

    assert _starts(segments) == [n * cycle for n in range(4)]


def test_teaser_after_the_preview_opens_the_next_episode() -> None:
    # Sgt. Frog S2 D1: some episodes are followed by a 15 s teaser (the next
    # episode's cold open) after their preview. It belongs to the next
    # episode even though that makes the lengths uneven; viewing confirmed
    # an earlier length-based tiebreak put it on the wrong side.
    teased = [*_EPISODE, 15.0]
    marks, duration = _layout([*_EPISODE, *teased, *_EPISODE, *_EPISODE])
    episode = sum(_EPISODE)

    segments = detect_packed_episodes(marks, duration)

    assert _starts(segments) == [0.0, episode, 2 * episode, 3 * episode + 15.0]
    lengths = [round(s.end - s.start) for s in segments]
    assert lengths == [1421, 1421, 1436, 1421]


def test_tail_beyond_a_typical_episode_becomes_an_extra() -> None:
    marks, duration = _layout([*_EPISODE * 4, 248.0])

    segments = detect_packed_episodes(marks, duration)

    assert [s.episode for s in segments] == [1, 2, 3, 4, None]
    assert segments[-1].end - segments[-1].start == pytest.approx(248.0)


@pytest.mark.parametrize(
    "lengths",
    [
        # A movie: irregular chapters.
        [312.0, 455.0, 610.0, 97.0, 388.0, 720.0, 505.0, 263.0, 840.0, 411.0],
        # Auto-chapters every 20 minutes: no short anchor segment.
        [1200.0] * 9,
        # Only two cycles.
        _EPISODE * 2,
    ],
    ids=["movie", "auto-chapters", "two-cycles"],
)
def test_non_episodic_playlists_are_not_split(lengths: list[float]) -> None:
    marks, duration = _layout(lengths)

    assert detect_packed_episodes(marks, duration) == []


@_needs("sgtfrog_s1d1_00000.mpls")
def test_real_season_1_disc_1_playlist() -> None:
    info = _parse_mpls(_FIXTURES / "sgtfrog_s1d1_00000.mpls")
    assert info is not None
    duration = sum(item["duration"] for item in info["play_items"])

    segments = detect_packed_episodes(info["chapter_times"], duration)

    episodes = [s for s in segments if s.episode is not None]
    assert len(episodes) == 40
    assert all(23.5 * 60 < s.end - s.start < 23.7 * 60 for s in episodes)
    assert [
        round((s.end - s.start) / 60, 1) for s in segments if s.episode is None
    ] == [3.0]


@_needs("sgtfrog_s2d1_00003.mpls")
def test_real_season_2_disc_1_playlist_with_intros() -> None:
    info = _parse_mpls(_FIXTURES / "sgtfrog_s2d1_00003.mpls")
    assert info is not None
    duration = sum(item["duration"] for item in info["play_items"])

    segments = detect_packed_episodes(info["chapter_times"], duration)

    episodes = [s for s in segments if s.episode is not None]
    assert len(episodes) == 38
    assert all(23.8 * 60 < s.end - s.start < 24.9 * 60 for s in episodes)
    # Confirmed by viewing: episode 6 opens with a 15 s teaser that follows
    # episode 5's preview, so episode 5 ends at its preview.
    sixth = episodes[5]
    marks = info["chapter_times"]
    after = min(m for m in marks if m > sixth.start)
    assert after - sixth.start == pytest.approx(15.0, abs=1.0)
    assert [
        round((s.end - s.start) / 60, 1) for s in segments if s.episode is None
    ] == [4.1]


# =============================================================================
# Splitting
# =============================================================================


def _packed_title(tmp_path: Path, *, iso: bool = False) -> Title:
    """Three 50-minute clips holding four 35-minute episodes (+ a 10 min extra)."""
    clips = [tmp_path / f"0000{i}.m2ts" for i in range(3)]
    title = Title(
        index=0,
        source_file=tmp_path / "disc.iso" if iso else clips[0],
        name="Show Disc 1",
        duration_seconds=9000.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    if iso:
        title.iso_internal_paths = [f"BDMV/STREAM/0000{i}.m2ts" for i in range(3)]
    else:
        title.append_clips = clips[1:]
    title.clip_durations = [3000.0, 3000.0, 3000.0]
    title.clip_sizes = [100, 200, 300]
    title.chapters = [0.0, 1000.0, 2100.0, 3100.0, 4200.0, 5200.0, 6300.0, 8400.0]
    title.playlist_name = "00000"
    title.packed_segments = [
        PackedSegment(0.0, 2100.0, 1),
        PackedSegment(2100.0, 4200.0, 2),
        PackedSegment(4200.0, 6300.0, 3),
        PackedSegment(6300.0, 8400.0, 4),
        PackedSegment(8400.0, 9000.0, None),
    ]
    return title


def test_split_cuts_each_episode_from_the_clips_it_spans(tmp_path: Path) -> None:
    parent = _packed_title(tmp_path)

    children = split_packed_title(parent)

    assert [c.name for c in children] == [
        "Show Disc 1 - Episode 1",
        "Show Disc 1 - Episode 2",
        "Show Disc 1 - Episode 3",
        "Show Disc 1 - Episode 4",
        "Show Disc 1 - Extra 1",
    ]
    second = children[1]  # 2100-4200 s spans clips 0 and 1
    assert second.source_file == tmp_path / "00000.m2ts"
    assert second.append_clips == [tmp_path / "00001.m2ts"]
    assert second.packed_range == (2100.0, 4200.0)
    assert second.chapters == [2100.0, 3100.0]
    assert second.clip_sizes == [100, 200]
    assert second.packed_episode_number == 2
    fourth = children[3]  # 6300-8400 s lies inside clip 2 (offset 6000)
    assert fourth.source_file == tmp_path / "00002.m2ts"
    assert fourth.append_clips == []
    assert fourth.packed_range == (300.0, 2400.0)
    assert fourth.chapters == [300.0]
    assert fourth.duration_seconds == 2100.0
    assert children[4].packed_episode_number is None
    assert all(not c.packed_segments for c in children)
    assert parent.packed_segments  # the parent itself is left untouched


def test_split_iso_title_slices_internal_paths(tmp_path: Path) -> None:
    children = split_packed_title(_packed_title(tmp_path, iso=True))

    assert children[1].source_file == tmp_path / "disc.iso"
    assert children[1].iso_internal_paths == [
        "BDMV/STREAM/00000.m2ts",
        "BDMV/STREAM/00001.m2ts",
    ]
    assert children[1].append_clips == []


def test_expand_replaces_only_requested_titles_and_reindexes(tmp_path: Path) -> None:
    packed = _packed_title(tmp_path)
    other = Title(
        index=1, source_file=tmp_path / "x.m2ts", name="X", duration_seconds=60
    )

    untouched = expand_packed_titles([packed, other], indices=[1])
    assert untouched == [packed, other]

    expanded = expand_packed_titles([packed, other])
    assert len(expanded) == 6
    assert [t.index for t in expanded] == list(range(6))
    assert expanded[-1] is other
    assert packed_episode_count(packed) == 4


# =============================================================================
# Muxing, scanning and the interactive prompt
# =============================================================================


def test_mkvmerge_timestamp_format() -> None:
    assert mkv._mkvmerge_timestamp(0.0) == "00:00:00.000000000"
    assert mkv._mkvmerge_timestamp(4383.15) == "01:13:03.150000000"


def test_mux_command_cuts_the_episode_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    episode = split_packed_title(_packed_title(tmp_path))[3]

    cleanup: list[Path] = []
    cmd = mkv._build_mkvmerge_command(
        episode,
        tmp_path / "ep.mkv",
        [episode.source_file],
        [],
        [],
        None,
        [],
        None,
        [],
        [],
        None,
        cleanup,
        [],
    )

    split = cmd[cmd.index("--split") + 1]
    assert split == "parts:00:05:00.000000000-00:40:00.000000000"
    # The lone chapter (at the cut start) survives the end-of-title filter.
    chapters_xml = Path(cmd[cmd.index("--chapters") + 1]).read_text()
    assert "00:05:00" in chapters_xml


def _detectable_title(tmp_path: Path) -> Title:
    """A one-clip playlist whose chapters hold four back-to-back episodes."""
    marks, duration = _layout(_EPISODE * 4)
    title = Title(
        index=0,
        source_file=tmp_path / "00000.m2ts",
        name="Show",
        duration_seconds=duration,
    )
    title.chapters = marks
    title.clip_durations = [duration]
    title.playlist_name = "00000"
    return title


@pytest.mark.parametrize("split", [False, True])
def test_scan_offers_but_only_splits_on_request(tmp_path: Path, split: bool) -> None:
    from scan import Scanner

    scanner = Scanner(
        tmp_path, runtime_state=RuntimeState(config=Config(split_episodes=split))
    )
    scanner.titles = [_detectable_title(tmp_path)]

    scanner._offer_packed_episodes()

    if split:
        assert [t.packed_episode_number for t in scanner.titles] == [1, 2, 3, 4]
    else:
        assert len(scanner.titles) == 1
        assert packed_episode_count(scanner.titles[0]) == 4


def test_interactive_se_splits_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shown: list[int] = []

    def record_display(titles: list[Title], *_args: object, **_kwargs: object) -> None:
        shown.append(len(titles))

    monkeypatch.setattr(cli, "display_titles", record_display)
    titles = [_packed_title(tmp_path)]
    state = RuntimeState()
    creator = cli.MKVCreator(tmp_path, runtime_state=state)
    tagging = cli._InteractiveTagState.from_options(state.tag_options, state.prompts)
    ripper = cli._InteractiveRipper(titles, creator, tagging, [])

    assert ripper._dispatch("se", ["7"]) is True
    assert len(ripper.titles) == 1  # invalid index: nothing split
    assert ripper._dispatch("se", []) is True
    assert len(ripper.titles) == 5 and titles is ripper.titles
    assert shown == [5]
    assert all(cli._is_episode_title(t) for t in ripper.titles[:4])


def test_split_episodes_flag_reaches_config() -> None:
    parser = cli._build_arg_parser()
    assert parser.parse_args(["disc"]).split_episodes is False
    assert parser.parse_args(["--split-episodes", "disc"]).split_episodes is True
