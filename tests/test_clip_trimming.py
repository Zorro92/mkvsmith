"""Blu-ray titles whose playlist plays only part of a clip.

A play item's in/out times can pick a stretch of a longer clip. The clips
are appended whole and cut to the played parts with ``--split parts:``; the
title's chapters are on the playlist's timeline and are written after the
mux (docs/formats/bluray.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import mkv
from mkvsmith.models import ClipSpan, Title


def _title(tmp_path: Path, spans: list[ClipSpan], chapters: list[float]) -> Title:
    title = Title(
        0, tmp_path / "00001.m2ts", "Extra", sum(s.end - s.start for s in spans)
    )
    title.append_clips = [tmp_path / f"0000{n}.m2ts" for n in range(2, len(spans) + 1)]
    title.clip_spans = spans
    title.clip_durations = [span.end - span.start for span in spans]
    title.chapters = chapters
    return title


def test_whole_clips_need_no_trim(tmp_path: Path) -> None:
    title = _title(tmp_path, [], [0.0])
    assert mkv._clip_trim_plan(title) is None


def test_the_played_parts_are_kept_and_joined(tmp_path: Path) -> None:
    # 65 s from the middle of an 8.5-minute clip, then a whole 0.25 s clip:
    # the second clip starts on mkvmerge's timeline where the first ends.
    title = _title(
        tmp_path,
        [ClipSpan(436.436, 501.501, 509.842667), ClipSpan(0.0, 0.25, 0.25)],
        [0.0, 30.0],
    )

    plan = mkv._clip_trim_plan(title)

    assert plan is not None
    assert plan.ranges == [
        (pytest.approx(436.436), pytest.approx(501.501)),
        (pytest.approx(509.842667), pytest.approx(510.092667)),
    ]
    assert plan.chapters == [0.0, 30.0]


def test_contiguous_parts_make_one_range(tmp_path: Path) -> None:
    # A clip played to its end, then the next from its start: one cut.
    title = _title(
        tmp_path, [ClipSpan(100.0, 200.0, 200.0), ClipSpan(0.0, 50.0, 300.0)], []
    )

    plan = mkv._clip_trim_plan(title)

    assert plan is not None
    assert plan.ranges == [(100.0, 250.0)]


def test_a_packed_episode_window_is_mapped_onto_the_clips(tmp_path: Path) -> None:
    # The title (playlist) timeline runs 0-100 over the first clip's 100-200
    # and 100-150 over the second's 0-50; an episode from 80 to 120 spans
    # both, with a chapter at 90 on the title timeline.
    title = _title(
        tmp_path, [ClipSpan(100.0, 200.0, 300.0), ClipSpan(0.0, 50.0, 60.0)], [90.0]
    )
    title.packed_range = (80.0, 120.0)

    plan = mkv._clip_trim_plan(title)

    assert plan is not None
    assert plan.ranges == [(180.0, 200.0), (300.0, 320.0)]
    assert plan.chapters == [10.0]


def test_a_dropped_clip_drops_its_part(tmp_path: Path) -> None:
    title = _title(
        tmp_path,
        [
            ClipSpan(10.0, 20.0, 30.0),
            ClipSpan(0.0, 5.0, 5.0),
            ClipSpan(0.0, 40.0, 60.0),
        ],
        [0.0, 12.0, 20.0],
    )

    # The second clip is left out of the append, so the third follows the
    # first on mkvmerge's timeline; the chapter in the dropped clip goes.
    plan = mkv._clip_trim_plan(title, dropped=[1])

    assert plan is not None
    assert plan.ranges == [(10.0, 20.0), (30.0, 70.0)]
    assert plan.chapters == [0.0, 15.0]


def test_the_mux_command_cuts_the_parts_and_leaves_chapters_for_later(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    title = _title(
        tmp_path,
        [ClipSpan(436.436, 501.501, 509.842667), ClipSpan(0.0, 0.25, 0.25)],
        [0.0],
    )
    plan = mkv._clip_trim_plan(title)

    cmd = mkv._build_mkvmerge_command(
        title,
        tmp_path / "extra.mkv",
        [title.source_file, *title.append_clips],
        [],
        [],
        None,
        [],
        None,
        [],
        [],
        None,
        [],
        [],
        trim=plan,
    )

    assert cmd[cmd.index("--split") + 1] == (
        "parts:00:07:16.436000000-00:08:21.501000000,"
        "+00:08:29.842667000-00:08:30.092667000"
    )
    assert "--chapters" not in cmd
