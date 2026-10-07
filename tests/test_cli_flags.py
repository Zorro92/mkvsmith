"""Command-line flags: lists, exclusive actions, two-way switches, settings.

Every list flag is one comma-separated argument (so it can't swallow the
source path), actions are mutually exclusive, and every switch a saved
setting can turn on can also be turned off for one run (and vice versa).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from mkvsmith import cli, mkv, settings
from mkvsmith.models import Config, RuntimeState, Stream, StreamType, Title, UserPrompts
from mkvsmith.settings import Settings


def _parse(*argv: str) -> argparse.Namespace:
    return cli._build_arg_parser().parse_args(list(argv))


def _resolve(saved: Settings, *argv: str) -> RuntimeState:
    state = RuntimeState(settings=saved)
    cli._apply_parsed_args(_parse("disc.iso", *argv), state)
    return state


# -----------------------------------------------------------------------------
# Lists, actions, required values
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "attr", "value"),
    [
        (("--tag-metadata", "Title,Overview"), "tag_metadata", ["Title", "Overview"]),
        (("-s", "v:0,a:eng,s:all"), "streams", ["v:0", "a:eng", "s:all"]),
        (("-l", "jpn,eng"), "lang", ["jpn", "eng"]),
        (("--languages", "jpn"), "lang", ["jpn"]),
        (("--lang", "jpn"), "lang", ["jpn"]),  # old spelling
        (("-t", "1,3,5"), "title", [1, 3, 5]),
        # Quoted, space-separated forms are one argument too.
        (("--tag-metadata", "Title Overview"), "tag_metadata", ["Title", "Overview"]),
        (("-l", "jpn, eng"), "lang", ["jpn", "eng"]),
        (("-s", "v:0 a:eng"), "streams", ["v:0", "a:eng"]),
        (("-t", "1 3"), "title", [1, 3]),
        (("--multi-edition", "1,2"), "multi_edition", [1, 2]),
    ],
)
def test_list_flags_never_swallow_the_source(
    argv: tuple[str, ...], attr: str, value: object
) -> None:
    args = _parse(*argv, "disc.iso", "out")
    assert getattr(args, attr) == value
    assert (args.source, args.output) == (Path("disc.iso"), Path("out"))


@pytest.mark.parametrize(
    "argv",
    [
        ("-i", "-a"),
        ("-t", "1", "-m"),
        ("-d", "2", "--multi-edition", "1,2"),
        ("--settings", "-i"),
        ("-t", "x"),
        ("--discdb-contribute",),  # needs a mode
        ("-e",),  # removed alias of --main
    ],
)
def test_bad_combinations_are_errors(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse("disc.iso", *argv)
    assert exc_info.value.code == 2


def test_set_with_an_action_is_an_error(
    monkeypatch: pytest.MonkeyPatch, settings_file: Path
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["mkvsmith", "disc.iso", "-a", "--set", "tmdb.tagging=never"]
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args(RuntimeState())
    assert exc_info.value.code == 2
    assert not settings_file.exists()


def test_discdb_contribute_mode_is_required_and_can_be_off() -> None:
    saved = Settings(discdb_contribute="browser")
    assert _resolve(saved).discdb_options.contribute is True
    off = _resolve(saved, "--discdb-contribute", "off").discdb_options
    assert off.contribute is False
    manual = _resolve(Settings(), "--discdb-contribute", "manual").discdb_options
    assert (manual.contribute, manual.contribute_mode) == (True, "manual")


# -----------------------------------------------------------------------------
# Two-way switches override the saved setting either way
# -----------------------------------------------------------------------------

_Read = Callable[[RuntimeState], object]

_SWITCHES: list[tuple[str, str, _Read]] = [
    ("tracks.subtitles", "--subs", lambda s: s.config.keep_all_subtitles),
    ("tracks.all_subtitles", "--all-subs", lambda s: s.config.all_subtitle_languages),
    ("tracks.forced_subtitles", "--forced", lambda s: s.config.include_forced),
    ("tracks.all_audio", "--all-audio", lambda s: s.config.keep_all_audio),
    ("scan.show_all", "--show-all", lambda s: s.config.show_all),
    ("tmdb.save_xml", "--save-tag-xml", lambda s: s.tag_options.save_xml),
    ("tmdb.confirm_match", "--tag-confirm", lambda s: s.tag_options.confirm),
    ("discdb.enabled", "--discdb", lambda s: s.discdb_options.enabled),
]


@pytest.mark.parametrize(("key", "flag", "read"), _SWITCHES)
def test_switches_work_both_ways(key: str, flag: str, read: _Read) -> None:
    on = settings.set_setting(Settings(), key, True)
    off = settings.set_setting(Settings(), key, False)
    assert read(_resolve(on)) is True
    assert read(_resolve(off)) is False
    assert read(_resolve(on, "--no-" + flag[2:])) is False
    assert read(_resolve(off, flag)) is True


_PER_DISC_SWITCHES: list[tuple[str, str, _Read]] = [
    ("scan.split_episodes", "--split-episodes", lambda s: s.config.split_episodes),
    ("tracks.closed_captions", "--cc", lambda s: s.config.extract_cc608),
    ("tracks.closed_captions", "--cc-srt", lambda s: s.config.extract_cc608),
]


@pytest.mark.parametrize(("key", "flag", "read"), _PER_DISC_SWITCHES)
def test_per_disc_switches_work_both_ways(key: str, flag: str, read: _Read) -> None:
    always = settings.set_setting(Settings(), key, "always")
    never = settings.set_setting(Settings(), key, "never")
    assert read(_resolve(always, "--no-" + flag[2:])) is False
    assert read(_resolve(never, flag)) is True


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ((), "never"),
        (("--overwrite", "ask"), "ask"),
        (("--overwrite", "always"), "always"),
        (("--force",), "always"),
    ],
)
def test_overwrite(argv: tuple[str, ...], expected: str) -> None:
    assert _resolve(Settings(overwrite="never"), *argv).config.overwrite == expected


@pytest.mark.parametrize(
    ("saved", "argv", "expected"),
    [
        ("poster", ("--tag-art", "none"), "none"),
        ("none", ("--tag-art", "both"), "both"),
        ("poster", ("--tag-art", "ask"), None),  # the interactive prompt asks
    ],
)
def test_tag_art(saved: str, argv: tuple[str, ...], expected: str | None) -> None:
    assert _resolve(Settings(tag_art=saved), *argv).tag_options.art == expected


# -----------------------------------------------------------------------------
# -l filters subtitles too
# -----------------------------------------------------------------------------


def _languages_title() -> Title:
    title = Title(index=0, source_file=Path("t.m2ts"), name="x", duration_seconds=100.0)
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO),
        Stream(index=1, stream_type=StreamType.AUDIO, language="jpn"),
        Stream(index=2, stream_type=StreamType.AUDIO, language="eng"),
        Stream(index=3, stream_type=StreamType.SUBTITLE, language="eng"),
        Stream(index=4, stream_type=StreamType.SUBTITLE, language="spa"),
        Stream(index=5, stream_type=StreamType.SUBTITLE, language="und"),
    ]
    return title


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (Config(preferred_languages=["eng", "und"]), [0, 1, 2, 3, 5]),
        (Config(preferred_languages=["spa"]), [0, 1, 2, 4]),
        (
            Config(preferred_languages=["spa"], all_subtitle_languages=True),
            [0, 1, 2, 3, 4, 5],
        ),
        (
            Config(preferred_languages=["eng"], keep_all_audio=False),
            [0, 2, 3],
        ),
    ],
)
def test_preferred_languages_filter_subtitles(
    config: Config, expected: list[int]
) -> None:
    selected = mkv.select_streams(_languages_title(), config=config)
    assert [stream.index for stream in selected] == expected


# -----------------------------------------------------------------------------
# -t 1,3,5
# -----------------------------------------------------------------------------


def test_title_list_reaches_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mkvsmith", "disc.iso", "-t", "1,3"])
    assert cli.parse_args(RuntimeState()) == (
        Path("disc.iso"),
        "rip_title",
        1,
        None,
        [1, 3],
    )


def test_title_list_rips_each_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ripped: list[int] = []

    class FakeCreator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def select_streams(
            self, _title: Title, _force: list[str] | None = None
        ) -> list[Stream]:
            return []

        def create_mkv(self, title: Title, _streams: list[Stream]) -> None:
            ripped.append(title.index)
            if title.index == 3:
                raise mkv.RipError(message="boom")

    monkeypatch.setattr(cli, "MKVCreator", FakeCreator)
    titles = [
        Title(index=i, source_file=tmp_path / f"{i}", name="x", duration_seconds=1.0)
        for i in range(4)
    ]

    with pytest.raises(SystemExit) as exc_info:
        cli._run_action("rip_title", titles, None, 1, None, [1, 3, 2])

    assert ripped == [1, 3, 2]
    assert exc_info.value.code == 1


def test_title_list_checks_every_index_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_creator(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ripped before validating")

    monkeypatch.setattr(cli, "MKVCreator", no_creator)
    titles = [
        Title(index=0, source_file=tmp_path / "0", name="x", duration_seconds=1.0)
    ]
    with pytest.raises(SystemExit):
        cli._run_action("rip_title", titles, None, 0, None, [0, 7])


# -----------------------------------------------------------------------------
# --settings / --set / --reset and the interactive commands
# -----------------------------------------------------------------------------


@pytest.fixture
def settings_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setenv(settings.CONFIG_ENV, str(path))
    return path


def _prompts(*answers: str) -> UserPrompts:
    replies = iter(answers)
    return UserPrompts(
        confirm=lambda _m: False,
        text=lambda _p, default=None: next(replies) or (default or ""),
        secret=lambda _p: next(replies),
    )


def test_set_and_reset_save(settings_file: Path) -> None:
    changed = cli._save_settings_changes(
        ["tmdb.tagging=never", "tracks.languages=jpn,eng"], [], _prompts()
    )
    assert changed is not None and changed.tagging == "never"
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["tmdb"] == {"tagging": "never"}
    assert data["tracks"] == {"languages": ["jpn", "eng"]}

    cli._save_settings_changes([], ["tmdb.tagging"], _prompts())
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["tmdb"] == {"tagging": "ask"}


def test_set_without_value_asks_and_hides_secrets(settings_file: Path) -> None:
    changed = cli._save_settings_changes(
        ["tmdb.api_key", "tmdb.region"], [], _prompts("hidden-key", "CA")
    )
    assert changed is not None
    assert (changed.tmdb_api_key, changed.tag_region) == ("hidden-key", "CA")


@pytest.mark.parametrize(
    "item", ["tmdb.nope=1", "tmdb.tagging=sometimes", "temp.ram_limit=2"]
)
def test_bad_set_saves_nothing(
    settings_file: Path, item: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._save_settings_changes(["tracks.subtitles=no"], [], _prompts())
    before = settings_file.read_text(encoding="utf-8")

    assert (
        cli._save_settings_changes(["tracks.subtitles=yes", item], [], _prompts())
        is None
    )
    assert settings_file.read_text(encoding="utf-8") == before
    assert "ERROR" in capsys.readouterr().out


def test_save_key_still_works(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["mkvsmith", "--save-key", "k"])
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args(RuntimeState())
    assert exc_info.value.code == 0
    assert settings.load_settings().settings.tmdb_api_key == "k"


def test_settings_listing_marks_unchosen_and_masks_secrets() -> None:
    saved = settings.set_setting(Settings(), "tmdb.api_key", "secret")
    lines = cli._settings_lines(saved, Path("/cfg.json"))

    assert lines[0].endswith(str(Path("/cfg.json")))
    assert "[tmdb]" in lines
    key_line = next(line for line in lines if "api_key" in line)
    assert "secret" not in key_line and "********" in key_line
    region = next(line for line in lines if line.strip().startswith("region"))
    assert "(default, not chosen yet)" in region


# -----------------------------------------------------------------------------
# -h shows the everyday options; --help-all shows everything
# -----------------------------------------------------------------------------

_ADVANCED = ["--tag-art", "--discdb", "--temp-dir", "--cc-format", "--min-duration"]
_EVERYDAY = ["--title", "--main", "--languages", "--subs", "--tag", "--set"]


def _help(capsys: pytest.CaptureFixture[str], flag: str) -> str:
    with pytest.raises(SystemExit) as exc_info:
        _parse(flag)
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def test_short_help_hides_advanced_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    short = _help(capsys, "-h")
    assert all(flag in short for flag in _EVERYDAY)
    assert not any(flag in short for flag in _ADVANCED)
    assert "--help-all" in short


def test_full_help_shows_every_option_in_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    full = _help(capsys, "--help-all")
    assert all(flag in full for flag in _EVERYDAY + _ADVANCED)
    for section in ("Actions", "Tracks", "TMDB tagging", "TheDiscDB", "Temporary"):
        assert section in full
    # Hidden aliases stay hidden even here.
    assert "--save-key" not in full and "--cc-srt" not in full


def test_advanced_options_parse_without_appearing_in_short_help() -> None:
    args = _parse("disc.iso", "--tag-art", "poster", "--min-duration", "30")
    assert (args.tag_art, args.min_duration) == ("poster", 30.0)
