"""Tests for temp-dir selection and tmpfs-aware spill budgeting.

Temp files default to disk-backed /var/tmp (see disc_reader.default_temp_dir);
when the effective temp dir is RAM-backed, the extraction budget is based on
the smaller of total RAM and the tmpfs size, with extra guards for
currently-free RAM and tmpfs space.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import cli
import disc_reader
from models import Config, RuntimeState


def _fake_filesystem(
    monkeypatch: pytest.MonkeyPatch, present: set[str], ram_backed: set[str]
) -> None:
    """Fake Path.is_dir/os.access so only *present* dirs look usable."""
    real_is_dir = Path.is_dir

    def fake_is_dir(self: Path) -> bool:
        # Compare with normalised separators: str(Path("/var/tmp")) is
        # "\\var\\tmp" on Windows, where /var/tmp never exists for real.
        if str(self).replace("\\", "/") == "/var/tmp":
            return "/var/tmp" in present
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(os, "access", lambda _p, _m: True)
    monkeypatch.setattr(
        disc_reader,
        "_is_ram_backed_dir",
        lambda p: str(p).replace("\\", "/") in ram_backed,
    )


def test_default_temp_dir_prefers_var_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TMPDIR", raising=False)
    _fake_filesystem(monkeypatch, {"/var/tmp"}, set())
    monkeypatch.setattr(
        disc_reader.tempfile, "gettempdir", lambda: str(tmp_path / "sys")
    )

    assert disc_reader.default_temp_dir() == Path("/var/tmp")


def test_default_temp_dir_honours_tmpdir_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setenv("TMPDIR", str(custom))

    assert disc_reader.default_temp_dir() == custom


def test_default_temp_dir_skips_ram_backed_var_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TMPDIR", raising=False)
    _fake_filesystem(monkeypatch, {"/var/tmp"}, {"/var/tmp"})
    fallback = tmp_path / "sys"
    fallback.mkdir()
    monkeypatch.setattr(disc_reader.tempfile, "gettempdir", lambda: str(fallback))

    assert disc_reader.default_temp_dir() == fallback


def test_default_temp_dir_falls_back_without_var_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TMPDIR", raising=False)
    _fake_filesystem(monkeypatch, set(), set())
    fallback = tmp_path / "sys"
    fallback.mkdir()
    monkeypatch.setattr(disc_reader.tempfile, "gettempdir", lambda: str(fallback))

    assert disc_reader.default_temp_dir() == fallback


def test_init_ram_budget_uses_tmpfs_size_when_smaller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_limit=0.8)
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", lambda _p: True)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 1000)
    monkeypatch.setattr(disc_reader, "_fs_sizes", lambda _p: (100, 90))

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes == 80


def test_init_ram_budget_uses_ram_when_tmpfs_larger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_limit=0.8)
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", lambda _p: True)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 100)
    monkeypatch.setattr(disc_reader, "_fs_sizes", lambda _p: (1000, 900))

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes == 80


def test_init_ram_budget_falls_back_to_ram_without_fs_sizes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_limit=0.5)
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", lambda _p: True)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 1000)
    monkeypatch.setattr(disc_reader, "_fs_sizes", lambda _p: None)

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes == 500


def test_spills_when_tmpfs_low_on_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_budget_bytes=10**12)
    monkeypatch.setattr(disc_reader, "_available_ram_bytes", lambda: 10**15)
    # Estimate fits the budget and free RAM but not the tmpfs free space.
    monkeypatch.setattr(disc_reader, "_fs_sizes", lambda _p: (10**12, 100))

    assert disc_reader._should_spill_to_disk(200, config) == "space"


def test_no_spill_when_tmpfs_space_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_budget_bytes=10**12)
    monkeypatch.setattr(disc_reader, "_available_ram_bytes", lambda: 10**15)
    monkeypatch.setattr(disc_reader, "_fs_sizes", lambda _p: (10**12, 10**12))

    assert disc_reader._should_spill_to_disk(200, config) is None


def test_configure_runtime_defaults_tempdir_off_tmpfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = tmp_path / "vartmp"
    default.mkdir()
    monkeypatch.setattr("disc_reader.default_temp_dir", lambda: default)
    monkeypatch.setattr("disc_reader.init_ram_budget", lambda _c: None)
    monkeypatch.setattr(cli.dvdifo, "set_debug", lambda _v: None)
    old_tempdir = tempfile.tempdir
    try:
        cli._configure_runtime(RuntimeState(config=Config(debug=True)))
        assert tempfile.tempdir == str(default)
    finally:
        tempfile.tempdir = old_tempdir
