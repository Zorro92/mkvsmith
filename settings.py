"""
Persisted user settings for mkvsmith.

A single JSON file (~/.mkvsmith_config.json) holds cross-run preferences:
    - "language": UI language code (e.g. "es"), see i18n.py
    - "api_key":  TMDB API key (used by tagger.py)

Previously the TMDB key lived in ~/.mkv_tagger_config.json; on first access we
migrate that file's contents into the new settings path so existing users keep
their key without re-entering it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path.home() / ".mkvsmith_config.json"
_LEGACY_CONFIG_PATH = Path.home() / ".mkv_tagger_config.json"


def load_settings() -> dict[str, Any]:
    """Load settings, migrating from the legacy tagger config if needed.

    Returns an empty dict (never raises) on missing/corrupt files.
    """
    # New settings file present -> use it directly.
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        pass

    # One-time migration from the old tagger-only config.
    try:
        legacy = json.loads(_LEGACY_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        legacy = {}

    if legacy:
        save_settings(legacy)
    return legacy


def save_settings(cfg: dict[str, Any]) -> None:
    """Write *cfg* to the settings path, creating it if necessary."""
    SETTINGS_PATH.write_text(json.dumps(cfg, indent=2))
