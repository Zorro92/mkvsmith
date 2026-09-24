"""
Persisted user settings for mkvsmith.

A single JSON file holds cross-run preferences:
    - "language": UI language code (e.g. "es"), see i18n.py
    - "api_key":  TMDB API key (used by tagger.py)
    - "discdb":   Optional TheDiscDB settings used by discdb.py/cli.py

The file lives under the XDG Base Directory spec
(``$XDG_CONFIG_HOME/mkvsmith/config.json``, default ``~/.config/...``).

Previously the settings lived in ``~/.mkvsmith_config.json`` (and before that
the TMDB key alone in ``~/.mkv_tagger_config.json``); on first access we
migrate those files' contents into the new settings path so existing users
keep their preferences without re-entering them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xdg_base_dirs import xdg_config_home

SETTINGS_PATH = xdg_config_home() / "mkvsmith" / "config.json"
_OLD_SETTINGS_PATH = Path.home() / ".mkvsmith_config.json"
_LEGACY_CONFIG_PATH = Path.home() / ".mkv_tagger_config.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return the JSON dict at *path*, or None on missing/corrupt files."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_settings() -> dict[str, Any]:
    """Load settings, migrating from the legacy home-dir files if needed.

    Returns an empty dict (never raises) on missing/corrupt files.
    """
    # New settings file present -> use it directly.
    cached = _read_json(SETTINGS_PATH)
    if cached is not None:
        return cached

    # One-time migration from the previous locations (newest first).
    for legacy_path in (_OLD_SETTINGS_PATH, _LEGACY_CONFIG_PATH):
        legacy = _read_json(legacy_path)
        if legacy:
            save_settings(legacy)
            return legacy
    return {}


def save_settings(cfg: dict[str, Any]) -> None:
    """Write *cfg* to the settings path, creating it if necessary."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
