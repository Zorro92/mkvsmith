"""The daily check for a newer mkvsmith (updates.py) and where it shows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mkvsmith import cli, updates
from mkvsmith.updates import UpdateInfo

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def test_versions_compare_as_numbers() -> None:
    assert updates.is_newer("0.26.10", "0.26.9")
    assert updates.is_newer("1.0.0", "0.99.0")
    assert not updates.is_newer("0.26.5", "0.26.5")
    assert not updates.is_newer("0.26.4", "0.26.5")
    assert not updates.is_newer("0.27.0.dev1", "0.26.5")  # not dotted numbers


def test_a_newer_version_is_fetched_once_a_day(tmp_path: Path) -> None:
    cache = tmp_path / "update-check.json"
    fetched: list[str] = []

    def fetch() -> str:
        fetched.append("x")
        return "0.27.0"

    info = updates.check_for_update("0.26.5", path=cache, fetch=fetch, now=NOW)
    assert info is not None and (info.current, info.latest) == ("0.26.5", "0.27.0")
    # Later the same day: the cached answer, no request.
    later = NOW + timedelta(hours=20)
    again = updates.check_for_update("0.26.5", path=cache, fetch=fetch, now=later)
    assert again is not None and again.latest == "0.27.0"
    assert len(fetched) == 1
    # A day on, or when asked to: a fresh request.
    updates.check_for_update(
        "0.26.5", path=cache, fetch=fetch, now=NOW + timedelta(days=1, minutes=1)
    )
    updates.check_for_update("0.26.5", path=cache, fetch=fetch, now=later, force=True)
    assert len(fetched) == 3


def test_up_to_date_or_offline_says_nothing(tmp_path: Path) -> None:
    cache = tmp_path / "update-check.json"
    assert (
        updates.check_for_update("0.26.5", path=cache, fetch=lambda: "0.26.5", now=NOW)
        is None
    )
    offline = tmp_path / "offline.json"
    assert (
        updates.check_for_update("0.26.5", path=offline, fetch=lambda: None, now=NOW)
        is None
    )
    # Nothing remembered offline: the next run tries again.
    assert not offline.exists()


def test_a_damaged_cache_is_fetched_over(tmp_path: Path) -> None:
    cache = tmp_path / "update-check.json"
    cache.write_text("{not json")
    info = updates.check_for_update(
        "0.26.5", path=cache, fetch=lambda: "0.27.0", now=NOW
    )
    assert info is not None and info.latest == "0.27.0"


@pytest.mark.parametrize(
    ("prefix", "command"),
    [
        ("/home/me/.local/share/uv/tools/mkvsmith", "uv tool upgrade mkvsmith"),
        (
            "C:\\Users\\me\\AppData\\Roaming\\uv\\tools\\mkvsmith",
            "uv tool upgrade mkvsmith",
        ),
        ("/home/me/.local/share/pipx/venvs/mkvsmith", "pipx upgrade mkvsmith"),
    ],
)
def test_the_upgrade_command_follows_the_installation(
    prefix: str, command: str
) -> None:
    assert updates.upgrade_command(prefix) == command


def test_checks_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(updates.DISABLE_ENV, raising=False)
    assert updates.checks_enabled(True)
    assert not updates.checks_enabled(False)
    monkeypatch.setenv(updates.DISABLE_ENV, "1")
    assert not updates.checks_enabled(True)


def test_a_background_check_hands_over_its_result() -> None:
    info = UpdateInfo("0.26.5", "0.27.0", "uv tool upgrade mkvsmith")
    assert updates.BackgroundCheck(lambda: info).result(wait=5) == info

    def broken() -> UpdateInfo | None:
        raise RuntimeError("boom")

    # A failing check never fails the run.
    assert updates.BackgroundCheck(broken).result(wait=5) is None


def test_the_cli_reports_a_newer_version_after_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    info = UpdateInfo("0.26.5", "0.27.0", "uv tool upgrade mkvsmith")
    cli._report_update(updates.BackgroundCheck(lambda: info))
    out = " ".join(capsys.readouterr().out.split())  # the terminal may wrap it
    assert "mkvsmith 0.27.0 is available (you have 0.26.5)" in out
    assert "uv tool upgrade mkvsmith" in out
    cli._report_update(None)  # checks off: nothing
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("latest", "code", "expected"),
    [
        ("9.0.0", 0, "mkvsmith 9.0.0 is available"),
        (cli.MKVSMITH_VERSION, 0, "is up to date"),
        (None, 1, "Could not check for updates"),
    ],
)
def test_check_updates_asks_now(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    latest: str | None,
    code: int,
    expected: str,
) -> None:
    monkeypatch.setattr(cli, "fetch_latest_version", lambda: latest)
    monkeypatch.setattr(updates, "cache_path", lambda: tmp_path / "check.json")
    with pytest.raises(SystemExit) as exited:
        cli._check_updates_now()
    assert exited.value.code == code
    captured = capsys.readouterr()
    assert expected in " ".join((captured.out + captured.err).split())
