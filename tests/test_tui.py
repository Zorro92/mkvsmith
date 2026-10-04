"""The full-screen UI (tui.py), driven headlessly with Textual's pilot."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets._footer import FooterKey

import cli
import models
import settings
import tui
from drives import OpticalDrive
from i18n import set_language
from models import RipCancelled, RipError, RuntimeState, Stream, StreamType, Title

AppTest = Callable[[Pilot[None], tui.MkvsmithApp], Awaitable[None]]


def complete_settings() -> settings.Settings:
    every = {spec.key for spec in settings.SETTING_SPECS}
    return settings.accept_advanced_defaults(
        replace(settings.Settings(), answered=every)
    )


@pytest.fixture(autouse=True)
def settings_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A complete settings file, so no first-run setup unless a test wants it."""
    path = tmp_path / "config.json"
    monkeypatch.setenv(settings.CONFIG_ENV, str(path))
    settings.save_settings(complete_settings(), path)
    return path


class OpenedApp(tui.MkvsmithApp):
    """Records the source chosen and stops there, instead of scanning it."""

    def __init__(self, *args: Any, scan: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.opened: list[Path] = []
        self._scan = scan

    def open_source(self, path: Path) -> None:
        self.opened.append(path)
        if self._scan:
            super().open_source(path)
        else:
            self.exit(None)


def run_app(
    body: AppTest,
    *,
    state: RuntimeState | None = None,
    drives: list[OpticalDrive] | None = None,
    labels: dict[Path, str] | None = None,
    ejected: list[Path] | None = None,
    scan: bool = False,
    size: tuple[int, int] = (60, 30),
    wait_for_menu: bool = True,
    **kwargs: object,
) -> OpenedApp:
    app = OpenedApp(
        state or RuntimeState(),
        find_drives=lambda: list(drives or []),
        read_label=lambda path: (labels or {}).get(path),
        eject_drive=lambda path: (ejected if ejected is not None else []).append(path),
        scan=scan,
        **kwargs,
    )

    async def drive() -> None:
        async with app.run_test(size=size) as pilot:
            if wait_for_menu:
                await wait_until(pilot, lambda: menu_ready(app))
            await body(pilot, app)

    asyncio.run(drive())
    return app


def menu_ready(app: tui.MkvsmithApp) -> bool:
    """The main menu is open, built and filled (not just pushed)."""
    for screen in app.screen_stack:
        if isinstance(screen, tui.MainMenu):
            found = screen.query("#menu")
            return bool(found) and found.first(OptionList).option_count > 0
    return False


async def wait_until(
    pilot: Pilot[None], ready: Callable[[], bool], timeout: float = 30.0
) -> None:
    """Pause until *ready* holds (workers report back between pauses)."""
    waited = 0.0
    while not ready():
        if waited > timeout:
            raise AssertionError("timed out waiting")
        await pilot.pause(0.02)
        waited += 0.02


def screen_name(app: tui.MkvsmithApp) -> str:
    return type(app.screen).__name__


async def open_menu_item(pilot: Pilot[None], index: int) -> None:
    """Choose main-menu line *index*; return once its screen is built."""
    app = pilot.app
    menu = app.screen
    await pilot.press(*["down"] * index, "enter")

    def settled() -> bool:
        screen = app.screen
        if screen is menu or not app.is_running:
            return not app.is_running
        lists = screen.query(OptionList)
        if lists:
            return lists.first().option_count > 0
        return bool(screen.query("Footer"))

    await wait_until(pilot, settled)


async def wait_for_workers(app: tui.MkvsmithApp, pilot: Pilot[None]) -> None:
    """Wait for the drive scan (not usable once a folder tree is showing)."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def option_texts(app: tui.MkvsmithApp, selector: str) -> list[str]:
    options = app.screen.query_one(selector, OptionList)
    return [
        str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
    ]


# =============================================================================
# Main menu and navigation
# =============================================================================


def test_quit_from_the_main_menu_returns_no_source() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        assert screen_name(app) == "MainMenu"
        await open_menu_item(pilot, 3)

    app = run_app(body)
    assert app.opened == []
    assert not app.is_running


def test_escape_goes_back_and_stops_at_the_main_menu() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 2)
        assert screen_name(app) == "AboutScreen"
        await pilot.press("escape")
        assert screen_name(app) == "MainMenu"
        await pilot.press("escape")
        assert screen_name(app) == "MainMenu"
        assert app.is_running

    run_app(body)


def click_footer_key(app: tui.MkvsmithApp, key: str) -> FooterKey:
    return next(k for k in app.screen.query(FooterKey) if k.key == key)


def test_footer_back_and_quit_are_clickable() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 1)
        await pilot.click(click_footer_key(app, "escape"))
        await pilot.pause()
        assert screen_name(app) == "MainMenu"
        await open_menu_item(pilot, 0)
        await pilot.click(click_footer_key(app, "q"))
        await pilot.pause()

    app = run_app(body)
    assert app.opened == [] and not app.is_running


def test_every_list_starts_with_a_line_highlighted() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        assert app.screen.query_one("#menu", OptionList).highlighted == 0
        await open_menu_item(pilot, 1)
        assert app.screen.query_one("#settings", OptionList).highlighted == 0
        await pilot.press("escape", "up")  # back on the menu, at "Rip a disc"
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        # "No optical drives found" can't be chosen; Browse is highlighted.
        sources = app.screen.query_one("#sources", OptionList)
        assert sources.highlighted == 1

    run_app(body)


def test_q_quits_from_any_screen() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await pilot.press("q")

    app = run_app(body)
    assert app.opened == [] and not app.is_running


# =============================================================================
# Source picker
# =============================================================================


DRIVES = [
    OpticalDrive(Path("/dev/sr0"), "ASUS DRW", True),
    OpticalDrive(Path("/dev/sr1"), "LG BD-RE", False),
]


def test_source_screen_lists_drives_with_their_disc_labels() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        texts = option_texts(app, "#sources")
        assert texts[0] == f"{Path('/dev/sr0')}  ASUS DRW\n  CATS_DONT_DANCE"
        assert texts[1] == f"{Path('/dev/sr1')}  LG BD-RE\n  No disc"
        assert texts[2].startswith("Browse")
        options = app.screen.query_one("#sources", OptionList)
        assert options.get_option_at_index(1).disabled

    run_app(body, drives=DRIVES, labels={Path("/dev/sr0"): "CATS_DONT_DANCE"})


def test_choosing_a_drive_returns_it() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        await pilot.press("enter")  # the first drive starts highlighted
        await pilot.pause()

    app = run_app(body, drives=DRIVES)
    assert app.opened == [Path("/dev/sr0")]


def test_no_drives_still_offers_browsing() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        assert option_texts(app, "#sources")[0] == "No optical drives found"
        await pilot.press("enter")  # Browse starts highlighted
        await pilot.pause()
        assert screen_name(app) == "BrowseScreen"

    run_app(body)


def test_eject_opens_the_highlighted_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui, "can_eject", lambda: True)
    ejected: list[Path] = []

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        await pilot.press("e")
        await wait_for_workers(app, pilot)

    run_app(body, drives=DRIVES, ejected=ejected)
    assert ejected == [Path("/dev/sr0")]


# =============================================================================
# Browsing for an ISO or a disc folder
# =============================================================================


def make_dvd_folder(root: Path) -> Path:
    disc = root / "Movie"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"")
    return disc


async def open_browser(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
    await open_menu_item(pilot, 0)
    await wait_for_workers(app, pilot)
    await pilot.press("end", "enter")  # Browse is the last line
    await wait_for_workers(app, pilot)
    assert screen_name(app) == "BrowseScreen"


def browser(app: tui.MkvsmithApp) -> tui.BrowseScreen:
    for screen in reversed(app.screen_stack):
        if isinstance(screen, tui.BrowseScreen):
            return screen
    raise AssertionError("no browser open")


async def choose_line(pilot: Pilot[None], app: tui.MkvsmithApp, text: str) -> None:
    """Highlight the browser line starting with *text* and press Enter."""
    texts = option_texts(app, "#entries")
    index = next(n for n, label in enumerate(texts) if label.startswith(text))
    app.screen.query_one("#entries", OptionList).highlighted = index
    await pilot.press("enter")
    await wait_for_workers(app, pilot)


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder to browse (resolved: macOS temp folders sit behind a symlink)."""
    root = tmp_path.resolve()
    make_dvd_folder(root)
    (root / "extras" / "inner").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "b.iso").write_bytes(b"")
    (root / "A.MKV").write_bytes(b"")
    (root / "notes.txt").write_text("x")
    monkeypatch.chdir(root)
    return root


def test_browser_lists_up_then_folders_then_discs_and_videos(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        assert option_texts(app, "#entries") == [
            "..",
            "extras/",
            "Movie/  (disc)",
            "A.MKV",
            "b.iso",
        ]
        # The first line below ".." is highlighted.
        assert app.screen.query_one("#entries", OptionList).highlighted == 1

    run_app(body)


def test_enter_opens_a_folder(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "extras/")
        assert browser(app).folder == library / "extras"
        assert option_texts(app, "#entries") == ["..", "inner/"]
        heading = app.screen.query_one("#folder", Static)
        assert str(heading.content) == str(library / "extras")

    assert run_app(body).opened == []


def test_clicking_a_folder_opens_it(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        entries = app.screen.query_one("#entries", OptionList)
        await pilot.click(entries, offset=(4, 1))  # line 2: "extras/"
        await wait_for_workers(app, pilot)
        assert browser(app).folder == library / "extras"

    run_app(body)


def test_dot_dot_goes_up(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "extras/")
        await choose_line(pilot, app, "..")
        assert browser(app).folder == library

    run_app(body)


@pytest.mark.parametrize("key", ["u", "backspace"])
def test_up_keys(library: Path, key: str) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "extras/")
        await pilot.press(key)
        await wait_for_workers(app, pilot)
        assert browser(app).folder == library

    run_app(body)


def test_disc_folders_open_and_offer_use_this_folder(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "Movie/")
        assert browser(app).folder == library / "Movie"
        assert option_texts(app, "#entries") == [
            "..",
            "Use this folder (DVD)",
            "VIDEO_TS/  (disc)",
        ]
        assert app.screen.query_one("#entries", OptionList).highlighted == 1
        await pilot.press("enter")
        await pilot.pause()

    assert run_app(body).opened == [library / "Movie"]


def test_ordinary_folders_offer_no_use_line(library: Path) -> None:
    # Regression: the general detection searches every subfolder, so an
    # ordinary folder with a .m2ts somewhere inside was taken for a disc.
    stream = library / "extras" / "inner" / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00001.m2ts").write_bytes(b"")

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "extras/")
        assert not any(t.startswith("Use this") for t in option_texts(app, "#entries"))

    run_app(body)


def test_a_folder_of_loose_streams_can_be_used(library: Path) -> None:
    stream = library / "extras" / "STREAM"
    stream.mkdir()
    (stream / "00001.m2ts").write_bytes(b"")

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "extras/")
        await choose_line(pilot, app, "STREAM/")
        assert option_texts(app, "#entries")[:3] == [
            "..",
            "Use this folder",
            "00001.m2ts",
        ]
        await choose_line(pilot, app, "Use this folder")

    assert run_app(body).opened == [library / "extras" / "STREAM"]


def test_choosing_an_iso_uses_it(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await choose_line(pilot, app, "b.iso")

    assert run_app(body).opened == [library / "b.iso"]


async def type_path(pilot: Pilot[None], app: tui.MkvsmithApp, path: str) -> None:
    await pilot.press("slash")
    await pilot.pause()
    assert screen_name(app) == "TextDialog"
    field = app.screen.query_one("#value", Input)
    field.value = path
    await pilot.press("enter")
    await wait_for_workers(app, pilot)


def test_typing_a_folder_opens_it(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await type_path(pilot, app, str(library / "extras" / "inner"))
        assert browser(app).folder == library / "extras" / "inner"

    assert run_app(body).opened == []


def test_typing_an_iso_uses_it(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await type_path(pilot, app, str(library / "b.iso"))

    assert run_app(body).opened == [library / "b.iso"]


def test_typing_something_else_is_refused(library: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await type_path(pilot, app, str(library / "notes.txt"))
        assert app.is_running and screen_name(app) == "BrowseScreen"
        await type_path(pilot, app, str(library / "missing"))
        assert app.is_running and screen_name(app) == "BrowseScreen"

    assert run_app(body).opened == []


def test_unreadable_folder_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_folder: Path) -> list[tuple[Path, bool]]:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(tui, "list_folder", refuse)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        texts = option_texts(app, "#entries")
        assert texts[-1] == "Can't read this folder: Permission denied"

    run_app(body)


def test_quitting_while_a_folder_is_loading_does_not_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: Textual's DirectoryTree hung the app's shutdown when it was
    # still loading a folder.
    real_list_folder = tui.list_folder
    # Holds the folder read until the quit has been asked for (a sleep is
    # timing-dependent: too short on a slow, emulated CI runner).
    loading = threading.Event()

    def slow(folder: Path) -> list[tuple[Path, bool]]:
        loading.wait(10)
        return real_list_folder(folder)

    monkeypatch.setattr(tui, "list_folder", slow)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 0)
        await wait_for_workers(app, pilot)
        await pilot.press("end", "enter")
        await pilot.pause()
        assert option_texts(app, "#entries") == ["Loading..."]
        await pilot.press("q")
        loading.set()

    try:
        app = run_app(body)
    finally:
        loading.set()
    assert not app.is_running and app.opened == []


# =============================================================================
# Settings
# =============================================================================


def saved(path: Path) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], json.loads(path.read_text()))


def setting_index(key: str) -> int:
    return [spec.key for spec in settings.SETTING_SPECS].index(key)


async def open_setting(pilot: Pilot[None], app: tui.MkvsmithApp, key: str) -> None:
    await open_menu_item(pilot, 1)
    assert screen_name(app) == "SettingsScreen"
    options = app.screen.query_one("#settings", OptionList)
    options.highlighted = setting_index(key)
    await pilot.press("enter")
    await pilot.pause()


def test_settings_screen_lists_every_setting() -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 1)
        texts = option_texts(app, "#settings")
        assert len(texts) == len(settings.SETTING_SPECS)
        assert "tracks.all_audio = yes" in texts[setting_index("tracks.all_audio")]

    run_app(body)


def test_choosing_a_value_saves_it(settings_file: Path) -> None:
    state = RuntimeState()

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "tracks.all_audio")
        assert screen_name(app) == "ChoiceDialog"
        # yes is highlighted (the current value); pick no.
        await pilot.press("down", "enter")
        await pilot.pause()
        assert screen_name(app) == "SettingsScreen"
        text = option_texts(app, "#settings")[setting_index("tracks.all_audio")]
        assert text.endswith("tracks.all_audio = no")

    run_app(body, state=state)
    assert saved(settings_file)["tracks"]["all_audio"] is False
    assert state.settings.all_audio is False


def test_cancelling_a_choice_saves_nothing(settings_file: Path) -> None:
    before = settings_file.read_text()

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "output.overwrite")
        await pilot.press("escape")
        await pilot.pause()
        assert screen_name(app) == "SettingsScreen"

    run_app(body)
    assert settings_file.read_text() == before


def test_typed_value_is_checked_before_saving(settings_file: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "scan.min_duration")
        assert screen_name(app) == "TextDialog"
        field = app.screen.query_one("#value", Input)
        field.value = "soon"
        await pilot.press("enter")
        await pilot.pause()
        assert screen_name(app) == "TextDialog"
        assert "Invalid value" in str(app.screen.query_one("#error", Label).content)
        field.value = "120"
        await pilot.press("enter")
        await pilot.pause()
        assert screen_name(app) == "SettingsScreen"

    run_app(body)
    assert saved(settings_file)["scan"]["min_duration"] == 120


def test_secret_settings_start_empty_and_hidden() -> None:
    state = RuntimeState()
    state.settings = settings.set_setting(state.settings, "tmdb.api_key", "abc")

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "tmdb.api_key")
        field = app.screen.query_one("#value", Input)
        assert field.value == "" and field.password

    run_app(body, state=state)


def test_default_key_resets_the_highlighted_setting(settings_file: Path) -> None:
    settings.save_settings(
        settings.set_setting(complete_settings(), "scan.min_duration", 5)
    )

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_menu_item(pilot, 1)
        options = app.screen.query_one("#settings", OptionList)
        options.highlighted = setting_index("scan.min_duration")
        await pilot.press("d")
        await pilot.pause()

    run_app(body)
    assert (
        saved(settings_file)["scan"]["min_duration"] == settings.Settings().min_duration
    )


# =============================================================================
# First-run setup
# =============================================================================


def test_first_run_setup_asks_then_opens_the_menu(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    settings_file.unlink()
    state = RuntimeState()
    reapplied: list[bool] = []
    questions: list[str] = []

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        answered: list[object] = []

        def next_question() -> bool:
            screen = app.screen
            return (
                isinstance(screen, tui.ChoiceDialog | tui.TextDialog)
                and screen not in answered
                and bool(screen.query("#choices, #actions"))
            )

        def menu_open() -> bool:
            return any(isinstance(x, tui.MainMenu) for x in app.screen_stack)

        while not menu_open():
            await wait_until(pilot, lambda: next_question() or menu_open())
            if menu_open():
                break
            screen = app.screen
            answered.append(screen)
            labels = [str(label.content) for label in screen.query(Label)]
            questions.append(labels[1])
            assert labels[0] == "First-time setup"
            await pilot.press("enter")  # the suggested answer
        assert screen_name(app) == "MainMenu"

    run_app(
        body, state=state, wait_for_menu=False, reapply=lambda: reapplied.append(True)
    )
    assert questions[0] == "Interface language"
    assert settings.missing_settings(settings.load_settings().settings) == []
    assert reapplied == [True]
    assert settings.missing_settings(state.settings) == []


# =============================================================================
# Scanning, titles, tracks
# =============================================================================


def make_title(index: int, duration: float = 6000.0, **attributes: object) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"title-{index}.m2ts"),
        name=f"Movie - Title {index}",
        duration_seconds=duration,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="h264"),
        Stream(index=0, stream_type=StreamType.AUDIO, language="eng"),
        Stream(index=0, stream_type=StreamType.AUDIO, language="fra", type_index=1),
        Stream(index=0, stream_type=StreamType.SUBTITLE, language="eng"),
    ]
    for name, value in attributes.items():
        setattr(title, name, value)
    return title


class FakeCreator:
    """Stands in for MKVCreator in the rip screen; records what it rips."""

    ripped: list[tuple[int, list[Stream] | None]] = []
    gate: threading.Event | None = None
    fail: set[int] = set()
    ask_overwrite: bool = False

    def __init__(self, out: Path, *_args: object, **_kwargs: object) -> None:
        self.out = out
        self.on_progress: Callable[[str, int], None] | None = None
        self.cancelled: Callable[[], bool] | None = None
        self.prompts = models.UserPrompts()

    def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path:
        if self.on_progress is not None:
            self.on_progress("x.mkv", 40)
        if FakeCreator.gate is not None:
            FakeCreator.gate.wait(10)
        if self.cancelled is not None and self.cancelled():
            raise RipCancelled(title)
        if FakeCreator.ask_overwrite and not self.prompts.confirm(
            "'x.mkv' already exists. Overwrite? [y/N]:"
        ):
            raise RipError(message="Output exists, not overwriting: x.mkv")
        if title.index in FakeCreator.fail:
            raise RipError(message=f"boom {title.index}")
        FakeCreator.ripped.append((title.index, streams))
        out = self.out / f"{title.index}.mkv"
        out.write_bytes(b"x" * 2048)
        return out


@pytest.fixture
def creator(monkeypatch: pytest.MonkeyPatch) -> type[FakeCreator]:
    FakeCreator.ripped = []
    FakeCreator.gate = None
    FakeCreator.fail = set()
    FakeCreator.ask_overwrite = False
    monkeypatch.setattr(tui, "MKVCreator", FakeCreator)
    return FakeCreator


def ripping_state(tmp_path: Path) -> RuntimeState:
    state = RuntimeState()
    state.config.output_dir = tmp_path
    state.config.preferred_languages = ["eng"]
    state.config.keep_all_audio = False
    return state


def titles_app(
    body: AppTest,
    titles: list[Title],
    tmp_path: Path,
    state: RuntimeState | None = None,
) -> OpenedApp:
    return run_app(
        body,
        state=state or ripping_state(tmp_path),
        titles=titles,
        disc_metadata=models.DiscMetadata(name="Movie"),
    )


async def dialog(pilot: Pilot[None], app: tui.MkvsmithApp, kind: type) -> None:
    """Wait until a *kind* dialog is open and built."""
    await wait_until(
        pilot,
        lambda: (
            isinstance(app.screen, kind)
            and bool(app.screen.query("#choices, #actions"))
        ),
    )


def has_lines(app: tui.MkvsmithApp, selector: str, id_: str | None = None) -> bool:
    """Whether the screen's *selector* list is built and filled (with *id_*)."""
    found = app.screen.query(selector)
    if not found:
        return False
    options = found.first(OptionList)
    if id_ is None:
        return options.option_count > 0
    try:
        options.get_option_index(id_)
    except Exception:  # noqa: BLE001 - OptionDoesNotExist: not filled yet
        return False
    return True


async def at_titles(pilot: Pilot[None], app: tui.MkvsmithApp) -> tui.TitlesScreen:
    await wait_until(
        pilot,
        lambda: isinstance(app.screen, tui.TitlesScreen) and has_lines(app, "#titles"),
    )
    screen = app.screen
    assert isinstance(screen, tui.TitlesScreen)
    return screen


async def choose(
    pilot: Pilot[None], app: tui.MkvsmithApp, selector: str, id_: str
) -> None:
    await wait_until(pilot, lambda: has_lines(app, selector, id_))
    options = app.screen.query_one(selector, OptionList)
    options.highlighted = options.get_option_index(id_)
    await pilot.press("enter")
    await pilot.pause()


async def rip_done(pilot: Pilot[None], app: tui.MkvsmithApp) -> tui.RipScreen:
    await wait_until(
        pilot,
        lambda: isinstance(app.screen, tui.RipScreen) and not app.screen.running,
    )
    screen = app.screen
    assert isinstance(screen, tui.RipScreen)
    return screen


def test_scanning_opens_the_title_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    titles = [make_title(0), make_title(1, 600.0)]

    def fake_scan(source: Path, state: RuntimeState) -> tuple[list[Title], object]:
        models.log_info("Source type: dvd")
        return titles, models.DiscMetadata(name="Movie")

    monkeypatch.setattr(tui, "scan_source", fake_scan)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        assert screen.titles is titles
        texts = option_texts(app, "#titles")
        assert texts[0] == "Rip main feature: Movie - Title 0"
        assert any(t.startswith("[ ]  0  Title 0 ★") for t in texts)
        # Back from the list goes to the menu, not to the finished scan.
        await pilot.press("escape")
        assert screen_name(app) == "MainMenu"

    run_app(body, source=Path("/discs/movie.iso"))


def test_scan_failure_is_shown_and_esc_goes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_titles(_source: Path, _state: RuntimeState) -> tuple[list[Title], None]:
        return [], None

    monkeypatch.setattr(tui, "scan_source", no_titles)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await wait_until(
            pilot,
            lambda: "No titles" in str(app.screen.query_one("#status", Static).content),
        )
        await pilot.press("escape")
        assert screen_name(app) == "MainMenu"

    run_app(body, source=Path("/discs/empty.iso"))


def test_scan_asks_the_per_disc_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packed_episodes import PackedSegment

    packed = make_title(0, 2800.0, playlist_name="00000", clip_durations=[2800.0])
    packed.packed_segments = [
        PackedSegment(0.0, 1400.0, 1),
        PackedSegment(1400.0, 2800.0, 2),
    ]
    monkeypatch.setattr(tui, "scan_source", lambda _s, _st: ([packed], None))
    state = RuntimeState()
    state.config.ask_split_episodes = True

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await dialog(pilot, app, tui.ConfirmDialog)
        assert "holds 2 episodes" in str(app.screen.query(Label).first().content)
        await pilot.press("enter")  # Yes
        screen = await at_titles(pilot, app)
        assert len(screen.titles) == 2
        assert all(title.is_episode for title in screen.titles)

    run_app(body, state=state, source=Path("/discs/show.iso"))


def test_marking_titles_offers_a_batch_rip(
    tmp_path: Path, creator: type[FakeCreator]
) -> None:
    titles = [make_title(0), make_title(1, 5000.0), make_title(2, 4000.0)]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        options = app.screen.query_one("#titles", OptionList)
        for index in (2, 0):
            options.highlighted = options.get_option_index(f"title-{index}")
            await pilot.press("space")
        assert screen.marked == {0, 2}
        texts = option_texts(app, "#titles")
        assert texts[:2] == [
            "Rip marked titles (2)",
            "Combine marked titles into one multi-edition MKV",
        ]
        await choose(pilot, app, "#titles", "rip-marked")
        rip = await rip_done(pilot, app)
        assert "Done: 2 ok, 0 failed" in str(rip.query_one("#status", Static).content)

    titles_app(body, titles, tmp_path)
    # In disc order, with the default tracks.
    assert creator.ripped == [(0, None), (2, None)]


def test_rip_main_feature_and_rip_all(
    tmp_path: Path, creator: type[FakeCreator]
) -> None:
    titles = [make_title(0, 5000.0), make_title(1, 7000.0), make_title(2, 10.0)]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-main")
        await rip_done(pilot, app)
        await pilot.press("escape")
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-all")
        await rip_done(pilot, app)

    titles_app(body, titles, tmp_path)
    # "All listed" leaves out the hidden 10-second title.
    assert [index for index, _ in creator.ripped] == [1, 0, 1]


def test_hidden_titles_can_be_shown(tmp_path: Path) -> None:
    titles = [make_title(0), make_title(1, 10.0)]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        assert option_texts(app, "#titles")[-1] == "Show 1 hidden titles"
        await choose(pilot, app, "#titles", "toggle-hidden")
        texts = option_texts(app, "#titles")
        assert any(t.startswith("[ ]  1") for t in texts)
        assert texts[-1] == "Hide short and duplicate titles"

    titles_app(body, titles, tmp_path)


def test_choosing_tracks_for_a_title(
    tmp_path: Path, creator: type[FakeCreator]
) -> None:
    title = make_title(0)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "title-0")
        assert screen_name(app) == "TitleScreen"
        texts = option_texts(app, "#tracks")
        # Defaults: video, English audio and subtitles (French audio off).
        boxes = [t[:3] for t in texts if t.startswith("[")][1:]
        assert boxes == ["[x]", "[x]", "[ ]", "[x]"]
        await choose(pilot, app, "#tracks", "track-2")  # French audio on
        await choose(pilot, app, "#tracks", "track-3")  # subtitles off
        assert "Reset tracks to the defaults" in option_texts(app, "#tracks")
        await pilot.press("escape")
        await at_titles(pilot, app)
        assert any("custom tracks" in t for t in option_texts(app, "#titles"))
        await choose(pilot, app, "#titles", "title-0")
        await choose(pilot, app, "#tracks", "rip")
        await rip_done(pilot, app)

    titles_app(body, [title], tmp_path)
    ((index, streams),) = creator.ripped
    assert index == 0 and streams is not None
    assert [s.display_id for s in streams] == ["v:0", "a:0", "a:1"]


def test_track_defaults_can_be_restored(tmp_path: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "title-0")
        await choose(pilot, app, "#tracks", "track-3")
        assert 0 in screen.streams
        await choose(pilot, app, "#tracks", "defaults")
        assert 0 not in screen.streams
        assert "Reset tracks to the defaults" not in option_texts(app, "#tracks")

    titles_app(body, [make_title(0)], tmp_path)


def test_marking_from_the_title_screen(tmp_path: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "title-0")
        await choose(pilot, app, "#tracks", "mark")
        assert screen.marked == {0}
        assert option_texts(app, "#tracks")[1].startswith("[x]")

    titles_app(body, [make_title(0)], tmp_path)


def test_combining_editions_asks_their_names(
    tmp_path: Path, creator: type[FakeCreator], monkeypatch: pytest.MonkeyPatch
) -> None:
    titles = [make_title(0), make_title(1, 5800.0)]
    combined = make_title(9)
    built: list[tuple[list[int], list[str] | None]] = []

    def fake_prepare(
        all_titles: list[Title], positions: list[int], names: list[str] | None = None
    ) -> Title:
        built.append((positions, names))
        return combined

    monkeypatch.setattr(tui, "prepare_multi_edition", fake_prepare)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        screen.marked = {0, 1}
        screen.refill()
        await choose(pilot, app, "#titles", "combine-marked")
        for name in ("Theatrical", ""):
            await dialog(pilot, app, tui.TextDialog)
            app.screen.query_one("#value", Input).value = name
            await pilot.press("enter")
            await pilot.pause()
        await rip_done(pilot, app)

    titles_app(body, titles, tmp_path)
    assert built == [([0, 1], ["Theatrical", "Edition 2"])]
    assert creator.ripped == [(9, None)]


def test_splitting_from_the_title_list(tmp_path: Path) -> None:
    from packed_episodes import PackedSegment

    packed = make_title(0, 2800.0, playlist_name="00000", clip_durations=[2800.0])
    packed.packed_segments = [
        PackedSegment(0.0, 1400.0, 1),
        PackedSegment(1400.0, 2800.0, 2),
    ]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "split-0")
        assert len(screen.titles) == 2
        assert option_texts(app, "#titles")[0] == "Rip all episodes (2)"

    titles_app(body, [packed], tmp_path)


# =============================================================================
# Ripping
# =============================================================================


def test_rip_asks_for_the_output_folder_first(
    tmp_path: Path, creator: type[FakeCreator]
) -> None:
    state = ripping_state(tmp_path)
    state.config.ask_output_dir = True
    target = tmp_path / "rips"

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-main")
        await dialog(pilot, app, tui.TextDialog)
        app.screen.query_one("#value", Input).value = str(target)
        await pilot.press("enter")
        rip = await rip_done(pilot, app)
        texts = option_texts(app, "#jobs")
        assert texts[0].endswith("Done: 0.mkv (2.0 KB)")
        assert texts[-1] == "Back"
        await choose(pilot, app, "#jobs", "back")
        assert screen_name(app) == "TitlesScreen"
        assert rip.result is not None and rip.result.ok == 1

    titles_app(body, [make_title(0)], tmp_path, state)
    assert (target / "0.mkv").exists()
    assert state.config.ask_output_dir is False


def test_failed_rips_are_reported(tmp_path: Path, creator: type[FakeCreator]) -> None:
    creator.fail = {0}

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-main")
        await rip_done(pilot, app)
        assert option_texts(app, "#jobs")[0].endswith("Failed: boom 0")
        status = str(app.screen.query_one("#status", Static).content)
        assert status == "Done: 0 ok, 1 failed"

    titles_app(body, [make_title(0)], tmp_path)


def test_questions_from_the_rip_become_dialogs(
    tmp_path: Path, creator: type[FakeCreator]
) -> None:
    creator.ask_overwrite = True

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-main")
        await dialog(pilot, app, tui.ConfirmDialog)
        question = str(app.screen.query(Label).first().content)
        assert question == "'x.mkv' already exists. Overwrite?"
        await pilot.press("enter")  # Yes
        await rip_done(pilot, app)

    titles_app(body, [make_title(0)], tmp_path)
    assert creator.ripped == [(0, None)]


def test_esc_while_ripping_asks_then_stops(
    tmp_path: Path, creator: type[FakeCreator], monkeypatch: pytest.MonkeyPatch
) -> None:
    creator.gate = threading.Event()
    state = ripping_state(tmp_path)
    killed: list[bool] = []

    def kill_muxers() -> None:
        killed.append(True)
        assert creator.gate is not None
        creator.gate.set()

    monkeypatch.setattr(state.active_processes, "kill_muxers", kill_muxers)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        screen.marked = {0, 1}
        screen.refill()
        await choose(pilot, app, "#titles", "rip-marked")
        await wait_until(
            pilot, lambda: "Muxing... 40%" in option_texts(app, "#jobs")[0]
        )
        await pilot.press("escape")
        await dialog(pilot, app, tui.ConfirmDialog)
        await pilot.press("enter")  # Yes, stop
        rip = await rip_done(pilot, app)
        status = str(rip.query_one("#status", Static).content)
        assert status == "Stopped · Done: 0 ok, 0 failed"
        jobs = option_texts(app, "#jobs")
        assert jobs[0].endswith("Stopped") and jobs[1].endswith("Skipped")

    titles_app(body, [make_title(0), make_title(1)], tmp_path, state)
    assert killed == [True]
    assert creator.ripped == []


@pytest.mark.parametrize("key", ["q", "ctrl+q"])
def test_quitting_while_ripping_asks_then_stops_and_quits(
    tmp_path: Path,
    creator: type[FakeCreator],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    creator.gate = threading.Event()
    state = ripping_state(tmp_path)
    monkeypatch.setattr(
        state.active_processes,
        "kill_muxers",
        lambda: creator.gate.set() if creator.gate else None,
    )

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "rip-main")
        await wait_until(pilot, lambda: app.running_rip() is not None)
        await pilot.press(key)
        await dialog(pilot, app, tui.ConfirmDialog)
        assert app.is_running
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app.is_running)

    app = titles_app(body, [make_title(0)], tmp_path, state)
    assert not app.is_running
    assert creator.ripped == []


# =============================================================================
# Language, settings and wiring
# =============================================================================


def test_switching_language_relabels_screens_and_keys(settings_file: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "ui.language")
        await choose(pilot, app, "#choices", "choice-2")  # Español
        await wait_until(pilot, lambda: isinstance(app.screen, tui.SettingsScreen))
        heading = app.screen.query(Static).first(Static)
        assert "Ajustes guardados" in str(
            app.screen.query(".heading").first(Static).content
        )
        keys = {k.key: k.description for k in app.screen.query(FooterKey)}
        assert keys["escape"] == "Atrás" and keys["q"] == "Salir"
        del heading

    try:
        run_app(body)
    finally:
        set_language("en")
        tui.relabel_keys()
    assert saved(settings_file)["ui"]["language"] == "es"


def test_saved_settings_apply_to_this_run(settings_file: Path) -> None:
    reapplied: list[bool] = []

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "tracks.all_audio")
        await pilot.press("down", "enter")

    run_app(body, reapply=lambda: reapplied.append(True))
    assert reapplied == [True]


def test_reapply_keeps_flags_and_the_chosen_output_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = RuntimeState()
    state.settings = settings.set_setting(
        complete_settings(), "tracks.all_audio", False
    )
    state.config.output_dir = tmp_path
    state.config.ask_output_dir = False
    monkeypatch.setattr("sys.argv", ["mkvsmith"])

    cli._reapply_options(state)

    assert state.config.keep_all_audio is False
    assert state.config.output_dir == tmp_path  # chosen this session, kept

    monkeypatch.setattr("sys.argv", ["mkvsmith", "--all-audio"])
    cli._reapply_options(state)
    assert state.config.keep_all_audio is True


def test_run_tui_routes_logs_and_prompts_and_restores_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RuntimeState()
    original_prompts = state.prompts
    seen: list[tuple[object, object]] = []

    def fake_run(app: tui.MkvsmithApp) -> None:
        seen.append((state.logger.sink, state.prompts))

    monkeypatch.setattr(tui.MkvsmithApp, "run", fake_run)

    tui.run_tui(state, Path("/dev/sr0"))

    ((sink, prompts),) = seen
    assert sink is not None and prompts is not original_prompts
    assert state.logger.sink is None and state.prompts is original_prompts


def test_dialog_answers_default_once_the_app_has_closed() -> None:
    app = tui.MkvsmithApp(RuntimeState())
    app.bridge.close()

    prompts = app.bridge.prompts()
    assert prompts.confirm("Overwrite? [y/N]:") is False
    assert prompts.text("Folder", "/x") == "/x"
    assert prompts.choose("Pick", ["a", "b"], 1) == 1


# =============================================================================
# Layout: long lists scroll instead of running off the screen
# =============================================================================


def laid_out(app: tui.MkvsmithApp, selector: str) -> bool:
    """Whether *selector*'s list has been sized and its lines measured.

    On a slow machine (the Windows CI runner) that can lag the list being
    filled by a few frames.
    """
    found = app.screen.query(selector)
    if not found:
        return False
    options = found.first(OptionList)
    return options.size.height > 0 and options.virtual_size.height > 0


async def list_fits(pilot: Pilot[None], app: tui.MkvsmithApp, selector: str) -> None:
    """*selector*'s list ends on screen and scrolls to show every line."""
    await wait_until(pilot, lambda: laid_out(app, selector))
    options = app.screen.query_one(selector, OptionList)
    footer = app.screen.query_one("Footer")
    assert options.region.bottom <= footer.region.y
    assert options.max_scroll_y > 0  # more lines than fit: it scrolls
    options.highlighted = options.option_count - 1
    options.scroll_to_highlight()


async def last_line_visible(
    pilot: Pilot[None], app: tui.MkvsmithApp, selector: str
) -> None:
    await list_fits(pilot, app, selector)
    await pilot.pause()
    options = app.screen.query_one(selector, OptionList)
    assert options.scroll_y == options.max_scroll_y


def test_a_long_title_list_scrolls(tmp_path: Path) -> None:
    titles = [make_title(n, 6000.0 - n) for n in range(20)]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await last_line_visible(pilot, app, "#titles")

    run_app(
        body,
        state=ripping_state(tmp_path),
        titles=titles,
        disc_metadata=models.DiscMetadata(name="Movie"),
        size=(50, 20),
    )


def test_long_settings_and_track_lists_scroll(tmp_path: Path) -> None:
    title = make_title(0)
    title.streams += [
        Stream(index=0, stream_type=StreamType.AUDIO, language="eng", type_index=n)
        for n in range(2, 12)
    ]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "title-0")
        await last_line_visible(pilot, app, "#tracks")
        app.push_screen(tui.SettingsScreen())
        await wait_until(pilot, lambda: bool(app.screen.query("#settings")))
        await pilot.pause()
        await last_line_visible(pilot, app, "#settings")

    run_app(
        body,
        state=ripping_state(tmp_path),
        titles=[title],
        disc_metadata=models.DiscMetadata(name="Movie"),
        size=(50, 20),
    )


def test_a_long_folder_scrolls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for n in range(30):
        (tmp_path / f"folder {n:02}").mkdir()
    monkeypatch.chdir(tmp_path)

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_browser(pilot, app)
        await last_line_visible(pilot, app, "#entries")

    run_app(body, size=(50, 20))


def screen_row(options: OptionList, option_id: str) -> int:
    """Where line *option_id* sits on screen, counted from the list's top."""
    index = options.get_option_index(option_id)
    return options._index_to_line[index] - round(options.scroll_y)


def test_space_marks_in_place_and_moves_to_the_next_title(tmp_path: Path) -> None:
    titles = [make_title(n, 6000.0 - n) for n in range(20)]

    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        screen = await at_titles(pilot, app)
        await wait_until(pilot, lambda: laid_out(app, "#titles"))
        options = app.screen.query_one("#titles", OptionList)
        start = options.get_option_index("title-8")
        options.highlighted = start
        await pilot.pause()
        line = options._index_to_line[start]
        options.scroll_to(y=line - 3, animate=False, immediate=True)
        await pilot.pause()
        row = screen_row(options, "title-8")
        assert 0 < row < options.size.height - 2

        # The first mark adds "Rip marked titles" lines above the titles.
        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        assert screen.marked == {8}
        assert options.highlighted_option is not None
        assert options.highlighted_option.id == "title-9"
        assert screen_row(options, "title-8") == row  # didn't snap

        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        assert screen.marked == {8, 9}
        assert options.highlighted_option.id == "title-10"
        assert screen_row(options, "title-8") == row

    run_app(
        body,
        state=ripping_state(tmp_path),
        titles=titles,
        disc_metadata=models.DiscMetadata(name="Movie"),
        size=(50, 20),
    )


def test_space_toggles_a_track_and_moves_to_the_next(tmp_path: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await at_titles(pilot, app)
        await choose(pilot, app, "#titles", "title-0")
        options = app.screen.query_one("#tracks", OptionList)
        options.highlighted = options.get_option_index("track-1")
        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        # "Reset tracks" appeared above; the toggled track kept its line.
        assert "Reset tracks to the defaults" in option_texts(app, "#tracks")
        assert options.highlighted_option is not None
        assert options.highlighted_option.id == "track-2"
        text = str(options.get_option("track-1").prompt)
        assert text.startswith("[ ]")

    titles_app(body, [make_title(0)], tmp_path)


def test_editing_a_setting_keeps_its_place(settings_file: Path) -> None:
    async def body(pilot: Pilot[None], app: tui.MkvsmithApp) -> None:
        await open_setting(pilot, app, "scan.min_duration")
        await dialog(pilot, app, tui.TextDialog)
        app.screen.query_one("#value", Input).value = "90"
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, tui.SettingsScreen))
        await pilot.pause()
        settings_list = app.screen.query_one("#settings", OptionList)
        assert settings_list.highlighted_option is not None
        assert settings_list.highlighted_option.id == "scan.min_duration"

    run_app(body, size=(50, 20))
