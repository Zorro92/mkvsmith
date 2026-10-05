"""UI-agnostic rip session helpers.

What a front end needs to scan a disc and rip from it: scanning (with the
TheDiscDB lookup), the per-disc questions, which titles a "main feature" or
"episodes" rip means, which tracks a rip keeps, and running a batch of rips
with progress reported through callbacks. The CLI, the full-screen TUI, and a
future GUI all drive rips through these helpers; nothing here prints, reads
stdin, or exits. Questions go through ``UserPrompts`` hooks, which each front
end supplies.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from i18n import tr
from mkv import select_streams
from models import (
    Config,
    DiscMetadata,
    RipError,
    RuntimeState,
    Stream,
    TagOptions,
    Title,
    UserPrompts,
    log_info,
    log_warn,
)


# =============================================================================
# Scanning and the per-disc questions
# =============================================================================


def scan_source(
    source: Path, state: RuntimeState
) -> tuple[list[Title], DiscMetadata | None]:
    """Scan *source* (and look it up on TheDiscDB when that is on).

    Raises ``FileNotFoundError`` for a missing source; an empty title list
    means the source holds nothing to rip.
    """
    from disc_reader import _is_device_path
    from scan import Scanner

    if not source.exists() and not _is_device_path(source):
        raise FileNotFoundError(tr("Not found: {path}", path=source))
    scanner = Scanner(source, runtime_state=state)
    titles = scanner.scan()
    if titles and state.discdb_options.enabled:
        apply_discdb_lookup(titles, scanner.disc_metadata, state)
    return titles, scanner.disc_metadata


def apply_discdb_lookup(
    titles: list[Title], disc_metadata: DiscMetadata | None, state: RuntimeState
) -> None:
    """Name *titles* from a TheDiscDB match; failures only log a warning."""
    from discdb import DiscDbClient, DiscDbError, apply_discdb_match

    if disc_metadata is None:
        return
    try:
        result = DiscDbClient(state.discdb_options).lookup(disc_metadata)
        applied = apply_discdb_match(titles, disc_metadata, result)
    except DiscDbError as exc:
        log_warn(tr("TheDiscDB lookup failed (using local metadata): {err}", err=exc))
        return
    except (KeyError, TypeError, ValueError) as exc:
        log_warn(
            tr(
                "TheDiscDB returned unusable match data; local metadata retained: {err}",
                err=exc,
            )
        )
        return

    if not result.items:
        log_info(tr("No TheDiscDB match found"))
        return
    if result.ambiguous:
        log_warn(tr("Multiple TheDiscDB disc layouts matched; local metadata retained"))
        return

    if applied is None:
        if result.match is None:
            log_warn(tr("TheDiscDB results were ambiguous; local metadata retained"))
        else:
            log_warn(
                tr(
                    "TheDiscDB disc matched, but no local title correlated uniquely; "
                    "local metadata retained"
                )
            )
        return

    media_title = applied.media_item["title"]
    release_title = applied.release.get("title") or media_title
    log_info(
        tr(
            "TheDiscDB match: {media} ({release}); applied {count} title name(s)",
            media=media_title,
            release=release_title,
            count=len(applied.title_indexes),
        )
    )
    # A match can label titles as episodes.
    state.refresh_series_disc(titles)


def ask_list_encrypted(
    disc_metadata: DiscMetadata | None, prompts: UserPrompts
) -> bool:
    """For a CSS-encrypted DVD, ask whether to list its titles anyway.

    Nothing on such a disc can be ripped, so this comes before the other
    per-disc questions. True (go on) for any other disc.
    """
    if disc_metadata is None or not disc_metadata.css_encrypted:
        return True
    return prompts.confirm(
        tr(
            "This DVD is CSS-encrypted, so mkvsmith can't rip its titles. "
            "Decrypt it first (for example with a full disc backup) and rip "
            "the copy.\n\nShow its titles anyway? [y/N]:"
        )
    )


def ask_closed_captions(
    titles: list[Title], config: Config, prompts: UserPrompts
) -> None:
    """Closed captions saved as "ask": keep this disc's captions, or not?"""
    from dvdbuild import drop_closed_caption_streams, has_closed_captions

    if not config.ask_closed_captions or not has_closed_captions(titles):
        return
    if not prompts.confirm(
        tr("This disc has closed captions. Add them as a subtitle track? [y/N]")
    ):
        drop_closed_caption_streams(titles)


def packed_split_offers(titles: Sequence[Title], prompts: UserPrompts) -> list[int]:
    """Ask, per packed playlist, whether to split it; the indices to split."""
    from packed_episodes import packed_episode_count

    return [
        title.index
        for title in titles
        if title.packed_segments
        and prompts.confirm(
            tr(
                "Title {idx} holds {n} episodes in one playlist. Split it "
                "into one title per episode? [y/N]",
                idx=title.index,
                n=packed_episode_count(title),
            )
        )
    ]


def split_packed_episodes(
    titles: list[Title],
    indices: Sequence[int] | None,
    disc_metadata: DiscMetadata | None,
    state: RuntimeState,
) -> None:
    """Split title N (or every packed playlist) into one title per episode.

    *titles* is changed in place. Raises ``ValueError`` (translated) when
    the disc has no packed playlist or an index isn't one.
    """
    from packed_episodes import expand_packed_titles

    packed = [t.index for t in titles if t.packed_segments]
    if not packed:
        raise ValueError(tr("No playlist on this disc holds packed episodes"))
    for index in indices or []:
        if index not in packed:
            raise ValueError(tr("Title {idx} holds no packed episodes", idx=index))
    series = disc_metadata.series_info if disc_metadata else None
    titles[:] = expand_packed_titles(titles, list(indices or []) or None, series)
    state.refresh_series_disc(titles)


def ask_output_dir(config: Config, prompts: UserPrompts) -> Path:
    """Ask where this session's rips go (once: later calls just return it)."""
    while config.ask_output_dir:
        raw = prompts.text(tr("Output folder"), str(config.output_dir.absolute()))
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log_warn(tr("Cannot use {path}: {err}", path=path, err=e))
            continue
        config.output_dir = path
        config.ask_output_dir = False
    return config.output_dir


@dataclass
class TaggingChoice:
    """Whether to TMDB-tag a rip, asked per rip when tagging is "ask".

    Built once from the run's options (before anything changes them):
    *always* when --tag or the saved setting said so, *offer* when a TMDB
    key is available and tagging isn't off, *art* when the cover art was
    decided up front.
    """

    options: TagOptions
    always: bool
    offer: bool
    art: str | None

    @classmethod
    def from_options(cls, options: TagOptions) -> TaggingChoice:
        from tagger import _resolve_tmdb_key

        return cls(
            options=options,
            always=options.enabled,
            offer=(not options.no_tag) and bool(_resolve_tmdb_key(options)),
            art=options.art,
        )

    def prepare(self, prompts: UserPrompts) -> None:
        """Decide whether to tag the next rip and which artwork to attach."""
        from tagger import _prompt_art_choice, _tag_confirm

        if self.always:
            want = True
        elif self.offer:
            want = _tag_confirm(tr("Look up & tag this rip on TMDB?"), prompts)
        else:
            want = False
        self.options.enabled = want
        if want and self.art is None:
            self.options.art = _prompt_art_choice(prompts)


def ask_edition_names(count: int, prompts: UserPrompts) -> list[str]:
    """Display names for *count* editions (players show them in a picker)."""
    defaults = default_edition_names(count)
    return [
        prompts.text(tr("Name of edition {n}", n=n + 1), defaults[n]).strip()
        or defaults[n]
        for n in range(count)
    ]


def episode_titles(titles: Sequence[Title]) -> list[Title]:
    return [title for title in titles if title.is_episode]


def main_feature_titles(titles: Sequence[Title], config: Config) -> list[Title]:
    """What "rip the main feature" rips: one title, or a series disc's episodes.

    A series disc has no single main feature, so it means every episode.
    Empty when there is nothing to rip.
    """
    from scan import pick_main_feature

    episodes = episode_titles(titles)
    if episodes:
        return episodes
    index = pick_main_feature(list(titles), config)
    return [titles[index]] if index >= 0 else []


@dataclass(frozen=True)
class TrackChoice:
    """One of a title's streams and whether a default rip keeps it."""

    stream: Stream
    keep: bool


def track_plan(title: Title, config: Config) -> list[TrackChoice]:
    """Every stream of *title*, marked with whether *config* keeps it.

    A front end shows this as the title's track list (with checkboxes) and
    passes the kept streams back as ``RipJob.streams``.
    """
    # By identity: Stream.index isn't unique (every DVD stream has index 0).
    kept = {id(stream) for stream in select_streams(title, None, config)}
    return [TrackChoice(stream, id(stream) in kept) for stream in title.streams]


def fmt_edition_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def default_edition_names(count: int) -> list[str]:
    """Default edition labels: uniform Edition 1/2/... numbering."""
    return [tr("Edition {n}", n=pos + 1) for pos in range(count)]


def prepare_multi_edition(
    titles: Sequence[Title], indices: list[int], names: list[str] | None = None
) -> Title:
    """Validate title indices and build the combined multi-edition Title.

    Raises ValueError (with a translated message) for an unusable selection.
    """
    from scan import build_multi_edition_title

    if len(indices) < 2:
        raise ValueError(tr("Multi-edition needs at least two titles"))
    for idx in indices:
        if not 0 <= idx < len(titles):
            raise ValueError(tr("Invalid: {idx}", idx=idx))
    edition_titles = [titles[i] for i in indices]
    if len({t.source_file for t in edition_titles}) > 1:
        raise ValueError(tr("Multi-edition titles must come from the same source disc"))
    combined = build_multi_edition_title(edition_titles, names)
    for n, ed in enumerate(combined.editions, start=1):
        log_info(
            tr(
                "Edition {n}/{total}: {name} ({dur})",
                n=n,
                total=len(combined.editions),
                name=ed.name,
                dur=fmt_edition_duration(ed.duration),
            )
        )
    log_info(
        tr(
            "Combining {n} editions into one multi-edition MKV "
            "(requires a player with ordered-chapters support)",
            n=len(combined.editions),
        )
    )
    return combined


@dataclass(frozen=True)
class RipJob:
    """One output file: a title, and the streams to keep (None = defaults)."""

    title: Title
    streams: list[Stream] | None = None


@dataclass(frozen=True)
class RipOutcome:
    job: RipJob
    output: Path | None = None
    error: RipError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class RipBatchResult:
    outcomes: list[RipOutcome] = field(default_factory=list[RipOutcome])
    cancelled: bool = False

    @property
    def ok(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.ok)


class Ripper(Protocol):
    """What ``run_rip_jobs`` needs from a muxer (``mkv.MKVCreator``)."""

    on_progress: Callable[[str, int], None] | None
    cancelled: Callable[[], bool] | None

    def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path: ...


@dataclass
class RipCallbacks:
    """Optional progress hooks for ``run_rip_jobs``.

    *started* gets (job, position, total) with a 1-based position,
    *progress* gets (job, percent) while mkvmerge runs, and *finished* gets
    each job's outcome.
    """

    started: Callable[[RipJob, int, int], None] | None = None
    progress: Callable[[RipJob, int], None] | None = None
    finished: Callable[[RipOutcome], None] | None = None


def run_rip_jobs(
    creator: Ripper,
    jobs: Sequence[RipJob],
    callbacks: RipCallbacks | None = None,
    *,
    before_each: Callable[[RipJob], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RipBatchResult:
    """Rip each job in turn; a failure never stops the rest of the batch.

    *before_each* runs before every rip (e.g. the per-rip tagging question).
    *cancelled* is polled between jobs and, through the muxer, while a
    title is prepared and muxed; once it returns True the title being
    ripped stops (``RipCancelled``), the remaining jobs are skipped, and the
    result is marked cancelled.
    """
    hooks = callbacks or RipCallbacks()
    if cancelled is not None:
        saved_cancelled = creator.cancelled
        creator.cancelled = cancelled
        try:
            return _run_reporting(creator, jobs, hooks, before_each, cancelled)
        finally:
            creator.cancelled = saved_cancelled
    return _run_reporting(creator, jobs, hooks, before_each, cancelled)


def _run_reporting(
    creator: Ripper,
    jobs: Sequence[RipJob],
    hooks: RipCallbacks,
    before_each: Callable[[RipJob], None] | None,
    cancelled: Callable[[], bool] | None,
) -> RipBatchResult:
    if hooks.progress is None:
        return _run_jobs(creator, jobs, hooks, before_each, cancelled)
    report = hooks.progress
    saved_progress = creator.on_progress
    current: list[RipJob] = []
    creator.on_progress = lambda _name, pct: report(current[-1], pct)
    try:
        return _run_jobs(creator, jobs, hooks, before_each, cancelled, current.append)
    finally:
        creator.on_progress = saved_progress


def _run_jobs(
    creator: Ripper,
    jobs: Sequence[RipJob],
    hooks: RipCallbacks,
    before_each: Callable[[RipJob], None] | None,
    cancelled: Callable[[], bool] | None,
    set_current: Callable[[RipJob], None] | None = None,
) -> RipBatchResult:
    result = RipBatchResult()
    for position, job in enumerate(jobs, start=1):
        if cancelled is not None and cancelled():
            result.cancelled = True
            break
        if set_current is not None:
            set_current(job)
        if hooks.started is not None:
            hooks.started(job, position, len(jobs))
        if before_each is not None:
            before_each(job)
        try:
            outcome = RipOutcome(job, output=creator.create_mkv(job.title, job.streams))
        except RipError as exc:
            outcome = RipOutcome(job, error=exc)
        result.outcomes.append(outcome)
        if hooks.finished is not None:
            hooks.finished(outcome)
    return result
