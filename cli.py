"""
CLI, interactive mode, and UI.

Extracted from main.py: terminal display of titles/streams, the interactive
rip prompt, argument parsing, UI language resolution, the first-run setup
wizard, and the ``main`` entry point wiring everything together.

Copyright (C) 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

# Licensed under GPL-3.0-or-later

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

import dvdifo
from models import (
    Config,
    RuntimeState,
    TagOptions,
    RUNTIME_STATE,
    DEFAULT_TAG_METADATA,
    Stream,
    StreamType,
    Title,
    RipError,
    log_info,
    log_warn,
    log_error,
    log_debug,
    _HAS_MKVMERGE,
)
from i18n import (
    tr,
    set_language,
    get_language,
    language_name,
    available_languages,
    detect_locale_language,
)
from settings import SETTINGS_PATH, load_settings, save_settings
from scan import Scanner, _get_notable_titles, pick_main_feature
from mkv import MKVCreator

__version__ = "0.1.0"  # keep in sync with pyproject.toml [project].version


# =============================================================================
# Display & UI
# =============================================================================


def get_terminal_width() -> int:
    try:
        return max(shutil.get_terminal_size().columns, 40)
    except OSError:
        return 80


def display_titles(
    titles: list[Title],
    disc_name: str | None = None,
    config: Config | None = None,
) -> None:
    w = get_terminal_width()
    visible, hidden = _get_notable_titles(titles, config)
    main_idx = pick_main_feature(titles, config)
    nw = max(w - 28, 10)
    rule_w = min(w, nw + 28)
    print("\n" + "═" * rule_w)
    print(tr("  SCANNED TITLES") + (f" - {disc_name}" if disc_name else ""))
    print("═" * rule_w)
    hdr_dur = tr("Dur")
    hdr_name = tr("Name")
    hdr_streams = tr("Streams")
    print(f"{'#':>2}  {hdr_dur:<8}  {hdr_name:<{nw}}  {hdr_streams}\n" + "─" * rule_w)
    for t in visible:
        n = t.name[: nw - 2] + ".." if len(t.name) > nw else t.name
        marker = " \u2605" if t.index == main_idx else ""
        print(
            f"{t.index:>2}  {t.duration_display:<8}  {n:<{nw}}  {t.streams_summary}{marker}"
        )
    total_msg = tr("Total: {n} title(s)", n=len(visible))
    ep_count = sum(1 for t in titles if t.dvd_episode_number is not None)
    if ep_count:
        total_msg += "  " + tr("({n} episode(s) detected)", n=ep_count)
    if hidden:
        total_msg += "  " + tr(
            "({n} low-quality titles hidden; use --show-all to view)", n=hidden
        )
    total_msg += "  " + tr("\u2605 = main feature")
    print("═" * rule_w + f"\n{total_msg}\n")


def _stream_flags(stream: Stream) -> str:
    if not stream.is_default and not stream.is_forced:
        return ""
    labels = ",".join(
        label
        for label in (
            "DEF" if stream.is_default else "",
            "FOR" if stream.is_forced else "",
        )
        if label
    )
    return f" [{labels}]"


def _video_stream_line(stream: Stream) -> str:
    dimensions = f"{stream.width}x{stream.height}" if stream.width else "?"
    return (
        f"  {stream.codec_display:<14} {dimensions:<10} "
        f"{stream.language_display}{_stream_flags(stream)}"
    )


def _subtitle_extension_info(title: Title, stream: Stream) -> str:
    if stream.sub_id is None or stream.sub_id not in title.dvd_subp_attrs:
        return ""
    extension_label = title.dvd_subp_attrs[stream.sub_id].code_extension_label
    if extension_label in ("", "unspecified", "normal"):
        return ""
    return f" ({extension_label})"


def _non_video_stream_line(title: Title, stream: Stream) -> str:
    channels = f"{stream.channels}ch" if stream.channels else "-"
    stream_title = f" - {stream.title}" if stream.title else ""
    return (
        f"  {stream.display_id:<5} {stream.codec_display:<14} {channels:<6} "
        f"{stream.language_display}{stream_title}"
        f"{_subtitle_extension_info(title, stream)}{_stream_flags(stream)}"
    )


def _print_stream_group(
    title: Title,
    stream_type: StreamType,
    streams: list[Stream],
    label: str,
) -> None:
    if not streams:
        return
    print(f"[{label}]")
    for stream in streams:
        if stream_type == StreamType.VIDEO:
            print(_video_stream_line(stream))
        else:
            print(_non_video_stream_line(title, stream))
    print()


def display_title_details(title: Title) -> None:
    width = get_terminal_width()
    print(
        f"\n{'═' * min(width, 60)}\n  "
        + tr("Title {idx}: {name}", idx=title.index, name=title.name)
        + f"\n{'═' * min(width, 60)}\n"
        + tr("Source: {name}", name=title.source_file.name)
        + "\n"
        + tr("Duration: {dur}", dur=title.duration_display)
        + "\n"
    )
    stream_groups = [
        (StreamType.VIDEO, title.video_streams, "VIDEO"),
        (StreamType.AUDIO, title.audio_streams, "AUDIO"),
        (StreamType.SUBTITLE, title.subtitle_streams, "SUBS"),
    ]
    for stream_type, streams, label in stream_groups:
        _print_stream_group(title, stream_type, streams, label)


# =============================================================================
# CLI & Interactive
# =============================================================================
@dataclass(frozen=True)
class _InteractiveTagState:
    tag_from_flag: bool
    offer_tag: bool
    art_from_flag: str | None
    options: TagOptions = field(default_factory=lambda: RUNTIME_STATE.tag_options)

    @classmethod
    def from_options(cls, opts: TagOptions) -> "_InteractiveTagState":
        from tagger import _resolve_tmdb_key

        return cls(
            tag_from_flag=opts.enabled,
            offer_tag=(not opts.no_tag) and bool(_resolve_tmdb_key(opts)),
            art_from_flag=opts.art,
            options=opts,
        )

    def announce(self) -> None:
        if self.offer_tag and not self.tag_from_flag:
            print(tr("TMDB tagging available (key found) -- you'll be asked per rip."))

    def prepare_for_rip(self) -> None:
        """Decide per-rip whether to tag and which artwork to attach."""
        from tagger import _prompt_art_choice, _tag_confirm

        if self.tag_from_flag:
            want = True
        elif self.offer_tag:
            want = _tag_confirm(tr("Look up & tag this rip on TMDB?"))
        else:
            want = False
        self.options.enabled = want
        if want and self.art_from_flag is None:
            self.options.art = _prompt_art_choice()


def _interactive_edition_groups(titles: list[Title], debug: bool) -> list[list[Title]]:
    edition_groups: list[list[Title]] = []
    if not debug:
        return edition_groups

    from scan import _detect_edition_groups

    edition_groups = _detect_edition_groups(titles)
    for group in edition_groups:
        indices = ", ".join(str(titles.index(title)) for title in group)
        print(
            tr(
                "Titles {idxs} look like editions of the same movie - "
                "combine them with: me {idxs}",
                idxs=indices,
            )
        )
    return edition_groups


@final
class _InteractiveRipper:
    def __init__(
        self,
        titles: list[Title],
        creator: MKVCreator,
        tagging: _InteractiveTagState,
        edition_groups: list[list[Title]],
        debug: bool,
    ):
        self.titles = titles
        self.creator = creator
        self.tagging = tagging
        self.edition_groups = edition_groups
        self.debug = debug

    def rip_index(self, idx: int, stream_ids: list[str] | None = None) -> None:
        if not 0 <= idx < len(self.titles):
            log_warn(tr("Invalid: {idx}", idx=idx))
            return
        self.tagging.prepare_for_rip()
        try:
            self.creator.create_mkv(
                self.titles[idx],
                self.creator.select_streams(self.titles[idx], stream_ids),
            )
        except RipError as exc:
            print(exc.format_verbose())

    def multi_edition_indices(self, args: list[str]) -> list[int] | None:
        if not args:
            if not self.edition_groups:
                log_error(tr("No edition groups detected; specify titles: me N N ..."))
                return None
            best = max(
                self.edition_groups,
                key=lambda group: sum(t.duration_seconds for t in group),
            )
            indices = [self.titles.index(title) for title in best]
            log_info(
                tr(
                    "Using detected edition group: {idxs}",
                    idxs=", ".join(str(index) for index in indices),
                )
            )
            return indices

        indices: list[int] = []
        for token in args:
            try:
                indices.append(int(token))
            except ValueError:
                log_error(tr("Num required"))
                return None
        return indices

    def rip_multi_edition(self, indices: list[int]) -> None:
        if len(indices) < 2:
            log_error(tr("Multi-edition needs at least two titles"))
            return
        try:
            combined = _prepare_multi_edition(self.titles, indices)
        except ValueError as exc:
            log_error(str(exc))
            return
        names = _prompt_edition_names(self.titles, indices)
        if names != _default_edition_names(self.titles, indices):
            combined = _prepare_multi_edition(self.titles, indices, names)
        self.tagging.prepare_for_rip()
        try:
            self.creator.create_mkv(combined, self.creator.select_streams(combined))
        except RipError as exc:
            print(exc.format_verbose())

    def rip_collection(self, selected_titles: list[Title]) -> tuple[int, int]:
        self.tagging.prepare_for_rip()
        ok = failed = 0
        for title in selected_titles:
            try:
                self.creator.create_mkv(title)
                ok += 1
            except RipError as exc:
                print(exc.format_verbose())
                failed += 1
        return ok, failed

    def _handle_rip_command(self, command: str, args: list[str]) -> None:
        if command == "r":
            if not args:
                log_error(tr("Usage: r N  (e.g. 'r 1')"))
                return
            target, stream_args = args[0], args[1:]
        else:
            target, stream_args = command[1:], args
        try:
            idx = int(target)
        except ValueError:
            log_error(tr("Num required"))
            return
        self.rip_index(idx, stream_args if stream_args else None)

    def _handle_main_feature(self) -> None:
        idx = pick_main_feature(self.titles, self.creator.config)
        if idx < 0:
            log_warn(tr("No titles"))
            return
        log_info(
            tr(
                "Main feature: #{idx} {name}",
                idx=idx,
                name=self.titles[idx].name,
            )
        )
        self.rip_index(idx)

    def _handle_all(self) -> None:
        ok, failed = self.rip_collection(self.titles)
        print(tr("\nDone: {ok} ok, {fail} failed", ok=ok, fail=failed))

    def _handle_episodes(self) -> None:
        episode_titles = [
            title for title in self.titles if title.dvd_episode_number is not None
        ]
        if not episode_titles:
            log_warn(tr("No episodes detected on this disc"))
            return
        log_info(tr("Ripping {n} episode(s)...", n=len(episode_titles)))
        ok, failed = self.rip_collection(episode_titles)
        print(tr("\nDone: {ok} ok, {fail} failed", ok=ok, fail=failed))

    def _dispatch(self, command: str, args: list[str]) -> bool:
        if command in ("q", "quit", "exit"):
            log_info(tr("Goodbye!"))
            return False
        if command.isdigit():
            idx = int(command)
            if 0 <= idx < len(self.titles):
                display_title_details(self.titles[idx])
            else:
                log_warn(tr("Invalid: {idx}", idx=idx))
        elif command == "r" or (command.startswith("r") and command[1:].isdigit()):
            self._handle_rip_command(command, args)
        elif command == "rm":
            self._handle_main_feature()
        elif command == "me" and self.debug:
            indices = self.multi_edition_indices(args)
            if indices:
                self.rip_multi_edition(indices)
        elif command == "ra":
            self._handle_all()
        elif command == "re":
            self._handle_episodes()
        else:
            log_warn(tr("Unknown: {cmd}", cmd=command))
        return True

    def _print_prompt(self, has_episodes: bool) -> None:
        if has_episodes:
            print(
                tr(
                    "[n]=details  r N=rip title N  re=rip all episodes  "
                    "ra=rip all  q=quit"
                )
            )
        else:
            print(
                tr("[n]=details  r N=rip title N  rm=main feature  ra=rip all  q=quit")
            )
        if self.edition_groups:
            print(tr("me N N ...=multi-edition rip (no args = auto-detect)"))

    def run(self) -> None:
        has_episodes = any(
            title.dvd_episode_number is not None for title in self.titles
        )
        while True:
            self._print_prompt(has_episodes)
            try:
                command_line = input("mkvsmith> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not command_line:
                continue
            parts = command_line.split()
            if not self._dispatch(parts[0], parts[1:]):
                return


def interactive_mode(
    titles: list[Title],
    disc_name: str | None = None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    display_titles(titles, disc_name, state.config)
    creator = MKVCreator(
        state.config.output_dir,
        state.tag_options,
        runtime_state=state,
    )
    tagging = _InteractiveTagState.from_options(state.tag_options)
    tagging.announce()
    print()
    _InteractiveRipper(
        titles,
        creator,
        tagging,
        _interactive_edition_groups(titles, state.logger.debug_enabled),
        state.logger.debug_enabled,
    ).run()


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=tr("MakeMKV-like ripper using mkvmerge (MKVToolNix) + 7z")
    )
    p.add_argument("source", type=Path, nargs="?")
    p.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=Path("."),
        help=tr("output directory (default: current directory)"),
    )
    p.add_argument("-t", "--title", type=int)
    p.add_argument(
        "-m",
        "--main",
        action="store_true",
        help=tr("rip only the detected main feature"),
    )
    p.add_argument("-a", "--all", action="store_true")
    p.add_argument(
        "-e",
        "--episodes",
        action="store_true",
        help=tr("rip all detected TV-series episodes"),
    )
    p.add_argument(
        "--multi-edition",
        metavar="N,N,...",
        default=None,
        help=argparse.SUPPRESS,  # experimental; hidden behind --debug
    )
    p.add_argument("-i", "--info", action="store_true")
    p.add_argument("-d", "--details", type=int)
    p.add_argument("-s", "--streams", nargs="+")
    p.add_argument("-l", "--lang", default="eng,en,und")
    p.add_argument("--all-audio", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--no-subs", action="store_true")
    p.add_argument("--no-forced", action="store_true")
    p.add_argument("--min-duration", type=float, default=60.0)
    p.add_argument(
        "--show-all",
        action="store_true",
        help=tr("show all titles including low-quality ones (menus, trailers, etc.)"),
    )
    p.add_argument("--debug", action="store_true")
    p.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help=tr(
            "directory for temporary files (default: system temp dir, often /tmp/tmpfs). "
        )
        + tr("Set to a disk-backed path when ripping large ISOs to avoid filling RAM."),
    )
    p.add_argument(
        "--ram-limit",
        type=float,
        default=0.8,
        metavar="FRAC",
        help=tr(
            "max fraction of installed RAM that RAM-backed (tmpfs) temp dirs may "
            "use before spilling to disk (default: 0.8). 0 disables the check."
        ),
    )
    p.add_argument(
        "--no-sudo",
        action="store_true",
        help=tr("skip all sudo-based ISO mounting (loop mount, etc.)"),
    )
    p.add_argument(
        "--no-tag",
        action="store_true",
        help=tr("do not tag, even in interactive mode when a TMDB key is available"),
    )
    p.add_argument(
        "--tag",
        action="store_true",
        help=tr("fetch TMDB metadata and tag each rip during muxing"),
    )
    p.add_argument(
        "--tmdb-key",
        help=tr("TMDB API key (or set TMDB_API_KEY, or store with --save-key)"),
    )
    p.add_argument(
        "--save-key",
        metavar="KEY",
        default=None,
        help=tr("store the TMDB API key to the config file and exit"),
    )
    p.add_argument(
        "--tag-metadata",
        nargs="+",
        metavar="PROP",
        help=tr("metadata properties to fetch (default: a sensible set)"),
    )
    p.add_argument(
        "--tag-region",
        default="US",
        help=tr("ISO 3166-1 region for content rating (default: US)"),
    )
    p.add_argument(
        "--tag-language",
        default=None,
        help=tr("TMDB language code for localized metadata (e.g. en, ja, fr)"),
    )
    p.add_argument(
        "--tag-art",
        choices=["poster", "backdrop", "both"],
        default=None,
        help=tr("download and embed cover art from TMDB into the MKV"),
    )
    p.add_argument(
        "--save-tag-xml",
        action="store_true",
        help=tr("keep the XML tag file after muxing"),
    )
    p.add_argument(
        "--no-tag-confirm",
        action="store_true",
        help=tr("skip the per-rip tagging confirmation prompt"),
    )
    p.add_argument(
        "--tag-title",
        default=None,
        help=tr("override the movie title used for the TMDB search"),
    )
    p.add_argument(
        "--tag-year",
        type=int,
        default=None,
        help=tr("override the release year used for the TMDB search"),
    )
    p.add_argument("-v", "--version", action="version", version=__version__)
    p.add_argument(
        "--ui-lang",
        default=None,
        help="UI language code (e.g. en, es); overrides the settings file",
    )
    return p


def _apply_parsed_args(
    a: argparse.Namespace,
    runtime_state: RuntimeState | None = None,
) -> tuple[Path | None, list[str] | None, int | None, int | None]:
    state = runtime_state or RUNTIME_STATE
    config = state.config
    tag_options = state.tag_options
    src: Path | None = a.source
    sids: list[str] | None = a.streams
    details: int | None = a.details
    title_num: int | None = a.title
    config.output_dir = a.output
    config.preferred_languages = a.lang.split(",")
    config.keep_all_audio = a.all_audio
    config.keep_all_subtitles = not a.no_subs
    config.include_forced = not a.no_forced
    config.min_duration = a.min_duration
    config.debug = a.debug
    config.temp_dir = a.temp_dir
    config.ram_limit = a.ram_limit
    config.no_sudo = a.no_sudo
    config.show_all = a.show_all
    config.ui_lang = a.ui_lang
    state.logger.configure(config)
    tag_options.enabled = a.tag
    tag_options.no_tag = a.no_tag
    tag_options.api_key = a.tmdb_key
    tag_options.metadata = (
        a.tag_metadata if a.tag_metadata else list(DEFAULT_TAG_METADATA)
    )
    tag_options.region = a.tag_region
    tag_options.language = a.tag_language
    tag_options.art = a.tag_art
    tag_options.save_xml = a.save_tag_xml
    tag_options.confirm = not a.no_tag_confirm
    tag_options.title_override = a.tag_title
    tag_options.year_override = a.tag_year
    return src, sids, details, title_num


def _save_tmdb_key_and_exit(api_key: str) -> None:
    from settings import SETTINGS_PATH, load_settings, save_settings

    cfg = load_settings()
    cfg["api_key"] = api_key
    try:
        save_settings(cfg)
        log_info(f"TMDB API key saved to {SETTINGS_PATH}")
    except OSError as e:
        log_error(tr("Could not write config: {err}", err=e))
        sys.exit(1)
    sys.exit(0)


def _select_action(
    a: argparse.Namespace,
    src: Path | None,
    sids: list[str] | None,
    details: int | None,
    title_num: int | None,
) -> tuple[Path | None, str, int | None, list[str] | None]:
    if details is not None:
        return src, "details", details, sids
    if a.main:
        return src, "rip_main", None, sids
    if title_num is not None:
        return src, "rip_title", title_num, sids
    if a.all:
        return src, "rip_all", None, sids
    if a.episodes:
        return src, "rip_episodes", None, sids
    if a.info:
        return src, "info", None, sids
    return src, "interactive", None, sids


def parse_args(
    runtime_state: RuntimeState | None = None,
) -> tuple[Path | None, str, int | None, list[str] | None, list[int] | None]:
    p = _build_arg_parser()
    a = p.parse_args()
    src, sids, details, title_num = _apply_parsed_args(a, runtime_state)

    # Persist the TMDB API key and exit (no ripping tools needed for this).
    if a.save_key:
        _save_tmdb_key_and_exit(a.save_key)

    if a.multi_edition:
        if not (runtime_state or RUNTIME_STATE).logger.debug_enabled:
            log_error(tr("--multi-edition is experimental; pass --debug to enable it"))
            sys.exit(1)
        try:
            me_idx = [int(x) for x in a.multi_edition.split(",") if x.strip()]
        except ValueError:
            log_error(tr("--multi-edition expects comma-separated title numbers"))
            sys.exit(1)
        if len(me_idx) < 2:
            log_error(tr("--multi-edition needs at least two titles"))
            sys.exit(1)
        return src, "rip_multi_edition", None, sids, me_idx
    source, action, number, selected_streams = _select_action(
        a, src, sids, details, title_num
    )
    return source, action, number, selected_streams, None


# =============================================================================
# UI language resolution + first-run setup
# =============================================================================


def _peek_ui_lang_flag() -> str | None:
    """Pre-scan argv for --ui-lang so --help can be translated.

    argparse itself reads the flag later; this only resolves the language early
    enough for help text / the first-run wizard. Handles both '--ui-lang es' and
    '--ui-lang=es' forms.
    """
    argv = sys.argv[1:]
    for i, tok in enumerate(argv):
        if tok == "--ui-lang" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--ui-lang="):
            return tok.split("=", 1)[1]
    return None


def _init_ui_language() -> str:
    """Resolve and activate the UI language.

    Priority: --ui-lang flag > settings file > LC_MESSAGES/LANG env > English.
    """
    flag = _peek_ui_lang_flag()
    if flag:
        return set_language(flag)
    cfg = load_settings()
    lang = cfg.get("language")
    if lang:
        return set_language(lang)
    env_lang = detect_locale_language()
    if env_lang:
        return set_language(env_lang)
    return set_language("en")


def _first_run_setup() -> None:
    """Interactive first-run wizard: pick language, optionally add TMDB key."""
    print(tr("First-time setup"))
    print("=" * 40)

    # Language selection.
    print(tr("Select language / Seleccione el idioma:"))
    langs = available_languages()
    for n, (_code, name) in enumerate(langs, 1):
        print(tr("  {n}. {name}", n=n, name=name))
    raw = input(tr("Choice") + " [1]: ").strip() or "1"
    try:
        idx = int(raw) - 1
    except ValueError:
        idx = 0
    if 0 <= idx < len(langs):
        chosen = langs[idx][0]
    else:
        chosen = "en"
    set_language(chosen)

    cfg: dict[str, object] = {"language": chosen}

    # Optional TMDB key.
    print()
    if _confirm(
        tr("Would you like to add a TMDB API key now? (optional, enables tagging)")
    ):
        key = input(tr("Enter TMDB API key (or press Enter to skip):") + " ").strip()
        if key:
            cfg["api_key"] = key

    try:
        save_settings(cfg)
        print()
        log_info(tr("Setup complete. Settings saved to {path}", path=SETTINGS_PATH))
    except OSError as e:
        log_error(tr("Could not write config: {err}", err=e))


def _confirm(prompt: str) -> bool:
    """Tiny y/N confirmation (local helper to avoid importing tagger early)."""
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _prepare_multi_edition(
    titles: list[Title], indices: list[int], names: list[str] | None = None
) -> Title:
    """Validate title indices and build the combined multi-edition Title."""
    from scan import build_multi_edition_title

    if len(indices) < 2:
        raise ValueError(tr("Multi-edition needs at least two titles"))
    for idx in indices:
        if not 0 <= idx < len(titles):
            raise ValueError(tr("Invalid: {idx}", idx=idx))
    edition_titles = [titles[i] for i in indices]
    if len({t.source_file for t in edition_titles}) > 1:
        raise ValueError(tr("Multi-edition titles must come from the same source disc"))
    combined = build_multi_edition_title(edition_titles, names)
    for n, ed in enumerate(combined.editions, start=1):
        log_info(
            tr(
                "Edition {n}/{total}: {name} ({dur})",
                n=n,
                total=len(combined.editions),
                name=ed.name,
                dur=_fmt_edition_duration(ed.duration),
            )
        )
    log_info(
        tr(
            "Combining {n} editions into one multi-edition MKV "
            "(requires a player with ordered-chapters support)",
            n=len(combined.editions),
        )
    )
    return combined


def _default_edition_names(titles: list[Title], indices: list[int]) -> list[str]:
    """Default edition labels: movie name first, playlist names after."""
    names: list[str] = []
    for pos, idx in enumerate(indices):
        t = titles[idx]
        if pos == 0:
            names.append(t.disc_name or t.name)
        else:
            names.append(f"Playlist {t.playlist_name}" if t.playlist_name else t.name)
    return names


def _prompt_edition_names(titles: list[Title], indices: list[int]) -> list[str]:
    """Prompt for edition display names with sensible defaults.

    Callers must validate *indices* first (see ``_prepare_multi_edition``).
    """
    defaults = _default_edition_names(titles, indices)
    print(
        tr(
            "Name each edition (shown by players in the edition picker)."
            " Press Enter to accept the default."
        )
    )
    names: list[str] = []
    for pos, idx in enumerate(indices):
        try:
            raw = input(
                tr("Edition {n} (title {idx}) name", n=pos + 1, idx=idx)
                + f" [{defaults[pos]}]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raw = ""
        names.append(raw or defaults[pos])
    return names


def _rip_multi_edition(
    creator: MKVCreator,
    titles: list[Title],
    indices: list[int],
    names: list[str] | None,
    streams: list[str] | None = None,
) -> None:
    """Build the combined title and rip it."""
    combined = _prepare_multi_edition(titles, indices, names)
    creator.create_mkv(combined, creator.select_streams(combined, streams))


def _fmt_edition_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _initialize_cli(
    runtime_state: RuntimeState | None = None,
) -> tuple[Path | None, str, int | None, list[str] | None, list[int] | None]:
    _init_ui_language()
    if not _HAS_MKVMERGE:
        log_error(tr("Missing: mkvmerge (install mkvtoolnix)"))
        sys.exit(1)

    quick_exit = any(
        argument in sys.argv for argument in ("-h", "--help", "-v", "--version")
    )
    saving_key = "--save-key" in sys.argv
    if not SETTINGS_PATH.exists() and not quick_exit and not saving_key:
        _first_run_setup()
        _init_ui_language()

    state = runtime_state or RUNTIME_STATE
    parsed = parse_args(state)
    if state.config.ui_lang:
        set_language(state.config.ui_lang)
    log_debug(
        tr(
            "Using language: {name} ({code})",
            name=language_name(get_language()),
            code=get_language(),
        )
    )
    return parsed


def _configure_runtime(runtime_state: RuntimeState | None = None) -> None:
    state = runtime_state or RUNTIME_STATE
    config = state.config
    state.logger.configure(config)
    dvdifo.set_debug(state.logger.debug)
    if config.temp_dir:
        config.temp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(config.temp_dir)

    from disc_reader import init_ram_budget

    init_ram_budget(config)


def _scan_source(
    source: Path, runtime_state: RuntimeState | None = None
) -> tuple[list[Title], str | None]:
    if not source.exists() and not str(source).startswith("/dev/"):
        log_error(tr("Not found: {path}", path=source))
        sys.exit(1)
    scanner = Scanner(source, runtime_state=runtime_state)
    titles = scanner.scan()
    if not titles:
        log_warn(tr("No titles found"))
        sys.exit(0)
    return titles, scanner.disc_name


def _run_main_feature_rip(
    titles: list[Title],
    stream_ids: list[str] | None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    index = pick_main_feature(titles, state.config)
    if index < 0:
        log_warn(tr("No titles found"))
        sys.exit(0)
    log_info(
        tr(
            "Main feature: #{idx} {name} ({dur})",
            idx=index,
            name=titles[index].name,
            dur=titles[index].duration_display,
        )
    )
    try:
        creator = MKVCreator(
            state.config.output_dir,
            state.tag_options,
            runtime_state=state,
        )
        creator.create_mkv(
            titles[index],
            creator.select_streams(titles[index], stream_ids),
        )
    except RipError as exc:
        print(exc.format_verbose())
        sys.exit(1)


def _rip_title_batch(
    titles: list[Title], runtime_state: RuntimeState | None = None
) -> None:
    state = runtime_state or RUNTIME_STATE
    ok = failed = 0
    creator = MKVCreator(
        state.config.output_dir,
        state.tag_options,
        runtime_state=state,
    )
    for title in titles:
        try:
            creator.create_mkv(title)
            ok += 1
        except RipError as exc:
            print(exc.format_verbose())
            failed += 1
    print(tr("\nSummary: {ok} ok, {fail} failed", ok=ok, fail=failed))


def _require_title_index(action: str, number: int | None, titles: list[Title]) -> int:
    if number is None or not 0 <= number < len(titles):
        log_error(tr("Invalid title for {action}: {idx}", action=action, idx=number))
        sys.exit(1)
    return number


def _show_action_info(
    titles: list[Title], disc_name: str | None, state: RuntimeState
) -> None:
    display_titles(titles, disc_name, state.config)


def _show_action_details(titles: list[Title], number: int | None, action: str) -> None:
    index = _require_title_index(action, number, titles)
    display_title_details(titles[index])


def _rip_selected_title(
    titles: list[Title],
    number: int | None,
    stream_ids: list[str] | None,
    state: RuntimeState,
) -> None:
    index = _require_title_index("rip_title", number, titles)
    try:
        creator = MKVCreator(
            state.config.output_dir,
            state.tag_options,
            runtime_state=state,
        )
        creator.create_mkv(
            titles[index], creator.select_streams(titles[index], stream_ids)
        )
    except RipError as exc:
        print(exc.format_verbose())
        sys.exit(1)


def _rip_selected_editions(
    titles: list[Title],
    edition_indices: list[int] | None,
    stream_ids: list[str] | None,
    state: RuntimeState,
) -> None:
    if not edition_indices:
        log_error(tr("No multi-edition title indexes supplied"))
        sys.exit(1)

    try:
        _rip_multi_edition(
            MKVCreator(
                state.config.output_dir,
                state.tag_options,
                runtime_state=state,
            ),
            titles,
            edition_indices,
            None,
            stream_ids,
        )
    except RipError as exc:
        print(exc.format_verbose())
        sys.exit(1)
    except ValueError as exc:
        log_error(str(exc))
        sys.exit(1)


def _rip_episode_batch(titles: list[Title], state: RuntimeState) -> None:
    episode_titles = [title for title in titles if title.dvd_episode_number is not None]
    if not episode_titles:
        log_warn(tr("No episodes detected on this disc"))
        sys.exit(0)
    log_info(tr("Ripping {n} episode(s)...", n=len(episode_titles)))
    _rip_title_batch(episode_titles, state)


def _reject_unknown_action(action: str) -> None:
    log_error(tr("Unknown action: {action}", action=action))
    sys.exit(1)


def _run_action(
    action: str,
    titles: list[Title],
    disc_name: str | None,
    number: int | None,
    stream_ids: list[str] | None,
    edition_indices: list[int] | None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    if action == "info":
        _show_action_info(titles, disc_name, state)
    elif action == "details":
        _show_action_details(titles, number, action)
    elif action == "rip_title":
        _rip_selected_title(titles, number, stream_ids, state)
    elif action == "rip_main":
        _run_main_feature_rip(titles, stream_ids, state)
    elif action == "rip_multi_edition":
        _rip_selected_editions(titles, edition_indices, stream_ids, state)
    elif action == "rip_all":
        _rip_title_batch(titles, state)
    elif action == "rip_episodes":
        _rip_episode_batch(titles, state)
    elif action == "interactive":
        interactive_mode(titles, disc_name, state)
    else:
        _reject_unknown_action(action)


def main():
    runtime_state = RUNTIME_STATE
    source, action, number, stream_ids, edition_indices = _initialize_cli(runtime_state)
    _configure_runtime(runtime_state)
    if source is None:
        log_error(tr("A source path is required"))
        log_error(tr("Run with -h to see usage, e.g. script.py /path/to/media"))
        sys.exit(1)

    titles, disc_name = _scan_source(source, runtime_state)
    _run_action(
        action,
        titles,
        disc_name,
        number,
        stream_ids,
        edition_indices,
        runtime_state,
    )
