"""Tests for interactive-mode state and command dispatch."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import cli
import models
import scan
import tagger
from cli import _InteractiveRipper, _InteractiveTagState
from models import Config, RuntimeState, Stream, StreamType, Title


@pytest.fixture
def preserved_cli_state():
    runtime_state = models.RUNTIME_STATE
    config_state = copy.deepcopy(runtime_state.config.__dict__)
    tag_state = copy.deepcopy(runtime_state.tag_options.__dict__)
    yield
    runtime_state.config.__dict__.clear()
    runtime_state.config.__dict__.update(config_state)
    runtime_state.tag_options.__dict__.clear()
    runtime_state.tag_options.__dict__.update(tag_state)


def make_title(index: int, *, episode: int | None = None) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"title-{index}.m2ts"),
        name=f"Title {index}",
        duration_seconds=100.0 + index,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="h264", pid=4113)
    ]
    title.dvd_episode_number = episode
    return title


def make_ripper(tmp_path: Path) -> _InteractiveRipper:
    creator = cli.MKVCreator(tmp_path)
    return _InteractiveRipper(
        [],
        creator,
        _InteractiveTagState(False, False, None),
        [],
        debug=False,
    )


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_state_resolves_availability(monkeypatch: Any) -> None:
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _opts: "tmdb-key")
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.no_tag = False
    models.RUNTIME_STATE.tag_options.art = None

    state = _InteractiveTagState.from_options(models.RUNTIME_STATE.tag_options)

    assert state == _InteractiveTagState(False, True, None)


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_prepare_prompts_when_available(monkeypatch: Any) -> None:
    monkeypatch.setattr(tagger, "_tag_confirm", lambda _prompt: True)
    monkeypatch.setattr(tagger, "_prompt_art_choice", lambda: "poster")
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.art = None
    state = _InteractiveTagState(False, True, None)

    state.prepare_for_rip()

    assert models.RUNTIME_STATE.tag_options.enabled is True
    assert models.RUNTIME_STATE.tag_options.art == "poster"


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_flag_skips_prompts(monkeypatch: Any) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(
        tagger, "_tag_confirm", lambda prompt: prompts.append(prompt) or False
    )
    monkeypatch.setattr(
        tagger, "_prompt_art_choice", lambda: prompts.append("art") or ""
    )
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.art = "both"
    state = _InteractiveTagState(True, True, "both")

    state.prepare_for_rip()

    assert models.RUNTIME_STATE.tag_options.enabled is True
    assert models.RUNTIME_STATE.tag_options.art == "both"
    assert prompts == []


def test_interactive_edition_groups_only_detects_in_debug(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    titles = [make_title(0), make_title(1)]
    calls: list[list[Title]] = []
    monkeypatch.setattr(
        scan,
        "_detect_edition_groups",
        lambda detected: calls.append(detected) or [titles],
    )
    assert cli._interactive_edition_groups(titles, debug=False) == []
    assert calls == []

    assert cli._interactive_edition_groups(titles, debug=True) == [titles]
    assert calls == [titles]
    assert "Titles 0, 1 look like editions" in capsys.readouterr().out


def test_multi_edition_indices_parses_and_auto_selects(
    tmp_path: Path,
) -> None:
    titles = [make_title(0), make_title(1), make_title(2)]
    ripper = make_ripper(tmp_path)
    ripper.titles = titles
    short_group = [titles[0]]
    long_group = [titles[1], titles[2]]
    ripper.edition_groups = [short_group, long_group]

    assert ripper.multi_edition_indices(["2", "0"]) == [2, 0]
    assert ripper.multi_edition_indices([]) == [1, 2]


def test_rip_command_accepts_attached_and_separated_forms(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ripper = make_ripper(tmp_path)
    calls: list[tuple[int, list[str] | None]] = []
    monkeypatch.setattr(
        ripper,
        "rip_index",
        lambda idx, stream_ids=None: calls.append((idx, stream_ids)),
    )

    ripper._handle_rip_command("r", ["1", "v:0"])
    ripper._handle_rip_command("r2", ["a:0"])
    ripper._handle_rip_command("r", [])

    assert calls == [(1, ["v:0"]), (2, ["a:0"])]


def test_dispatch_supports_details_rip_and_quit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    titles = [make_title(0)]
    ripper = make_ripper(tmp_path)
    ripper.titles = titles
    details: list[Title] = []
    rip_calls: list[tuple[int, list[str] | None]] = []
    monkeypatch.setattr(
        cli, "display_title_details", lambda title: details.append(title)
    )
    monkeypatch.setattr(
        ripper,
        "rip_index",
        lambda idx, stream_ids=None: rip_calls.append((idx, stream_ids)),
    )

    assert ripper._dispatch("0", []) is True
    assert ripper._dispatch("r", ["0", "v:0"]) is True
    assert ripper._dispatch("exit", []) is False

    assert details == titles
    assert rip_calls == [(0, ["v:0"])]


def test_interactive_run_dispatches_until_quit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ripper = make_ripper(tmp_path)
    title = make_title(0)
    ripper.titles = [title]
    rip_calls: list[tuple[int, list[str] | None]] = []
    monkeypatch.setattr(ripper, "_print_prompt", lambda _has_episodes: None)
    monkeypatch.setattr(
        ripper,
        "rip_index",
        lambda idx, stream_ids=None: rip_calls.append((idx, stream_ids)),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: next(commands),
    )

    commands = iter(["r 0 v:0", "bogus", "quit"])

    ripper.run()

    assert rip_calls == [(0, ["v:0"])]


def test_interactive_tag_state_uses_injected_options(monkeypatch: Any) -> None:
    options = models.TagOptions(enabled=False, art=None)
    monkeypatch.setattr(tagger, "_tag_confirm", lambda _prompt: True)
    monkeypatch.setattr(tagger, "_prompt_art_choice", lambda: "poster")
    state = _InteractiveTagState(False, True, None, options=options)

    state.prepare_for_rip()

    assert options.enabled is True
    assert options.art == "poster"
    assert models.RUNTIME_STATE.tag_options.enabled is False


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_mode_uses_injected_runtime_state(
    monkeypatch: Any, tmp_path: Path
) -> None:
    runtime_state = RuntimeState(
        config=Config(output_dir=tmp_path), tag_options=models.TagOptions()
    )
    title = make_title(0)
    displayed: list[tuple[list[Title], str | None]] = []
    creators: list[cli.MKVCreator] = []
    rippers: list[Any] = []

    class _FakeRipper:
        def __init__(
            self,
            titles: list[Title],
            creator: cli.MKVCreator,
            tagging: _InteractiveTagState,
            edition_groups: list[list[Title]],
            debug: bool,
        ) -> None:
            self.titles = titles
            self.creator = creator
            self.tagging = tagging
            self.edition_groups = edition_groups
            self.debug = debug
            self.completed = False
            rippers.append(self)

        def run(self) -> None:
            self.completed = True

    monkeypatch.setattr(
        cli,
        "display_titles",
        lambda titles, disc_name=None, config=None: displayed.append(
            (titles, disc_name)
        ),
    )
    monkeypatch.setattr(
        cli.MKVCreator,
        "__init__",
        lambda self, output, tag_opts=None, runtime_state=None: (
            creators.append(self)
            or object.__setattr__(self, "out", output)
            or object.__setattr__(self, "tag_opts", tag_opts)
            or object.__setattr__(
                self,
                "cleanup",
                runtime_state.cleanup if runtime_state else None,
            )
        ),
    )
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _options: None)
    monkeypatch.setattr(cli, "_InteractiveRipper", _FakeRipper)

    cli.interactive_mode([title], "Injected", runtime_state)

    assert displayed == [([title], "Injected")]
    assert len(creators) == 1
    assert creators[0].out == tmp_path
    assert creators[0].tag_opts is runtime_state.tag_options
    assert creators[0].cleanup is runtime_state.cleanup
    assert len(rippers) == 1
    assert rippers[0].titles == [title]
    assert rippers[0].tagging.options is runtime_state.tag_options
    assert rippers[0].edition_groups == []
    assert rippers[0].debug is False
    assert rippers[0].completed is True
