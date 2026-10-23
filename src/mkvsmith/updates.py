"""Checking for a newer mkvsmith.

At most once a day the version on the project's main branch is read
(``pyproject.toml``) and remembered in the user cache dir; when it's newer
than the running one, the caller says so, with the command that updates
this installation. Nothing is installed here. The check is skipped when
the ``updates.check`` setting is off or ``MKVSMITH_NO_UPDATE_CHECK`` is
set (tests, CI), and stays quiet when offline.

Releases come from the main branch today; with releases on PyPI,
``fetch_latest_version`` would read PyPI's JSON API instead
(``https://pypi.org/pypi/mkvsmith/json``, ``info.version``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xdg_base_dirs import xdg_cache_home

from mkvsmith.i18n import tr
from mkvsmith.models import MKVSMITH_VERSION, log_debug

LATEST_URL = "https://raw.githubusercontent.com/Zorro92/mkvsmith/main/pyproject.toml"
DISABLE_ENV = "MKVSMITH_NO_UPDATE_CHECK"
CHECK_INTERVAL = timedelta(days=1)
_TIMEOUT = 5.0


@dataclass(frozen=True)
class UpdateInfo:
    """A newer mkvsmith than the one running, and how to get it."""

    current: str
    latest: str
    command: str


def _version_key(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except ValueError:
        return None


def is_newer(latest: str, current: str) -> bool:
    """Whether version *latest* is past *current* (dotted numbers only)."""
    new, old = _version_key(latest), _version_key(current)
    return new is not None and old is not None and new > old


def fetch_latest_version(timeout: float = _TIMEOUT) -> str | None:
    """The version on the main branch, or None when it can't be read."""
    request = urllib.request.Request(
        LATEST_URL, headers={"User-Agent": f"mkvsmith/{MKVSMITH_VERSION}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
        version = tomllib.loads(data.decode("utf-8"))["project"]["version"]
    except (OSError, ValueError, KeyError, UnicodeDecodeError) as exc:
        log_debug(f"Update check failed: {exc}")
        return None
    return version if isinstance(version, str) else None


def cache_path() -> Path:
    return xdg_cache_home() / "mkvsmith" / "update-check.json"


def _read_cache(path: Path) -> tuple[datetime, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["checked"])
        latest = data["latest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if checked.tzinfo is None or not isinstance(latest, str):
        return None
    return checked, latest


def _write_cache(path: Path, checked: datetime, latest: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked": checked.isoformat(), "latest": latest}),
            encoding="utf-8",
        )
    except OSError as exc:
        log_debug(f"Could not save the update check: {exc}")


def upgrade_command(prefix: str | None = None) -> str:
    """The command that updates this installation of mkvsmith."""
    where = (prefix or sys.prefix).replace("\\", "/")
    if "/pipx/venvs/" in where:
        return "pipx upgrade mkvsmith"
    if "/uv/tools/" in where:
        return "uv tool upgrade mkvsmith"
    source = Path(__file__).resolve().parents[2]
    if (source / ".git").exists():
        return f"git -C {source} pull"
    return "uv tool upgrade mkvsmith"


def check_for_update(
    current: str = MKVSMITH_VERSION,
    *,
    force: bool = False,
    path: Path | None = None,
    fetch: Callable[[], str | None] = fetch_latest_version,
    now: datetime | None = None,
) -> UpdateInfo | None:
    """A newer version than *current*, from the day's cached check or a
    fresh one (*force* always fetches). None when up to date or unknown."""
    path = path or cache_path()
    now = now or datetime.now(UTC)
    cached = None if force else _read_cache(path)
    if cached is not None and now - cached[0] < CHECK_INTERVAL:
        latest: str | None = cached[1]
    else:
        latest = fetch()
        if latest is not None:
            _write_cache(path, now, latest)
    if latest is None or not is_newer(latest, current):
        return None
    return UpdateInfo(current, latest, upgrade_command())


def checks_enabled(setting: bool) -> bool:
    """Whether to check at all: the setting, unless the environment says no."""
    return setting and not os.environ.get(DISABLE_ENV, "").strip()


def update_notice(info: UpdateInfo) -> str:
    """The line that tells the user about *info*."""
    return tr(
        "mkvsmith {latest} is available (you have {current}). Update with: {command}",
        latest=info.latest,
        current=info.current,
        command=info.command,
    )


class BackgroundCheck:
    """A check run on its own thread, so it never holds up the work."""

    def __init__(
        self, check: Callable[[], UpdateInfo | None] = check_for_update
    ) -> None:
        self._result: UpdateInfo | None = None
        self._thread = threading.Thread(target=self._run, args=(check,), daemon=True)
        self._thread.start()

    def _run(self, check: Callable[[], UpdateInfo | None]) -> None:
        try:
            self._result = check()
        except Exception as exc:  # noqa: BLE001 - a check must never fail a run
            log_debug(f"Update check failed: {exc}")

    def result(self, wait: float = 0.0) -> UpdateInfo | None:
        """The newer version found, waiting at most *wait* seconds for it."""
        self._thread.join(wait)
        return None if self._thread.is_alive() else self._result
