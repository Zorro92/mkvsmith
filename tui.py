"""Full-screen terminal UI (Textual).

The interactive front end: first-run setup, a main menu, a source picker
(optical drives or a browsed ISO / disc folder), scanning, the title list, a
title's tracks, the rip queue, the settings screen, and an about screen.

Every screen looks the same: plain lines, the highlighted one in orange,
moved with the arrows or the mouse wheel and chosen with Enter or a click.
Esc goes back and q quits; the key footer at the bottom is clickable too.

Like ``cli.py``, this module owns only presentation. Scanning, the per-disc
questions, tagging and ripping go through ``session.py``; questions those
helpers ask (from a worker thread) arrive through ``UserPrompts`` hooks that
this module answers with dialogs (see ``_DialogBridge``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from cli import (
    _list_name_base,
    _non_video_stream_line,
    _title_list_name,
    _title_source_id,
    _video_stream_line,
)
from disc_reader import SourceType, detect_source_type
from drives import OpticalDrive, can_eject, disc_label, eject, list_optical_drives
from i18n import available_languages, get_language, language_name, set_language, tr
from mkv import MKVCreator
from models import (
    MKVSMITH_VERSION,
    DiscMetadata,
    RipError,
    RuntimeState,
    Stream,
    StreamType,
    Title,
    UserPrompts,
    log_error,
)
from scan import _detect_edition_groups, _get_notable_titles, pick_main_feature
from session import (
    RipBatchResult,
    RipCallbacks,
    RipJob,
    RipOutcome,
    TaggingChoice,
    ask_closed_captions,
    ask_edition_names,
    ask_output_dir,
    main_feature_titles,
    packed_split_offers,
    prepare_multi_edition,
    run_rip_jobs,
    scan_source,
    split_packed_episodes,
    track_plan,
)
from settings import (
    CHOICE_LABELS,
    SETTING_SPECS,
    Settings,
    SettingSpec,
    SettingsFileUnreadable,
    complete_settings,
    format_value,
    load_settings,
    save_settings,
    settings_path,
    update_saved_setting,
)

if TYPE_CHECKING:
    from textual.dom import DOMNode

T = TypeVar("T")

# =============================================================================
# Sources: what the browser lists and what it can rip
# =============================================================================

# Files the browser lists besides folders.
_SOURCE_FILE_SUFFIXES = frozenset({".iso", ".img", ".m2ts", ".vob", ".evo", ".mkv"})

# Loose disc streams: a folder holding these directly (e.g. BDMV/STREAM or a
# copied VIDEO_TS) can be ripped as it is.
_STREAM_SUFFIXES = frozenset({".m2ts", ".vob", ".evo"})


def _holds_streams(folder: Path) -> bool:
    try:
        with os.scandir(folder) as entries:
            return any(
                Path(entry.name).suffix.lower() in _STREAM_SUFFIXES for entry in entries
            )
    except OSError:
        return False


def disc_folder_kind(path: Path) -> str | None:
    """What a folder that is a disc in itself holds, or None.

    "DVD" (VIDEO_TS inside, or the VIDEO_TS folder itself), "Blu-ray"
    (BDMV), "HD DVD" (HVDVD_TS). Cheap enough to check for every folder in a
    listing: it doesn't look at the files.
    """
    try:
        if path.name.upper() == "VIDEO_TS" or (path / "VIDEO_TS").is_dir():
            return "DVD"
        if (path / "BDMV").is_dir() or (path / "bdmv").is_dir():
            return "Blu-ray"
        if (path / "HVDVD_TS").is_dir():
            return "HD DVD"
    except OSError:
        pass
    return None


def is_disc_folder(path: Path) -> bool:
    return disc_folder_kind(path) is not None


def is_usable_source(path: Path) -> bool:
    """Whether the browser can rip *path* as it is.

    Folders are judged by what they hold directly: the general source
    detection searches every subfolder for video files, which would take an
    ordinary folder (say, Downloads) for a disc and could take minutes.
    """
    if path.is_dir():
        return is_disc_folder(path) or _holds_streams(path)
    return detect_source_type(path) != SourceType.UNKNOWN


def list_folder(folder: Path) -> list[tuple[Path, bool]]:
    """*folder*'s subfolders, then the files mkvsmith can open: (path, is_dir).

    Hidden entries are left out; names sort case-insensitively. Raises
    OSError when the folder can't be read.
    """
    folders: list[Path] = []
    files: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                folders.append(Path(entry.path))
            elif Path(entry.name).suffix.lower() in _SOURCE_FILE_SUFFIXES:
                files.append(Path(entry.path))
    return [
        *((path, True) for path in sorted(folders, key=lambda p: p.name.lower())),
        *((path, False) for path in sorted(files, key=lambda p: p.name.lower())),
    ]


# =============================================================================
# Key labels (translated, and relabelled when the language changes)
# =============================================================================

# Textual reads a class's key bindings once, when the class is defined, and
# the footer shows their labels. The labels are translated, so each class
# that has keys registers how to build them, and relabel_keys() rebuilds
# them all after the interface language changes.
_KEYED: list[tuple[type[DOMNode], Callable[[], list[BindingType]]]] = []


def _set_keys(cls: type[DOMNode], make: Callable[[], list[BindingType]]) -> None:
    cls.BINDINGS = make()
    cls._merged_bindings = cls._merge_bindings()


def _keys(make: Callable[[], list[BindingType]]) -> Callable[[type[T]], type[T]]:
    """Class decorator: key bindings built by *make* (see relabel_keys)."""

    def apply(cls: type[T]) -> type[T]:
        dom_cls: Any = cls
        _KEYED.append((dom_cls, make))
        _set_keys(dom_cls, make)
        return cls

    return apply


def _with_subclasses(cls: type[DOMNode]) -> list[type[DOMNode]]:
    found: list[type[DOMNode]] = [cls]
    for sub in cls.__subclasses__():
        found.extend(_with_subclasses(sub))
    return found


def relabel_keys() -> None:
    """Translate every key label again (for screens created from now on).

    Subclasses cache the keys they inherit too, so theirs are merged again.
    """
    for cls, make in _KEYED:
        cls.BINDINGS = make()
    for cls, _make in _KEYED:
        for each in _with_subclasses(cls):
            each._merged_bindings = each._merge_bindings()


class Lines(OptionList):
    """The app's list: the mouse wheel moves the highlight, like the arrows.

    A plain OptionList scrolls its view under a still highlight instead.
    The view follows the highlight, so long lists still scroll. Unlike the
    arrow keys, the wheel stops at the ends instead of wrapping around.
    """

    def _wheel(self, event: events.MouseEvent, down: bool) -> None:
        event.stop()
        event.prevent_default()
        self.focus()
        before = self.highlighted
        if down:
            self.action_cursor_down()
        else:
            self.action_cursor_up()
        after = self.highlighted
        if before is not None and after is not None and (after < before) == down:
            self.highlighted = before  # it wrapped around an end

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, down=True)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, down=False)


def _restore_highlight(options: OptionList, index: int | None) -> None:
    """Highlight line *index* again after a refill, else the first usable line.

    Every list starts with a line highlighted, so Enter always does
    something.
    """
    count = options.option_count
    if index is not None and index < count:
        if not options.get_option_at_index(index).disabled:
            options.highlighted = index
            return
    for n in range(count):
        if not options.get_option_at_index(n).disabled:
            options.highlighted = n
            return


def _refill(options: OptionList, lines: Sequence[Option | None]) -> None:
    """Replace *options*' lines in place.

    The highlighted line stays highlighted (found again by its id, as lines
    above it may come or go) and stays where it was on screen. Rebuilding a
    list otherwise scrolls it home and then just far enough to show the
    highlight, snapping that line to the bottom.
    """
    old_index = options.highlighted
    old = options.highlighted_option
    offset: int | None = None
    if old_index is not None and options.is_mounted:
        # OptionList keeps no public line positions; _index_to_line is
        # where its own scroll_to_highlight gets them.
        offset = options._index_to_line.get(old_index, 0) - round(options.scroll_y)
    options.clear_options()
    options.add_options(lines)
    index = old_index
    if old is not None and old.id is not None:
        try:
            index = options.get_option_index(old.id)
        except OptionDoesNotExist:
            pass
    _restore_highlight(options, index)
    if offset is None:
        return

    def keep_place() -> None:
        if options.highlighted is None:
            return
        y = options._index_to_line.get(options.highlighted)
        if y is not None:
            options.scroll_to(y=max(0, y - offset), animate=False, immediate=True)

    options.call_after_refresh(keep_place)


def _advance(options: OptionList) -> None:
    """Move to the next line once a refill (see _refill) has settled."""
    options.call_after_refresh(options.action_cursor_down)


def _mkvsmith(node: DOMNode) -> MkvsmithApp:
    app = node.app
    assert isinstance(app, MkvsmithApp)
    return app


# =============================================================================
# Dialogs
# =============================================================================


def _strip_yes_no(message: str) -> str:
    """Drop a text prompt's "[y/N]:" hint: the dialog shows Yes / No lines."""
    return re.sub(r"\s*\[y/N\]:?\s*$", "", message)


class _Dialog(ModalScreen[T]):
    """A box over the screen: an optional heading, the question, then lines."""

    def __init__(self, question: str, heading: str | None = None) -> None:
        super().__init__()
        self._question = question
        self._heading = heading

    def compose_question(self) -> ComposeResult:
        if self._heading:
            yield Label(self._heading, classes="dialog-heading")
        yield Label(self._question)


@_keys(lambda: [Binding("escape", "cancel", tr("Cancel"))])
class ChoiceDialog(_Dialog[str | None]):
    """Pick one of *choices* ((value, label) pairs); Esc or Cancel -> None."""

    def __init__(
        self,
        question: str,
        choices: Sequence[tuple[str, str]],
        current: str | None = None,
        *,
        heading: str | None = None,
        cancel: bool = True,
    ) -> None:
        super().__init__(question, heading)
        self._choices = list(choices)
        self._current = current
        self._cancel = cancel

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.compose_question()
            options: list[Option | None] = [
                Option(label, id=f"choice-{n}")
                for n, (_value, label) in enumerate(self._choices)
            ]
            if self._cancel:
                options += [None, Option(tr("Cancel"), id="cancel")]
            yield Lines(*options, id="choices")

    def on_mount(self) -> None:
        options = self.query_one("#choices", OptionList)
        values = [value for value, _label in self._choices]
        options.highlighted = (
            values.index(self._current) if self._current in values else 0
        )
        options.focus()

    @on(OptionList.OptionSelected, "#choices")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "cancel":
            self.dismiss(None)
        else:
            self.dismiss(self._choices[event.option_index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


@_keys(lambda: [Binding("escape", "cancel", tr("No"))])
class ConfirmDialog(_Dialog[bool]):
    """A yes/no question; Esc answers No."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.compose_question()
            yield Lines(
                Option(tr("Yes"), id="yes"), Option(tr("No"), id="no"), id="choices"
            )

    def on_mount(self) -> None:
        options = self.query_one("#choices", OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#choices")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


@_keys(lambda: [Binding("escape", "cancel", tr("Cancel"))])
class TextDialog(_Dialog[str | None]):
    """Type a value; Enter or OK saves, Esc or Cancel -> None.

    *check* returns an error message for an unacceptable value (shown under
    the field, keeping the dialog open), or None.
    """

    def __init__(
        self,
        question: str,
        current: str,
        *,
        heading: str | None = None,
        secret: bool = False,
        check: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__(question, heading)
        self._current = current
        self._secret = secret
        self._check = check

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.compose_question()
            yield Input(self._current, password=self._secret, id="value")
            yield Label("", id="error")
            yield Lines(
                Option(tr("OK"), id="save"),
                Option(tr("Cancel"), id="cancel"),
                id="actions",
            )

    def on_mount(self) -> None:
        field = self.query_one("#value", Input)
        field.focus()
        field.cursor_position = len(field.value)

    @on(OptionList.OptionSelected, "#actions")
    def _action(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "save":
            self._save()
        else:
            self.dismiss(None)

    @on(Input.Submitted, "#value")
    def _save(self) -> None:
        value = self.query_one("#value", Input).value
        problem = self._check(value) if self._check is not None else None
        if problem:
            error = self.query_one("#error", Label)
            error.update(problem)
            error.add_class("shown")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


@_keys(lambda: [Binding("escape", "close", tr("OK"))])
class InfoDialog(_Dialog[None]):
    """A titled list of (label, value) rows, e.g. the TMDB metadata preview."""

    def __init__(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        super().__init__(title)
        self._rows = list(rows)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._question, classes="dialog-heading")
            yield Static(
                "\n".join(f"{label}: {value}" for label, value in self._rows),
                classes="rows",
                markup=False,
            )
            yield Lines(Option(tr("OK"), id="ok"), id="actions")

    def on_mount(self) -> None:
        options = self.query_one("#actions", OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#actions")
    def action_close(self) -> None:
        self.dismiss(None)


class _DialogBridge:
    """Answers ``UserPrompts`` questions from worker threads with dialogs.

    Scanning and ripping run in worker threads, and the code they call asks
    the user things through ``UserPrompts`` (overwrite this file? which TMDB
    match?). Each hook shows a dialog on the UI thread and blocks the worker
    until it is answered. When the app is closing, pending and later
    questions get their default answer instead.
    """

    def __init__(self, app: MkvsmithApp) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._pending: set[threading.Event] = set()
        self._closed = False

    def prompts(self) -> UserPrompts:
        return UserPrompts(
            confirm=self.confirm,
            text=self.text,
            secret=self.secret,
            choose=self.choose,
            show_table=self.show_table,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for done in self._pending:
                done.set()

    def _ask(self, make: Callable[[], ModalScreen[Any]], default: Any) -> Any:
        done = threading.Event()
        answer: list[Any] = []

        def show() -> None:
            def answered(result: Any) -> None:
                answer.append(result)
                done.set()

            self._app.push_screen(make(), answered)

        with self._lock:
            if self._closed:
                return default
            self._pending.add(done)
        try:
            self._app.call_from_thread(show)
            done.wait()
        except RuntimeError:  # the app has stopped
            return default
        finally:
            with self._lock:
                self._pending.discard(done)
        return answer[0] if answer else default

    def ask_dialog(self, make: Callable[[], ModalScreen[Any]]) -> Any:
        """Show the dialog *make* builds; its answer (None if the app closed)."""
        return self._ask(make, None)

    def confirm(self, message: str) -> bool:
        return bool(self._ask(lambda: ConfirmDialog(_strip_yes_no(message)), False))

    def text(self, prompt: str, default: str | None = None) -> str:
        answer = self._ask(lambda: TextDialog(prompt, default or ""), None)
        return (default or "") if answer is None else str(answer)

    def secret(self, prompt: str) -> str:
        answer = self._ask(lambda: TextDialog(prompt, "", secret=True), None)
        return "" if answer is None else str(answer)

    def choose(self, question: str, options: list[str], default: int = 0) -> int:
        choices = [(str(n), option) for n, option in enumerate(options)]
        answer = self._ask(lambda: ChoiceDialog(question, choices, str(default)), None)
        return default if answer is None else int(answer)

    def show_table(self, title: str, rows: list[tuple[str, str]]) -> None:
        self._ask(lambda: InfoDialog(title, rows), None)


# =============================================================================
# Pages
# =============================================================================


@_keys(
    lambda: [
        Binding("escape", "go_back", tr("Back")),
        Binding("q", "quit_app", tr("Quit")),
    ]
)
class _Page(Screen[None]):
    """A full screen with the app header, a body, and the key footer.

    Back and Quit live in the footer (Esc / q, or a click on them).
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        yield from ()

    @property
    def mkvsmith(self) -> MkvsmithApp:
        return _mkvsmith(self)

    @property
    def state(self) -> RuntimeState:
        return self.mkvsmith.state

    def action_go_back(self) -> None:
        self.mkvsmith.go_back()

    def action_quit_app(self) -> None:
        self.mkvsmith.request_quit()


# =============================================================================
# Main menu
# =============================================================================


class MainMenu(_Page):
    def compose_body(self) -> ComposeResult:
        yield Static(tr("What would you like to do?"), classes="heading")
        yield Lines(
            Option(tr("Rip a disc"), id="rip"),
            Option(tr("Settings"), id="settings"),
            Option(tr("About / keys"), id="about"),
            Option(tr("Quit"), id="quit"),
            id="menu",
        )

    def on_mount(self) -> None:
        options = self.query_one("#menu", OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#menu")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        choice = event.option.id
        if choice == "rip":
            self.app.push_screen(SourceScreen())
        elif choice == "settings":
            self.app.push_screen(SettingsScreen())
        elif choice == "about":
            self.app.push_screen(AboutScreen())
        elif choice == "quit":
            self.mkvsmith.request_quit()


# =============================================================================
# Source picker
# =============================================================================


def _drive_prompt(drive: OpticalDrive, label: str | None) -> str:
    if drive.has_disc is False:
        status = tr("No disc")
    elif label:
        status = label
    elif drive.has_disc:
        status = tr("Disc inserted")
    else:
        status = tr("Checking...")
    return f"{drive.path}  {drive.name}\n  {status}"


@_keys(
    lambda: [
        Binding("r", "refresh", tr("Refresh")),
        Binding("e", "eject", tr("Eject"), show=can_eject()),
    ]
)
class SourceScreen(_Page):
    def __init__(self) -> None:
        super().__init__()
        self._drives: list[OpticalDrive] = []

    def compose_body(self) -> ComposeResult:
        yield Static(tr("Choose a disc"), classes="heading")
        yield Lines(id="sources")

    def on_mount(self) -> None:
        self.action_refresh()
        self.query_one("#sources", OptionList).focus()

    def _fill(self, labels: dict[Path, str | None]) -> None:
        lines: list[Option | None] = []
        if not self._drives:
            lines.append(Option(tr("No optical drives found"), disabled=True))
        for n, drive in enumerate(self._drives):
            lines.append(
                Option(
                    _drive_prompt(drive, labels.get(drive.path)),
                    id=f"drive-{n}",
                    disabled=drive.has_disc is False,
                )
            )
        lines.append(Option(tr("Browse for an ISO or disc folder..."), id="browse"))
        _refill(self.query_one("#sources", OptionList), lines)

    def action_refresh(self) -> None:
        options = self.query_one("#sources", OptionList)
        options.clear_options()
        options.add_option(Option(tr("Looking for drives..."), disabled=True))
        app = self.mkvsmith
        self.run_worker(
            lambda: self._scan_drives(app.find_drives, app.read_label),
            thread=True,
            exclusive=True,
            group="drives",
        )

    def _scan_drives(
        self,
        find_drives: Callable[[], list[OpticalDrive]],
        read_label: Callable[[Path], str | None],
    ) -> None:
        drives = find_drives()
        labels: dict[Path, str | None] = {}

        def show() -> None:
            self._drives = drives
            self._fill(dict(labels))

        self.app.call_from_thread(show)
        for drive in drives:
            if drive.has_disc is not False:
                labels[drive.path] = read_label(drive.path)
        self.app.call_from_thread(show)

    def _highlighted_drive(self) -> OpticalDrive | None:
        options = self.query_one("#sources", OptionList)
        if options.highlighted is None:
            return None
        option_id = options.get_option_at_index(options.highlighted).id or ""
        if not option_id.startswith("drive-"):
            return None
        return self._drives[int(option_id.removeprefix("drive-"))]

    def action_eject(self) -> None:
        drive = self._highlighted_drive()
        if drive is None or not can_eject():
            return
        try:
            self.mkvsmith.eject_drive(drive.path)
        except OSError as exc:
            self.notify(
                tr("Could not eject {path}: {err}", path=drive.path, err=exc),
                severity="error",
            )
            return
        self.action_refresh()

    @on(OptionList.OptionSelected, "#sources")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if option_id == "browse":
            self.app.push_screen(BrowseScreen())
        elif option_id.startswith("drive-"):
            drive = self._drives[int(option_id.removeprefix("drive-"))]
            self.mkvsmith.open_source(drive.path)


_PARENT_ID = "parent"
_USE_FOLDER_ID = "use-folder"


@_keys(
    lambda: [
        Binding("u", "up", tr("Up")),
        Binding("backspace", "up", tr("Up"), show=False),
        Binding("slash", "go_to", tr("Type a path")),
    ]
)
class BrowseScreen(_Page):
    """One folder at a time, as a list.

    ``..`` goes up; Enter or a click opens a folder or uses an ISO / video
    file. Inside a disc folder (or one holding loose disc streams) a "Use
    this folder" line appears under ``..``. ``/`` types a path instead.
    """

    def __init__(self, start: Path | None = None) -> None:
        super().__init__()
        self.folder = (start or Path.cwd()).resolve()
        self._entries: list[tuple[Path, bool, bool]] = []

    def compose_body(self) -> ComposeResult:
        yield Static(str(self.folder), id="folder", classes="heading", markup=False)
        yield Lines(id="entries")

    def on_mount(self) -> None:
        self.open_folder(self.folder)
        self.query_one("#entries", OptionList).focus()

    # -- listing ------------------------------------------------------------

    def open_folder(self, folder: Path) -> None:
        """Show *folder*'s contents (read in the background)."""
        self.folder = folder
        self.query_one("#folder", Static).update(str(folder))
        entries = self.query_one("#entries", OptionList)
        entries.clear_options()
        entries.add_option(Option(tr("Loading..."), disabled=True))
        self.run_worker(
            lambda: self._read_folder(folder),
            thread=True,
            exclusive=True,
            group="folder",
        )

    def _read_folder(self, folder: Path) -> None:
        try:
            listing = [
                (path, is_dir, is_dir and is_disc_folder(path))
                for path, is_dir in list_folder(folder)
            ]
            problem = None
        except OSError as exc:
            listing, problem = [], str(exc)
        usable = is_usable_source(folder)
        kind = disc_folder_kind(folder)
        self.app.call_from_thread(
            self._show_folder, folder, listing, problem, usable, kind
        )

    def _show_folder(
        self,
        folder: Path,
        listing: list[tuple[Path, bool, bool]],
        problem: str | None,
        usable: bool,
        kind: str | None,
    ) -> None:
        if folder != self.folder:
            return  # superseded by a newer open_folder()
        self._entries = listing
        entries = self.query_one("#entries", OptionList)
        entries.clear_options()
        if folder.parent != folder:
            entries.add_option(Option("..", id=_PARENT_ID))
        if usable:
            label = (
                tr("Use this folder ({kind})", kind=kind)
                if kind
                else tr("Use this folder")
            )
            entries.add_option(Option(label, id=_USE_FOLDER_ID))
        for n, (path, is_dir, is_disc) in enumerate(listing):
            if is_disc:
                label = f"{path.name}/  " + tr("(disc)")
            elif is_dir:
                label = f"{path.name}/"
            else:
                label = path.name
            entries.add_option(Option(Text(label), id=f"entry-{n}"))
        if problem is not None:
            entries.add_option(
                Option(tr("Can't read this folder: {err}", err=problem), disabled=True)
            )
        elif not listing and not usable:
            entries.add_option(
                Option(tr("No folders, ISOs or video files here"), disabled=True)
            )
        # Start on the first line below "..", where there is one, with ".."
        # still in view.
        _restore_highlight(entries, 1 if folder.parent != folder else 0)
        self.call_after_refresh(entries.scroll_home, animate=False)

    def _entry(self, option_id: str | None) -> tuple[Path, bool, bool] | None:
        if not option_id or not option_id.startswith("entry-"):
            return None
        return self._entries[int(option_id.removeprefix("entry-"))]

    # -- choosing -----------------------------------------------------------

    def action_up(self) -> None:
        if self.folder.parent != self.folder:
            self.open_folder(self.folder.parent)

    def action_go_to(self) -> None:
        self.app.push_screen(
            TextDialog(tr("Folder, ISO or video file"), str(self.folder)),
            self._go_to,
        )

    def _go_to(self, typed: str | None) -> None:
        if not typed or not typed.strip():
            return
        path = Path(typed.strip()).expanduser()
        if path.is_dir():
            self.open_folder(path.resolve())
        elif path.exists() and is_usable_source(path):
            self.mkvsmith.open_source(path)
        else:
            self.notify(
                tr("Not a folder, ISO or video file: {path}", path=path),
                severity="error",
            )

    @on(OptionList.OptionSelected, "#entries")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == _PARENT_ID:
            self.action_up()
            return
        if event.option.id == _USE_FOLDER_ID:
            self.mkvsmith.open_source(self.folder)
            return
        entry = self._entry(event.option.id)
        if entry is None:
            return
        path, is_dir, _is_disc = entry
        if is_dir:
            self.open_folder(path)
        else:
            self.mkvsmith.open_source(path)


# =============================================================================
# Scanning
# =============================================================================


class _LogPage(_Page):
    """A page with a log pane: log messages land there while it is open."""

    def log_line(self, level: str, message: str) -> None:
        styles = {"warn": "yellow", "error": "bold red", "debug": "dim"}
        self.query_one("#log", RichLog).write(
            Text(message, style=styles.get(level, ""))
        )


class ScanScreen(_LogPage):
    """Scans the source (log shown as it goes), then opens the title list."""

    DEFAULT_CSS = """
    ScanScreen #log { height: 1fr; }
    """

    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def compose_body(self) -> ComposeResult:
        yield Static(
            tr("Scanning {path}...", path=self.source),
            id="status",
            classes="heading",
            markup=False,
        )
        yield RichLog(id="log", wrap=True, markup=False, min_width=20)

    def on_mount(self) -> None:
        app = self.mkvsmith
        self.run_worker(
            lambda: self._scan(app), thread=True, exclusive=True, group="scan"
        )

    def _scan(self, app: MkvsmithApp) -> None:
        state = app.state
        try:
            titles, metadata = scan_source(self.source, state)
        except FileNotFoundError as exc:
            app.call_from_thread(self._failed, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - shown to the user, not raised
            log_error(tr("Scan failed: {err}", err=exc))
            app.call_from_thread(self._failed, tr("Scan failed: {err}", err=exc))
            return
        if not titles:
            app.call_from_thread(self._failed, tr("No titles found"))
            return
        # The per-disc questions, asked before the list.
        prompts = app.bridge.prompts()
        ask_closed_captions(titles, state.config, prompts)
        if state.config.ask_split_episodes:
            chosen = packed_split_offers(titles, prompts)
            if chosen:
                split_packed_episodes(titles, chosen, metadata, state)
        app.call_from_thread(self._scanned, titles, metadata)

    def _failed(self, message: str) -> None:
        self.query_one("#status", Static).update(message)
        self.notify(message, severity="error")

    def _scanned(self, titles: list[Title], metadata: DiscMetadata | None) -> None:
        if self.app.screen is not self:
            return  # the user went back while it scanned
        self.app.switch_screen(TitlesScreen(titles, metadata))


# =============================================================================
# Titles
# =============================================================================


@_keys(lambda: [Binding("space", "mark", tr("Mark"))])
class TitlesScreen(_Page):
    """The disc's titles, with what can be ripped from them listed first.

    Space marks titles for a batch rip; Enter on a title opens its tracks.
    """

    def __init__(self, titles: list[Title], metadata: DiscMetadata | None) -> None:
        super().__init__()
        self.titles = titles
        self.metadata = metadata
        self.marked: set[int] = set()
        # Track choices made on a title's screen (absent = the defaults).
        self.streams: dict[int, list[Stream]] = {}
        self.show_all = False
        self._edition_groups: list[list[Title]] = []

    def compose_body(self) -> ComposeResult:
        yield Static("", id="disc", classes="heading", markup=False)
        yield Lines(id="titles")

    def on_mount(self) -> None:
        self.refill()
        self.query_one("#titles", OptionList).focus()

    def on_screen_resume(self) -> None:
        self.refill()

    # -- listing ------------------------------------------------------------

    def by_index(self, index: int) -> Title:
        return next(t for t in self.titles if t.index == index)

    def visible_titles(self) -> tuple[list[Title], int]:
        if self.show_all:
            return list(self.titles), 0
        return _get_notable_titles(self.titles, self.state.config)

    def _title_prompt(
        self, title: Title, base: str | None, main_index: int, series: bool
    ) -> Text:
        mark = "[x]" if title.index in self.marked else "[ ]"
        if series:
            marker = " ○" if title.is_episode and not title.play_all else ""
        else:
            marker = " ★" if title.index == main_index else ""
        name = _title_list_name(title, 10_000, base)
        details = [
            title.duration_display,
            _title_source_id(title),
            title.streams_summary,
        ]
        if title.index in self.streams:
            details.append(tr("custom tracks"))
        line2 = "  ".join(part for part in details if part)
        return Text(f"{mark} {title.index:>2}  {name}{marker}\n       {line2}")

    def refill(self) -> None:
        config = self.state.config
        visible, hidden = self.visible_titles()
        self._edition_groups = _detect_edition_groups(self.titles)
        base = _list_name_base(self.titles, self.metadata)
        series = any(title.is_episode for title in self.titles)
        main_index = pick_main_feature(self.titles, config)

        heading = base or (self.metadata.name if self.metadata else "") or ""
        summary = tr("{n} title(s)", n=len(visible))
        if hidden:
            summary += " · " + tr("{n} hidden", n=hidden)
        summary += " · " + (tr("○ = episode") if series else tr("★ = main feature"))
        self.query_one("#disc", Static).update(
            f"{heading}\n{summary}" if heading else summary
        )

        lines: list[Option | None] = []
        if self.marked:
            lines.append(
                Option(
                    tr("Rip marked titles ({n})", n=len(self.marked)), id="rip-marked"
                )
            )
            if len(self.marked) >= 2:
                lines.append(
                    Option(
                        tr("Combine marked titles into one multi-edition MKV"),
                        id="combine-marked",
                    )
                )
        targets = main_feature_titles(self.titles, config)
        if targets and targets[0].is_episode:
            lines.append(
                Option(tr("Rip all episodes ({n})", n=len(targets)), id="rip-main")
            )
        elif targets:
            lines.append(
                Option(
                    Text(tr("Rip main feature: {name}", name=targets[0].name)),
                    id="rip-main",
                )
            )
        for n, group in enumerate(self._edition_groups):
            lines.append(
                Option(
                    tr(
                        "Combine titles {idxs} into one multi-edition MKV",
                        idxs=", ".join(str(t.index) for t in group),
                    ),
                    id=f"editions-{n}",
                )
            )
        from packed_episodes import packed_episode_count

        for title in self.titles:
            if title.packed_segments:
                lines.append(
                    Option(
                        tr(
                            "Split title {idx} into its {n} episodes",
                            idx=title.index,
                            n=packed_episode_count(title),
                        ),
                        id=f"split-{title.index}",
                    )
                )
        lines.append(
            Option(tr("Rip all listed titles ({n})", n=len(visible)), id="rip-all")
        )
        lines.append(None)
        for title in visible:
            lines.append(
                Option(
                    self._title_prompt(title, base, main_index, series),
                    id=f"title-{title.index}",
                )
            )
        if hidden:
            lines.append(
                Option(tr("Show {n} hidden titles", n=hidden), id="toggle-hidden")
            )
        elif self.show_all and _get_notable_titles(self.titles, config)[1]:
            lines.append(
                Option(tr("Hide short and duplicate titles"), id="toggle-hidden")
            )
        _refill(self.query_one("#titles", OptionList), lines)

    # -- actions ------------------------------------------------------------

    def _highlighted_title(self) -> Title | None:
        options = self.query_one("#titles", OptionList)
        if options.highlighted is None:
            return None
        option_id = options.get_option_at_index(options.highlighted).id or ""
        if not option_id.startswith("title-"):
            return None
        return self.by_index(int(option_id.removeprefix("title-")))

    def action_mark(self) -> None:
        """Mark or unmark the highlighted title, then move to the next line."""
        title = self._highlighted_title()
        if title is None:
            return
        self.marked ^= {title.index}
        self.refill()
        _advance(self.query_one("#titles", OptionList))

    def _rip(self, titles: Sequence[Title]) -> None:
        jobs = [RipJob(title, self.streams.get(title.index)) for title in titles]
        if jobs:
            self.app.push_screen(RipScreen(jobs))

    def _combine(self, titles: Sequence[Title]) -> None:
        positions = [self.titles.index(title) for title in titles]
        app = self.mkvsmith

        def build() -> None:
            names = ask_edition_names(len(positions), app.bridge.prompts())
            try:
                combined = prepare_multi_edition(self.titles, positions, names)
            except ValueError as exc:
                app.call_from_thread(self.notify, str(exc), severity="error")
                return
            app.call_from_thread(app.push_screen, RipScreen([RipJob(combined)]))

        self.run_worker(build, thread=True, group="combine")

    def _split(self, index: int) -> None:
        try:
            split_packed_episodes(self.titles, [index], self.metadata, self.state)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self.marked.discard(index)
        self.refill()

    @on(OptionList.OptionSelected, "#titles")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if option_id == "rip-marked":
            self._rip([t for t in self.titles if t.index in self.marked])
        elif option_id == "combine-marked":
            self._combine([t for t in self.titles if t.index in self.marked])
        elif option_id == "rip-main":
            self._rip(main_feature_titles(self.titles, self.state.config))
        elif option_id == "rip-all":
            self._rip(self.visible_titles()[0])
        elif option_id.startswith("editions-"):
            self._combine(
                self._edition_groups[int(option_id.removeprefix("editions-"))]
            )
        elif option_id.startswith("split-"):
            self._split(int(option_id.removeprefix("split-")))
        elif option_id == "toggle-hidden":
            self.show_all = not self.show_all
            self.refill()
        elif option_id.startswith("title-"):
            title = self.by_index(int(option_id.removeprefix("title-")))
            self.app.push_screen(TitleScreen(self, title))


@_keys(lambda: [Binding("space", "toggle_line", tr("Toggle"))])
class TitleScreen(_Page):
    """One title: rip it, mark it, and choose its tracks."""

    def __init__(self, titles_screen: TitlesScreen, title: Title) -> None:
        super().__init__()
        self.titles_screen = titles_screen
        self.disc_title = title
        # Tracks are kept by their position in title.streams (Stream.index
        # isn't unique: every DVD stream has index 0).
        self._default = {
            n
            for n, choice in enumerate(track_plan(title, titles_screen.state.config))
            if choice.keep
        }
        chosen = titles_screen.streams.get(title.index)
        self.keep = (
            {n for n, s in enumerate(title.streams) if any(s is c for c in chosen)}
            if chosen is not None
            else set(self._default)
        )

    def compose_body(self) -> ComposeResult:
        title = self.disc_title
        info = [
            title.duration_display,
            tr("{n} chapter(s)", n=len(title.chapters)) if title.chapters else "",
            title.source_file.name,
        ]
        yield Static(
            f"#{title.index}  {title.name}\n" + "  ".join(p for p in info if p),
            classes="heading",
            markup=False,
        )
        yield Lines(id="tracks")

    def on_mount(self) -> None:
        self.refill()
        self.query_one("#tracks", OptionList).focus()

    def _track_prompt(self, position: int, stream: Stream) -> Text:
        if stream.stream_type == StreamType.VIDEO:
            line = _video_stream_line(stream)
        else:
            line = _non_video_stream_line(self.disc_title, stream)
        # The CLI's columns waste width on a narrow screen.
        line = re.sub(r"\s{2,}", "  ", line.strip())
        box = "[x]" if position in self.keep else "[ ]"
        if not stream.is_muxable:
            box = "[-]"
        return Text(f"{box} {line}")

    def refill(self) -> None:
        title = self.disc_title
        marked = title.index in self.titles_screen.marked
        lines: list[Option | None] = [
            Option(tr("Rip this title"), id="rip"),
            Option(
                Text(("[x] " if marked else "[ ] ") + tr("Marked for a batch rip")),
                id="mark",
            ),
        ]
        if self.keep != self._default:
            lines.append(Option(tr("Reset tracks to the defaults"), id="defaults"))
        groups = [
            (tr("Video"), StreamType.VIDEO),
            (tr("Audio"), StreamType.AUDIO),
            (tr("Subtitles"), StreamType.SUBTITLE),
        ]
        for label, stream_type in groups:
            tracks = [
                (n, stream)
                for n, stream in enumerate(title.streams)
                if stream.stream_type == stream_type
            ]
            if not tracks:
                continue
            lines += [None, Option(label, disabled=True)]
            lines += [
                Option(
                    self._track_prompt(n, stream),
                    id=f"track-{n}",
                    disabled=not stream.is_muxable,
                )
                for n, stream in tracks
            ]
        _refill(self.query_one("#tracks", OptionList), lines)

    def selected_streams(self) -> list[Stream] | None:
        """The chosen tracks, or None when they are the defaults."""
        if self.keep == self._default:
            return None
        return [s for n, s in enumerate(self.disc_title.streams) if n in self.keep]

    def _store(self) -> None:
        chosen = self.selected_streams()
        if chosen is None:
            self.titles_screen.streams.pop(self.disc_title.index, None)
        else:
            self.titles_screen.streams[self.disc_title.index] = chosen

    def action_toggle_line(self) -> None:
        """Toggle the highlighted track (or mark), then move to the next line."""
        options = self.query_one("#tracks", OptionList)
        if options.highlighted is not None:
            option = options.get_option_at_index(options.highlighted)
            if option.id != "rip" and option.id != "defaults":
                self._choose(option.id or "")
                _advance(options)

    @on(OptionList.OptionSelected, "#tracks")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self._choose(event.option.id or "")

    def _choose(self, option_id: str) -> None:
        if option_id == "rip":
            if not self.keep:
                self.notify(tr("Choose at least one track"), severity="error")
                return
            self.app.push_screen(
                RipScreen([RipJob(self.disc_title, self.selected_streams())])
            )
            return
        if option_id == "mark":
            self.titles_screen.marked ^= {self.disc_title.index}
        elif option_id == "defaults":
            self.keep = set(self._default)
        elif option_id.startswith("track-"):
            self.keep ^= {int(option_id.removeprefix("track-"))}
        else:
            return
        self._store()
        self.refill()


# =============================================================================
# Ripping
# =============================================================================


def _size_text(path: Path) -> str:
    try:
        size = float(path.stat().st_size)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


@_keys(
    lambda: [
        Binding("escape", "stop_or_back", tr("Back")),
        Binding("q", "quit_app", tr("Quit")),
    ]
)
class RipScreen(_LogPage):
    """Rips a batch, one line per output with its progress, plus the log."""

    DEFAULT_CSS = """
    RipScreen #log { height: 1fr; border-top: solid $panel; }
    """

    def __init__(self, jobs: list[RipJob]) -> None:
        super().__init__()
        self.jobs = jobs
        self.running = False
        self.result: RipBatchResult | None = None
        self._cancel = False
        self._shown_pct: dict[int, int] = {}

    def compose_body(self) -> ComposeResult:
        yield Static(
            tr("Ripping {n} title(s)", n=len(self.jobs)),
            id="status",
            classes="heading",
        )
        yield Lines(
            *(
                Option(self._job_prompt(job, tr("Waiting")), id=f"job-{n}")
                for n, job in enumerate(self.jobs)
            ),
            id="jobs",
        )
        yield RichLog(id="log", wrap=True, markup=False, min_width=20)

    @staticmethod
    def _job_prompt(job: RipJob, status: str) -> Text:
        return Text(f"#{job.title.index}  {job.title.name}\n    {status}")

    def _set_status(self, job: RipJob, status: str) -> None:
        n = self.jobs.index(job)
        self.query_one("#jobs", OptionList).replace_option_prompt_at_index(
            n, self._job_prompt(job, status)
        )

    def on_mount(self) -> None:
        app = self.mkvsmith
        self.running = True
        self.run_worker(
            lambda: self._rip(app), thread=True, exclusive=True, group="rip"
        )

    # -- the worker ---------------------------------------------------------

    def _rip(self, app: MkvsmithApp) -> None:
        state = app.state
        prompts = app.bridge.prompts()
        try:
            ask_output_dir(state.config, prompts)
            app.tagging.prepare(prompts)
            creator = MKVCreator(
                state.config.output_dir, state.tag_options, runtime_state=state
            )
            creator.prompts = prompts
            callbacks = RipCallbacks(
                started=lambda job, pos, total: app.call_from_thread(
                    self._started, job, pos, total
                ),
                progress=lambda job, pct: self._progress(app, job, pct),
                finished=lambda outcome: app.call_from_thread(self._finished, outcome),
            )
            result = run_rip_jobs(
                creator, self.jobs, callbacks, cancelled=lambda: self._cancel
            )
        except Exception as exc:  # noqa: BLE001 - shown to the user, not raised
            log_error(tr("Ripping stopped: {err}", err=exc))
            result = RipBatchResult(cancelled=True)
        app.call_from_thread(self._done, result)

    def _progress(self, app: MkvsmithApp, job: RipJob, pct: int) -> None:
        n = self.jobs.index(job)
        if self._shown_pct.get(n) == pct:
            return
        self._shown_pct[n] = pct
        app.call_from_thread(self._set_status, job, tr("Muxing... {pct}%", pct=pct))

    # -- UI updates ---------------------------------------------------------

    def _started(self, job: RipJob, position: int, total: int) -> None:
        self.query_one("#status", Static).update(
            tr("Ripping {pos} of {total}", pos=position, total=total)
        )
        self._set_status(job, tr("Preparing..."))
        self.query_one("#jobs", OptionList).highlighted = self.jobs.index(job)

    def _finished(self, outcome: RipOutcome) -> None:
        if outcome.output is not None:
            status = tr(
                "Done: {name} ({size})",
                name=outcome.output.name,
                size=_size_text(outcome.output),
            )
        elif self._cancel:
            status = tr("Stopped")
        else:
            error = outcome.error
            message = error.message if isinstance(error, RipError) else str(error)
            status = tr("Failed: {err}", err=message)
            if isinstance(error, RipError):
                self.log_line("error", error.format_verbose())
        self._set_status(outcome.job, status)

    def _done(self, result: RipBatchResult) -> None:
        self.running = False
        self.result = result
        finished = {self.jobs.index(o.job) for o in result.outcomes}
        for n, job in enumerate(self.jobs):
            if n not in finished:
                self._set_status(job, tr("Skipped"))
        failed = result.failed
        if self._cancel and result.outcomes and not result.outcomes[-1].ok:
            failed -= 1  # the title being ripped when it was stopped
        summary = tr("Done: {ok} ok, {fail} failed", ok=result.ok, fail=failed)
        if self._cancel or result.cancelled:
            summary = tr("Stopped") + " · " + summary
        self.query_one("#status", Static).update(summary)
        options = self.query_one("#jobs", OptionList)
        options.add_options([None, Option(tr("Back"), id="back")])
        options.highlighted = options.option_count - 1
        options.focus()
        if self.mkvsmith.quit_after_rip:
            self.app.exit(None)

    @on(OptionList.OptionSelected, "#jobs")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "back":
            self.app.pop_screen()

    # -- stopping -----------------------------------------------------------

    def stop(self) -> None:
        """Stop after killing the mux in progress (its partial file is deleted)."""
        self._cancel = True
        self.state.active_processes.kill_muxers()
        self.query_one("#status", Static).update(tr("Stopping..."))

    def action_stop_or_back(self) -> None:
        if not self.running:
            self.app.pop_screen()
            return

        def answered(stop: bool | None) -> None:
            if stop:
                self.stop()

        self.app.push_screen(
            ConfirmDialog(tr("Stop ripping? The title being ripped is deleted.")),
            answered,
        )


# =============================================================================
# Settings
# =============================================================================


def _display_value(spec: SettingSpec, value: object) -> str:
    shown = format_value(spec, value)
    if spec.kind == "language":
        return dict(available_languages()).get(shown, shown) or tr("Automatic")
    if spec.kind == "choice":
        return tr(CHOICE_LABELS.get(shown, shown))
    if spec.kind == "bool":
        return tr(shown)
    return shown or "-"


def _setting_prompt(spec: SettingSpec, value: object) -> Text:
    return Text(f"{tr(spec.description)}\n  {spec.key} = {_display_value(spec, value)}")


def _choices_for(spec: SettingSpec) -> list[tuple[str, str]] | None:
    """(value, label) pairs for a pick-one setting, else None (typed in)."""
    if spec.kind == "bool":
        return [("yes", tr("yes")), ("no", tr("no"))]
    if spec.kind == "choice":
        return [(c, tr(CHOICE_LABELS.get(c, c))) for c in spec.choices]
    if spec.kind == "language":
        return [("", tr("Automatic")), *available_languages()]
    return None


def _setting_problem(spec: SettingSpec, value: str) -> str | None:
    try:
        spec.parse(value)
    except ValueError as exc:
        return tr("Invalid value: {err}", err=exc)
    return None


def setting_dialog(
    spec: SettingSpec,
    saved: Settings,
    heading: str | None = None,
    *,
    cancel: bool = True,
) -> ModalScreen[str | None]:
    """The dialog that asks for *spec*: its choices, or a field to type in.

    *cancel* adds a Cancel line to a list of choices (first-run setup has
    none: Esc keeps the suggested value there).
    """
    current = getattr(saved, spec.attr)
    shown = "" if spec.secret else format_value(spec, current)
    question = tr(spec.description)
    choices = _choices_for(spec)
    if choices is not None:
        return ChoiceDialog(question, choices, shown, heading=heading, cancel=cancel)
    return TextDialog(
        question,
        shown,
        heading=heading,
        secret=spec.secret,
        check=lambda value: _setting_problem(spec, value),
    )


@_keys(lambda: [Binding("d", "reset", tr("Default"))])
class SettingsScreen(_Page):
    def compose_body(self) -> ComposeResult:
        yield Static(
            tr("Saved settings: the defaults for every disc"), classes="heading"
        )
        yield Lines(id="settings")

    def on_mount(self) -> None:
        self._fill()
        self.query_one("#settings", OptionList).focus()

    def _fill(self) -> None:
        settings = self.state.settings
        _refill(
            self.query_one("#settings", OptionList),
            [
                Option(_setting_prompt(spec, getattr(settings, spec.attr)), id=spec.key)
                for spec in SETTING_SPECS
            ],
        )

    def _highlighted_spec(self) -> SettingSpec | None:
        options = self.query_one("#settings", OptionList)
        if options.highlighted is None:
            return None
        return SETTING_SPECS[options.highlighted]

    @on(OptionList.OptionSelected, "#settings")
    def _edit(self, event: OptionList.OptionSelected) -> None:
        spec = SETTING_SPECS[event.option_index]

        def save(value: str | None) -> None:
            if value is not None:
                self._save(spec, value)

        self.app.push_screen(setting_dialog(spec, self.state.settings), save)

    def _save(self, spec: SettingSpec, value: str | None, reset: bool = False) -> None:
        try:
            self.state.settings = update_saved_setting(spec.key, value, reset=reset)
        except SettingsFileUnreadable as exc:
            self.notify(tr("Could not write config: {err}", err=exc), severity="error")
            return
        except (KeyError, ValueError) as exc:
            self.notify(tr("Invalid value: {err}", err=exc), severity="error")
            return
        except OSError as exc:
            self.notify(tr("Could not write config: {err}", err=exc), severity="error")
            return
        self.mkvsmith.settings_changed()
        if spec.key == "ui.language":
            self.mkvsmith.language_changed(reopen_settings=True)
            return
        self._fill()
        self.notify(
            tr(
                "{key} = {value}",
                key=spec.key,
                value=_display_value(spec, getattr(self.state.settings, spec.attr)),
            )
        )

    def action_reset(self) -> None:
        spec = self._highlighted_spec()
        if spec is not None:
            self._save(spec, None, reset=True)


# =============================================================================
# About
# =============================================================================


class AboutScreen(_Page):
    def compose_body(self) -> ComposeResult:
        mkvmerge = shutil.which("mkvmerge")
        lines = [
            f"mkvsmith {MKVSMITH_VERSION}",
            "",
            tr("mkvmerge: {path}", path=mkvmerge or tr("not found")),
            tr("Settings file: {path}", path=settings_path()),
            tr("Interface language: {name}", name=language_name(get_language())),
            "",
            tr("Keys"),
            tr("  Arrows / wheel   move"),
            tr("  Enter / click    choose"),
            tr("  Space            mark a title / toggle a track"),
            tr("  Esc              go back"),
            tr("  q                quit"),
        ]
        yield Static("\n".join(lines), markup=False)


# =============================================================================
# The app
# =============================================================================


class MkvsmithApp(App[None]):
    """The whole interactive session: setup, menu, scan, titles, rips."""

    TITLE = "mkvsmith"
    SUB_TITLE = MKVSMITH_VERSION
    ENABLE_COMMAND_PALETTE = False

    # One look everywhere: borderless lists of lines, the highlighted line
    # orange whether or not the list has focus.
    CSS = """
    #body { padding: 0 1; }
    .heading { margin: 1 0; text-style: bold; }
    OptionList, OptionList:focus {
        border: none;
        padding: 0;
        background: transparent;
        background-tint: transparent;
    }
    /* A page's list fills the space left under its heading and scrolls;
       (app CSS outranks the screens' own, so the sizes all live here). */
    #body OptionList { height: 1fr; }
    RipScreen #jobs { height: auto; max-height: 50%; }
    OptionList > .option-list--option { padding: 0 1; }
    OptionList > .option-list--option-highlighted,
    OptionList:focus > .option-list--option-highlighted {
        color: black;
        background: darkorange;
        text-style: bold;
    }
    OptionList > .option-list--option-hover { background: $boost; }
    RichLog { background: transparent; }
    ModalScreen { align: center middle; }
    .dialog {
        width: 90%;
        max-width: 72;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round darkorange;
        background: $surface;
    }
    .dialog Label { width: 100%; }
    .dialog .dialog-heading { text-style: bold; color: darkorange; margin-bottom: 1; }
    .dialog .rows { margin-top: 1; max-height: 20; }
    .dialog OptionList { height: auto; max-height: 16; margin-top: 1; }
    .dialog #error { color: $error; display: none; }
    .dialog #error.shown { display: block; }
    """

    def __init__(
        self,
        state: RuntimeState,
        source: Path | None = None,
        *,
        titles: list[Title] | None = None,
        disc_metadata: DiscMetadata | None = None,
        reapply: Callable[[], None] | None = None,
        find_drives: Callable[[], list[OpticalDrive]] = list_optical_drives,
        read_label: Callable[[Path], str | None] = disc_label,
        eject_drive: Callable[[Path], None] = eject,
    ) -> None:
        super().__init__()
        self.state = state
        self.source = source
        self.initial_titles = titles
        self.initial_metadata = disc_metadata
        self.reapply = reapply
        self.find_drives = find_drives
        self.read_label = read_label
        self.eject_drive = eject_drive
        self.bridge = _DialogBridge(self)
        self.tagging = TaggingChoice.from_options(state.tag_options)
        self.quit_after_rip = False
        self._thread_id = threading.get_ident()

    # -- start-up -----------------------------------------------------------

    def on_mount(self) -> None:
        self.run_worker(self._start(), exclusive=True, group="start")

    async def _start(self) -> None:
        await asyncio.to_thread(self._setup)
        self.push_screen(MainMenu())
        if self.initial_titles:
            self.push_screen(TitlesScreen(self.initial_titles, self.initial_metadata))
        elif self.source is not None:
            self.push_screen(ScanScreen(self.source))

    def _setup(self) -> None:
        """First-run setup (or the settings new since the last run).

        Runs off the UI thread: each question is a dialog through the
        bridge. Esc keeps the suggested value. Choosing the interface
        language switches to it straight away.
        """
        loaded = load_settings()
        heading = (
            tr("New settings to choose since your last run")
            if loaded.exists
            else tr("First-time setup")
        )

        def ask(spec: SettingSpec, saved: Settings) -> object | None:
            return self.bridge.ask_dialog(
                lambda: setting_dialog(spec, saved, heading, cancel=False)
            )

        def answered(spec: SettingSpec, saved: Settings) -> None:
            nonlocal heading
            if spec.key == "ui.language":
                set_language(saved.ui_language)
                relabel_keys()
                if not loaded.exists:
                    heading = tr("First-time setup")

        saved = complete_settings(loaded, ask, answered)
        if saved.answered == loaded.settings.answered:
            return
        try:
            save_settings(saved, loaded.path)
        except OSError as exc:
            self.call_from_thread(
                self.notify,
                tr("Could not write config: {err}", err=exc),
                severity="error",
            )
        self.state.settings = saved
        self.call_from_thread(self.settings_changed)

    def exit(self, *args: Any, **kwargs: Any) -> None:
        # Release worker threads waiting on a dialog, or shutting down would
        # wait for them forever.
        self.bridge.close()
        super().exit(*args, **kwargs)

    def on_unmount(self) -> None:
        self.bridge.close()

    async def action_quit(self) -> None:
        # Textual's own Ctrl+Q quit: ask first while ripping, like q does.
        # Exiting under a running rip would strand its worker thread.
        self.request_quit()

    # -- navigation ---------------------------------------------------------

    def go_back(self) -> None:
        # screen_stack[0] is Textual's default screen; [1] is the main menu.
        if len(self.screen_stack) > 2:
            self.pop_screen()

    def open_source(self, path: Path) -> None:
        self.push_screen(ScanScreen(path))

    def running_rip(self) -> RipScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, RipScreen) and screen.running:
                return screen
        return None

    def request_quit(self) -> None:
        """Quit; while ripping, ask first, then stop the rip and quit."""
        rip = self.running_rip()
        if rip is None:
            self.exit(None)
            return

        def answered(stop: bool | None) -> None:
            if stop:
                self.quit_after_rip = True
                rip.stop()

        self.push_screen(
            ConfirmDialog(
                tr("Stop ripping and quit? The title being ripped is deleted.")
            ),
            answered,
        )

    # -- settings -----------------------------------------------------------

    def settings_changed(self) -> None:
        """The saved settings changed: this run's options follow them."""
        if self.reapply is not None:
            self.reapply()
        self.tagging = TaggingChoice.from_options(self.state.tag_options)

    def language_changed(self, reopen_settings: bool = False) -> None:
        """Switch the interface language now, rebuilding the open screens."""
        set_language(self.state.settings.ui_language)
        relabel_keys()
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(MainMenu())
        if reopen_settings:
            self.push_screen(SettingsScreen())

    # -- logging ------------------------------------------------------------

    def log_message(self, level: str, message: str) -> None:
        """Logger sink: the open log pane, else warnings and errors as
        notifications."""
        if threading.get_ident() != self._thread_id:
            try:
                self.call_from_thread(self.log_message, level, message)
            except RuntimeError:  # the app has stopped
                pass
            return
        for screen in reversed(self.screen_stack):
            if isinstance(screen, _LogPage):
                screen.log_line(level, message)
                return
        if level in ("warn", "error"):
            self.notify(message, severity="error" if level == "error" else "warning")


def run_tui(
    state: RuntimeState,
    source: Path | None = None,
    *,
    titles: list[Title] | None = None,
    disc_metadata: DiscMetadata | None = None,
    reapply: Callable[[], None] | None = None,
) -> None:
    """Run the interactive session until the user quits.

    Opens the main menu; with *source* it scans that straight away, and with
    already-scanned *titles* it opens their list. *reapply* resolves the
    run's options again after the saved settings change (flags still win).
    """
    app = MkvsmithApp(
        state, source, titles=titles, disc_metadata=disc_metadata, reapply=reapply
    )
    previous_sink, previous_prompts = state.logger.sink, state.prompts
    state.logger.sink = app.log_message
    state.prompts = app.bridge.prompts()
    try:
        app.run()
    finally:
        app.bridge.close()
        state.logger.sink, state.prompts = previous_sink, previous_prompts
