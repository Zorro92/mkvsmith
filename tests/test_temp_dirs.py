"""Tests for temp-dir selection and tmpfs-aware spill budgeting.

Temp files default to disk-backed /var/tmp (see disc_reader.default_temp_dir);
when the effective temp dir is RAM-backed, the extraction budget is based on
the smaller of total RAM and the tmpfs size, with extra guards for
currently-free RAM and tmpfs space.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from mkvsmith import cli, disc_reader
from mkvsmith.models import SESSION_DIR_PREFIX, Config, RuntimeState


def _always_ram_backed(_p: Path) -> bool:
    return True


def _fs_sizes_returning(
    sizes: tuple[int, int] | None,
) -> Callable[[Path], tuple[int, int] | None]:
    def fake_fs_sizes(_p: Path) -> tuple[int, int] | None:
        return sizes

    return fake_fs_sizes


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

    def fake_access(_p: object, _m: int) -> bool:
        return True

    def fake_is_ram_backed_dir(p: Path) -> bool:
        return str(p).replace("\\", "/") in ram_backed

    monkeypatch.setattr(os, "access", fake_access)
    monkeypatch.setattr(
        disc_reader,
        "_is_ram_backed_dir",
        fake_is_ram_backed_dir,
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
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", _always_ram_backed)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 1000)
    monkeypatch.setattr(disc_reader, "_fs_sizes", _fs_sizes_returning((100, 90)))

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes == 80


def test_init_ram_budget_uses_ram_when_tmpfs_larger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_limit=0.8)
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", _always_ram_backed)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 100)
    monkeypatch.setattr(disc_reader, "_fs_sizes", _fs_sizes_returning((1000, 900)))

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes == 80


def test_init_ram_budget_falls_back_to_ram_without_fs_sizes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_limit=0.5)
    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", _always_ram_backed)
    monkeypatch.setattr(disc_reader, "_total_ram_bytes", lambda: 1000)
    monkeypatch.setattr(disc_reader, "_fs_sizes", _fs_sizes_returning(None))

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
    monkeypatch.setattr(disc_reader, "_fs_sizes", _fs_sizes_returning((10**12, 100)))

    assert disc_reader._should_spill_to_disk(200, config) == "space"


def test_no_spill_when_tmpfs_space_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "tmp"
    work.mkdir()
    config = Config(temp_dir=work, ram_budget_bytes=10**12)
    monkeypatch.setattr(disc_reader, "_available_ram_bytes", lambda: 10**15)
    monkeypatch.setattr(disc_reader, "_fs_sizes", _fs_sizes_returning((10**12, 10**12)))

    assert disc_reader._should_spill_to_disk(200, config) is None


def test_configure_runtime_defaults_tempdir_off_tmpfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = tmp_path / "vartmp"
    default.mkdir()
    monkeypatch.setattr("mkvsmith.disc_reader.default_temp_dir", lambda: default)

    def skip_init_ram_budget(_c: Config | None = None) -> None:
        return None

    def skip_set_debug(_v: Callable[[str], None] | None) -> None:
        return None

    monkeypatch.setattr("mkvsmith.disc_reader.init_ram_budget", skip_init_ram_budget)
    monkeypatch.setattr(cli.dvdifo, "set_debug", skip_set_debug)

    def only_default(_config: Config | None = None) -> list[Path]:
        return [default]

    monkeypatch.setattr("mkvsmith.disc_reader.temp_base_candidates", only_default)
    old_tempdir = tempfile.tempdir
    state = RuntimeState(config=Config(debug=True))
    try:
        cli._configure_runtime(state)
        session = Path(tempfile.tempdir or "")
    finally:
        tempfile.tempdir = old_tempdir
    # Temp files go into a marked per-run session dir under the default base.
    assert session.parent == default
    assert session.name.startswith(SESSION_DIR_PREFIX)
    assert state.cleanup.temp_dirs == [session]
