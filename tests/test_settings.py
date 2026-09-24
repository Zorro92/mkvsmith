"""Tests for XDG-based settings (settings.py).

Settings live at $XDG_CONFIG_HOME/mkvsmith/config.json (default
~/.config/...); the previous ~/.mkvsmith_config.json and the legacy
~/.mkv_tagger_config.json migrate forward on first access.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import settings


def _point_settings(monkeypatch, new: Path, old: Path, legacy: Path) -> None:
    monkeypatch.setattr(settings, "SETTINGS_PATH", new)
    monkeypatch.setattr(settings, "_OLD_SETTINGS_PATH", old)
    monkeypatch.setattr(settings, "_LEGACY_CONFIG_PATH", legacy)


def test_settings_path_uses_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    reloaded = importlib.reload(settings)
    try:
        assert reloaded.SETTINGS_PATH == tmp_path / "xdg" / "mkvsmith" / "config.json"
    finally:
        importlib.reload(settings)


def test_save_creates_parents_and_roundtrips(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "cfg" / "mkvsmith" / "config.json"
    _point_settings(monkeypatch, new, tmp_path / "old.json", tmp_path / "legacy.json")
    settings.save_settings({"language": "es"})
    assert new.is_file()
    assert settings.load_settings() == {"language": "es"}


def test_migrates_old_home_settings(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.json"
    old = tmp_path / "home_dotfile.json"
    _point_settings(monkeypatch, new, old, tmp_path / "legacy.json")
    old.write_text(json.dumps({"api_key": "k"}), encoding="utf-8")

    assert settings.load_settings() == {"api_key": "k"}
    # Migrated content is persisted at the new XDG path.
    assert json.loads(new.read_text(encoding="utf-8")) == {"api_key": "k"}


def test_migrates_legacy_tagger_config(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.json"
    legacy = tmp_path / "tagger_dotfile.json"
    _point_settings(monkeypatch, new, tmp_path / "old.json", legacy)
    legacy.write_text(json.dumps({"api_key": "t"}), encoding="utf-8")

    assert settings.load_settings() == {"api_key": "t"}
    assert json.loads(new.read_text(encoding="utf-8")) == {"api_key": "t"}


def test_new_settings_take_precedence(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.json"
    old = tmp_path / "old.json"
    _point_settings(monkeypatch, new, old, tmp_path / "legacy.json")
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"language": "es"}), encoding="utf-8")
    old.write_text(json.dumps({"language": "en"}), encoding="utf-8")

    assert settings.load_settings() == {"language": "es"}


def test_missing_and_corrupt_settings_return_empty(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.json"
    _point_settings(monkeypatch, new, tmp_path / "old.json", tmp_path / "legacy.json")
    assert settings.load_settings() == {}

    new.parent.mkdir(parents=True)
    new.write_text("{not json", encoding="utf-8")
    assert settings.load_settings() == {}
