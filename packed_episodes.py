"""Episodes packed back to back into one long Blu-ray playlist.

Some TV-series Blu-rays (Sgt. Frog, for one) play a whole disc's episodes
from a single 15-20 hour playlist instead of one playlist per episode. The
episode boundaries are still recoverable from the playlist's chapter marks
alone, with no bitstream probing: every episode repeats the same short
segments (opening song, ending song, next-episode preview) around its long
story parts.

Detection:

1. Anchor: a kind of *short* segment (under ``_SHORT`` seconds, grouped by
   length) that recurs at near-uniform 15-70 minute spacing. The opening and
   ending songs often have the same length, so occurrences are also split by
   whether a short or a long segment precedes them. The anchor kind whose
   first occurrence is earliest wins (the opening song, not the ending).
2. Boundaries: the run of short segments directly before an anchor holds
   the previous episode's ending; the shortest such run on the disc is that
   ending pattern. Extra short segments in a longer run (an intro or cold
   open) open the next episode, even when that leaves episode lengths
   uneven: a teaser after the preview belongs to the episode it introduces.
   When every episode has an intro (all runs equally long), the playlist's
   own lead-in before episode 1 identifies it.
3. Tail: a final episode running more than ``_SNAP`` seconds past the typical
   length is cut at the mark nearest that length; the rest is an extra.

Movies never match: their chapters are irregular, and step 1 needs at least
three near-uniform cycles anchored on a short segment.
"""

from __future__ import annotations

import statistics
from collections.abc import Collection, Sequence
from dataclasses import replace

from episode_naming import episode_number_width, episode_title, series_name
from models import PackedSegment, SeriesInfo, Title

_SHORT = 180.0  # seconds; a segment shorter than this can be an anchor
_BUCKET = 5.0  # short segments within this many seconds are the same kind
_MIN_EPISODE = 15 * 60.0
_MAX_EPISODE = 70 * 60.0
_MAX_SPREAD = 0.12  # every cycle within ±12% of the median
_MIN_CYCLES = 3
_LEAD_MATCH = 1.5  # seconds; a recurring intro matches the playlist lead-in
_SNAP = 60.0  # tail beyond typical length that is split off as an extra
_EPSILON = 1e-3


def detect_packed_episodes(
    chapters: Sequence[float], duration: float
) -> list[PackedSegment]:
    """Episodes (and any trailing extra) packed into one playlist, or ``[]``.

    *chapters* are playlist-timeline chapter starts in seconds; *duration* is
    the playlist length.
    """
    marks = sorted(c for c in chapters if 0 <= c < duration)
    if len(marks) < _MIN_CYCLES:
        return []
    bounds = [*marks, duration]
    gaps = [b - a for a, b in zip(bounds, bounds[1:])]
    found = _find_anchors(marks, gaps)
    if found is None:
        return []
    anchors, typical = found
    starts = _episode_starts(marks, gaps, anchors)
    segments: list[PackedSegment] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else duration
        is_last = n + 1 == len(starts)
        if is_last and end - start > typical + _SNAP:
            cut = min(
                (t for t in bounds if t > start),
                key=lambda t: abs(t - (start + typical)),
            )
            if abs(cut - (start + typical)) <= _SNAP and cut < end:
                segments.append(PackedSegment(start, cut, n + 1))
                segments.append(PackedSegment(cut, end, None))
                break
        segments.append(PackedSegment(start, end, n + 1))
    return segments


def _find_anchors(
    marks: list[float], gaps: list[float]
) -> tuple[list[int], float] | None:
    kinds: dict[int, list[int]] = {}
    for i, gap in enumerate(gaps[: len(marks)]):
        if 0 < gap < _SHORT:
            kinds.setdefault(round(gap / _BUCKET), []).append(i)
    best: tuple[tuple[float, int], list[int], float] | None = None
    for indices in kinds.values():
        for after_short in (True, False):
            anchors = [
                i for i in indices if (i == 0 or gaps[i - 1] < _SHORT) == after_short
            ]
            if len(anchors) < _MIN_CYCLES:
                continue
            spacing = [marks[b] - marks[a] for a, b in zip(anchors, anchors[1:])]
            typical = statistics.median(spacing)
            if not _MIN_EPISODE <= typical <= _MAX_EPISODE:
                continue
            if any(abs(s - typical) > _MAX_SPREAD * typical for s in spacing):
                continue
            score = (marks[anchors[0]], -len(anchors))
            if best is None or score < best[0]:
                best = (score, anchors, typical)
    return None if best is None else (best[1], best[2])


def _episode_starts(
    marks: list[float], gaps: list[float], anchors: list[int]
) -> list[float]:
    def short_run_before(i: int) -> int:
        n = 0
        while i - n - 1 >= 0 and gaps[i - n - 1] < _SHORT:
            n += 1
        return n

    runs = [short_run_before(a) for a in anchors[1:]]
    ending = min(runs)
    # When every run is the same length, the shortest run can't separate an
    # intro (recurring before each opening) from the ending. The playlist's
    # own lead-in before episode 1's anchor is the reference: if every run
    # ends in segments of those lengths, they are intros.
    lead = anchors[0] if all(g < _SHORT for g in gaps[: anchors[0]]) else 0
    if 0 < lead < ending and len(set(runs)) == 1:
        lead_in = gaps[:lead]
        if all(
            all(
                abs(g - want) <= _LEAD_MATCH
                for g, want in zip(gaps[a - lead : a], lead_in)
            )
            for a in anchors[1:]
        ):
            ending -= lead
    starts = [marks[0]]
    for anchor, run in zip(anchors[1:], runs):
        starts.append(marks[anchor - (run - ending)])
    return starts


# =============================================================================
# Splitting packed titles into one title per episode
# =============================================================================


def annotate_packed_titles(titles: Sequence[Title]) -> None:
    """Record detected packed episodes on each eligible Blu-ray title."""
    for title in titles:
        if title.hddvd_title_number is not None or not title.playlist_name:
            continue
        if len(title.clip_durations) != _clip_count(title):
            continue
        title.packed_segments = detect_packed_episodes(
            title.chapters, title.duration_seconds
        )


def packed_episode_count(title: Title) -> int:
    return sum(1 for segment in title.packed_segments if segment.episode is not None)


def _clip_count(title: Title) -> int:
    if title.iso_internal_paths:
        return len(title.iso_internal_paths)
    return 1 + len(title.append_clips)


def split_packed_title(
    parent: Title, series_info: SeriesInfo | None = None
) -> list[Title]:
    """One title per packed segment of *parent*, cut from the clips it spans.

    Each child keeps only the clips overlapping its segment; its chapters and
    ``packed_range`` are on the timeline of those clips, which is what
    mkvmerge sees when it cuts with ``--split parts:``.
    """
    offsets: list[float] = []
    elapsed = 0.0
    for clip_duration in parent.clip_durations:
        offsets.append(elapsed)
        elapsed += clip_duration
    children: list[Title] = []
    extra_number = 0
    width = episode_number_width(
        max((s.episode or 0 for s in parent.packed_segments), default=0)
    )
    for segment in parent.packed_segments:
        first = max(
            i for i, off in enumerate(offsets) if off <= segment.start + _EPSILON
        )
        last = min(
            i
            for i, off in enumerate(offsets)
            if off + parent.clip_durations[i] >= segment.end - _EPSILON
        )
        base = offsets[first]
        if segment.episode is not None:
            name = episode_title(series_info, parent.name, segment.episode, width=width)
        else:
            extra_number += 1
            name = f"{series_name(series_info, parent.name)} - Extra {extra_number}"
        # ISO titles name their clips by internal path; folder titles by file.
        iso_paths = list(parent.iso_internal_paths[first : last + 1])
        clips = [parent.source_file, *parent.append_clips]
        source_file = parent.source_file if iso_paths else clips[first]
        append_clips = [] if iso_paths else clips[first + 1 : last + 1]
        sizes = parent.clip_sizes[first : last + 1]
        children.append(
            replace(
                parent,
                source_file=source_file,
                name=name,
                duration_seconds=segment.end - segment.start,
                streams=list(parent.streams),
                iso_internal_paths=iso_paths,
                append_clips=append_clips,
                estimated_size_bytes=sum(sizes) or parent.estimated_size_bytes,
                chapters=[
                    c - base
                    for c in parent.chapters
                    if segment.start <= c < segment.end
                ],
                clip_durations=parent.clip_durations[first : last + 1],
                clip_sizes=sizes,
                packed_segments=[],
                packed_range=(segment.start - base, segment.end - base),
                packed_episode_number=segment.episode,
                editions=[],
            )
        )
    return children


def expand_packed_titles(
    titles: Sequence[Title],
    indices: Collection[int] | None = None,
    series_info: SeriesInfo | None = None,
) -> list[Title]:
    """*titles* with packed playlists replaced in place by their episodes.

    *indices* limits the split to those title indices (all packed titles when
    ``None``). The result is re-indexed by position, so title numbers change
    after the first split title.
    """
    expanded: list[Title] = []
    for title in titles:
        if title.packed_segments and (indices is None or title.index in indices):
            expanded.extend(split_packed_title(title, series_info))
        else:
            expanded.append(title)
    for position, title in enumerate(expanded):
        title.index = position
    return expanded
