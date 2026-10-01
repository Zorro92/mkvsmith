"""Movie-only TMDB tagging is skipped on series discs.

Tagging searches TMDB for a movie named after the disc, so on a series disc
it would tag episodes (and extras) with a wrong film.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli
import mkv
import tagger
from models import PackedSegment, RuntimeState, TagOptions, Title


def _title(index: int = 0, **episode: int) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"t{index}.m2ts"),
        name=f"Show - Title {index}",
        duration_seconds=1400.0,
    )
    for field, value in episode.items():
        setattr(title, field, value)
    return title


@pytest.mark.parametrize(
    "field", ["dvd_episode_number", "discdb_episode_number", "packed_episode_number"]
)
def test_any_episode_source_marks_an_episode(field: str) -> None:
    assert _title(**{field: 3}).is_episode
    assert not _title().is_episode


def test_refresh_series_disc() -> None:
    state = RuntimeState()
    state.refresh_series_disc([_title(0), _title(1)])
    assert state.series_disc is False
    state.refresh_series_disc([_title(0), _title(1, dvd_episode_number=1)])
    assert state.series_disc is True


@pytest.fixture
def tagging_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_prepare_tagging(
        name: str, *_args: object
    ) -> tuple[tagger.MovieMetadata | None, list[tagger.ArtAttachment]]:
        calls.append(name)
        return tagger.MovieMetadata(title=name), []

    monkeypatch.setattr(tagger, "_prepare_tagging", fake_prepare_tagging)
    return calls


def test_movie_disc_is_tagged(tagging_calls: list[str]) -> None:
    tagged, _art = mkv._prepare_mux_tags(_title(), TagOptions(enabled=True), [])

    assert tagged is not None
    assert tagging_calls == ["Show - Title 0"]


@pytest.mark.parametrize(
    ("title", "series_disc"),
    [
        (_title(dvd_episode_number=2), False),  # an episode
        (_title(), True),  # an extra on a series disc
    ],
    ids=["episode", "extra-on-series-disc"],
)
def test_series_titles_are_not_tagged(
    tagging_calls: list[str],
    capsys: pytest.CaptureFixture[str],
    title: Title,
    series_disc: bool,
) -> None:
    tagged, art = mkv._prepare_mux_tags(
        title, TagOptions(enabled=True), [], series_disc=series_disc
    )

    assert (tagged, art) == (None, [])
    assert tagging_calls == []
    assert "Skipping TMDB tagging" in capsys.readouterr().out


def test_play_all_chain_is_not_tagged(tagging_calls: list[str]) -> None:
    title = _title()
    title.dvd_play_all = True

    assert mkv._prepare_mux_tags(title, TagOptions(enabled=True), []) == (None, [])
    assert tagging_calls == []


def test_disabled_tagging_stays_silent(capsys: pytest.CaptureFixture[str]) -> None:
    mkv._prepare_mux_tags(_title(dvd_episode_number=1), TagOptions(), [])

    assert capsys.readouterr().out == ""


def test_creator_passes_the_series_flag(tmp_path: Path) -> None:
    state = RuntimeState()
    creator = cli.MKVCreator(tmp_path, runtime_state=state)
    assert creator.runtime_state is state
    state.series_disc = True
    assert creator.runtime_state.series_disc


def test_splitting_packed_episodes_makes_a_series_disc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_display(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli, "display_titles", no_display)
    packed = _title()
    packed.clip_durations = [2800.0]
    packed.playlist_name = "00000"
    packed.packed_segments = [
        PackedSegment(0.0, 1400.0, 1),
        PackedSegment(1400.0, 2800.0, 2),
    ]
    state = RuntimeState()
    creator = cli.MKVCreator(tmp_path, runtime_state=state)
    tagging = cli._InteractiveTagState.from_options(state.tag_options, state.prompts)
    ripper = cli._InteractiveRipper([packed], creator, tagging, [])

    assert state.series_disc is False
    ripper.split_packed_episodes([])
    assert state.series_disc is True
