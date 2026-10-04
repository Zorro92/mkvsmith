"""UI-agnostic session helpers (session.py) and the hooks they rely on."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

import models
import session
import tagger
from mkv import MKVCreator
from models import Config, RipError, RuntimeState, Stream, StreamType, Title
from session import (
    RipCallbacks,
    RipJob,
    RipOutcome,
    episode_titles,
    main_feature_titles,
    run_rip_jobs,
    track_plan,
)


def make_title(index: int, duration: float = 100.0, **attributes: object) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"title-{index}.m2ts"),
        name=f"Title {index}",
        duration_seconds=duration,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO),
        Stream(index=1, stream_type=StreamType.AUDIO, language="eng"),
        Stream(index=2, stream_type=StreamType.AUDIO, language="fra", type_index=1),
        Stream(index=3, stream_type=StreamType.SUBTITLE, language="eng"),
        Stream(index=4, stream_type=StreamType.SUBTITLE, language="fra", type_index=1),
    ]
    for name, value in attributes.items():
        setattr(title, name, value)
    return title


class FakeCreator:
    """Stands in for MKVCreator: "rips" by recording, fails on request."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.fail = fail or set()
        self.ripped: list[tuple[int, list[Stream] | None]] = []
        self.on_progress: Callable[[str, int], None] | None = None

    def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path:
        if self.on_progress is not None:
            self.on_progress(f"{title.name}.mkv", 50)
        if title.index in self.fail:
            raise RipError(message=f"boom {title.index}", title=title)
        self.ripped.append((title.index, streams))
        return Path(f"{title.name}.mkv")


def test_track_plan_marks_what_a_default_rip_keeps() -> None:
    title = make_title(0)
    config = Config(
        preferred_languages=["eng"], keep_all_audio=False, keep_all_subtitles=True
    )

    plan = track_plan(title, config)

    assert [choice.stream.index for choice in plan] == [0, 1, 2, 3, 4]
    assert [choice.keep for choice in plan] == [True, True, False, True, False]


def test_track_plan_follows_session_option_changes() -> None:
    title = make_title(0)
    config = Config(preferred_languages=["eng"], keep_all_audio=False)

    config.keep_all_audio = True
    config.all_subtitle_languages = True

    assert all(choice.keep for choice in track_plan(title, config))


def test_main_feature_titles_picks_one_title_on_a_movie_disc() -> None:
    titles = [make_title(0, 600.0), make_title(1, 6000.0)]

    assert main_feature_titles(titles, Config()) == [titles[1]]


def test_main_feature_titles_means_the_episodes_on_a_series_disc() -> None:
    titles = [
        make_title(0, 1300.0, episode_number=1),
        make_title(1, 7000.0),
        make_title(2, 1300.0, episode_number=2),
    ]

    assert main_feature_titles(titles, Config()) == [titles[0], titles[2]]
    assert episode_titles(titles) == [titles[0], titles[2]]


def test_main_feature_titles_is_empty_without_titles() -> None:
    assert main_feature_titles([], Config()) == []


def test_run_rip_jobs_keeps_going_after_a_failure() -> None:
    titles = [make_title(0), make_title(1), make_title(2)]
    creator = FakeCreator(fail={1})
    finished: list[RipOutcome] = []

    result = run_rip_jobs(
        creator,
        [RipJob(title) for title in titles],
        RipCallbacks(finished=finished.append),
    )

    assert [index for index, _ in creator.ripped] == [0, 2]
    assert (result.ok, result.failed, result.cancelled) == (2, 1, False)
    assert [outcome.ok for outcome in finished] == [True, False, True]
    assert finished[1].error is not None and finished[1].output is None
    assert finished[0].output == Path("Title 0.mkv")


def test_run_rip_jobs_passes_explicit_streams() -> None:
    title = make_title(0)
    creator = FakeCreator()

    run_rip_jobs(creator, [RipJob(title, title.streams[:2])])

    assert creator.ripped == [(0, title.streams[:2])]


def test_run_rip_jobs_reports_start_and_progress_per_job() -> None:
    jobs = [RipJob(make_title(0)), RipJob(make_title(1))]
    creator = FakeCreator()
    events: list[tuple[str, int, int]] = []

    run_rip_jobs(
        creator,
        jobs,
        RipCallbacks(
            started=lambda job, pos, total: events.append(
                ("start", job.title.index, pos * 10 + total)
            ),
            progress=lambda job, pct: events.append(("pct", job.title.index, pct)),
        ),
    )

    assert events == [
        ("start", 0, 12),
        ("pct", 0, 50),
        ("start", 1, 22),
        ("pct", 1, 50),
    ]
    # The creator's own progress display is restored afterwards.
    assert creator.on_progress is None


def test_run_rip_jobs_stops_between_jobs_when_cancelled() -> None:
    jobs = [RipJob(make_title(i)) for i in range(3)]
    creator = FakeCreator()

    def cancel_after_first() -> bool:
        return len(creator.ripped) >= 1

    result = run_rip_jobs(creator, jobs, cancelled=cancel_after_first)

    assert [index for index, _ in creator.ripped] == [0]
    assert result.cancelled is True
    assert len(result.outcomes) == 1


def test_run_rip_jobs_runs_before_each_ahead_of_every_rip() -> None:
    order: list[str] = []

    class OrderedCreator(FakeCreator):
        def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path:
            order.append(f"rip {title.index}")
            return super().create_mkv(title, streams)

    def before(job: RipJob) -> None:
        order.append(f"before {job.title.index}")

    run_rip_jobs(
        OrderedCreator(),
        [RipJob(make_title(0)), RipJob(make_title(1))],
        before_each=before,
    )

    assert order == ["before 0", "rip 0", "before 1", "rip 1"]


def test_mkv_creator_sends_progress_to_the_callback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creator = MKVCreator(tmp_path, runtime_state=RuntimeState())
    seen: list[tuple[str, int]] = []
    creator.on_progress = lambda name, pct: seen.append((name, pct))

    creator._show_progress("movie.mkv", 42)

    assert seen == [("movie.mkv", 42)]
    assert capsys.readouterr().err == ""


def test_logger_sink_replaces_console_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = models.RuntimeLogger()
    lines: list[tuple[str, str]] = []
    logger.sink = lambda level, message: lines.append((level, message))

    logger.info("one")
    logger.warn("two")
    logger.error("three")
    logger.debug("hidden")
    logger.set_debug(True)
    logger.debug("four")

    assert lines == [
        ("info", "one"),
        ("warn", "two"),
        ("error", "three"),
        ("debug", "four"),
    ]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_stdin_choose_numbers_options_and_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["2", "9", "x"])
    monkeypatch.setattr(builtins, "input", lambda _p: next(answers))

    assert models._stdin_choose("Pick", ["a", "b", "c"], 0) == 1
    assert models._stdin_choose("Pick", ["a", "b", "c"], 2) == 2
    assert models._stdin_choose("Pick", ["a", "b", "c"], 0) == 0
    assert "  2. b" in capsys.readouterr().out


def test_art_choice_goes_through_the_choose_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_input(_p: str) -> NoReturn:
        raise AssertionError()

    monkeypatch.setattr(builtins, "input", reject_input)
    asked: list[list[str]] = []

    def choose(_question: str, options: list[str], default: int) -> int:
        asked.append(options)
        assert default == 0
        return 3

    choice = tagger._prompt_art_choice(models.UserPrompts(choose=choose))

    assert choice == "both"
    assert asked == [["None", "Poster", "Backdrop", "Both"]]


def test_multiple_tmdb_matches_go_through_the_choose_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = tagger.TmdbClient("key")
    results = [
        {"id": 1, "title": "Movie", "release_date": "1991-01-01"},
        {"id": 2, "title": "Movie", "release_date": "2017-01-01"},
    ]
    monkeypatch.setattr(client, "_get", lambda *_a, **_k: {"results": results})
    asked: list[list[str]] = []

    def choose(_question: str, options: list[str], _default: int) -> int:
        asked.append(options)
        return 1

    movie_id = client.get_movie_id("Movie", None, models.UserPrompts(choose=choose))

    assert movie_id == 2
    assert asked == [["Movie (1991-01-01)", "Movie (2017-01-01)"]]


# =============================================================================
# Per-disc questions, splitting, tagging, output folder
# =============================================================================


def scripted(*answers: str) -> models.UserPrompts:
    """Prompts answered from *answers* in order ("y" confirms)."""
    replies = iter(answers)

    def text(_prompt: str, default: str | None = None) -> str:
        reply = next(replies)
        return reply or (default or "")

    def confirm(_message: str) -> bool:
        return next(replies) == "y"

    def choose(_question: str, _options: list[str], default: int = 0) -> int:
        reply = next(replies)
        return int(reply) if reply else default

    return models.UserPrompts(confirm=confirm, text=text, choose=choose)


def captioned_title() -> Title:
    from cc608 import CC608_CODEC_SRT

    title = Title(
        index=0, source_file=Path("VTS_01_1.VOB"), name="x", duration_seconds=1400.0
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="mpeg2"),
        Stream(index=1, stream_type=StreamType.SUBTITLE, codec="dvd_subtitle"),
        Stream(index=2, stream_type=StreamType.SUBTITLE, codec=CC608_CODEC_SRT),
    ]
    return title


@pytest.mark.parametrize(("answer", "kept"), [("y", 3), ("n", 2)])
def test_closed_captions_are_asked_per_disc(answer: str, kept: int) -> None:
    title = captioned_title()

    session.ask_closed_captions(
        [title], Config(ask_closed_captions=True), scripted(answer)
    )

    assert len(title.streams) == kept
    assert title.streams[1].codec == "dvd_subtitle"


def test_closed_captions_not_asked_without_captions_or_ask() -> None:
    title = captioned_title()
    title.streams.pop()
    # An empty script: any question would fail.
    session.ask_closed_captions([title], Config(ask_closed_captions=True), scripted())
    session.ask_closed_captions([captioned_title()], Config(), scripted())


def packed_title(index: int = 0) -> Title:
    from packed_episodes import PackedSegment

    title = make_title(index, 2800.0)
    title.clip_durations = [2800.0]
    title.playlist_name = "00000"
    title.packed_segments = [
        PackedSegment(0.0, 1400.0, 1),
        PackedSegment(1400.0, 2800.0, 2),
    ]
    return title


def test_packed_split_is_offered_per_playlist() -> None:
    titles = [packed_title(0), make_title(1), packed_title(2)]

    assert session.packed_split_offers(titles, scripted("n", "y")) == [2]


def test_splitting_packed_episodes_in_place_makes_a_series_disc() -> None:
    titles = [packed_title()]
    state = RuntimeState()

    with pytest.raises(ValueError, match="holds no packed episodes"):
        session.split_packed_episodes(titles, [7], None, state)
    assert len(titles) == 1 and state.series_disc is False

    original = titles
    session.split_packed_episodes(titles, None, None, state)

    assert titles is original and len(titles) == 2
    assert all(title.is_episode for title in titles)
    assert state.series_disc is True


def test_splitting_needs_a_packed_playlist() -> None:
    with pytest.raises(ValueError, match="No playlist"):
        session.split_packed_episodes([make_title(0)], None, None, RuntimeState())


def test_output_folder_is_asked_once(tmp_path: Path) -> None:
    config = Config(output_dir=Path("."), ask_output_dir=True)
    target = tmp_path / "rips"

    assert session.ask_output_dir(config, scripted(str(target))) == target
    # A second question would exhaust the script.
    assert session.ask_output_dir(config, scripted()) == target
    assert config.output_dir == target and target.is_dir()


def test_output_folder_is_asked_again_when_unusable(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    config = Config(ask_output_dir=True)

    chosen = session.ask_output_dir(
        config, scripted(str(blocker / "sub"), str(tmp_path / "ok"))
    )

    assert chosen == tmp_path / "ok"


def test_edition_names_default_to_numbered_editions() -> None:
    assert session.ask_edition_names(3, scripted("Theatrical", "", "  ")) == [
        "Theatrical",
        "Edition 2",
        "Edition 3",
    ]


@pytest.fixture
def tmdb_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _opts: "tmdb-key")


@pytest.mark.usefixtures("tmdb_key")
def test_tagging_is_offered_when_a_key_is_available() -> None:
    options = models.TagOptions()
    choice = session.TaggingChoice.from_options(options)
    assert (choice.always, choice.offer, choice.art) == (False, True, None)

    choice.prepare(scripted("y", "1"))  # tag it, with the poster

    assert options.enabled is True and options.art == "poster"

    choice.prepare(scripted("n"))
    assert options.enabled is False


@pytest.mark.usefixtures("tmdb_key")
def test_tagging_always_and_fixed_art_ask_nothing() -> None:
    options = models.TagOptions(enabled=True, art="both")
    choice = session.TaggingChoice.from_options(options)

    choice.prepare(scripted())

    assert options.enabled is True and options.art == "both"


def test_tagging_without_a_key_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _opts: None)
    options = models.TagOptions()

    session.TaggingChoice.from_options(options).prepare(scripted())

    assert options.enabled is False


def test_scan_source_reports_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Not found"):
        session.scan_source(tmp_path / "missing", RuntimeState())


def test_track_plan_tells_apart_streams_sharing_an_index() -> None:
    # Every DVD stream has Stream.index 0; the plan must still mark each.
    title = make_title(0)
    for stream in title.streams:
        stream.index = 0
    config = Config(preferred_languages=["eng"], keep_all_audio=False)

    assert [c.keep for c in track_plan(title, config)] == [
        True,
        True,
        False,
        True,
        False,
    ]


def test_explicit_track_selection_keeps_streams_sharing_an_index() -> None:
    # Regression: "-s a:0,a:1,s:0" on a DVD kept only a:0, because the
    # selection was deduplicated on Stream.index (0 for every DVD stream).
    from mkv import select_streams

    title = make_title(0)
    for stream in title.streams:
        stream.index = 0

    picked = select_streams(title, ["a:0", "a:1", "s:0", "a:0"], Config())

    assert [s.display_id for s in picked] == ["a:0", "a:1", "s:0"]
