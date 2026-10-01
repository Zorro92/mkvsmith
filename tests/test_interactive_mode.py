"""Tests for interactive-mode state and command dispatch."""

from __future__ import annotations

import builtins
import copy
from pathlib import Path
from collections.abc import Iterator
from typing import NoReturn

import pytest

import cli
import models
import tagger
from cli import _InteractiveRipper, _InteractiveTagState
from models import Config, DiscMetadata, RuntimeState, Stream, StreamType, Title


@pytest.fixture
def preserved_cli_state() -> Iterator[None]:
    runtime_state = models.RUNTIME_STATE
    config_state = copy.deepcopy(runtime_state.config.__dict__)
    tag_state = copy.deepcopy(runtime_state.tag_options.__dict__)
    discdb_state = copy.deepcopy(runtime_state.discdb_options.__dict__)
    yield
    runtime_state.config.__dict__.clear()
    runtime_state.config.__dict__.update(config_state)
    runtime_state.tag_options.__dict__.clear()
    runtime_state.tag_options.__dict__.update(tag_state)
    runtime_state.discdb_options.__dict__.clear()
    runtime_state.discdb_options.__dict__.update(discdb_state)


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
    )


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_state_resolves_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolve_tmdb_key(_opts: models.TagOptions) -> str:
        return "tmdb-key"

    monkeypatch.setattr(tagger, "_resolve_tmdb_key", fake_resolve_tmdb_key)
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.no_tag = False
    models.RUNTIME_STATE.tag_options.art = None

    state = _InteractiveTagState.from_options(models.RUNTIME_STATE.tag_options)

    assert state == _InteractiveTagState(False, True, None)


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_prepare_prompts_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def confirm_yes(_prompt: str, *_args: object, **_kwargs: object) -> bool:
        return True

    def choose_poster(*_args: object, **_kwargs: object) -> str:
        return "poster"

    monkeypatch.setattr(tagger, "_tag_confirm", confirm_yes)
    monkeypatch.setattr(tagger, "_prompt_art_choice", choose_poster)
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.art = None
    state = _InteractiveTagState(False, True, None)

    state.prepare_for_rip()

    assert models.RUNTIME_STATE.tag_options.enabled is True
    assert models.RUNTIME_STATE.tag_options.art == "poster"


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_flag_skips_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def record_confirm(prompt: str, *_args: object, **_kwargs: object) -> bool:
        prompts.append(prompt)
        return False

    def record_art_choice(*_args: object, **_kwargs: object) -> str:
        prompts.append("art")
        return ""

    monkeypatch.setattr(tagger, "_tag_confirm", record_confirm)
    monkeypatch.setattr(tagger, "_prompt_art_choice", record_art_choice)
    models.RUNTIME_STATE.tag_options.enabled = False
    models.RUNTIME_STATE.tag_options.art = "both"
    state = _InteractiveTagState(True, True, "both")

    state.prepare_for_rip()

    assert models.RUNTIME_STATE.tag_options.enabled is True
    assert models.RUNTIME_STATE.tag_options.art == "both"
    assert prompts == []


def test_interactive_edition_groups_detects_without_debug(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    titles = [make_title(0), make_title(1)]
    calls: list[list[Title]] = []

    def record_detect(detected: list[Title]) -> list[list[Title]]:
        calls.append(detected)
        return [titles]

    monkeypatch.setattr(cli, "_detect_edition_groups", record_detect)
    assert cli._interactive_edition_groups(titles) == [titles]
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ripper = make_ripper(tmp_path)
    calls: list[tuple[int, list[str] | None]] = []

    def record_rip(idx: int, stream_ids: list[str] | None = None) -> None:
        calls.append((idx, stream_ids))

    monkeypatch.setattr(ripper, "rip_index", record_rip)

    ripper._handle_rip_command("r", ["1", "v:0"])
    ripper._handle_rip_command("r2", ["a:0"])
    ripper._handle_rip_command("r", [])

    assert calls == [(1, ["v:0"]), (2, ["a:0"])]


def test_dispatch_supports_details_rip_and_quit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    titles = [make_title(0)]
    ripper = make_ripper(tmp_path)
    ripper.titles = titles
    details: list[Title] = []
    rip_calls: list[tuple[int, list[str] | None]] = []

    def record_details(title: Title) -> None:
        details.append(title)

    def record_rip(idx: int, stream_ids: list[str] | None = None) -> None:
        rip_calls.append((idx, stream_ids))

    monkeypatch.setattr(cli, "display_title_details", record_details)
    monkeypatch.setattr(ripper, "rip_index", record_rip)

    assert ripper._dispatch("0", []) is True
    assert ripper._dispatch("r", ["0", "v:0"]) is True
    assert ripper._dispatch("exit", []) is False

    assert details == titles
    assert rip_calls == [(0, ["v:0"])]


def test_interactive_run_dispatches_until_quit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ripper = make_ripper(tmp_path)
    title = make_title(0)
    ripper.titles = [title]
    rip_calls: list[tuple[int, list[str] | None]] = []

    def skip_prompt(_has_episodes: bool) -> None:
        return None

    def record_rip(idx: int, stream_ids: list[str] | None = None) -> None:
        rip_calls.append((idx, stream_ids))

    def scripted_input(_prompt: str) -> str:
        return next(commands)

    monkeypatch.setattr(ripper, "_print_prompt", skip_prompt)
    monkeypatch.setattr(ripper, "rip_index", record_rip)
    monkeypatch.setattr("builtins.input", scripted_input)

    commands = iter(["r 0 v:0", "bogus", "quit"])

    ripper.run()

    assert rip_calls == [(0, ["v:0"])]


def test_interactive_tag_state_uses_injected_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = models.TagOptions(enabled=False, art=None)

    def confirm_yes(_prompt: str, *_args: object, **_kwargs: object) -> bool:
        return True

    def choose_poster(*_args: object, **_kwargs: object) -> str:
        return "poster"

    monkeypatch.setattr(tagger, "_tag_confirm", confirm_yes)
    monkeypatch.setattr(tagger, "_prompt_art_choice", choose_poster)
    state = _InteractiveTagState(False, True, None, options=options)

    state.prepare_for_rip()

    assert options.enabled is True
    assert options.art == "poster"
    assert models.RUNTIME_STATE.tag_options.enabled is False


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_tag_prepare_forwards_injected_prompts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per-rip tagging answers via hooks instead of reading stdin."""
    options = models.TagOptions(enabled=False, art=None)
    seen: list[str] = []
    injected = models.UserPrompts(
        confirm=lambda message: seen.append(message) or True,
        text=lambda _prompt, _default: "1",
    )

    def reject_input(_p: str) -> NoReturn:
        raise AssertionError()

    monkeypatch.setattr(builtins, "input", reject_input)
    state = _InteractiveTagState(False, True, None, options=options, prompts=injected)

    state.prepare_for_rip()

    assert options.enabled is True
    assert options.art is None
    assert len(seen) == 1
    capsys.readouterr()


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_mode_uses_injected_runtime_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = RuntimeState(
        config=Config(output_dir=tmp_path), tag_options=models.TagOptions()
    )
    title = make_title(0)
    displayed: list[tuple[list[Title], DiscMetadata | None]] = []
    creators: list[cli.MKVCreator] = []
    rippers: list[_FakeRipper] = []

    class _FakeRipper:
        def __init__(
            self,
            titles: list[Title],
            creator: cli.MKVCreator,
            tagging: _InteractiveTagState,
            edition_groups: list[list[Title]],
        ) -> None:
            self.titles = titles
            self.creator = creator
            self.tagging = tagging
            self.edition_groups = edition_groups
            self.completed = False
            rippers.append(self)

        def run(self) -> None:
            self.completed = True

    def record_display(
        titles: list[Title],
        disc_name: DiscMetadata | None = None,
        config: Config | None = None,
    ) -> None:
        displayed.append((titles, disc_name))

    def fake_creator_init(
        self: cli.MKVCreator,
        output: Path,
        tag_opts: models.TagOptions | None = None,
        runtime_state: RuntimeState | None = None,
    ) -> None:
        creators.append(self)
        object.__setattr__(self, "out", output)
        object.__setattr__(self, "tag_opts", tag_opts)
        object.__setattr__(
            self,
            "cleanup",
            runtime_state.cleanup if runtime_state else None,
        )

    def no_tmdb_key(_options: models.TagOptions) -> None:
        return None

    monkeypatch.setattr(cli, "display_titles", record_display)
    monkeypatch.setattr(cli.MKVCreator, "__init__", fake_creator_init)
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", no_tmdb_key)
    monkeypatch.setattr(cli, "_InteractiveRipper", _FakeRipper)

    metadata = DiscMetadata(name="Injected")
    cli.interactive_mode([title], metadata, runtime_state)

    assert displayed == [([title], metadata)]
    assert len(creators) == 1
    assert creators[0].out == tmp_path
    assert creators[0].tag_opts is runtime_state.tag_options
    assert creators[0].cleanup is runtime_state.cleanup
    assert len(rippers) == 1
    assert rippers[0].titles == [title]
    assert rippers[0].tagging.options is runtime_state.tag_options
    assert rippers[0].edition_groups == []
    assert rippers[0].completed is True


@pytest.mark.usefixtures("preserved_cli_state")
def test_interactive_main_feature_falls_back_to_episodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rm on a series disc rips the episodes instead of the star-scored
    title (which can be an unrelated bonus)."""
    episode = make_title(0, episode=1)
    bonus = make_title(1)
    ripper = make_ripper(tmp_path)
    ripper.titles = [episode, bonus]
    ripped: list[Title] = []

    def rip_collection(selected: list[Title]) -> tuple[int, int]:
        ripped.extend(selected)
        return len(selected), 0

    monkeypatch.setattr(ripper, "rip_collection", rip_collection)

    ripper._handle_main_feature()

    assert ripped == [episode]


def test_print_prompt_always_lists_multi_edition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ripper = make_ripper(tmp_path)
    ripper.edition_groups = []
    ripper._print_prompt(False)
    without = capsys.readouterr().out
    assert "me N N ...=multi-edition rip" in without
    assert "auto-detect" not in without

    ripper.edition_groups = [[make_title(0), make_title(1)]]
    ripper._print_prompt(False)
    assert "auto-detect" in capsys.readouterr().out


def test_default_edition_names_numbers_non_default_editions() -> None:
    first = make_title(0)
    first.disc_name = "Movie"
    second = make_title(1)
    assert cli._default_edition_names([first, second], [0, 1]) == [
        "Edition 1",
        "Edition 2",
    ]
