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
import json
import os
import re
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

import dvdifo
from models import (
    Config,
    DiscMetadata,
    DiscDbOptions,
    RuntimeState,
    TagOptions,
    RUNTIME_STATE,
    UserPrompts,
    Stream,
    StreamType,
    Title,
    RipError,
    log_info,
    log_warn,
    log_error,
    log_debug,
    _HAS_MKVMERGE,
    MKVSMITH_VERSION,
    sweep_stale_session_dirs,
)
from i18n import (
    tr,
    set_language,
    get_language,
    language_name,
    available_languages,
    detect_locale_language,
)
from settings import (
    ASK_MODES,
    DISCDB_CONTRIBUTE_MODES,
    TAG_ART_CHOICES,
    LoadedSettings,
    Settings,
    SettingSpec,
    SETTING_SPECS,
    accept_advanced_defaults,
    format_value,
    get_setting,
    load_settings,
    missing_settings,
    reset_setting,
    save_settings,
    set_setting,
    setting_spec,
    settings_path,
)
from scan import Scanner, _detect_edition_groups, _get_notable_titles, pick_main_feature
from mkv import MKVCreator
from discdb import DiscDbError

__version__ = MKVSMITH_VERSION


# =============================================================================
# Display & UI
# =============================================================================


def get_terminal_width() -> int:
    try:
        return max(shutil.get_terminal_size().columns, 40)
    except OSError:
        return 80


def _truncate_display_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 2:
        return text[:width]
    return text[: width - 2].rstrip() + ".."


def _series_list_base(
    titles: list[Title], disc_metadata: DiscMetadata | None
) -> str | None:
    """A series disc's shared name base ("Show - S01D01"), else None.

    The title list shows it once in its header and drops it from each row,
    so narrow screens see "Episode 3" rather than a truncated prefix.
    """
    from episode_naming import series_name

    if disc_metadata is None or not disc_metadata.name:
        return None
    if not any(t.is_episode or t.play_all or t.packed_segments for t in titles):
        return None
    return series_name(disc_metadata.series_info, disc_metadata.name)


def _title_list_name(title: Title, width: int, base: str | None = None) -> str:
    """*title*'s name for the list, without the series base (see above)."""
    name = title.name
    if base and name.startswith(base + " - "):
        name = name[len(base) + 3 :]
    base_name = re.sub(
        r"\s+-\s+Blu-ray(?:\s*3D)?(?:\s*[™℠])?\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    base_name = base_name or name
    return _truncate_display_text(base_name, width)


def _is_episode_title(title: Title) -> bool:
    return title.is_episode


def display_titles(
    titles: list[Title],
    disc_metadata: DiscMetadata | None = None,
    config: Config | None = None,
) -> None:
    w = get_terminal_width()
    visible, hidden = _get_notable_titles(titles, config)
    main_idx = pick_main_feature(titles, config)
    # A series disc has no "main feature": episodes are peers, and the
    # scoring would land the star on an arbitrary episode or even a bonus
    # title (Superman 1988's star went to a 13-minute featurette). Mark
    # every episode instead; the star stays for movie discs.
    series_disc = any(_is_episode_title(title) for title in titles)
    index_width = max([1, *(len(str(title.index)) for title in visible)])
    summary_width = max(
        [len("Streams"), *(len(title.streams_summary) for title in visible)]
    )
    playlists = [title.playlist_name for title in visible if title.playlist_name]
    show_playlist = bool(playlists)
    hdr_playlist = tr("PL")
    playlist_width = max([len(hdr_playlist), *(len(value) for value in playlists)])
    full_playlist_header = tr("Playlist")
    full_playlist_width = max(playlist_width, len(full_playlist_header))
    if (
        show_playlist
        and w - summary_width - index_width - full_playlist_width - 18 >= 5
    ):
        hdr_playlist = full_playlist_header
        playlist_width = full_playlist_width
    if show_playlist:
        nw = max(w - summary_width - index_width - playlist_width - 18, 1)
    else:
        nw = max(w - summary_width - index_width - 16, 8)
    rule_w = w
    series_base = _series_list_base(titles, disc_metadata)
    disc_name = series_base or (disc_metadata.name if disc_metadata else None)
    print("\n" + "═" * rule_w)
    print(tr("  SCANNED TITLES") + (f" - {disc_name}" if disc_name else ""))
    for identifier_line in _disc_identifier_lines(disc_metadata):
        print(identifier_line)
    print("═" * rule_w)
    hdr_dur = tr("Dur")
    hdr_name = tr("Name")
    hdr_streams = tr("Streams")
    playlist_header = f"{hdr_playlist:<{playlist_width}}  " if show_playlist else ""
    print(
        f"{'#':>{index_width}}  {hdr_dur:<8}  {hdr_name:<{nw}}  "
        f"{playlist_header}{hdr_streams}\n" + "─" * rule_w
    )
    for t in visible:
        n = _title_list_name(t, nw, series_base)
        playlist = t.playlist_name or ""
        playlist_value = f"{playlist:<{playlist_width}}  " if show_playlist else ""
        if series_disc:
            is_episode = _is_episode_title(t) and not t.play_all
            marker = " \u25cb" if is_episode else ""
        else:
            marker = " \u2605" if t.index == main_idx else ""
        print(
            f"{t.index:>{index_width}}  {t.duration_display:<8}  {n:<{nw}}  "
            f"{playlist_value}{t.streams_summary}{marker}"
        )
    total_msg = tr("Total: {n} title(s)", n=len(visible))
    ep_count = sum(1 for t in titles if _is_episode_title(t))
    if ep_count:
        total_msg += "  " + tr("({n} episode(s) detected)", n=ep_count)
    if hidden:
        total_msg += "  " + tr(
            "({n} low-quality titles hidden; use --show-all to view)", n=hidden
        )
    total_msg += (
        "  " + tr("\u25cb = episode")
        if series_disc
        else "  " + tr("\u2605 = main feature")
    )
    print("═" * rule_w + f"\n{total_msg}\n")


def _disc_identifier_lines(metadata: DiscMetadata | None) -> list[str]:
    if metadata is None:
        return []
    identifiers: list[str] = []
    if metadata.matrix256_fingerprint:
        identifiers.append(
            tr(
                "Fingerprint: {value}",
                value=metadata.matrix256_fingerprint,
            )
        )
    if metadata.disc_hash:
        identifiers.append(tr("Disc Hash: {value}", value=metadata.disc_hash))
    return [f"  {identifier}" for identifier in identifiers]


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
    not_muxed = "" if stream.is_muxable else f" {tr('(sub-path, not muxed)')}"
    return (
        f"  {stream.display_id:<5} {stream.codec_display:<14} {channels:<6} "
        f"{stream.language_display}{stream_title}"
        f"{_subtitle_extension_info(title, stream)}{_stream_flags(stream)}"
        f"{not_muxed}"
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
    prompts: UserPrompts | None = None

    @classmethod
    def from_options(
        cls, opts: TagOptions, prompts: UserPrompts | None = None
    ) -> "_InteractiveTagState":
        from tagger import _resolve_tmdb_key

        return cls(
            tag_from_flag=opts.enabled,
            offer_tag=(not opts.no_tag) and bool(_resolve_tmdb_key(opts)),
            art_from_flag=opts.art,
            options=opts,
            prompts=prompts,
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
            want = _tag_confirm(tr("Look up & tag this rip on TMDB?"), self.prompts)
        else:
            want = False
        self.options.enabled = want
        if want and self.art_from_flag is None:
            self.options.art = _prompt_art_choice(self.prompts)


def _print_packed_episode_hints(titles: list[Title], *, interactive: bool) -> None:
    """Point out playlists holding back-to-back episodes (split on request)."""
    from packed_episodes import packed_episode_count

    for title in titles:
        if not title.packed_segments:
            continue
        n = packed_episode_count(title)
        if interactive:
            print(
                tr(
                    "Title {idx} holds {n} episodes in one playlist - "
                    "split it with: se {idx}",
                    idx=title.index,
                    n=n,
                )
            )
        else:
            print(
                tr(
                    "Title {idx} holds {n} episodes in one playlist - "
                    "split it with --split-episodes",
                    idx=title.index,
                    n=n,
                )
            )


def _interactive_edition_groups(titles: list[Title]) -> list[list[Title]]:
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
        disc_metadata: DiscMetadata | None = None,
    ):
        self.titles = titles
        self.creator = creator
        self.tagging = tagging
        self.edition_groups = edition_groups
        self.disc_metadata = disc_metadata

    def ask_output_dir(self) -> None:
        """Ask where this session's rips go (once, before the first rip)."""
        config = self.creator.config
        if not config.ask_output_dir:
            return
        prompts = self.creator.prompts
        while True:
            raw = prompts.text(tr("Output folder"), str(self.creator.out))
            path = Path(raw).expanduser()
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log_warn(tr("Cannot use {path}: {err}", path=path, err=e))
                continue
            break
        self.creator.out = config.output_dir = path
        config.ask_output_dir = False

    def offer_packed_split(self) -> None:
        """Ask, per packed playlist, whether to split it into episodes."""
        from packed_episodes import packed_episode_count

        prompts = self.creator.prompts
        chosen = [
            str(title.index)
            for title in self.titles
            if title.packed_segments
            and prompts.confirm(
                tr(
                    "Title {idx} holds {n} episodes in one playlist. Split it "
                    "into one title per episode? [y/N]",
                    idx=title.index,
                    n=packed_episode_count(title),
                )
            )
        ]
        if chosen:
            self.split_packed_episodes(chosen)

    def settings_command(self, command: str, args: list[str]) -> None:
        """``settings`` lists, ``set KEY [VALUE]`` / ``reset KEY`` save.

        Saved changes are the defaults for the next run; this session keeps
        the options it started with.
        """
        state = self.creator.runtime_state
        if command == "settings":
            print("\n".join(_settings_lines(state.settings, settings_path())))
            return
        if not args:
            log_warn(tr("Usage: {cmd} KEY", cmd=command))
            return
        if command == "set":
            key, value = args[0], " ".join(args[1:])
            sets, resets = [f"{key}={value}" if value else key], []
        else:
            sets, resets = [], args
        changed = _save_settings_changes(sets, resets, self.creator.prompts)
        if changed is not None:
            state.settings = changed
            log_info(tr("Takes effect from the next run."))

    def _before_rip(self) -> None:
        self.ask_output_dir()
        self.tagging.prepare_for_rip()

    def split_packed_episodes(self, args: list[str]) -> None:
        """Split title N (or every packed playlist) into one title per episode."""
        from packed_episodes import expand_packed_titles

        packed = [t.index for t in self.titles if t.packed_segments]
        if not packed:
            log_warn(tr("No playlist on this disc holds packed episodes"))
            return
        indices: list[int] = []
        for arg in args:
            if not arg.isdigit() or int(arg) not in packed:
                log_warn(tr("Title {idx} holds no packed episodes", idx=arg))
                return
            indices.append(int(arg))
        series = self.disc_metadata.series_info if self.disc_metadata else None
        self.titles[:] = expand_packed_titles(self.titles, indices or None, series)
        self.creator.runtime_state.refresh_series_disc(self.titles)
        display_titles(self.titles, self.disc_metadata, self.creator.config)

    def rip_index(self, idx: int, stream_ids: list[str] | None = None) -> None:
        if not 0 <= idx < len(self.titles):
            log_warn(tr("Invalid: {idx}", idx=idx))
            return
        self._before_rip()
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
        self._before_rip()
        try:
            self.creator.create_mkv(combined, self.creator.select_streams(combined))
        except RipError as exc:
            print(exc.format_verbose())

    def rip_collection(self, selected_titles: list[Title]) -> tuple[int, int]:
        self._before_rip()
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
        # A series disc has no single main feature; ripping "the feature"
        # means the episodes.
        if any(_is_episode_title(title) for title in self.titles):
            log_info(tr("Series disc: no single main feature; ripping all episodes"))
            self._handle_episodes()
            return
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
        episode_titles = [title for title in self.titles if _is_episode_title(title)]
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
        elif command == "rm" or command == "re":
            if command == "re":
                log_warn(tr("re is deprecated; use rm (episodes on series discs)"))
            self._handle_main_feature()
        elif command == "me":
            indices = self.multi_edition_indices(args)
            if indices:
                self.rip_multi_edition(indices)
        elif command == "ra":
            self._handle_all()
        elif command == "se":
            self.split_packed_episodes(args)
        elif command in ("settings", "set", "reset"):
            self.settings_command(command, args)
        else:
            log_warn(tr("Unknown: {cmd}", cmd=command))
        return True

    def _print_prompt(self, has_episodes: bool) -> None:
        if has_episodes:
            print(
                tr("[n]=details  r N=rip title N  rm=rip episodes  ra=rip all  q=quit")
            )
        else:
            print(
                tr("[n]=details  r N=rip title N  rm=main feature  ra=rip all  q=quit")
            )
        if self.edition_groups:
            print(tr("me N N ...=multi-edition rip (no args = auto-detect)"))
        else:
            print(tr("me N N ...=multi-edition rip"))
        if any(title.packed_segments for title in self.titles):
            print(tr("se [N]=split packed episodes into one title each"))
        print(tr("settings  set KEY [VALUE]  reset KEY=saved settings"))

    def run(self) -> None:
        while True:
            # Re-checked each round: "se" can turn a packed playlist into episodes.
            self._print_prompt(any(_is_episode_title(t) for t in self.titles))
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
    disc_metadata: DiscMetadata | None = None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    _ask_closed_captions(titles, state)
    display_titles(titles, disc_metadata, state.config)
    creator = MKVCreator(
        state.config.output_dir,
        state.tag_options,
        runtime_state=state,
    )
    tagging = _InteractiveTagState.from_options(state.tag_options, state.prompts)
    ripper = _InteractiveRipper(
        titles,
        creator,
        tagging,
        _interactive_edition_groups(titles),
        disc_metadata,
    )
    if state.config.ask_split_episodes:
        ripper.offer_packed_split()
    else:
        _print_packed_episode_hints(titles, interactive=True)
    tagging.announce()
    print()
    ripper.run()


def _ask_closed_captions(titles: list[Title], state: RuntimeState) -> None:
    """Closed captions saved as "ask": keep this disc's captions, or not?"""
    from dvdbuild import drop_closed_caption_streams, has_closed_captions

    if not state.config.ask_closed_captions or not has_closed_captions(titles):
        return
    if not state.prompts.confirm(
        tr("This disc has closed captions. Add them as a subtitle track? [y/N]")
    ):
        drop_closed_caption_streams(titles)


def _comma_list(text: str) -> list[str]:
    """argparse type: "a,b,c" -> ["a", "b", "c"].

    Lists are a single argument so they can never swallow the source path
    that follows them. Commas and/or spaces separate items (no list value
    contains a space), so the quoted "a b" and "a, b" work too.
    """
    items = text.replace(",", " ").split()
    if not items:
        raise argparse.ArgumentTypeError(tr("expected a comma-separated list"))
    return items


def _index_list(text: str) -> list[int]:
    """argparse type: "1,3,5" -> [1, 3, 5]."""
    try:
        return [int(item) for item in _comma_list(text)]
    except ValueError:
        raise argparse.ArgumentTypeError(
            tr("expected comma-separated title numbers")
        ) from None


class _HelpAllAction(argparse.Action):
    """--help-all: print the full help (every option) and exit."""

    def __init__(
        self, option_strings: list[str], dest: str, help: str | None = None
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, help=help)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        _build_arg_parser(full=True).print_help()
        parser.exit()


def _help_description(prog: str) -> str:
    """The text under the usage line: what the tool is, then examples."""
    examples = [
        (f'{prog} "/media/My Disc.iso"', tr("open the interactive prompt")),
        (
            f'{prog} "/media/My Disc.iso" -m ~/rips',
            tr("rip the main feature to ~/rips"),
        ),
        (
            f"{prog} disc.iso -t 1,3 -l jpn,eng",
            tr("rip titles 1 and 3, Japanese then English"),
        ),
        (f"{prog} --set tmdb.tagging=never", tr("change a saved setting")),
    ]
    width = max(len(command) for command, _ in examples) + 3
    lines = [
        tr("DVD/Blu-ray ripper using mkvmerge (MKVToolNix)") + ".",
        tr("Run without an action to open the interactive prompt."),
        "",
        tr("examples:"),
        *(f"  {command:<{width}}{text}" for command, text in examples),
    ]
    return "\n".join(lines)


def _build_arg_parser(full: bool = False) -> argparse.ArgumentParser:
    """The command-line parser; *full* shows every option in --help.

    Both parsers accept the same options. ``-h`` shows the everyday ones;
    ``--help-all`` (``full``) adds tagging, TheDiscDB, temp files and the
    other advanced options.
    """

    def more(text: str) -> str:
        """Help shown only by --help-all."""
        return text if full else argparse.SUPPRESS

    p = argparse.ArgumentParser(
        usage=tr("%(prog)s [options] SOURCE [OUTPUT]"),
        epilog=None
        if full
        else tr(
            "More options (stream selection, captions, titles, TMDB tagging, "
            "TheDiscDB, temporary files): --help-all"
        ),
        # Keeps the examples' layout; option help is still wrapped.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    p.description = _help_description(p.prog)

    # --- Source and output ---------------------------------------------------
    io = p.add_argument_group(tr("Source and output"))
    io.add_argument(
        "source",
        type=Path,
        nargs="?",
        metavar=tr("SOURCE"),
        help=tr(
            "disc, folder, or image to read; quote paths with spaces, e.g. "
            '"/media/My Disc.iso"'
        ),
    )
    io.add_argument(
        "output",
        type=Path,
        nargs="?",
        metavar=tr("OUTPUT"),
        default=None,
        help=tr(
            "output directory (default: current directory), e.g. "
            '"/media/rips/New Movies"'
        ),
    )

    # --- Actions: one per run; none opens the interactive prompt -------------
    action_group = p.add_argument_group(
        tr("Actions (pick one; none opens the interactive prompt)")
    )
    actions = action_group.add_mutually_exclusive_group()
    actions.add_argument(
        "-t",
        "--title",
        type=_index_list,
        metavar="N[,N...]",
        help=tr('rip title N (or several: 1,3,5 or "1 3 5")'),
    )
    actions.add_argument(
        "-m",
        "--main",
        action="store_true",
        help=tr("rip the detected main feature (all episodes on series discs)"),
    )
    actions.add_argument(
        "-a", "--all", action="store_true", help=tr("rip every listed title")
    )
    actions.add_argument(
        "-i",
        "--info",
        action="store_true",
        help=tr("scan the disc and list its titles"),
    )
    actions.add_argument(
        "-d",
        "--details",
        type=int,
        metavar="N",
        help=tr("show title N's tracks and chapters"),
    )
    actions.add_argument(
        "--multi-edition",
        type=_index_list,
        metavar="N,N,...",
        default=None,
        help=more(
            tr('combine playlist titles into one multi-edition MKV, e.g. 1,2 or "1 2"')
        ),
    )
    actions.add_argument(
        "--settings",
        action="store_true",
        help=tr("show the saved settings and exit"),
    )

    # Flags left unset (None) fall back to the settings file (settings.py).
    # --- Tracks --------------------------------------------------------------
    tracks = p.add_argument_group(tr("Tracks"))
    tracks.add_argument(
        "-l",
        "--languages",
        dest="lang",
        type=_comma_list,
        default=None,
        metavar="LANG[,LANG...]",
        help=tr(
            'preferred languages, most preferred first, e.g. jpn,eng or "jpn '
            'eng": keeps their subtitles and marks the default audio track'
        ),
    )
    tracks.add_argument("--lang", dest="lang", type=_comma_list, help=argparse.SUPPRESS)
    tracks.add_argument(
        "-s",
        "--streams",
        type=_comma_list,
        metavar="SEL[,SEL...]",
        help=more(tr('streams to rip, e.g. v:0,a:eng,s:all or "v:0 a:eng s:all"')),
    )
    tracks.add_argument(
        "--all-audio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=tr("keep audio in every language (--no-all-audio: only --languages)"),
    )
    tracks.add_argument(
        "--subs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=tr("keep subtitle tracks"),
    )
    tracks.add_argument(
        "--all-subs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("keep subtitles in every language, not only the preferred ones")),
    )
    tracks.add_argument(
        "--forced",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("keep forced subtitle tracks")),
    )
    tracks.add_argument(
        "--cc",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(
            tr(
                "extract EIA-608 closed captions as a text subtitle track "
                "(format: --cc-format)"
            )
        ),
    )
    # Old spelling, from when captions were SRT only.
    tracks.add_argument(
        "--cc-srt",
        dest="cc",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=argparse.SUPPRESS,
    )
    tracks.add_argument(
        "--cc-format",
        choices=["srt", "ass"],
        default=None,
        help=more(
            tr(
                "closed-caption sidecar format: srt (portable plain text) or "
                "ass (preserves speaker positioning and italics)"
            )
        ),
    )

    # --- Titles --------------------------------------------------------------
    titles = p.add_argument_group(tr("Titles"))
    titles.add_argument(
        "--min-duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help=more(tr("hide titles shorter than this many seconds (default: 60)")),
    )
    titles.add_argument(
        "--show-all",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(
            tr("show all titles including low-quality ones (menus, trailers, etc.)")
        ),
    )
    titles.add_argument(
        "--split-episodes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(
            tr(
                "split playlists holding several back-to-back episodes into one "
                "title per episode"
            )
        ),
    )

    # --- Output files --------------------------------------------------------
    output = p.add_argument_group(tr("Output files"))
    output.add_argument(
        "--overwrite",
        choices=list(ASK_MODES),
        default=None,
        help=tr(
            "an output file that already exists: ask first, always overwrite, "
            "or never (skip the title)"
        ),
    )
    output.add_argument(
        "--force",
        dest="overwrite",
        action="store_const",
        const="always",
        help=more(tr("same as --overwrite always")),
    )

    # --- TMDB tagging --------------------------------------------------------
    tagging = p.add_argument_group(tr("TMDB tagging"))
    tagging.add_argument(
        "--tag",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=tr("fetch TMDB metadata and tag each rip during muxing"),
    )
    tagging.add_argument(
        "--tag-art",
        choices=list(TAG_ART_CHOICES),
        default=None,
        help=more(
            tr(
                "cover art to embed from TMDB (ask: the interactive prompt asks "
                "per rip)"
            )
        ),
    )
    tagging.add_argument(
        "--tag-confirm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("confirm the TMDB match before tagging each rip")),
    )
    tagging.add_argument(
        "--tag-title",
        default=None,
        metavar="TITLE",
        help=more(
            tr('override the movie title used for the TMDB search, e.g. "The Matrix"')
        ),
    )
    tagging.add_argument(
        "--tag-year",
        type=int,
        default=None,
        metavar="YEAR",
        help=more(tr("override the release year used for the TMDB search")),
    )
    tagging.add_argument(
        "--tag-metadata",
        type=_comma_list,
        metavar="PROP[,PROP...]",
        help=more(
            tr(
                "metadata properties to fetch (default: a sensible set), e.g. "
                'Title,Overview or "Title Overview"'
            )
        ),
    )
    tagging.add_argument(
        "--tag-region",
        default=None,
        metavar="REGION",
        help=more(tr("ISO 3166-1 region for content rating (default: US)")),
    )
    tagging.add_argument(
        "--tag-language",
        default=None,
        metavar="LANG",
        help=more(tr("TMDB language code for localized metadata (e.g. en, ja, fr)")),
    )
    tagging.add_argument(
        "--save-tag-xml",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("keep the XML tag file after muxing")),
    )
    tagging.add_argument(
        "--tmdb-key",
        metavar="KEY",
        help=more(
            tr(
                "TMDB API key for this run (visible to other users; prefer "
                "TMDB_API_KEY or --set tmdb.api_key)"
            )
        ),
    )
    # Old way to store the key; --set tmdb.api_key replaces it.
    tagging.add_argument(
        "--save-key", metavar="KEY", default=None, help=argparse.SUPPRESS
    )

    # --- TheDiscDB -----------------------------------------------------------
    discdb = p.add_argument_group(tr("TheDiscDB"))
    discdb.add_argument(
        "--discdb",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("query TheDiscDB and automatically apply a unique disc match")),
    )
    discdb.add_argument(
        "--discdb-contribute",
        choices=list(DISCDB_CONTRIBUTE_MODES),
        default=None,
        metavar="MODE",
        help=more(
            tr(
                "write a TheDiscDB contribution bundle: browser, manual, "
                "authenticated direct, or off"
            )
        ),
    )
    discdb.add_argument(
        "--discdb-bundle-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=more(
            tr(
                "output directory for TheDiscDB contribution files, e.g. "
                '"/media/rips/DiscDB bundles"'
            )
        ),
    )
    discdb.add_argument(
        "--discdb-open",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=more(tr("open TheDiscDB in a browser after preparing a contribution")),
    )
    discdb.add_argument(
        "--discdb-contribution-id",
        default=None,
        metavar="ID",
        help=more(tr("existing TheDiscDB contribution ID for browser/direct handoff")),
    )
    discdb.add_argument(
        "--discdb-disc-name",
        default=None,
        metavar="NAME",
        help=more(
            tr(
                "disc name for a direct TheDiscDB contribution (default: Disc 1), "
                'e.g. "Bonus Disc"'
            )
        ),
    )
    discdb.add_argument(
        "--discdb-cookie",
        default=None,
        metavar="COOKIE",
        help=more(
            tr(
                "authenticated TheDiscDB browser cookie (or set THEDISCDB_COOKIE); "
                'quote it, e.g. "name=value; other=value"'
            )
        ),
    )
    discdb.add_argument(
        "--discdb-url",
        default=None,
        metavar="URL",
        help=more(tr("TheDiscDB base URL (or set THEDISCDB_BASE_URL)")),
    )
    discdb.add_argument(
        "--discdb-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=more(tr("TheDiscDB network timeout in seconds")),
    )

    # --- Temporary files -----------------------------------------------------
    temp = p.add_argument_group(tr("Temporary files"))
    temp.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=more(
            tr(
                "directory for temporary files (default: /var/tmp when usable, "
                "else system temp). "
            )
            + tr(
                "Set explicitly to use tmpfs/RAM (see --ram-limit) or another "
                'disk path, e.g. "/mnt/big disk/tmp".'
            )
        ),
    )
    temp.add_argument(
        "--ram-limit",
        type=float,
        default=None,
        metavar="FRAC",
        help=more(
            tr(
                "max fraction of RAM-backed (tmpfs) temp capacity that extractions "
                "may use before spilling to disk (default: 0.8). 0 disables the "
                "check."
            )
        ),
    )

    # --- Settings ------------------------------------------------------------
    saved = p.add_argument_group(
        tr("Saved settings (defaults for every run; list them with --settings)")
    )
    saved.add_argument(
        "--set",
        dest="set_settings",
        action="append",
        default=[],
        metavar="KEY[=VALUE]",
        help=tr(
            'save a setting and exit, e.g. tmdb.tagging=never or "temp.dir=/mnt/'
            'big disk/tmp"; with no value it asks (secrets typed hidden)'
        ),
    )
    saved.add_argument(
        "--reset",
        dest="reset_settings",
        action="append",
        default=[],
        metavar="KEY",
        help=tr("put a saved setting back to its default and exit"),
    )

    # --- Other ---------------------------------------------------------------
    other = p.add_argument_group(tr("Other"))
    other.add_argument(
        "-h", "--help", action="help", help=tr("show the common options and exit")
    )
    other.add_argument(
        "--help-all", action=_HelpAllAction, help=tr("show every option and exit")
    )
    other.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
        help=tr("show the version and exit"),
    )
    other.add_argument(
        "--ui-lang",
        default=None,
        metavar="LANG",
        help=more(tr("UI language code (e.g. en, es); overrides the settings file")),
    )
    other.add_argument(
        "--debug",
        action="store_true",
        default=None,
        help=more(tr("print verbose debug output")),
    )
    return p


def _pick[T](flag: T | None, saved: T) -> T:
    """A flag's value when given on the command line, else the saved setting."""
    return saved if flag is None else flag


def _resolve_discdb_options(a: argparse.Namespace, saved: Settings) -> DiscDbOptions:
    """TheDiscDB options: flag > environment > settings file > default."""
    mode = _pick(a.discdb_contribute, saved.discdb_contribute)
    contribute_mode = None if mode == "off" else mode
    timeout = _pick(a.discdb_timeout, saved.discdb_timeout)
    if timeout <= 0:
        timeout = DiscDbOptions().timeout_seconds
    return DiscDbOptions(
        enabled=_pick(a.discdb, saved.discdb_enabled),
        base_url=(
            a.discdb_url
            or os.environ.get("THEDISCDB_BASE_URL")
            or saved.discdb_base_url
        ),
        timeout_seconds=timeout,
        contribute=contribute_mode is not None,
        contribute_mode=contribute_mode or DiscDbOptions().contribute_mode,
        bundle_dir=a.discdb_bundle_dir,
        open_browser=_pick(a.discdb_open, saved.discdb_open_browser),
        contribution_id=a.discdb_contribution_id,
        disc_name=a.discdb_disc_name.strip() if a.discdb_disc_name else None,
        cookie=(a.discdb_cookie or os.environ.get("THEDISCDB_COOKIE") or "").strip()
        or None,
    )


def _tagging_mode(a: argparse.Namespace, saved: Settings) -> str:
    """never / ask / always: --tag / --no-tag, else the saved setting."""
    if a.tag is None:
        return saved.tagging
    return "always" if a.tag else "never"


def _per_disc(flag: bool | None, saved: str, interactive: bool) -> tuple[bool, bool]:
    """(on, ask) for a never/ask/always setting a flag can override.

    "ask" asks only in the interactive prompt; a plain CLI run takes the
    built-in default ("never") instead.
    """
    if flag is not None:
        return flag, False
    if saved == "ask":
        return False, interactive
    return saved == "always", False


def _has_action(a: argparse.Namespace) -> bool:
    """Whether *a* names an action (-t, -m, -a, -i, -d, ...)."""
    return bool(
        a.details is not None
        or a.main
        or a.title is not None
        or a.all
        or a.info
        or a.multi_edition
        or a.settings
    )


def _is_interactive_run(a: argparse.Namespace) -> bool:
    """Whether *a* opens the interactive prompt (no action flag given)."""
    return not (_has_action(a) or a.save_key or a.set_settings or a.reset_settings)


def _apply_parsed_args(
    a: argparse.Namespace,
    runtime_state: RuntimeState | None = None,
    interactive: bool = False,
) -> tuple[Path | None, list[str] | None, int | None, int | None]:
    """Fill the run's options from *a*, falling back to the saved settings.

    Every option resolves the same way: flag > environment variable (TMDB
    key, TheDiscDB URL/cookie) > settings file (``runtime_state.settings``)
    > built-in default. Settings saved as "ask" become questions only when
    *interactive* (the interactive prompt); a plain CLI run never asks.
    """
    state = runtime_state or RUNTIME_STATE
    saved = state.settings
    config = state.config
    tag_options = state.tag_options
    src: Path | None = a.source
    sids: list[str] | None = a.streams
    details: int | None = a.details
    title_num: int | None = a.title[0] if a.title else None
    # Not a setting: the CLI writes to the current directory unless told
    # otherwise; the interactive prompt asks before its first rip.
    config.output_dir = a.output or Path(".")
    config.ask_output_dir = interactive and a.output is None
    config.preferred_languages = list(a.lang or saved.languages)
    config.keep_all_audio = _pick(a.all_audio, saved.all_audio)
    config.keep_all_subtitles = _pick(a.subs, saved.subtitles)
    config.all_subtitle_languages = _pick(a.all_subs, saved.all_subtitles)
    config.include_forced = _pick(a.forced, saved.forced_subtitles)
    config.min_duration = _pick(a.min_duration, saved.min_duration)
    config.debug = bool(a.debug)
    config.temp_dir = _pick(a.temp_dir, saved.temp_dir)
    config.ram_limit = _pick(a.ram_limit, saved.ram_limit)
    config.overwrite = _pick(a.overwrite, saved.overwrite)
    config.show_all = _pick(a.show_all, saved.show_all)
    config.split_episodes, config.ask_split_episodes = _per_disc(
        a.split_episodes, saved.split_episodes, interactive
    )
    # Asking still detects captions during the scan (on=True), so discs
    # without any skip the question.
    cc_on, config.ask_closed_captions = _per_disc(
        a.cc, saved.closed_captions, interactive
    )
    config.extract_cc608 = cc_on or config.ask_closed_captions
    config.cc608_format = _pick(a.cc_format, saved.cc_format)
    config.ui_lang = a.ui_lang
    state.discdb_options = _resolve_discdb_options(a, saved)
    state.logger.configure(config)
    # "ask" tags only from the interactive prompt, which asks per rip; a
    # plain CLI run tags only when the mode is "always".
    mode = _tagging_mode(a, saved)
    tag_options.enabled = mode == "always"
    tag_options.no_tag = mode == "never"
    tag_options.api_key = (
        a.tmdb_key or os.environ.get("TMDB_API_KEY") or saved.tmdb_api_key
    )
    tag_options.metadata = list(a.tag_metadata or saved.tag_metadata)
    tag_options.region = a.tag_region or saved.tag_region
    tag_options.language = a.tag_language or saved.tag_language
    # The saved "ask" leaves art unset: the interactive prompt asks per rip.
    art = _pick(a.tag_art, saved.tag_art)
    tag_options.art = None if art == "ask" else art
    tag_options.save_xml = _pick(a.save_tag_xml, saved.tag_save_xml)
    tag_options.confirm = _pick(a.tag_confirm, saved.tag_confirm_match)
    tag_options.title_override = a.tag_title
    tag_options.year_override = a.tag_year
    return src, sids, details, title_num


def _settings_lines(saved: Settings, path: Path) -> list[str]:
    """The settings listing for --settings / the ``settings`` command."""
    lines = [tr("Settings file: {path}", path=path)]
    section = ""
    for spec in SETTING_SPECS:
        if spec.section != section:
            section = spec.section
            lines.append(f"[{section}]")
        value = format_value(spec, getattr(saved, spec.attr)) or "-"
        if spec.key not in saved.answered:
            value += " " + tr("(default, not chosen yet)")
        lines.append(f"  {spec.name} = {value}    # {tr(spec.description)}")
    return lines


def _show_settings() -> None:
    loaded = load_settings()
    for problem in loaded.problems:
        log_warn(tr("Settings: {problem}", problem=problem))
    print("\n".join(_settings_lines(loaded.settings, loaded.path)))
    sys.exit(0)


def _parse_assignment(
    assignment: str, saved: Settings, prompts: UserPrompts
) -> tuple[str, str]:
    """ "key=value" -> (key, value); a bare "key" asks for the value.

    Secrets are typed hidden, so they stay out of the shell history and the
    process list.
    """
    key, separator, value = assignment.partition("=")
    key = key.strip()
    if separator:
        return key, value
    spec = setting_spec(key)
    question = tr(spec.description)
    if spec.secret:
        return key, prompts.secret(question)
    current = format_value(spec, getattr(saved, spec.attr))
    return key, prompts.text(question, current or None)


def change_settings(
    saved: Settings, assignments: list[tuple[str, str]], resets: list[str]
) -> Settings:
    """*saved* with each (key, value) set and each key reset.

    Raises ``KeyError`` (unknown key) or ``ValueError`` (bad value) before
    anything is changed, so a typo saves nothing.
    """
    for key, value in assignments:
        saved = set_setting(saved, key, value)
    for key in resets:
        saved = reset_setting(saved, key)
    return saved


def _save_settings_changes(
    sets: list[str], resets: list[str], prompts: UserPrompts
) -> Settings | None:
    """Apply "key[=value]" *sets* and *resets* to the file and save it.

    Shared by --set/--reset and the interactive ``set``/``reset`` commands.
    Reports what changed; returns the new settings, or None (after
    reporting why) when nothing was saved.
    """
    loaded = load_settings()
    if loaded.unreadable:
        log_error(tr("Could not write config: {err}", err="; ".join(loaded.problems)))
        return None
    try:
        assignments = [
            _parse_assignment(item, loaded.settings, prompts) for item in sets
        ]
        changed = change_settings(loaded.settings, assignments, resets)
    except KeyError as e:
        log_error(tr("{err} (see --settings for every key)", err=e.args[0]))
        return None
    except ValueError as e:
        log_error(tr("Invalid value: {err}", err=e))
        return None
    try:
        path = save_settings(changed, loaded.path)
    except OSError as e:
        log_error(tr("Could not write config: {err}", err=e))
        return None
    for key, _value in assignments:
        log_info(
            tr(
                "{key} = {value}",
                key=key,
                value=format_value(setting_spec(key), get_setting(changed, key)),
            )
        )
    for key in resets:
        log_info(tr("{key} reset to its default", key=key))
    log_info(tr("Settings saved to {path}", path=path))
    return changed


def _run_settings_changes(sets: list[str], resets: list[str]) -> None:
    """--set / --reset: change the saved settings and exit."""
    changed = _save_settings_changes(sets, resets, RUNTIME_STATE.prompts)
    sys.exit(0 if changed is not None else 1)


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
    if a.info:
        return src, "info", None, sids
    return src, "interactive", None, sids


def parse_args(
    runtime_state: RuntimeState | None = None,
    before_apply: Callable[[bool], None] | None = None,
) -> tuple[Path | None, str, int | None, list[str] | None, list[int] | None]:
    """Parse argv into the run's action, with options resolved into state.

    *before_apply* is called with whether the run is interactive, before
    the options are resolved (the startup settings check hooks in here).
    """
    p = _build_arg_parser()
    a = p.parse_args()

    # Settings commands exit here (no ripping tools needed for them).
    if a.save_key:
        a.set_settings.append(f"tmdb.api_key={a.save_key}")
    if a.set_settings or a.reset_settings:
        if _has_action(a):
            p.error(tr("--set/--reset can't be combined with an action"))
        _run_settings_changes(a.set_settings, a.reset_settings)
    if a.settings:
        _show_settings()

    interactive = _is_interactive_run(a)
    if before_apply is not None:
        before_apply(interactive)
    src, sids, details, title_num = _apply_parsed_args(a, runtime_state, interactive)

    me_idx: list[int] | None = a.multi_edition
    if me_idx is not None:
        if len(me_idx) < 2:
            log_error(tr("--multi-edition needs at least two titles"))
            sys.exit(1)
        return src, "rip_multi_edition", None, sids, me_idx
    source, action, number, selected_streams = _select_action(
        a, src, sids, details, title_num
    )
    # -t 1,3,5: every index rides along (number is the first).
    title_indices: list[int] | None = a.title
    return source, action, number, selected_streams, title_indices


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


def _init_ui_language(saved: Settings) -> str:
    """Resolve and activate the UI language.

    Priority: --ui-lang flag > settings file > LC_MESSAGES/LANG env > English.
    """
    flag = _peek_ui_lang_flag()
    if flag:
        return set_language(flag)
    if saved.ui_language:
        return set_language(saved.ui_language)
    env_lang = detect_locale_language()
    if env_lang:
        return set_language(env_lang)
    return set_language("en")


# Choice values as shown when asking (the saved value stays the English word).
_CHOICE_LABELS = {
    "never": "never",
    "ask": "ask every time",
    "always": "always",
    "none": "none",
    "poster": "poster",
    "backdrop": "backdrop",
    "both": "poster and backdrop",
    "srt": "srt",
    "ass": "ass",
}
# Spanish yes/no answers, accepted alongside the English ones.
_YES_NO_ALIASES = {"s": "yes", "si": "yes", "sí": "yes"}


def _read_answer(
    spec: SettingSpec, saved: Settings, prompts: UserPrompts
) -> Settings | None:
    """One answer for *spec*, or None when it doesn't parse (ask again)."""
    current = getattr(saved, spec.attr)
    if spec.kind == "language":
        langs = available_languages()
        for n, (_code, name) in enumerate(langs, 1):
            print(tr("  {n}. {name}", n=n, name=name))
        codes = [code for code, _name in langs]
        default = codes.index(get_language()) + 1 if get_language() in codes else 1
        raw = prompts.text(tr("Choice"), str(default))
        if not raw.isdigit() or not 1 <= int(raw) <= len(codes):
            return None
        set_language(codes[int(raw) - 1])
        return set_setting(saved, spec.key, codes[int(raw) - 1])
    if spec.kind == "choice":
        for n, choice in enumerate(spec.choices, 1):
            print(f"  {n}. {tr(_CHOICE_LABELS.get(choice, choice))}")
        raw = prompts.text(tr("Choice"), str(spec.choices.index(current) + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(spec.choices):
            raw = spec.choices[int(raw) - 1]
    elif spec.kind == "bool":
        raw = prompts.text(tr("yes/no"), tr("yes") if current else tr("no"))
        raw = _YES_NO_ALIASES.get(raw.strip().lower(), raw)
    elif spec.secret:
        raw = prompts.secret(tr("Value (Enter to skip)"))
    else:
        raw = prompts.text(tr("Value"), format_value(spec, current) or None)
    try:
        return set_setting(saved, spec.key, raw)
    except ValueError as e:
        log_warn(tr("Invalid value: {err}", err=e))
        return None


def _ask_setting(spec: SettingSpec, saved: Settings, prompts: UserPrompts) -> Settings:
    """Ask for *spec* until the answer is valid; returns *saved* with it."""
    print()
    print(tr(spec.description))
    while (answered := _read_answer(spec, saved, prompts)) is None:
        pass
    return answered


def _complete_settings(
    loaded: LoadedSettings, prompts: UserPrompts | None = None
) -> Settings:
    """Ask for every everyday setting the file is missing, then save.

    Everything on a first run; only the new settings after an update. The
    answers are saved together with the defaults of any advanced settings
    the file is missing, so the file is complete. An unreadable file is
    left alone (its problem was already reported).
    """
    saved = loaded.settings
    if loaded.unreadable:
        return saved
    prompts = prompts or RUNTIME_STATE.prompts
    pending = missing_settings(saved)
    asked = bool(pending)
    if pending:
        if loaded.exists:
            print(
                tr(
                    "{n} new setting(s) to choose since your last run",
                    n=len(pending),
                )
            )
        else:
            print(tr("First-time setup"))
        print("=" * 40)
        print(tr("Press Enter to keep the suggested value."))
        while pending:
            saved = _ask_setting(pending[0], saved, prompts)
            pending = missing_settings(saved)
    saved = accept_advanced_defaults(saved)
    if saved.answered == loaded.settings.answered:
        return saved
    try:
        path = save_settings(saved, loaded.path)
    except OSError as e:
        log_error(tr("Could not write config: {err}", err=e))
        return saved
    if asked:
        print()
        log_info(tr("Settings saved to {path}", path=path))
    return saved


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
    """Default edition labels: uniform Edition 1/2/... numbering."""
    return [tr("Edition {n}", n=pos + 1) for pos in range(len(indices))]


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
    state = runtime_state or RUNTIME_STATE
    loaded = load_settings()
    state.settings = loaded.settings
    _init_ui_language(state.settings)
    for problem in loaded.problems:
        log_warn(tr("Settings: {problem}", problem=problem))
    if not _HAS_MKVMERGE:
        log_error(tr("Missing: mkvmerge (install mkvtoolnix)"))
        sys.exit(1)

    def check_settings(interactive: bool) -> None:
        # Only the interactive prompt asks (and only at a terminal); plain
        # CLI runs and scripts use the built-in default for anything missing.
        if interactive and sys.stdin.isatty():
            state.settings = _complete_settings(loaded, state.prompts)
            _init_ui_language(state.settings)

    parsed = parse_args(state, check_settings)
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
    from disc_reader import default_temp_dir, init_ram_budget, temp_base_candidates

    if config.temp_dir:
        config.temp_dir.mkdir(parents=True, exist_ok=True)
        base = config.temp_dir
    else:
        # Default to disk-backed /var/tmp (see disc_reader.default_temp_dir)
        # so temp files never silently land on a RAM-backed /tmp.
        base = default_temp_dir()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path(tempfile.gettempdir())

    _sweep_stale_temp(temp_base_candidates(config))
    # All of this run's temp files live in one marked session dir, so a run
    # killed before cleanup can be swept by the next one.
    try:
        tempfile.tempdir = str(state.cleanup.session_dir(base))
    except OSError as exc:
        log_debug(f"Could not create session temp dir in {base}: {exc}")
        tempfile.tempdir = str(base)

    init_ram_budget(config)


def _sweep_stale_temp(bases: list[Path]) -> None:
    removed = sweep_stale_session_dirs(bases)
    if not removed:
        return
    log_info(
        tr(
            "Removed {count} leftover temp folder(s) ({gb:.1f} GB) from interrupted runs.",
            count=len(removed),
            gb=sum(size for _path, size in removed) / 1e9,
        )
    )
    for path, _size in removed:
        log_debug(f"Removed stale session dir {path}")


def _apply_discdb_lookup(
    titles: list[Title], disc_metadata: DiscMetadata | None, state: RuntimeState
) -> None:
    from discdb import DiscDbClient, DiscDbError, apply_discdb_match

    if disc_metadata is None:
        return
    try:
        result = DiscDbClient(state.discdb_options).lookup(disc_metadata)
        applied = apply_discdb_match(titles, disc_metadata, result)
    except DiscDbError as exc:
        log_warn(tr("TheDiscDB lookup failed (using local metadata): {err}", err=exc))
        return
    except (KeyError, TypeError, ValueError) as exc:
        log_warn(
            tr(
                "TheDiscDB returned unusable match data; local metadata retained: {err}",
                err=exc,
            )
        )
        return

    if not result.items:
        log_info(tr("No TheDiscDB match found"))
        return
    if result.ambiguous:
        log_warn(tr("Multiple TheDiscDB disc layouts matched; local metadata retained"))
        return

    if applied is None:
        if result.match is None:
            log_warn(tr("TheDiscDB results were ambiguous; local metadata retained"))
        else:
            log_warn(
                tr(
                    "TheDiscDB disc matched, but no local title correlated uniquely; "
                    "local metadata retained"
                )
            )
        return

    media_title = applied.media_item["title"]
    release_title = applied.release.get("title") or media_title
    log_info(
        tr(
            "TheDiscDB match: {media} ({release}); applied {count} title name(s)",
            media=media_title,
            release=release_title,
            count=len(applied.title_indexes),
        )
    )
    # A match can label titles as episodes.
    state.refresh_series_disc(titles)


def _prepare_discdb_contribution(
    source: Path,
    titles: list[Title],
    disc_metadata: DiscMetadata | None,
    state: RuntimeState,
) -> None:
    from discdb import (
        build_contribution_bundle,
        contribution_url,
        open_contribution_url,
        submit_contribution_bundle,
    )

    metadata = disc_metadata or DiscMetadata()
    bundle_dir = build_contribution_bundle(
        source, titles, metadata, state.discdb_options
    )
    log_info(
        tr(
            "TheDiscDB contribution bundle written to {path}",
            path=bundle_dir,
        )
    )

    mode = state.discdb_options.contribute_mode
    if mode == "direct":
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        logs = (bundle_dir / "scan_log.txt").read_text(encoding="utf-8")
        _disc_id, review_url = submit_contribution_bundle(
            manifest, logs, state.discdb_options
        )
        log_info(tr("TheDiscDB contribution disc uploaded: {url}", url=review_url))
        if state.discdb_options.open_browser:
            webbrowser.open(review_url)
        return

    if mode == "browser":
        if open_contribution_url(state.discdb_options):
            log_info(tr("Opened TheDiscDB contribution flow in your browser"))
        else:
            log_warn(
                tr(
                    "Could not open a browser; continue at {url}",
                    url=contribution_url(state.discdb_options),
                )
            )


def _scan_source(
    source: Path, runtime_state: RuntimeState | None = None
) -> tuple[list[Title], DiscMetadata | None]:
    from disc_reader import _is_device_path

    if not source.exists() and not _is_device_path(source):
        log_error(tr("Not found: {path}", path=source))
        sys.exit(1)
    scanner = Scanner(source, runtime_state=runtime_state)
    titles = scanner.scan()
    if not titles:
        log_warn(tr("No titles found"))
        sys.exit(0)
    return titles, scanner.disc_metadata


def _run_main_feature_rip(
    titles: list[Title],
    stream_ids: list[str] | None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    # A series disc has no single main feature; -m (and deprecated -e)
    # means the episodes.
    if any(_is_episode_title(title) for title in titles):
        log_info(tr("Series disc: no single main feature; ripping all episodes"))
        _rip_episode_batch(titles, state)
        return
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
    titles: list[Title], disc_metadata: DiscMetadata | None, state: RuntimeState
) -> None:
    display_titles(titles, disc_metadata, state.config)
    _print_packed_episode_hints(titles, interactive=False)


def _show_action_details(titles: list[Title], number: int | None, action: str) -> None:
    index = _require_title_index(action, number, titles)
    display_title_details(titles[index])


def _rip_selected_titles(
    titles: list[Title],
    indices: list[int],
    stream_ids: list[str] | None,
    state: RuntimeState,
) -> None:
    """Rip each of *indices* (``-t 1,3,5``) with the selected streams.

    Every index is checked before anything is ripped. Exits 1 when any rip
    failed.
    """
    if not indices:
        _require_title_index("rip_title", None, titles)
    for index in indices:
        _require_title_index("rip_title", index, titles)
    creator = MKVCreator(
        state.config.output_dir,
        state.tag_options,
        runtime_state=state,
    )
    failed = 0
    for index in indices:
        try:
            creator.create_mkv(
                titles[index], creator.select_streams(titles[index], stream_ids)
            )
        except RipError as exc:
            print(exc.format_verbose())
            failed += 1
    if len(indices) > 1:
        print(
            tr(
                "\nSummary: {ok} ok, {fail} failed",
                ok=len(indices) - failed,
                fail=failed,
            )
        )
    if failed:
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
    episode_titles = [title for title in titles if _is_episode_title(title)]
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
    disc_metadata: DiscMetadata | None,
    number: int | None,
    stream_ids: list[str] | None,
    edition_indices: list[int] | None,
    runtime_state: RuntimeState | None = None,
) -> None:
    state = runtime_state or RUNTIME_STATE
    if action == "info":
        _show_action_info(titles, disc_metadata, state)
    elif action == "details":
        _show_action_details(titles, number, action)
    elif action == "rip_title":
        indices = edition_indices or ([number] if number is not None else [])
        _rip_selected_titles(titles, indices, stream_ids, state)
    elif action == "rip_main":
        _run_main_feature_rip(titles, stream_ids, state)
    elif action == "rip_multi_edition":
        _rip_selected_editions(titles, edition_indices, stream_ids, state)
    elif action == "rip_all":
        _rip_title_batch(titles, state)
    elif action == "interactive":
        interactive_mode(titles, disc_metadata, state)
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

    titles, disc_metadata = _scan_source(source, runtime_state)
    if runtime_state.discdb_options.enabled:
        _apply_discdb_lookup(titles, disc_metadata, runtime_state)

    if runtime_state.discdb_options.contribute:
        try:
            _prepare_discdb_contribution(source, titles, disc_metadata, runtime_state)
        except (DiscDbError, OSError, ValueError, json.JSONDecodeError) as exc:
            log_error(tr("TheDiscDB contribution failed: {err}", err=exc))
            if not action.startswith("rip_"):
                sys.exit(1)

        # Contribution preparation is a terminal action unless the user also
        # explicitly requested a rip. Interactive mode would otherwise look
        # like it is waiting for the browser flow to finish.
        if not action.startswith("rip_"):
            sys.exit(0)

    _run_action(
        action,
        titles,
        disc_metadata,
        number,
        stream_ids,
        edition_indices,
        runtime_state,
    )
