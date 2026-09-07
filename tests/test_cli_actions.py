"""Tests for CLI action validation and dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli
import models
from models import Stream, StreamType, Title


def make_title(index: int) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"title-{index}.m2ts"),
        name=f"Title {index}",
        duration_seconds=100.0,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="h264", pid=4113)
    ]
    return title


def test_run_action_displays_requested_title(monkeypatch: pytest.MonkeyPatch) -> None:
    title = make_title(0)
    displayed: list[Title] = []
    monkeypatch.setattr(
        cli, "display_title_details", lambda selected: displayed.append(selected)
    )

    cli._run_action("details", [title], None, 0, None, None)

    assert displayed == [title]


def test_run_action_rejects_invalid_details_title(
    capsys: pytest.CaptureFixture[str],
) -> None:
    title = make_title(0)

    with pytest.raises(SystemExit) as exc_info:
        cli._run_action("details", [title], None, 99, None, None)

    assert exc_info.value.code == 1
    assert "Invalid title for details: 99" in capsys.readouterr().out


def test_run_action_rejects_unknown_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._run_action("bogus", [], None, None, None, None)

    assert exc_info.value.code == 1
    assert "Unknown action: bogus" in capsys.readouterr().out


def test_run_action_rips_only_detected_episodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode = make_title(0)
    episode.dvd_episode_number = 1
    regular = make_title(1)
    state = models.RuntimeState()
    batch_calls: list[tuple[list[Title], models.RuntimeState]] = []

    def rip_batch(titles: list[Title], runtime_state=None) -> None:
        assert runtime_state is not None
        batch_calls.append((titles, runtime_state))

    monkeypatch.setattr(cli, "_rip_title_batch", rip_batch)

    cli._run_action(
        "rip_episodes",
        [regular, episode],
        None,
        None,
        None,
        None,
        runtime_state=state,
    )

    assert batch_calls == [([episode], state)]


def test_run_action_rejects_missing_multi_edition_indexes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_rip_multi_edition",
        lambda *_args: (_ for _ in ()).throw(AssertionError),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli._run_action(
            "rip_multi_edition",
            [make_title(0), make_title(1)],
            None,
            None,
            None,
            None,
            runtime_state=models.RuntimeState(),
        )

    assert exc_info.value.code == 1
    assert "No multi-edition title indexes supplied" in capsys.readouterr().out


def test_run_action_rip_title_uses_injected_runtime_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = models.RuntimeState(config=models.Config(output_dir=tmp_path))
    title = make_title(0)
    creators: list[FakeCreator] = []

    class FakeCreator:
        def __init__(
            self,
            output,
            tag_opts=None,
            config=None,
            runtime_state=None,
        ):
            self.output = output
            self.tag_opts = tag_opts
            self.config = config
            self.runtime_state = runtime_state
            self.rips: list[tuple[Title, list[str]]] = []
            creators.append(self)

        def select_streams(self, selected, force=None):
            return [f"{selected.index}:{value}" for value in (force or [])]

        def create_mkv(self, selected, selected_streams=None):
            self.rips.append((selected, selected_streams or []))
            return tmp_path / "output.mkv"

    monkeypatch.setattr(cli, "MKVCreator", FakeCreator)

    cli._run_action(
        "rip_title",
        [title],
        None,
        0,
        ["v:0"],
        None,
        runtime_state=state,
    )

    assert len(creators) == 1
    assert creators[0].output == tmp_path
    assert creators[0].tag_opts is state.tag_options
    assert creators[0].runtime_state is state
    assert creators[0].rips == [(title, ["0:v:0"])]


def test_run_action_forwards_interactive_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = models.RuntimeState()
    title = make_title(0)
    calls: list[tuple[list[Title], str | None, models.RuntimeState]] = []

    def interactive(
        titles: list[Title],
        disc_name: str | None = None,
        runtime_state=None,
    ) -> None:
        assert runtime_state is not None
        calls.append((titles, disc_name, runtime_state))

    monkeypatch.setattr(cli, "interactive_mode", interactive)

    cli._run_action(
        "interactive",
        [title],
        "Test Disc",
        None,
        None,
        None,
        runtime_state=state,
    )

    assert calls == [([title], "Test Disc", state)]
