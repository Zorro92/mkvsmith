"""Tests for persisted settings (settings.py) and how runs resolve them.

Settings live at $XDG_CONFIG_HOME/mkvsmith/config.json (default
~/.config/...) or $MKVSMITH_CONFIG; the flat version-1 file, the previous
~/.mkvsmith_config.json and the legacy ~/.mkv_tagger_config.json migrate
forward on first load. Each run resolves options as flag > environment >
settings file > built-in default; the interactive prompt asks for missing
settings and for per-disc choices saved as "ask".
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest

from mkvsmith import cli
from mkvsmith import settings
from mkvsmith.models import (
    RuntimeState,
    UserPrompts,
)
from mkvsmith.settings import SettingSpec, Settings


def _point_settings(
    monkeypatch: pytest.MonkeyPatch, new: Path, old: Path, legacy: Path
) -> None:
    monkeypatch.delenv(settings.CONFIG_ENV, raising=False)
    monkeypatch.setattr(settings, "SETTINGS_PATH", new)
    monkeypatch.setattr(settings, "_OLD_SETTINGS_PATH", old)
    monkeypatch.setattr(settings, "_LEGACY_CONFIG_PATH", legacy)


@pytest.fixture
def new_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    new = tmp_path / "cfg" / "mkvsmith" / "config.json"
    _point_settings(monkeypatch, new, tmp_path / "old.json", tmp_path / "legacy.json")
    return new


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _complete(**values: object) -> Settings:
    """Settings with every setting answered (as if setup had finished)."""
    every = {spec.key for spec in settings.SETTING_SPECS}
    return replace(Settings(), answered=every, **values)


def test_settings_path_uses_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(settings.CONFIG_ENV, raising=False)
    monkeypatch.setattr(settings, "SETTINGS_PATH", None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert settings.settings_path() == tmp_path / "xdg" / "mkvsmith" / "config.json"


# -----------------------------------------------------------------------------
# Reading and writing
# -----------------------------------------------------------------------------


def test_save_roundtrips_privately(new_path: Path) -> None:
    saved = _complete(
        ui_language="es",
        languages=["jpn", "eng"],
        tagging="never",
        tmdb_api_key="k",
        ram_limit=0.5,
    )
    assert settings.save_settings(saved) == new_path

    loaded = settings.load_settings()
    assert (loaded.settings, loaded.exists, loaded.problems) == (saved, True, [])
    assert loaded.settings.answered == saved.answered
    if os.name == "posix":  # Windows has no Unix permission bits
        assert stat.S_IMODE(new_path.stat().st_mode) == 0o600
    data = json.loads(new_path.read_text(encoding="utf-8"))
    assert data["version"] == settings.SETTINGS_VERSION
    assert data["tmdb"]["tagging"] == "never"
    assert sum(len(v) for k, v in data.items() if k != "version") == len(
        settings.SETTING_SPECS
    )


def test_only_answered_settings_are_written(new_path: Path) -> None:
    saved = settings.set_setting(Settings(), "tmdb.api_key", "k")
    settings.save_settings(saved)

    data = json.loads(new_path.read_text(encoding="utf-8"))
    assert data == {"version": 2, "tmdb": {"api_key": "k"}}


def test_missing_file_gives_defaults(new_path: Path) -> None:
    loaded = settings.load_settings()
    assert (loaded.settings, loaded.exists, loaded.problems) == (Settings(), False, [])
    assert loaded.settings.answered == set()


def test_corrupt_file_is_reported_not_overwritten(new_path: Path) -> None:
    new_path.parent.mkdir(parents=True)
    new_path.write_text("{not json", encoding="utf-8")

    loaded = settings.load_settings()

    assert loaded.settings == Settings()
    assert loaded.unreadable and loaded.exists
    assert "not valid JSON" in loaded.problems[0]
    assert settings.complete_settings(loaded, _forbidden_ask) == Settings()
    assert new_path.read_text(encoding="utf-8") == "{not json"


def test_invalid_values_fall_back_and_unknown_keys_survive(new_path: Path) -> None:
    _write(
        new_path,
        {
            "version": 2,
            "tracks": {"all_audio": "maybe", "languages": "jpn, eng"},
            "tmdb": {"tagging": "sometimes", "future_option": 1},
            "plugins": {"x": True},
        },
    )

    loaded = settings.load_settings()

    assert loaded.settings.all_audio is True
    assert loaded.settings.languages == ["jpn", "eng"]
    assert loaded.settings.tagging == "ask"
    assert len(loaded.problems) == 4
    # Invalid values count as unanswered, so they are asked again.
    assert "tracks.all_audio" not in loaded.settings.answered
    assert "tracks.languages" in loaded.settings.answered
    settings.save_settings(loaded.settings)
    data = json.loads(new_path.read_text(encoding="utf-8"))
    assert data["tmdb"]["future_option"] == 1
    assert data["plugins"] == {"x": True}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, "always"), (False, "never"), ("ask", "ask"), ("yes", "always")],
)
def test_per_disc_choices_accept_booleans(raw: object, expected: str) -> None:
    changed = settings.set_setting(Settings(), "scan.split_episodes", raw)
    assert changed.split_episodes == expected


def test_old_boolean_overwrite_means_ask() -> None:
    assert settings.set_setting(Settings(), "output.overwrite", False).overwrite == (
        "ask"
    )
    assert settings.set_setting(Settings(), "output.overwrite", True).overwrite == (
        "always"
    )


def test_migrates_flat_version_1_in_place(new_path: Path) -> None:
    _write(
        new_path,
        {
            "language": "es",
            "api_key": "k",
            "discdb": {
                "enabled": True,
                "timeout_seconds": 4,
                "contribute": True,
                "contribute_mode": "manual",
                "contribution_id": "dropped",
            },
        },
    )

    loaded = settings.load_settings()

    assert loaded.problems == []
    assert loaded.settings == Settings(
        ui_language="es",
        tmdb_api_key="k",
        discdb_enabled=True,
        discdb_timeout=4.0,
        discdb_contribute="manual",
    )
    data = json.loads(new_path.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["ui"]["language"] == "es"
    assert "contribution_id" not in data["discdb"]


@pytest.mark.parametrize("which", ["old", "legacy"])
def test_migrates_home_dotfiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, which: str
) -> None:
    new = tmp_path / "new" / "config.json"
    old, legacy = tmp_path / "old.json", tmp_path / "legacy.json"
    _point_settings(monkeypatch, new, old, legacy)
    _write(old if which == "old" else legacy, {"api_key": "k"})

    loaded = settings.load_settings()

    assert loaded.settings.tmdb_api_key == "k" and loaded.exists
    assert json.loads(new.read_text(encoding="utf-8"))["tmdb"]["api_key"] == "k"


def test_new_settings_take_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    new = tmp_path / "new" / "config.json"
    old = tmp_path / "old.json"
    _point_settings(monkeypatch, new, old, tmp_path / "legacy.json")
    _write(new, {"version": 2, "ui": {"language": "es"}})
    _write(old, {"language": "en"})

    assert settings.load_settings().settings.ui_language == "es"


def test_config_env_points_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, new_path: Path
) -> None:
    script_config = tmp_path / "script.json"
    _write(script_config, {"version": 2, "tmdb": {"tagging": "always"}})
    monkeypatch.setenv(settings.CONFIG_ENV, str(script_config))

    loaded = settings.load_settings()

    assert loaded.path == script_config
    assert loaded.settings.tagging == "always"


def test_get_set_reset_and_format() -> None:
    base = Settings()

    changed = settings.set_setting(base, "tracks.languages", "jpn,eng")
    changed = settings.set_setting(changed, "temp.ram_limit", "0.25")
    changed = settings.set_setting(changed, "tmdb.api_key", "secret")

    assert base == Settings() and base.answered == set()  # never modified
    assert changed.answered == {"tracks.languages", "temp.ram_limit", "tmdb.api_key"}
    assert settings.get_setting(changed, "tracks.languages") == ["jpn", "eng"]
    assert settings.format_setting(changed, "tracks.languages") == "jpn,eng"
    assert settings.format_setting(changed, "temp.ram_limit") == "0.25"
    assert settings.format_setting(changed, "tmdb.api_key") == "********"
    assert settings.format_setting(base, "scan.min_duration") == "60"
    reset = settings.reset_setting(changed, "temp.ram_limit")
    assert reset.ram_limit == 0.8 and "temp.ram_limit" in reset.answered
    with pytest.raises(ValueError, match="between 0 and 1"):
        settings.set_setting(base, "temp.ram_limit", "2")
    with pytest.raises(ValueError, match="never, ask, always"):
        settings.set_setting(base, "tmdb.tagging", "sometimes")
    with pytest.raises(KeyError, match="unknown setting"):
        settings.set_setting(base, "tmdb.nope", "x")


def test_every_default_survives_a_roundtrip() -> None:
    data = settings.settings_to_dict(_complete())
    assert settings.settings_from_dict(data) == (Settings(), [])


# -----------------------------------------------------------------------------
# Completeness
# -----------------------------------------------------------------------------


def _missing(saved: Settings) -> list[str]:
    return [spec.key for spec in settings.missing_settings(saved)]


def test_first_run_misses_every_everyday_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    assert _missing(Settings()) == [
        "ui.language",
        "output.overwrite",
        "tracks.languages",
        "tracks.all_audio",
        "tracks.subtitles",
        "tracks.all_subtitles",
        "tracks.forced_subtitles",
        "tracks.closed_captions",
        # tracks.cc_format matters once captions aren't "never"
        "scan.split_episodes",
        "tmdb.api_key",
        # tmdb.tagging / confirm_match / art need a key
        "discdb.enabled",
    ]


def test_follow_up_questions_appear_once_they_matter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    saved = _complete()
    saved.answered -= {
        "tracks.cc_format",
        "tmdb.tagging",
        "tmdb.confirm_match",
        "tmdb.art",
    }
    assert _missing(saved) == []

    saved = settings.set_setting(saved, "tracks.closed_captions", "ask")
    saved = settings.set_setting(saved, "tmdb.api_key", "k")
    assert _missing(saved) == [
        "tracks.cc_format",
        "tmdb.tagging",
        "tmdb.confirm_match",
        "tmdb.art",
    ]
    saved = settings.set_setting(saved, "tmdb.tagging", "never")
    assert _missing(saved) == ["tracks.cc_format"]


def test_a_new_setting_from_an_update_is_missing() -> None:
    saved = _complete()
    saved.answered.discard("scan.split_episodes")  # as if new in this version
    assert _missing(saved) == ["scan.split_episodes"]


def test_advanced_settings_are_never_asked() -> None:
    saved = _complete()
    saved.answered -= {"temp.ram_limit", "discdb.timeout"}
    assert _missing(saved) == []
    filled = settings.accept_advanced_defaults(saved)
    assert {"temp.ram_limit", "discdb.timeout"} <= filled.answered


def _scripted(*answers: str) -> UserPrompts:
    replies = iter(answers)

    def text(_prompt: str, default: str | None = None) -> str:
        reply = next(replies)
        return reply or (default or "")

    def confirm(_message: str) -> bool:
        return next(replies) == "y"

    def secret(_prompt: str) -> str:
        return next(replies)

    return UserPrompts(confirm=confirm, text=text, secret=secret)


def _answers(*answers: object | None) -> Callable[[SettingSpec, Settings], object]:
    """A setup ``ask`` answering from *answers* in order (None = suggested)."""
    replies = iter(answers)

    def ask(_spec: SettingSpec, _saved: Settings) -> object | None:
        return next(replies)

    return ask


def _forbidden_ask(_spec: SettingSpec, _saved: Settings) -> NoReturn:
    raise AssertionError("asked a question")


def test_setup_asks_only_the_new_setting(new_path: Path) -> None:
    saved = _complete(split_episodes="never")
    saved.answered -= {"scan.split_episodes", "temp.ram_limit"}
    settings.save_settings(saved)
    loaded = settings.load_settings()
    asked: list[str] = []

    def ask(spec: SettingSpec, _saved: Settings) -> str:
        asked.append(spec.key)
        return "ask"

    result = settings.complete_settings(loaded, ask)

    assert asked == ["scan.split_episodes"]
    assert result.split_episodes == "ask"
    # The advanced setting takes its default, so the file can be completed.
    assert {"scan.split_episodes", "temp.ram_limit"} <= result.answered
    assert settings.missing_settings(result) == []


def test_setup_reasks_invalid_answers_and_keeps_suggestions(
    new_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    saved = _complete()
    saved.answered -= {"tracks.languages", "tracks.subtitles", "tmdb.api_key"}
    settings.save_settings(saved)
    seen: list[str] = []

    result = settings.complete_settings(
        settings.load_settings(),
        _answers(" , ", "jpn,eng", "maybe", None, ""),
        lambda spec, _saved: seen.append(spec.key),
    )

    assert result.languages == ["jpn", "eng"]
    assert result.subtitles is Settings().subtitles  # None keeps the suggestion
    assert result.tmdb_api_key is None and "tmdb.api_key" in result.answered
    assert seen == ["tracks.languages", "tracks.subtitles", "tmdb.api_key"]


def test_complete_settings_ask_nothing(new_path: Path) -> None:
    settings.save_settings(_complete())
    loaded = settings.load_settings()

    result = settings.complete_settings(loaded, _forbidden_ask)

    assert result.answered == loaded.settings.answered


# -----------------------------------------------------------------------------
# Resolution: flag > environment > settings file > built-in default
# -----------------------------------------------------------------------------


def _resolve(saved: Settings, *argv: str, interactive: bool = False) -> RuntimeState:
    state = RuntimeState(settings=saved)
    cli._apply_parsed_args(
        cli._build_arg_parser().parse_args(["src", *argv]), state, interactive
    )
    return state


def test_builtin_defaults_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    state = _resolve(Settings())

    assert state.config.output_dir == Path(".")
    assert state.config.preferred_languages == ["eng", "en", "und"]
    assert state.config.keep_all_subtitles is True
    assert state.config.overwrite == "ask"
    assert state.config.extract_cc608 is False
    assert (state.tag_options.enabled, state.tag_options.no_tag) == (False, False)
    assert state.tag_options.art is None
    assert state.tag_options.api_key is None


def test_settings_become_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    saved = Settings(
        languages=["jpn"],
        subtitles=False,
        closed_captions="always",
        cc_format="ass",
        split_episodes="always",
        overwrite="never",
        tagging="never",
        tag_art="poster",
        tag_confirm_match=False,
        tmdb_api_key="saved-key",
        min_duration=120.0,
    )

    state = _resolve(saved)

    assert state.config.preferred_languages == ["jpn"]
    assert state.config.keep_all_subtitles is False
    assert (state.config.extract_cc608, state.config.cc608_format) == (True, "ass")
    assert state.config.split_episodes is True
    assert state.config.overwrite == "never"
    assert state.config.min_duration == 120.0
    # A stored key with tagging "never": no --no-tag needed every run.
    assert state.tag_options.no_tag is True
    assert state.tag_options.api_key == "saved-key"
    assert state.tag_options.art == "poster"
    assert state.tag_options.confirm is False


def test_flags_and_environment_override_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "env-key")
    saved = Settings(
        languages=["jpn"],
        tagging="never",
        cc_format="ass",
        all_audio=False,
        overwrite="never",
        split_episodes="never",
        tmdb_api_key="saved-key",
    )

    state = _resolve(
        saved,
        "out",
        "--tag",
        "-l",
        "fra",
        "--cc-format",
        "srt",
        "--all-audio",
        "--force",
        "--split-episodes",
    )

    assert state.config.output_dir == Path("out")
    assert state.config.preferred_languages == ["fra"]
    assert state.config.cc608_format == "srt"
    assert state.config.keep_all_audio is True
    assert state.config.overwrite == "always"
    assert state.config.split_episodes is True
    assert state.tag_options.enabled is True and state.tag_options.no_tag is False
    assert state.tag_options.api_key == "env-key"
    assert _resolve(saved, "--tmdb-key", "flag").tag_options.api_key == "flag"


@pytest.mark.parametrize(
    ("mode", "argv", "enabled", "no_tag"),
    [
        ("ask", (), False, False),  # interactive prompt asks; plain CLI doesn't tag
        ("always", (), True, False),
        ("never", (), False, True),
        ("always", ("--no-tag",), False, True),
        ("never", ("--tag",), True, False),
    ],
)
def test_tagging_mode(
    mode: str, argv: tuple[str, ...], enabled: bool, no_tag: bool
) -> None:
    opts = _resolve(Settings(tagging=mode), *argv).tag_options
    assert (opts.enabled, opts.no_tag) == (enabled, no_tag)


def test_ask_every_time_only_asks_interactively() -> None:
    saved = Settings(split_episodes="ask", closed_captions="ask")

    cli_run = _resolve(saved).config
    tui = _resolve(saved, interactive=True).config

    # Plain CLI: built-in default ("never"), no questions.
    assert (cli_run.split_episodes, cli_run.ask_split_episodes) == (False, False)
    assert (cli_run.extract_cc608, cli_run.ask_closed_captions) == (False, False)
    assert cli_run.ask_output_dir is False
    # Interactive: ask; captions are still detected so the question can be
    # skipped on discs without any.
    assert (tui.split_episodes, tui.ask_split_episodes) == (False, True)
    assert (tui.extract_cc608, tui.ask_closed_captions) == (True, True)
    assert tui.ask_output_dir is True


def test_flags_answer_ask_every_time() -> None:
    saved = Settings(split_episodes="ask", closed_captions="ask")
    config = _resolve(
        saved, "out", "--no-cc-srt", "--split-episodes", interactive=True
    ).config

    assert (config.extract_cc608, config.ask_closed_captions) == (False, False)
    assert (config.split_episodes, config.ask_split_episodes) == (True, False)
    assert config.ask_output_dir is False  # given on the command line


@pytest.mark.parametrize(
    ("argv", "interactive"),
    [
        ((), True),
        (("-i",), False),
        (("-m",), False),
        (("-t", "1"), False),
        (("-a",), False),
        (("-d", "1"), False),
        (("--multi-edition", "1,2"), False),
    ],
)
def test_interactive_run_detection(argv: tuple[str, ...], interactive: bool) -> None:
    args = cli._build_arg_parser().parse_args(["src", *argv])
    assert cli._is_interactive_run(args) is interactive
