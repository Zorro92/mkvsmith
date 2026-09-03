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

import shutil
import sys
import tempfile
from pathlib import Path

import dvdifo
from models import (
    CONFIG,
    TAG_OPTS,
    DEFAULT_TAG_METADATA,
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
from mkv import MKVCreator, select_streams

__version__ = "0.1.0"  # keep in sync with pyproject.toml [project].version


# =============================================================================
# Display & UI
# =============================================================================


def get_terminal_width() -> int:
    try:
        return max(shutil.get_terminal_size().columns, 40)
    except Exception:
        return 80


def display_titles(titles: list[Title], disc_name: str | None = None) -> None:
    w = get_terminal_width()
    visible, hidden = _get_notable_titles(titles)
    main_idx = pick_main_feature(titles)
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


def display_title_details(title: Title) -> None:
    w = get_terminal_width()
    print(
        f"\n{'═' * min(w, 60)}\n  "
        + tr("Title {idx}: {name}", idx=title.index, name=title.name)
        + f"\n{'═' * min(w, 60)}\n"
        + tr("Source: {name}", name=title.source_file.name)
        + "\n"
        + tr("Duration: {dur}", dur=title.duration_display)
        + "\n"
    )
    for stype, streams, label in [
        (StreamType.VIDEO, title.video_streams, "VIDEO"),
        (StreamType.AUDIO, title.audio_streams, "AUDIO"),
        (StreamType.SUBTITLE, title.subtitle_streams, "SUBS"),
    ]:
        if not streams:
            continue
        print(f"[{label}]")
        for s in streams:
            flags = (
                " ["
                + ",".join(
                    ["DEF" if s.is_default else "", "FOR" if s.is_forced else ""]
                ).strip(",")
                + "]"
                if (s.is_default or s.is_forced)
                else ""
            )
            if stype == StreamType.VIDEO:
                print(
                    f"  {s.codec_display:<14} {f'{s.width}x{s.height}' if s.width else '?':<10} {s.language_display}{flags}"
                )
            else:
                # Append subpicture code extension label when meaningful.
                ext_info = ""
                if (
                    stype == StreamType.SUBTITLE
                    and s.sub_id is not None
                    and s.sub_id in title.dvd_subp_attrs
                ):
                    ext_label = title.dvd_subp_attrs[s.sub_id].code_extension_label
                    if ext_label not in ("", "unspecified", "normal"):
                        ext_info = f" ({ext_label})"
                print(
                    f"  {s.display_id:<5} {s.codec_display:<14} {f'{s.channels}ch' if s.channels else '-':<6} {s.language_display}{' - ' + s.title if s.title else ''}{ext_info}{flags}"
                )
        print()


# =============================================================================
# CLI & Interactive
# =============================================================================
def interactive_mode(titles: list[Title], disc_name: str | None = None) -> None:
    from tagger import (
        _prompt_art_choice,
        _resolve_tmdb_key,
        _tag_confirm,
    )

    display_titles(titles, disc_name)
    creator = MKVCreator(CONFIG.output_dir, TAG_OPTS)
    # Whether tagging was enabled with --tag (always tag, no prompt).
    tag_from_flag = TAG_OPTS.enabled
    # Whether to OFFER tagging per-rip: a key is available and not opted out.
    offer_tag = (not TAG_OPTS.no_tag) and bool(_resolve_tmdb_key(TAG_OPTS))
    # Whether artwork was set on the CLI (None == not set) -> prompt otherwise.
    art_from_flag = TAG_OPTS.art
    if offer_tag and not tag_from_flag:
        print(tr("TMDB tagging available (key found) -- you'll be asked per rip."))
    print()

    def prep_tag_for_rip() -> None:
        """Decide per-rip whether to tag (and which art) in interactive mode."""
        if tag_from_flag:
            want = True
        elif offer_tag:
            want = _tag_confirm(tr("Look up & tag this rip on TMDB?"))
        else:
            want = False
        TAG_OPTS.enabled = want
        if want and art_from_flag is None:
            TAG_OPTS.art = _prompt_art_choice()

    has_episodes = any(t.dvd_episode_number is not None for t in titles)

    # Seamless-branching edition-group hints are experimental; only detected
    # (and acted on) when --debug is set.
    edition_groups: list[list[Title]] = []
    if CONFIG.debug:
        from scan import _detect_edition_groups

        edition_groups = _detect_edition_groups(titles)
        for group in edition_groups:
            idxs = ", ".join(str(titles.index(t)) for t in group)
            print(
                tr(
                    "Titles {idxs} look like editions of the same movie - "
                    "combine them with: me {idxs}",
                    idxs=idxs,
                )
            )

    while True:
        if has_episodes:
            print(
                tr(
                    "[n]=details  r N=rip title N  re=rip all episodes  ra=rip all  q=quit"
                )
            )
        else:
            print(
                tr("[n]=details  r N=rip title N  rm=main feature  ra=rip all  q=quit")
            )
        if edition_groups:
            print(tr("me N N ...=multi-edition rip (no args = auto-detect)"))
        try:
            ci = input("mkvsmith> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not ci:
            continue
        p = ci.split()
        c, a = p[0], p[1:]
        if c in ("q", "quit", "exit"):
            log_info(tr("Goodbye!"))
            break
        elif c.isdigit():
            idx = int(c)
            if 0 <= idx < len(titles):
                display_title_details(titles[idx])
            else:
                log_warn(tr("Invalid: {idx}", idx=idx))
        elif c == "r" or (c.startswith("r") and c[1:].isdigit()):
            if c == "r":
                if not a:
                    log_error(tr("Usage: r N  (e.g. 'r 1')"))
                    continue
                num_str, rest = a[0], a[1:]
            else:
                num_str, rest = c[1:], a
            try:
                idx = int(num_str)
            except ValueError:
                log_error(tr("Num required"))
                continue
            if 0 <= idx < len(titles):
                prep_tag_for_rip()
                try:
                    creator.create_mkv(
                        titles[idx], select_streams(titles[idx], rest if rest else None)
                    )
                except RipError as e:
                    print(e.format_verbose())
            else:
                log_warn(tr("Invalid: {idx}", idx=idx))
        elif c == "rm":
            idx = pick_main_feature(titles)
            if idx < 0:
                log_warn(tr("No titles"))
                continue
            log_info(tr("Main feature: #{idx} {name}", idx=idx, name=titles[idx].name))
            prep_tag_for_rip()
            try:
                creator.create_mkv(titles[idx], select_streams(titles[idx]))
            except RipError as e:
                print(e.format_verbose())
        elif c == "me" and CONFIG.debug:
            if not a:
                if not edition_groups:
                    log_error(
                        tr("No edition groups detected; specify titles: me N N ...")
                    )
                    continue
                # Auto-detect: use the largest group.
                best = max(
                    edition_groups, key=lambda g: sum(t.duration_seconds for t in g)
                )
                indices = [titles.index(t) for t in best]
                log_info(
                    tr(
                        "Using detected edition group: {idxs}",
                        idxs=", ".join(str(i) for i in indices),
                    )
                )
            else:
                indices = []
                for tok in a:
                    try:
                        indices.append(int(tok))
                    except ValueError:
                        log_error(tr("Num required"))
                        indices = []
                        break
                if not indices:
                    continue
            if len(indices) < 2:
                log_error(tr("Multi-edition needs at least two titles"))
                continue
            # Validate and build with default names first so bad selections
            # fail before the user types anything.
            try:
                combined = _prepare_multi_edition(titles, indices)
            except ValueError as e:
                log_error(str(e))
                continue
            names = _prompt_edition_names(titles, indices)
            if names != _default_edition_names(titles, indices):
                combined = _prepare_multi_edition(titles, indices, names)
            prep_tag_for_rip()
            try:
                creator.create_mkv(combined, select_streams(combined))
            except RipError as e:
                print(e.format_verbose())
        elif c == "ra":
            prep_tag_for_rip()
            s, f = 0, 0
            for title in titles:
                try:
                    creator.create_mkv(title)
                    s += 1
                except RipError as e:
                    print(e.format_verbose())
                    f += 1
            print(tr("\nDone: {ok} ok, {fail} failed", ok=s, fail=f))
        elif c == "re":
            ep_titles = [t for t in titles if t.dvd_episode_number is not None]
            if not ep_titles:
                log_warn(tr("No episodes detected on this disc"))
                continue
            prep_tag_for_rip()
            log_info(tr("Ripping {n} episode(s)...", n=len(ep_titles)))
            s, f = 0, 0
            for ep in ep_titles:
                try:
                    creator.create_mkv(ep)
                    s += 1
                except RipError as e:
                    print(e.format_verbose())
                    f += 1
            print(tr("\nDone: {ok} ok, {fail} failed", ok=s, fail=f))
        else:
            log_warn(tr("Unknown: {cmd}", cmd=c))


def parse_args() -> tuple[
    Path | None, str, int | None, list[str] | None, list[int] | None
]:
    import argparse

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
    a = p.parse_args()
    # argparse attributes are untyped (Any); narrow them once so the return
    # statements below type-check soundly.
    src: Path | None = a.source
    sids: list[str] | None = a.streams
    details: int | None = a.details
    title_num: int | None = a.title
    CONFIG.output_dir, CONFIG.preferred_languages = a.output, a.lang.split(",")
    CONFIG.keep_all_audio, CONFIG.keep_all_subtitles = a.all_audio, not a.no_subs
    CONFIG.include_forced, CONFIG.min_duration, CONFIG.debug = (
        not a.no_forced,
        a.min_duration,
        a.debug,
    )
    CONFIG.temp_dir = a.temp_dir
    CONFIG.ram_limit = a.ram_limit
    CONFIG.no_sudo = a.no_sudo
    CONFIG.show_all = a.show_all
    CONFIG.ui_lang = a.ui_lang
    TAG_OPTS.enabled = a.tag
    TAG_OPTS.no_tag = a.no_tag
    TAG_OPTS.api_key = a.tmdb_key
    TAG_OPTS.metadata = a.tag_metadata if a.tag_metadata else list(DEFAULT_TAG_METADATA)
    TAG_OPTS.region = a.tag_region
    TAG_OPTS.language = a.tag_language
    TAG_OPTS.art = a.tag_art
    TAG_OPTS.save_xml = a.save_tag_xml
    TAG_OPTS.confirm = not a.no_tag_confirm
    TAG_OPTS.title_override = a.tag_title
    TAG_OPTS.year_override = a.tag_year

    # Persist the TMDB API key and exit (no ripping tools needed for this).
    if a.save_key:
        from settings import SETTINGS_PATH, load_settings, save_settings

        cfg = load_settings()
        cfg["api_key"] = a.save_key
        try:
            save_settings(cfg)
            log_info(f"TMDB API key saved to {SETTINGS_PATH}")
        except OSError as e:
            log_error(tr("Could not write config: {err}", err=e))
            sys.exit(1)
        sys.exit(0)

    if a.multi_edition:
        if not CONFIG.debug:
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
    if details is not None:
        return src, "details", details, sids, None
    if a.main:
        return src, "rip_main", None, sids, None
    if title_num is not None:
        return src, "rip_title", title_num, sids, None
    if a.all:
        return src, "rip_all", None, sids, None
    if a.episodes:
        return src, "rip_episodes", None, sids, None
    if a.info:
        return src, "info", None, sids, None
    return src, "interactive", None, sids, None


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
    creator.create_mkv(combined, select_streams(combined, streams))


def _fmt_edition_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    # Resolve the UI language before parsing args so that --help and the
    # first-run wizard are already localised.
    _init_ui_language()

    # mkvmerge is required for muxing.
    if not _HAS_MKVMERGE:
        log_error(tr("Missing: mkvmerge (install mkvtoolnix)"))
        sys.exit(1)

    # First-run wizard: if no settings file exists yet, walk the user through
    # language + (optional) TMDB key once, then re-resolve the language.
    # Skip it for quick-exit invocations (--help / --version / --save-key) so
    # those never block on interactive input.
    _quick_exit = any(a in sys.argv for a in ("-h", "--help", "-v", "--version"))
    _saving_key = "--save-key" in sys.argv
    if not SETTINGS_PATH.exists() and not _quick_exit and not _saving_key:
        _first_run_setup()
        _init_ui_language()

    src, act, num, sids, me_idx = parse_args()

    # An explicit --ui-lang flag wins for this run even over the wizard.
    if CONFIG.ui_lang:
        set_language(CONFIG.ui_lang)
    log_debug(
        tr(
            "Using language: {name} ({code})",
            name=language_name(get_language()),
            code=get_language(),
        )
    )

    # Propagate the debug flag to the extracted DVD IFO parser module so its
    # log_debug output matches cli.py's verbosity.
    dvdifo.set_debug(CONFIG.debug)

    # Redirect temp files to a user-specified disk directory when present.
    # Without this, temp files default to /tmp (often tmpfs / RAM-backed),
    # which can fill memory when extracting multi-GB ISOs.
    if CONFIG.temp_dir:
        CONFIG.temp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(CONFIG.temp_dir)
    # Detect whether the effective temp dir is RAM-backed (tmpfs) and, if so,
    # cap extraction at ram_limit of installed RAM (oversized titles spill to
    # disk). See disc_reader.init_ram_budget / temp_base_for_title.
    from disc_reader import init_ram_budget

    init_ram_budget()
    if src is None:
        log_error(tr("A source path is required"))
        log_error(tr("Run with -h to see usage, e.g. script.py /path/to/media"))
        sys.exit(1)
    if not src.exists() and not str(src).startswith("/dev/"):
        log_error(tr("Not found: {path}", path=src))
        sys.exit(1)
    scanner = Scanner(src)
    titles = scanner.scan()
    if not titles:
        log_warn(tr("No titles found"))
        sys.exit(0)
    if act == "info":
        display_titles(titles, scanner.disc_name)
    elif act == "details" and num is not None and 0 <= num < len(titles):
        display_title_details(titles[num])
    elif act == "rip_title" and num is not None and 0 <= num < len(titles):
        try:
            MKVCreator(CONFIG.output_dir, TAG_OPTS).create_mkv(
                titles[num], select_streams(titles[num], sids)
            )
        except RipError as e:
            print(e.format_verbose())
            sys.exit(1)
    elif act == "rip_main":
        idx = pick_main_feature(titles)
        if idx < 0:
            log_warn(tr("No titles found"))
            sys.exit(0)
        log_info(
            tr(
                "Main feature: #{idx} {name} ({dur})",
                idx=idx,
                name=titles[idx].name,
                dur=titles[idx].duration_display,
            )
        )
        try:
            MKVCreator(CONFIG.output_dir, TAG_OPTS).create_mkv(
                titles[idx], select_streams(titles[idx], sids)
            )
        except RipError as e:
            print(e.format_verbose())
            sys.exit(1)
    elif act == "rip_multi_edition" and me_idx:
        try:
            _rip_multi_edition(
                MKVCreator(CONFIG.output_dir, TAG_OPTS),
                titles,
                me_idx,
                None,
                sids,
            )
        except RipError as e:
            print(e.format_verbose())
            sys.exit(1)
        except ValueError as e:
            log_error(str(e))
            sys.exit(1)
    elif act == "rip_all":
        s, f = 0, 0
        creator = MKVCreator(CONFIG.output_dir, TAG_OPTS)
        for title in titles:
            try:
                creator.create_mkv(title)
                s += 1
            except RipError as e:
                print(e.format_verbose())
                f += 1
        print(tr("\nSummary: {ok} ok, {fail} failed", ok=s, fail=f))
    elif act == "rip_episodes":
        ep_titles = [t for t in titles if t.dvd_episode_number is not None]
        if not ep_titles:
            log_warn(tr("No episodes detected on this disc"))
            sys.exit(0)
        log_info(tr("Ripping {n} episode(s)...", n=len(ep_titles)))
        s, f = 0, 0
        creator = MKVCreator(CONFIG.output_dir, TAG_OPTS)
        for ep in ep_titles:
            try:
                creator.create_mkv(ep)
                s += 1
            except RipError as e:
                print(e.format_verbose())
                f += 1
        print(tr("\nSummary: {ok} ok, {fail} failed", ok=s, fail=f))
    elif act == "interactive":
        interactive_mode(titles, scanner.disc_name)
