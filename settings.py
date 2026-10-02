"""
Persisted user settings for mkvsmith.

One JSON file holds the user's defaults for everything that can be switched
on or off or tuned per run: track selection, scanning, temp space, TMDB
tagging, TheDiscDB, and the UI language. The file lives under the XDG Base
Directory spec (``$XDG_CONFIG_HOME/mkvsmith/config.json``, default
``~/.config/...``); ``$MKVSMITH_CONFIG`` points at a different file, so a
script can run with its own settings.

Every run resolves each option in the same order (first match wins):

    1. command-line flag
    2. environment variable (only TMDB_API_KEY / THEDISCDB_* have one)
    3. settings file
    4. built-in default (the ``Settings`` field defaults)

Completeness: the file holds only settings the user has answered (or, for
advanced settings, accepted the default of). ``missing_settings`` lists the
everyday settings still unanswered: every setting on a first run, and only
the new ones after an update adds settings. The interactive prompt asks
those before it starts; a plain CLI run never asks and uses the built-in
default for anything unanswered.

"Ask every time": per-disc choices (closed captions, splitting packed
episodes, overwriting, tagging, cover art) can be saved as ``"ask"``. The
interactive prompt then asks on each disc; a plain CLI run treats ``"ask"``
as the built-in default.

Settings are a flat ``Settings`` dataclass in code and a sectioned JSON file
on disk, mapped by ``SETTING_SPECS``. The table also drives validation and
the generic ``get_setting`` / ``set_setting`` / ``reset_setting`` helpers, so
a settings screen (interactive prompt or GUI) needs no per-option code.

The file is versioned. Version 1 was flat (``language``, ``api_key``,
``discdb``); it, and the older ``~/.mkvsmith_config.json`` /
``~/.mkv_tagger_config.json`` files, migrate forward on first load.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, cast

from xdg_base_dirs import xdg_config_home

SETTINGS_VERSION = 2
# Fixed settings path; None (the default) follows $XDG_CONFIG_HOME per call.
SETTINGS_PATH: Path | None = None
_OLD_SETTINGS_PATH = Path.home() / ".mkvsmith_config.json"
_LEGACY_CONFIG_PATH = Path.home() / ".mkv_tagger_config.json"
CONFIG_ENV = "MKVSMITH_CONFIG"

# TMDB metadata fetched when tagging (matches tagger.py's property names).
DEFAULT_TAG_METADATA = [
    "TMDbID",
    "IMDbID",
    "Cast",
    "Writers",
    "Directors",
    "Title",
    "Overview",
    "Genres",
    "ReleaseDate",
    "Runtime",
]

# Per-disc choices: a fixed answer, or "ask" (every time, interactive only).
ASK_MODES = ("never", "ask", "always")
TAG_ART_CHOICES = ("none", "poster", "backdrop", "both", "ask")
CC_FORMATS = ("srt", "ass")
DISCDB_CONTRIBUTE_MODES = ("off", "browser", "manual", "direct")


@dataclass
class Settings:
    """The user's defaults; field defaults are the built-in defaults."""

    # [ui]
    ui_language: str | None = None  # None = detect from the locale
    # [output]
    overwrite: str = "ask"  # an existing output file: ask / always / never
    # [tracks]
    languages: list[str] = field(default_factory=lambda: ["eng", "en", "und"])
    all_audio: bool = True
    subtitles: bool = True
    all_subtitles: bool = False  # False: only the preferred languages
    forced_subtitles: bool = True
    closed_captions: str = "never"
    cc_format: str = "srt"
    # [scan]
    min_duration: float = 60.0
    show_all: bool = False
    split_episodes: str = "never"
    # [temp]
    temp_dir: Path | None = None  # None = /var/tmp when usable, else system temp
    ram_limit: float = 0.8
    # [tmdb]
    tmdb_api_key: str | None = None
    tagging: str = "ask"
    tag_confirm_match: bool = True
    tag_art: str = "ask"
    tag_metadata: list[str] = field(default_factory=lambda: list(DEFAULT_TAG_METADATA))
    tag_region: str = "US"
    tag_language: str | None = None
    tag_save_xml: bool = False
    # [discdb]
    discdb_enabled: bool = False
    discdb_base_url: str = "https://thediscdb.com"
    discdb_timeout: float = 10.0
    discdb_contribute: str = "off"
    discdb_open_browser: bool = True

    # ``section.name`` keys the user has answered (present in the file, or
    # set through ``set_setting``). Only these are written back, so a
    # setting nobody chose stays missing and gets asked.
    answered: set[str] = field(default_factory=set[str], compare=False)
    # Keys this version doesn't know (from a newer mkvsmith, or typos), kept
    # so saving never drops them. Section -> key -> raw JSON value.
    unknown: dict[str, dict[str, Any]] = field(
        default_factory=dict[str, dict[str, Any]], compare=False
    )


# =============================================================================
# Value parsers
# =============================================================================
#
# Each parser takes a JSON value (or, from set_setting, a user-typed string)
# and returns the typed value, raising ValueError with a short reason.

_TRUE = ("1", "true", "yes", "y", "on")
_FALSE = ("0", "false", "no", "n", "off")


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ValueError("expected yes or no")


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("expected a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValueError("expected a number")


def _parse_float(low: float, high: float | None = None) -> Callable[[object], float]:
    def parse(value: object) -> float:
        number = _number(value)
        if number < low or (high is not None and number > high):
            bound = (
                f"between {low:g} and {high:g}"
                if high is not None
                else f"at least {low:g}"
            )
            raise ValueError(f"must be {bound}")
        return number

    return parse


def _parse_positive(value: object) -> float:
    number = _number(value)
    if number <= 0:
        raise ValueError("must be greater than 0")
    return number


def _parse_str(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("expected text")


def _optional(parse: Callable[[object], Any]) -> Callable[[object], Any]:
    """*parse*, with JSON null / empty text meaning "unset"."""

    def parse_optional(value: object) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return parse(value)

    return parse_optional


def _parse_path(value: object) -> Path:
    return Path(_parse_str(value)).expanduser()


def _parse_list(value: object) -> list[str]:
    """A list of strings, or comma-separated text."""
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [
            item.strip() if isinstance(item, str) else None
            for item in cast(list[object], value)
        ]
    else:
        raise ValueError("expected a list")
    if any(item is None for item in items):
        raise ValueError("expected a list of text")
    cleaned = [item for item in items if item is not None and item]
    if not cleaned:
        raise ValueError("must not be empty")
    return cleaned


def _parse_choice(
    choices: tuple[str, ...], *, yes: str | None = None, no: str | None = None
) -> Callable[[object], str]:
    """One of *choices*; *yes* / *no* also accept true/false (or yes/no)."""

    def parse(value: object) -> str:
        if isinstance(value, bool) or (
            isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE
        ):
            answer = _parse_bool(value)
            mapped = yes if answer else no
            if mapped is not None:
                return mapped
        text = _parse_str(value).lower() if not isinstance(value, bool) else ""
        if text not in choices:
            raise ValueError("expected one of: " + ", ".join(choices))
        return text

    return parse


# =============================================================================
# The settings table
# =============================================================================

# How a settings screen should ask for a value.
SettingKind = Literal["bool", "choice", "number", "text", "list", "path", "language"]


def _always_relevant(_settings: Settings) -> bool:
    return True


@dataclass(frozen=True)
class SettingSpec:
    """One setting: its ``section.name`` file key and ``Settings`` field.

    *prompt* settings are asked when missing (interactive prompt only);
    the rest are advanced and silently take the built-in default.
    *relevant* says whether asking makes sense yet (the cover-art choice
    only matters once tagging is on); an irrelevant setting isn't asked,
    and stays missing so it is asked once it matters.
    """

    key: str
    attr: str
    kind: SettingKind
    parse: Callable[[object], Any]
    description: str
    choices: tuple[str, ...] = ()
    prompt: bool = False
    secret: bool = False
    relevant: Callable[[Settings], bool] = _always_relevant

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]


def _has_tmdb_key(settings: Settings) -> bool:
    return bool(settings.tmdb_api_key or os.environ.get("TMDB_API_KEY"))


def _tags(settings: Settings) -> bool:
    return _has_tmdb_key(settings) and settings.tagging != "never"


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "ui.language",
        "ui_language",
        "language",
        _optional(_parse_str),
        "Interface language",
        prompt=True,
    ),
    SettingSpec(
        "output.overwrite",
        "overwrite",
        "choice",
        _parse_choice(ASK_MODES, yes="always", no="ask"),
        "Overwrite an output file that already exists?",
        ASK_MODES,
        prompt=True,
    ),
    SettingSpec(
        "tracks.languages",
        "languages",
        "list",
        _parse_list,
        "Preferred audio/subtitle languages, most preferred first",
        prompt=True,
    ),
    SettingSpec(
        "tracks.all_audio",
        "all_audio",
        "bool",
        _parse_bool,
        "Keep every audio track, not only the preferred languages?",
        prompt=True,
    ),
    SettingSpec(
        "tracks.subtitles",
        "subtitles",
        "bool",
        _parse_bool,
        "Keep subtitle tracks?",
        prompt=True,
    ),
    SettingSpec(
        "tracks.all_subtitles",
        "all_subtitles",
        "bool",
        _parse_bool,
        "Keep subtitles in every language, not only the preferred languages?",
        prompt=True,
        relevant=lambda s: s.subtitles,
    ),
    SettingSpec(
        "tracks.forced_subtitles",
        "forced_subtitles",
        "bool",
        _parse_bool,
        "Keep forced subtitle tracks?",
        prompt=True,
    ),
    SettingSpec(
        "tracks.closed_captions",
        "closed_captions",
        "choice",
        _parse_choice(ASK_MODES, yes="always", no="never"),
        "Extract DVD closed captions (EIA-608) as a text subtitle track?",
        ASK_MODES,
        prompt=True,
    ),
    SettingSpec(
        "tracks.cc_format",
        "cc_format",
        "choice",
        _parse_choice(CC_FORMATS),
        "Closed-caption format (srt: plain text; ass: keeps positioning and italics)",
        CC_FORMATS,
        prompt=True,
        relevant=lambda s: s.closed_captions != "never",
    ),
    SettingSpec(
        "scan.min_duration",
        "min_duration",
        "number",
        _parse_float(0.0),
        "Hide titles shorter than this many seconds",
    ),
    SettingSpec(
        "scan.show_all",
        "show_all",
        "bool",
        _parse_bool,
        "Show low-quality titles too (menus, trailers, etc.)?",
    ),
    SettingSpec(
        "scan.split_episodes",
        "split_episodes",
        "choice",
        _parse_choice(ASK_MODES, yes="always", no="never"),
        "Split playlists holding back-to-back episodes into one title each?",
        ASK_MODES,
        prompt=True,
    ),
    SettingSpec(
        "temp.dir",
        "temp_dir",
        "path",
        _optional(_parse_path),
        "Directory for temporary files (empty: /var/tmp when usable)",
    ),
    SettingSpec(
        "temp.ram_limit",
        "ram_limit",
        "number",
        _parse_float(0.0, 1.0),
        "Max fraction of RAM-backed temp space to use (0 disables the check)",
    ),
    SettingSpec(
        "tmdb.api_key",
        "tmdb_api_key",
        "text",
        _optional(_parse_str),
        "TMDB API key (optional, enables tagging)",
        prompt=True,
        secret=True,
    ),
    SettingSpec(
        "tmdb.tagging",
        "tagging",
        "choice",
        _parse_choice(ASK_MODES, yes="always", no="never"),
        "Tag rips with TMDB metadata?",
        ASK_MODES,
        prompt=True,
        relevant=_has_tmdb_key,
    ),
    SettingSpec(
        "tmdb.confirm_match",
        "tag_confirm_match",
        "bool",
        _parse_bool,
        "Confirm the TMDB match before tagging?",
        prompt=True,
        relevant=_tags,
    ),
    SettingSpec(
        "tmdb.art",
        "tag_art",
        "choice",
        _parse_choice(TAG_ART_CHOICES),
        "Cover art to attach",
        TAG_ART_CHOICES,
        prompt=True,
        relevant=_tags,
    ),
    SettingSpec(
        "tmdb.metadata",
        "tag_metadata",
        "list",
        _parse_list,
        "TMDB metadata properties to fetch",
    ),
    SettingSpec(
        "tmdb.region",
        "tag_region",
        "text",
        _parse_str,
        "ISO 3166-1 region for the content rating",
    ),
    SettingSpec(
        "tmdb.language",
        "tag_language",
        "text",
        _optional(_parse_str),
        "TMDB language for localized metadata (empty: TMDB's default)",
    ),
    SettingSpec(
        "tmdb.save_xml",
        "tag_save_xml",
        "bool",
        _parse_bool,
        "Keep the XML tag file after muxing?",
    ),
    SettingSpec(
        "discdb.enabled",
        "discdb_enabled",
        "bool",
        _parse_bool,
        "Look discs up on TheDiscDB (real episode numbers and titles)?",
        prompt=True,
    ),
    SettingSpec(
        "discdb.base_url",
        "discdb_base_url",
        "text",
        _parse_str,
        "TheDiscDB base URL",
    ),
    SettingSpec(
        "discdb.timeout",
        "discdb_timeout",
        "number",
        _parse_positive,
        "TheDiscDB network timeout in seconds",
    ),
    SettingSpec(
        "discdb.contribute",
        "discdb_contribute",
        "choice",
        _parse_choice(DISCDB_CONTRIBUTE_MODES),
        "Write a TheDiscDB contribution bundle (off, browser, manual, direct)",
        DISCDB_CONTRIBUTE_MODES,
    ),
    SettingSpec(
        "discdb.open_browser",
        "discdb_open_browser",
        "bool",
        _parse_bool,
        "Open TheDiscDB in a browser after preparing a contribution?",
    ),
)

_SPECS_BY_KEY = {spec.key: spec for spec in SETTING_SPECS}
assert {spec.attr for spec in SETTING_SPECS} == {
    f.name for f in fields(Settings) if f.name not in ("answered", "unknown")
}, "every Settings field needs exactly one SettingSpec"


def setting_spec(key: str) -> SettingSpec:
    """The spec for ``section.name`` *key*; ``KeyError`` names unknown keys."""
    try:
        return _SPECS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown setting: {key}") from None


# =============================================================================
# Completeness
# =============================================================================


def missing_settings(settings: Settings) -> list[SettingSpec]:
    """Everyday settings still unanswered that matter now, in asking order.

    Relevance depends on earlier answers, so a settings screen should
    re-check after each answer rather than ask this whole list.
    """
    return [
        spec
        for spec in SETTING_SPECS
        if spec.prompt and spec.key not in settings.answered and spec.relevant(settings)
    ]


def accept_advanced_defaults(settings: Settings) -> Settings:
    """*settings* with every unanswered advanced setting marked answered.

    Advanced settings are never asked: once the everyday ones are complete,
    they are written with their defaults so the file shows them for editing.
    """
    advanced = {spec.key for spec in SETTING_SPECS if not spec.prompt}
    return replace(settings, answered=settings.answered | advanced)


# =============================================================================
# Reading and writing
# =============================================================================


@dataclass
class LoadedSettings:
    """Settings as read, with what was wrong with the file (if anything)."""

    settings: Settings
    path: Path
    exists: bool
    # Human-readable problems: invalid values (replaced by the default),
    # an unreadable file. Never fatal; the caller shows them.
    problems: list[str] = field(default_factory=list[str])
    # The file exists but couldn't be read at all (bad JSON, permissions):
    # saving over it would lose the user's settings, so callers mustn't.
    unreadable: bool = False


def settings_path() -> Path:
    """The settings file: ``$MKVSMITH_CONFIG`` when set, else the XDG path."""
    override = os.environ.get(CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return SETTINGS_PATH or xdg_config_home() / "mkvsmith" / "config.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object at *path*, or None when missing.

    Raises ``ValueError`` for a file that exists but isn't a valid JSON
    object, so a typo in a hand-edited file is reported rather than ignored.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(str(e)) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return cast(dict[str, Any], data)


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Version 1's flat file as version 2's sections."""
    sections: dict[str, dict[str, Any]] = {}
    if "language" in data:
        sections["ui"] = {"language": data["language"]}
    if "api_key" in data:
        sections["tmdb"] = {"api_key": data["api_key"]}
    old = data.get("discdb")
    if isinstance(old, dict):
        old = cast(dict[str, Any], old)
        discdb: dict[str, Any] = {}
        for old_key, new_key in (
            ("enabled", "enabled"),
            ("base_url", "base_url"),
            ("timeout_seconds", "timeout"),
            ("open_browser", "open_browser"),
        ):
            if old_key in old:
                discdb[new_key] = old[old_key]
        if "contribute" in old:
            discdb["contribute"] = (
                old.get("contribute_mode") or "browser" if old["contribute"] else "off"
            )
        sections["discdb"] = discdb
    return {"version": SETTINGS_VERSION, **sections}


def settings_from_dict(data: dict[str, Any]) -> tuple[Settings, list[str]]:
    """Parse a version-2 settings object; invalid values fall back.

    A setting with an invalid value counts as unanswered, so it is asked
    again rather than silently replaced in the file.
    """
    values: dict[str, Any] = {}
    answered: set[str] = set()
    unknown: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for section, body in data.items():
        if section == "version":
            continue
        if not isinstance(body, dict):
            problems.append(f"{section}: expected a section of settings")
            continue
        for name, raw in cast(dict[str, Any], body).items():
            key = f"{section}.{name}"
            spec = _SPECS_BY_KEY.get(key)
            if spec is None:
                unknown.setdefault(section, {})[name] = raw
                problems.append(f"{key}: unknown setting (kept, but unused)")
                continue
            try:
                values[spec.attr] = spec.parse(raw)
            except ValueError as e:
                problems.append(f"{key}: {e}; using the default")
            else:
                answered.add(key)
    return Settings(**values, answered=answered, unknown=unknown), problems


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    """*settings* as the sectioned file object (answered settings only)."""
    data: dict[str, Any] = {"version": SETTINGS_VERSION}
    for spec in SETTING_SPECS:
        if spec.key not in settings.answered:
            continue
        value = getattr(settings, spec.attr)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, list):
            value = list(cast(list[str], value))
        data.setdefault(spec.section, {})[spec.name] = value
    for section, body in settings.unknown.items():
        known = data.setdefault(section, {})
        known.update({k: v for k, v in body.items() if k not in known})
    return data


def load_settings(path: Path | None = None) -> LoadedSettings:
    """Read the settings file. Never raises; problems are reported instead.

    A version-1 file, or one of the legacy home-directory files, is
    migrated and written back in the current format.
    """
    path = path or settings_path()
    problems: list[str] = []
    try:
        data = _read_json(path)
    except ValueError as e:
        return LoadedSettings(Settings(), path, True, [f"{path}: {e}"], True)
    exists = data is not None
    migrate = False
    # The legacy home-directory files only stand in for the default file.
    if data is None and not os.environ.get(CONFIG_ENV):
        for legacy_path in (_OLD_SETTINGS_PATH, _LEGACY_CONFIG_PATH):
            try:
                legacy = _read_json(legacy_path)
            except ValueError:
                continue
            if legacy:
                data, migrate = legacy, True
                break
    if data is None:
        return LoadedSettings(Settings(), path, False)
    version = data.get("version", 1)
    if version == 1:
        data = _migrate_v1(data)
        migrate = True
    elif version != SETTINGS_VERSION:
        problems.append(
            f"{path}: settings version {version} is newer than this mkvsmith "
            f"understands ({SETTINGS_VERSION}); reading what it can"
        )
    settings, parse_problems = settings_from_dict(data)
    problems.extend(parse_problems)
    if migrate:
        try:
            save_settings(settings, path)
            exists = True
        except OSError as e:
            problems.append(f"{path}: could not save migrated settings ({e})")
    return LoadedSettings(settings, path, exists, problems)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write *settings* to *path* (default: ``settings_path()``) atomically.

    The file holds an API key, so it is created readable by the user only.
    Returns the path written.
    """
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(settings_to_dict(settings), indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


# =============================================================================
# Generic access (for a settings screen)
# =============================================================================


def get_setting(settings: Settings, key: str) -> Any:
    return getattr(settings, setting_spec(key).attr)


def set_setting(settings: Settings, key: str, value: object) -> Settings:
    """A copy of *settings* with *key* answered as *value* (JSON or text).

    Raises ``KeyError`` for an unknown key and ``ValueError`` for a value the
    setting doesn't accept; *settings* itself is never modified.
    """
    spec = setting_spec(key)
    return replace(
        settings,
        answered=settings.answered | {key},
        **{spec.attr: spec.parse(value)},
    )


def reset_setting(settings: Settings, key: str) -> Settings:
    """A copy of *settings* with *key* answered as its built-in default."""
    spec = setting_spec(key)
    return replace(
        settings,
        answered=settings.answered | {key},
        **{spec.attr: getattr(Settings(), spec.attr)},
    )


def format_setting(settings: Settings, key: str) -> str:
    """*key*'s value as text for display (secrets masked)."""
    spec = setting_spec(key)
    return format_value(spec, getattr(settings, spec.attr))


def format_value(spec: SettingSpec, value: object) -> str:
    """*value* of *spec* as text (secrets masked, lists comma-separated)."""
    if value is None:
        return ""
    if spec.secret:
        return "********"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ",".join(cast(list[str], value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
