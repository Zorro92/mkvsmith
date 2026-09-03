"""
Source scanning and title ranking.

Extracted from main.py: the Scanner class (ISO 7z / loop-mount handling),
per-source-type scan functions (DVD VIDEO_TS, Blu-ray BDMV, raw M2TS, video
files, optical devices), duplicate-playlist collapsing, and the
notable-title / main-feature ranking heuristics used by the display.

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

import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import final

from bluray import (
    _apply_stn_languages,
    _parse_bdmv_catalog_number,
    _parse_bdmv_disc_name,
    _parse_mpls,
    _set_video_color_from_info,
)
from dvdifo import (
    DvdIfoError,
    VmgInfo,
    _read_u16,
    _read_u32,
    _find_alternate_edition_pgcs,
    _parse_vmg_ifo,
)
from dvdbuild import (
    _apply_dvd_ifo_languages,
    _build_title_from_ifo,
    _create_title,
    _scan_dvd_source,
)
from i18n import tr
from models import (
    CONFIG,
    _DIRECT_MOUNT_CLEANUP,
    _TEMP_DIRS,
    _TEMP_FILES,
    _HAS_MKVMERGE,
    EditionAtom,
    EditionSpec,
    StreamType,
    Stream,
    Title,
    log_info,
    log_warn,
    log_error,
    log_debug,
)


# =============================================================================
# Scanner
# =============================================================================
_RELEASE_NAME_TAGS = {
    "ntsc",
    "pal",
    "dvd",
    "dvd5",
    "dvd9",
    "bd",
    "bd25",
    "bd50",
    "bluray",
    "blu-ray",
    "remux",
    "web",
    "webrip",
    "webdl",
    "hdtv",
    "pdtv",
    "dsr",
    "divx",
    "xvid",
    "x264",
    "h264",
    "h265",
    "hevc",
    "avc",
    "dd",
    "ac3",
    "dts",
    "5.1",
    "2.0",
    "7.1",
    "mono",
    "usa",
    "uk",
    "eu",
    "jpn",
    "ger",
    "fre",
    "ita",
    "spa",
    "kor",
    "cn",
    "multi",
    "retail",
    "internal",
    "proper",
    "repack",
    "limited",
    "extended",
    "unrated",
    "remastered",
    "1080p",
    "1080i",
    "720p",
    "480p",
    "576p",
    "4k",
    "uhd",
    "hdr",
    "hdr10",
    "dovi",
    "ws",
    "fs",
    "cust",
    "custom",
    "dtsonly",
    "thd",
}


def _clean_release_name(name: str) -> str:
    """Turn a release-style folder/file name into a human title.

    Handles the common 'Banjo.The.Woodpile.Cat.1979.USA.NTSC.DVD5' convention:
    separators become spaces and tokens are cut at the first year or scene tag
    (codec, resolution, source, region, etc.). Returns '' if nothing usable.
    """
    base = re.sub(r"\.(mkv|mp4|avi|iso|m2ts|vob|ts|m4v)$", "", name, flags=re.I)
    s = re.sub(r"[\._\-]+", " ", base).strip()
    s = re.sub(r"[\[\]\(\)]", " ", s)
    tokens = s.split()
    kept: list[str] = []
    for tok in tokens:
        low = tok.lower().strip(":,;!?")
        if re.fullmatch(r"(19|20)\d{2}", low):  # year: keep then stop
            kept.append(low)
            break
        if re.fullmatch(r"s\d{1,2}e\d{1,3}", low):  # SxxExx tag
            kept.append(tok.upper())
            break
        if low in _RELEASE_NAME_TAGS:
            break
        if re.fullmatch(r"\d{3,4}p", low) or re.fullmatch(r"\d{3,4}x\d{3,4}", low):
            break
        kept.append(tok)
    title = " ".join(kept).strip(" -,.;:!?")
    if not title:
        return ""
    small = {
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "but",
        "for",
        "to",
        "at",
        "in",
        "on",
        "by",
        "de",
        "du",
        "la",
        "le",
        "el",
        "il",
        "und",
        "der",
        "das",
    }
    words = title.split()
    out: list[str] = []
    for i, w in enumerate(words):
        wl = w.lower()
        if re.fullmatch(r"s\d{1,2}e\d{1,3}", wl):
            out.append(w.upper())
        elif i > 0 and wl in small:
            out.append(wl)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w[:1].isalpha() else w)
    return " ".join(out)


# _read_u16 and _read_u32 now live in dvdifo.py (imported explicitly above).


# =============================================================================
# Standalone scanner helpers (extracted from the original Scanner class)
# =============================================================================


def _dedup_duplicate_playlists(titles: list[Title]) -> list[Title]:
    """Collapse Blu-ray titles that resolve to the same clip sequence.

    Multiple MPLS playlists on a disc frequently reference the same
    underlying M2TS clip(s) — for region/menu branching, "favourite scenes"
    modes, or plain duplicate authoring. They yield identical video/audio
    bytes and differ only in chapter-table completeness or stream ordering.

    Such titles are grouped by their resolved clip sequence + total duration
    and only the richest representative is kept (most chapters, then most
    streams, then most audio+subtitle tracks). The first-built title wins
    ties, which — because PLAYLIST is iterated sorted — is the lowest MPLS
    number. Collapsed duplicates are logged at DEBUG level.

    Titles whose clip sequence or duration differs (seamless-branching
    editions, partial selections) are left untouched. Intentionally
    Blu-ray-only in effect: DVD deliberately exposes same-content PGCs
    (angles) as separate titles and must not be collapsed here.
    """
    groups: dict[tuple[tuple[str, ...], int], list[Title]] = {}
    order: list[tuple[tuple[str, ...], int]] = []
    for t in titles:
        if t.iso_internal_paths:
            clip_ids = tuple(Path(p).name for p in t.iso_internal_paths)
        else:
            clip_ids = (t.source_file.name,) + tuple(
                Path(p).name for p in t.append_clips
            )
        key = (clip_ids, round(t.duration_seconds))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)

    if all(len(groups[k]) == 1 for k in order):
        return titles

    deduped: list[Title] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            deduped.extend(group)
            continue
        best = group[0]
        best_rank = (
            len(best.chapters),
            len(best.streams),
            len(best.audio_streams) + len(best.subtitle_streams),
        )
        for t in group[1:]:
            rank = (
                len(t.chapters),
                len(t.streams),
                len(t.audio_streams) + len(t.subtitle_streams),
            )
            if rank > best_rank:
                best, best_rank = t, rank
        for t in group:
            if t is not best:
                log_debug(
                    f"Collapsed duplicate playlist {t.name} "
                    f"(same clips+duration as {best.name})"
                )
        deduped.append(best)
    return deduped


# =============================================================================
# Multi-edition (seamless branching) title building
# =============================================================================

# Sub-millisecond epsilon for chapter/segment boundary comparisons. Chapter
# times and clip durations both derive from 45 kHz MPLS timestamps, so exact
# boundary hits can differ from cumulative float sums by ~1e-12 s; 1 µs is
# far below one frame yet far above float noise.
_EDITION_EPS = 1e-6


def _title_clip_keys(t: Title) -> list[str]:
    """Canonical per-clip identity list for a Blu-ray title.

    Folder sources identify clips by resolved path, ISO sources by internal
    path; both are stable across titles built from the same disc.
    """
    if t.iso_internal_paths:
        return [str(p) for p in t.iso_internal_paths]
    return [str(t.source_file), *(str(p) for p in t.append_clips)]


def _stream_signature(t: Title) -> tuple[tuple[object, ...], ...]:
    """Identity of a title's stream layout (type, codec, lang, pid, channels)."""
    return tuple(
        (
            s.stream_type,
            s.codec,
            s.language,
            s.pid,
            s.channels,
        )
        for s in t.streams
    )


def _edition_atoms(
    clip_indices: list[int],
    clip_starts: list[float],
    clip_durations: list[float],
    chapters: list[float],
) -> list[EditionAtom]:
    """Compute ordered-chapter atoms for one edition over the combined timeline.

    Faithful port of xin1generator's GenerateChaptersAndTags loop
    (https://github.com/RollingStar/xin1generator): each clip of the playlist
    contributes one atom on the global timeline, split at real chapter marks.
    Atoms starting at a branch-point boundary mid-chapter are hidden
    (continuations); atoms starting at a real chapter are visible. The
    inclusive upper / strict lower bounds plus the exact-alignment ``continue``
    handle chapters that sit exactly on a segment boundary.
    """
    atoms: list[EditionAtom] = []
    virtual_offset = 0.0
    hide_next = False  # we always preserve real chapters
    for idx in clip_indices:
        start = clip_starts[idx]
        end = start + clip_durations[idx]
        seg_len = clip_durations[idx]
        next_start = start
        for ch in chapters:
            if (
                virtual_offset + _EDITION_EPS
                < ch
                <= virtual_offset + seg_len + _EDITION_EPS
            ):
                atoms.append(
                    EditionAtom(
                        next_start,
                        start + (ch - virtual_offset),
                        hidden=hide_next,
                    )
                )
                next_start = start + (ch - virtual_offset)
                hide_next = False
        virtual_offset += seg_len
        if next_start >= end - _EDITION_EPS:
            continue
        atoms.append(EditionAtom(next_start, end, hidden=hide_next))
        hide_next = True
    return atoms


def build_multi_edition_title(
    edition_titles: list[Title], edition_names: list[str] | None = None
) -> Title:
    """Combine seamless-branching playlist titles into one multi-edition Title.

    The result carries the union of all unique clips (first-appearance order
    across the given editions) as its append sequence, plus one ordered-
    edition chapter spec per input title. The first input is the default
    edition; the muxer writes one ``EditionEntry`` per spec and edition TITLE
    tags naming each cut.

    All titles must come from the same disc/source mode, be Blu-ray playlist
    titles, and share an identical stream layout (editions of one movie differ
    in clip order/selection, not in tracks). Raises ``ValueError`` otherwise.
    """
    if len(edition_titles) < 2:
        raise ValueError("multi-edition needs at least two titles")
    first = edition_titles[0]
    for t in edition_titles:
        if not t.playlist_name:
            raise ValueError(
                f"'{t.name}' is not a Blu-ray playlist title; "
                "multi-edition MKVs can only combine playlists"
            )
        if _stream_signature(t) != _stream_signature(first):
            raise ValueError(
                f"'{t.name}' has a different stream layout than '{first.name}'; "
                "editions combined into one MKV must share the same tracks"
            )

    is_iso = bool(first.iso_internal_paths)
    if any(bool(t.iso_internal_paths) != is_iso for t in edition_titles):
        raise ValueError("cannot mix ISO and folder sources in one multi-edition title")

    # Union of unique clips in first-appearance order, with per-clip duration
    # and byte size from the first playlist that references the clip.
    clip_index: dict[str, int] = {}
    clip_keys: list[str] = []
    clip_durs: list[float] = []
    clip_sizes: list[int] = []
    for t in edition_titles:
        keys = _title_clip_keys(t)
        durs = t.clip_durations
        sizes = t.clip_sizes
        if len(durs) != len(keys):
            log_debug(
                f"{t.name}: clip_durations mismatch ({len(durs)} vs {len(keys)}); "
                "multi-edition atoms may be approximate"
            )
            durs = (durs + [0.0] * len(keys))[: len(keys)]
            sizes = (sizes + [0] * len(keys))[: len(keys)]
        for i, key in enumerate(keys):
            if key in clip_index:
                prev_dur = clip_durs[clip_index[key]]
                if durs[i] and abs(durs[i] - prev_dur) > _EDITION_EPS:
                    log_debug(
                        f"clip {Path(key).name}: duration differs between playlists "
                        f"({prev_dur:.3f}s vs {durs[i]:.3f}s); using the first"
                    )
                continue
            clip_index[key] = len(clip_keys)
            clip_keys.append(key)
            clip_durs.append(durs[i])
            clip_sizes.append(sizes[i] if i < len(sizes) else 0)

    # Global timeline: each unique clip's [start, end) on the combined file.
    clip_starts: list[float] = []
    running = 0.0
    for d in clip_durs:
        clip_starts.append(running)
        running += d
    union_duration = running

    # One edition spec per input title.
    editions: list[EditionSpec] = []
    if edition_names is not None and len(edition_names) != len(edition_titles):
        raise ValueError("edition name count does not match title count")
    for ei, t in enumerate(edition_titles):
        keys = _title_clip_keys(t)
        indices = [clip_index[k] for k in keys if k in clip_index]
        chapters = list(t.chapters)
        # Re-apply the trailing end-chapter strip relative to the edition's own
        # duration (scan already did this, but chapters may have been touched).
        if chapters and chapters[-1] >= t.duration_seconds - 0.5:
            chapters = chapters[:-1]
        atoms = _edition_atoms(indices, clip_starts, clip_durs, chapters)
        # Name visible atoms sequentially ("Chapter 01"...), matching the flat
        # single-edition writer. Every real chapter yields exactly one visible
        # atom, so numbering follows the playlist's chapter order.
        visible_count = 0
        for atom in atoms:
            if not atom.hidden:
                visible_count += 1
                atom.name = f"Chapter {visible_count:02d}"
        if edition_names is not None:
            name = edition_names[ei]
        elif ei == 0:
            # Default edition carries the movie name (disc name when known) —
            # not the " - Title N" list label the scanner gave it.
            name = first.disc_name or first.name
        else:
            name = f"Playlist {t.playlist_name}"
        editions.append(
            EditionSpec(uid=ei + 1, name=name, is_default=(ei == 0), atoms=atoms)
        )

    # Build the synthetic combined title from the first edition's layout.
    base_name = first.disc_name or first.name
    if is_iso:
        combined = Title(
            first.index,
            first.source_file,
            base_name,
            union_duration,
        )
        combined.iso_internal_paths = clip_keys
    else:
        combined = Title(
            first.index,
            Path(clip_keys[0]),
            base_name,
            union_duration,
        )
        combined.append_clips = [Path(k) for k in clip_keys[1:]]
    combined.streams = [Stream(**vars(s)) for s in first.streams]
    combined.duration_seconds = union_duration
    combined.disc_name = first.disc_name
    combined.disc_barcode = first.disc_barcode
    combined.playlist_name = first.playlist_name
    combined.clip_durations = clip_durs
    combined.clip_sizes = clip_sizes
    combined.estimated_size_bytes = sum(clip_sizes)
    combined.editions = editions
    log_debug(
        f"Multi-edition title: {len(clip_keys)} unique clips "
        f"({union_duration:.0f}s total), {len(editions)} editions "
        f"({', '.join(e.name for e in editions)})"
    )
    return combined


def _detect_edition_groups(titles: list[Title]) -> list[list[Title]]:
    """Find groups of playlist titles that look like editions of one movie.

    Candidates must be Blu-ray playlist titles with an identical stream
    layout, at least three shared clips, and durations within 25% of each
    other. This only feeds the interactive hint / ``me`` default selection —
    users can always combine any matching set explicitly.
    """
    groups: list[list[Title]] = []
    pending = [
        t for t in titles if t.playlist_name and t.clip_durations and len(t.streams) > 1
    ]
    while pending:
        head, pending = pending[0], pending[1:]
        group = [head]
        head_clips = set(_title_clip_keys(head))
        rest: list[Title] = []
        for t in pending:
            if (
                _stream_signature(t) == _stream_signature(head)
                and len(head_clips & set(_title_clip_keys(t))) >= 3
                and abs(t.duration_seconds - head.duration_seconds)
                <= 0.25 * max(t.duration_seconds, head.duration_seconds)
            ):
                group.append(t)
            else:
                rest.append(t)
        pending = rest
        if len(group) > 1:
            groups.append(group)
    return groups


def _scan_bluray_source(source: Path) -> tuple[list[Title], str | None]:
    """Scan a Blu-ray BDMV directory and return (titles, disc_name)."""
    bdmv = source / "BDMV" if (source / "BDMV").is_dir() else source / "bdmv"
    titles: list[Title] = []
    disc_name = _parse_bdmv_disc_name(bdmv)
    disc_barcode = _parse_bdmv_catalog_number(bdmv)
    if disc_name:
        log_info(tr("Disc name: {name}", name=disc_name))
    if disc_barcode:
        log_debug(f"BD catalog number: {disc_barcode}")

    clpi_dir = bdmv / "CLIPINF"
    if not clpi_dir.is_dir():
        clpi_dir = None
    pd, stream_dir = bdmv / "PLAYLIST", bdmv / "STREAM"
    if pd.is_dir():
        for mpls in sorted(pd.glob("*.mpls")):
            info = _parse_mpls(mpls, clpi_dir=clpi_dir)
            if not info:
                continue
            total_duration = sum(pi["duration"] for pi in info["play_items"])
            if total_duration < CONFIG.min_duration:
                continue
            clip_paths: list[Path] = []
            complete = True
            for pi in info["play_items"]:
                cp = stream_dir / f"{pi['clip']}.m2ts"
                if not cp.exists():
                    complete = False
                    break
                clip_paths.append(cp)
            if not complete or not clip_paths:
                continue

            # Build title from MPLS STN data --- no ffprobe needed.
            mpls_streams = info.get("streams", [])
            if mpls_streams:
                type_counts = {
                    StreamType.VIDEO: 0,
                    StreamType.AUDIO: 0,
                    StreamType.SUBTITLE: 0,
                }
                title_streams: list[Stream] = []
                for si in mpls_streams:
                    st = si["type"]
                    if st == StreamType.VIDEO:
                        s = Stream(
                            0,
                            StreamType.VIDEO,
                            si["codec"],
                            "und",
                            "",
                            False,
                            False,
                            type_index=0,
                            pid=si.get("pid"),
                        )
                        _set_video_color_from_info(s, si)
                        title_streams.append(s)
                        type_counts[StreamType.VIDEO] += 1
                    elif st == StreamType.AUDIO:
                        s = Stream(
                            0,
                            StreamType.AUDIO,
                            si["codec"],
                            si["lang"],
                            "",
                            False,
                            False,
                            type_index=type_counts[StreamType.AUDIO],
                            pid=si.get("pid"),
                        )
                        s.channels = si.get("channels")
                        title_streams.append(s)
                        type_counts[StreamType.AUDIO] += 1
                    elif st == StreamType.SUBTITLE:
                        s = Stream(
                            0,
                            StreamType.SUBTITLE,
                            si["codec"],
                            si["lang"],
                            "",
                            False,
                            False,
                            type_index=type_counts[StreamType.SUBTITLE],
                            pid=si.get("pid"),
                        )
                        title_streams.append(s)
                        type_counts[StreamType.SUBTITLE] += 1

                if title_streams:
                    t = Title(
                        len(titles),
                        clip_paths[0],
                        f"Playlist {mpls.stem}",
                        total_duration,
                    )
                    t.streams = title_streams
                    t.duration_seconds = total_duration
                    t.append_clips = clip_paths[1:]
                    t.chapters = info.get("chapter_times", [])
                    t.disc_name = disc_name
                    t.disc_barcode = disc_barcode
                    t.playlist_name = mpls.stem
                    t.clip_durations = [pi["duration"] for pi in info["play_items"]]
                    try:
                        t.clip_sizes = [cp.stat().st_size for cp in clip_paths]
                    except OSError:
                        t.clip_sizes = []

                    # Log SubPath entries (secondary audio/video in separate clips).
                    subpath_entries = info.get("subpath_entries", [])
                    if subpath_entries:
                        log_debug(
                            f"{mpls.stem}: {len(subpath_entries)} SubPath entries"
                        )
                        for sp in subpath_entries:
                            sp_type = sp.get("type", 0)
                            sp_clips = sp.get("clips", [])
                            # Type 4 = secondary audio out-of-mux, type 6 = secondary video out-of-mux.
                            if sp_type in (4, 6) and sp_clips:
                                log_debug(f"  SubPath type {sp_type}: clips {sp_clips}")
                                # Check if SubPath clips exist alongside the main stream.
                                for sp_clip in sp_clips:
                                    sp_path = stream_dir / f"{sp_clip}.m2ts"
                                    if sp_path.exists():
                                        log_debug(
                                            f"    SubPath clip found: {sp_clip}.m2ts "
                                            f"({sp_path.stat().st_size / 1e6:.1f} MB)"
                                        )

                    # Strip trailing end chapter (matches MakeMKV).
                    if t.chapters and t.chapters[-1] >= total_duration:
                        t.chapters = t.chapters[:-1]
                    if len(clip_paths) > 1:
                        log_debug(
                            f"{mpls.stem}: {len(clip_paths)} clips will be appended"
                        )
                    titles.append(t)
                    log_debug(
                        f"Built from MPLS (native): {mpls.stem}, "
                        f"duration={total_duration:.0f}s, "
                        f"{len(title_streams)} streams"
                    )
                    continue

            # Fallback: ffprobe-based title creation when MPLS has no STN.
            log_debug(f"No STN streams in {mpls.stem}, falling back to ffprobe")
            t = _create_title(
                titles,
                clip_paths[0],
                f"Playlist {mpls.stem}",
                override_duration=total_duration,
            )
            if not t:
                continue
            t.duration_seconds = total_duration
            t.append_clips = clip_paths[1:]
            t.chapters = info.get("chapter_times", [])
            t.disc_name = disc_name
            t.disc_barcode = disc_barcode
            t.playlist_name = mpls.stem
            t.clip_durations = [pi["duration"] for pi in info["play_items"]]
            try:
                t.clip_sizes = [cp.stat().st_size for cp in clip_paths]
            except OSError:
                t.clip_sizes = []
            # Strip trailing end chapter (matches MakeMKV).
            if t.chapters and t.chapters[-1] >= total_duration:
                t.chapters = t.chapters[:-1]
            _apply_stn_languages(t, info["audio_langs"], info["subtitle_langs"])
            if len(clip_paths) > 1:
                log_debug(f"{mpls.stem}: {len(clip_paths)} clips will be appended")
            titles.append(t)

    if not titles:
        stream_dir_lower = (
            source / "BDMV" / "STREAM"
            if (source / "BDMV").is_dir()
            else source / "bdmv" / "STREAM"
        )
        if stream_dir_lower.is_dir():
            _scan_m2ts_dir(stream_dir_lower, titles)

    titles = _dedup_duplicate_playlists(titles)
    return titles, disc_name


def _scan_bluray_raw_source(source: Path) -> tuple[list[Title], str | None]:
    """Scan a directory of raw .m2ts files (no BDMV structure)."""
    titles: list[Title] = []
    for d in [
        source / "BDMV" / "STREAM",
        source / "bdmv" / "STREAM",
        source,
    ]:
        if d.is_dir():
            _scan_m2ts_dir(d, titles)
            break
    return titles, None


def _scan_m2ts_dir(sd: Path, titles: list[Title]) -> None:
    """Append titles probed from every .m2ts file in *sd*."""
    for m2ts in sorted(sd.glob("*.m2ts")):
        if t := _create_title(titles, m2ts, m2ts.stem):
            titles.append(t)


def _scan_video_source(source: Path) -> list[Title]:
    """Scan a single video file and return its title."""
    titles: list[Title] = []
    if not _HAS_MKVMERGE:
        log_warn(tr("mkvmerge not available, cannot scan video file."))
        return titles
    if t := _create_title(titles, source, source.stem):
        titles.append(t)
    return titles


def _scan_device_source(source: Path) -> list[Title]:
    """Scan a DVD/BD device (e.g. /dev/sr0)."""
    titles: list[Title] = []
    if t := _create_title(titles, source, "Optical Disc"):
        titles.append(t)
    else:
        log_error(tr("Device read failed (needs libdvdcss/libaacs)"))
    return titles


@final
class Scanner:
    def __init__(self, source: Path):
        self.source = source
        self.titles: list[Title] = []
        self.disc_name: str | None = None

    def scan(self) -> list[Title]:
        from disc_reader import SourceType, detect_source_type

        st = detect_source_type(self.source)
        log_info(tr("Source type: {type}", type=st.value))
        if st == SourceType.ISO_UNKNOWN:
            if self.source.is_dir():
                isos = sorted(self.source.glob("*.iso"))
                if isos:
                    self.source = isos[0]
                    log_info(
                        tr("Using ISO file in directory: {name}", name=self.source.name)
                    )
                else:
                    log_warn(tr("No ISO file found in {path}", path=self.source))
            self._scan_iso()
        elif st in (SourceType.DVD, SourceType.DVD_RAW):
            self.titles, self.disc_name = _scan_dvd_source(self.source)
        elif st == SourceType.BLURAY:
            self.titles, self.disc_name = _scan_bluray_source(self.source)
        elif st == SourceType.BLURAY_RAW:
            self.titles, self.disc_name = _scan_bluray_raw_source(self.source)
        elif st == SourceType.VIDEO_FILE:
            self.titles = _scan_video_source(self.source)
        elif st == SourceType.DEVICE:
            self.titles = _scan_device_source(self.source)
        self.titles.sort(
            key=lambda t: (
                # Episodes first (ordered by episode number), then other titles,
                # then the "play all" chain last so it doesn't dominate the list.
                2 if t.dvd_play_all else (0 if t.dvd_episode_number is not None else 1),
                t.dvd_episode_number if t.dvd_episode_number is not None else 0,
                -t.duration_seconds,
            )
        )
        for i, t in enumerate(self.titles):
            t.index = i
        for t in self.titles:
            self._apply_disc_name()
        return self.titles

    def _apply_disc_name(self) -> None:
        """Name titles after the disc/folder.

        When a disc name is known (e.g. from Blu-ray bdmt.xml metadata) it is
        used directly. Otherwise the source folder/file name is cleaned up and
        used as the disc name. The main feature gets the bare name; extras get
        a " - Title N" suffix so they stay distinct.

        TV-series episodes (``dvd_episode_number``) are labelled "Episode N"
        regardless of main-feature status, and the "play all" chain is
        explicitly marked so it isn't mistaken for the series itself.
        """
        if not self.disc_name:
            source_name = self.source.name if self.source.is_dir() else self.source.stem
            disc = _clean_release_name(source_name)
            if not disc:
                return
            self.disc_name = disc
        main_idx = pick_main_feature(self.titles)
        for t in self.titles:
            if t.dvd_episode_number is not None:
                t.name = f"{self.disc_name} - Episode {t.dvd_episode_number}"
            elif t.dvd_play_all:
                t.name = f"{self.disc_name} - Play All"
            elif t.index == main_idx:
                t.name = self.disc_name
            elif t.dvd_edition_label:
                t.name = (
                    f"{self.disc_name} - Title {t.index + 1} ({t.dvd_edition_label})"
                )
            else:
                t.name = f"{self.disc_name} - Title {t.index + 1}"

    def _scan_iso(self) -> None:
        from disc_reader import _probe_has_iso9660_pvd

        if not _probe_has_iso9660_pvd(self.source):
            log_error(
                tr(
                    "{path} is not a valid ISO image (missing ISO9660 PVD)",
                    path=self.source,
                )
            )
            return
        self._scan_iso_7z()
        if not self.titles:
            self._scan_iso_mount()

    def _scan_iso_7z(self) -> None:
        from disc_reader import (
            _extract_partial_7z,
            _extract_with_7z,
            _list_iso_files_7z,
        )

        log_info(tr("Scanning ISO with 7z..."))
        paths, sizes = _list_iso_files_7z(self.source)
        if not paths:
            log_error(
                tr("7z could not find any .mpls, .m2ts, or .vob files inside the ISO.")
            )
            return
        mpls_files = [p for p in paths if p.lower().endswith(".mpls")]
        m2ts_files = [
            p for p in paths if "stream" in p.lower() and p.lower().endswith(".m2ts")
        ]
        if mpls_files:
            tmp_dir = Path(tempfile.mkdtemp(prefix="mkv_scan_"))
            _TEMP_DIRS.append(tmp_dir)
            # Build lookup: clip name -> internal M2TS path (e.g. "00000" -> "BDMV/STREAM/00000.m2ts")
            m2ts_by_clip: dict[str, str] = {}
            for p in m2ts_files:
                stem = Path(p).stem
                m2ts_by_clip[stem] = p
            # Build lookup: clip name -> internal CLPI path (e.g. "00000" -> "BDMV/CLIPINF/00000.clpi")
            clpi_internal: dict[str, str] = {}
            for p in paths:
                if p.lower().endswith(".clpi"):
                    stem = Path(p).stem
                    clpi_internal[stem] = p
            # Find bdmt.xml (Blu-ray disc name metadata) inside the ISO.
            bdmt_files: list[str] = [
                p
                for p in paths
                if p.lower().endswith(".xml")
                and "meta" in p.lower()
                and Path(p).stem.startswith("bdmt")
            ]
            # Extract MPLS + CLPI + bdmt files in one pass so 7z reads the ISO once.
            files_to_extract = list(mpls_files)
            if clpi_internal:
                files_to_extract.extend(clpi_internal.values())
            if bdmt_files:
                files_to_extract.extend(bdmt_files)
            extracted_paths = _extract_with_7z(self.source, files_to_extract, tmp_dir)
            # Map extracted CLPI paths back to their clip name for fast lookup.
            # (MPLS files have the same stem but we only want CLPI here.)
            extracted_clpi: dict[str, Path] = {}
            for ep in extracted_paths:
                if ep.suffix.lower() == ".clpi":
                    extracted_clpi[ep.stem] = ep

            # Parse bdmt.xml for disc name if present in the ISO.
            if bdmt_files and not self.disc_name:
                for ep in extracted_paths:
                    if ep.suffix.lower() == ".xml" and ep.stem.startswith("bdmt"):
                        try:
                            for elem in ET.parse(ep).iter():
                                if (
                                    elem.tag.endswith("name")
                                    and elem.text
                                    and elem.text.strip()
                                ):
                                    raw = elem.text.strip()
                                    # Preserve spaces but convert newlines to " - "
                                    self.disc_name = (
                                        raw.replace("\r\n", " - ")
                                        .replace("\r", " - ")
                                        .replace("\n", " - ")
                                    )
                                    log_info(
                                        tr(
                                            "Disc name from bdmt.xml: {name}",
                                            name=self.disc_name,
                                        )
                                    )
                                    break
                        except Exception as e:
                            log_debug(f"Failed to parse bdmt.xml: {e}")
                        break

            for ext_path in extracted_paths:
                if not ext_path.suffix.lower() == ".mpls":
                    continue
                # Try to parse CLPI for the first playitem and pass to _parse_mpls.
                first_clip_clpi: Path | None = None
                try:
                    data = ext_path.read_bytes()
                    if len(data) >= 40 and data[0:4] == b"MPLS":
                        from_pos = _read_u32(data, 8) + 10
                        if from_pos + 2 <= len(data):
                            item_len = _read_u16(data, from_pos)
                            item = data[from_pos + 2 : from_pos + 2 + item_len]
                            if len(item) >= 32:
                                clip_name = item[0:5].decode("ascii", "ignore")
                                if clip_name in extracted_clpi:
                                    first_clip_clpi = extracted_clpi[clip_name]
                except Exception:
                    pass

                clpi_dir_arg: Path | None = None
                if first_clip_clpi and first_clip_clpi.suffix.lower() == ".clpi":
                    clpi_dir_arg = first_clip_clpi.parent

                # Pass the CLPI directory so _parse_mpls merges attributes.
                info = _parse_mpls(ext_path, clpi_dir=clpi_dir_arg)
                if not info:
                    log_debug(f"MPLS parse failed for {ext_path.name}")
                    continue
                total_duration = sum(pi["duration"] for pi in info["play_items"])
                if total_duration < CONFIG.min_duration:
                    continue
                # Map playitem clips to their ISO M2TS internal paths.
                clip_internals: list[str] = []
                for pi in info["play_items"]:
                    ip = m2ts_by_clip.get(pi["clip"])
                    if ip:
                        clip_internals.append(ip)
                    else:
                        log_debug(f"  Clip {pi['clip']}.m2ts not found in ISO")
                if not clip_internals:
                    log_debug(f"  No M2TS files found for {ext_path.name}, skipping")
                    continue
                # Build stream list from MPLS STN table (no ffprobe needed).
                mpls_streams = info.get("streams", [])
                if not mpls_streams:
                    log_debug(f"  No STN stream info in {ext_path.name}, skipping")
                    continue
                type_counts = {
                    StreamType.VIDEO: 0,
                    StreamType.AUDIO: 0,
                    StreamType.SUBTITLE: 0,
                }
                title_streams: list[Stream] = []
                for si in mpls_streams:
                    st = si["type"]
                    if st == StreamType.VIDEO:
                        s = Stream(
                            0,
                            StreamType.VIDEO,
                            si["codec"],
                            "und",
                            "",
                            False,
                            False,
                            type_index=0,
                            pid=si.get("pid"),
                        )
                        _set_video_color_from_info(s, si)
                        title_streams.append(s)
                        type_counts[StreamType.VIDEO] += 1
                    elif st == StreamType.AUDIO:
                        s = Stream(
                            0,
                            StreamType.AUDIO,
                            si["codec"],
                            si["lang"],
                            "",
                            False,
                            False,
                            type_index=type_counts[StreamType.AUDIO],
                            pid=si.get("pid"),
                        )
                        s.channels = si.get("channels")
                        title_streams.append(s)
                        type_counts[StreamType.AUDIO] += 1
                    elif st == StreamType.SUBTITLE:
                        s = Stream(
                            0,
                            StreamType.SUBTITLE,
                            si["codec"],
                            si["lang"],
                            "",
                            False,
                            False,
                            type_index=type_counts[StreamType.SUBTITLE],
                            pid=si.get("pid"),
                        )
                        title_streams.append(s)
                        type_counts[StreamType.SUBTITLE] += 1
                if not title_streams:
                    log_debug(f"  No usable streams from {ext_path.name}, skipping")
                    continue
                t = Title(
                    len(self.titles),
                    self.source,
                    f"Playlist {ext_path.stem}",
                    total_duration,
                )
                t.streams = title_streams
                t.chapters = info.get("chapter_times", [])
                # Strip trailing end chapter (matches MakeMKV).
                if t.chapters and t.chapters[-1] >= total_duration:
                    t.chapters = t.chapters[:-1]
                t.iso_internal_paths = clip_internals
                t.estimated_size_bytes = sum(sizes.get(p, 0) for p in clip_internals)
                t.playlist_name = ext_path.stem
                t.clip_durations = [pi["duration"] for pi in info["play_items"]]
                t.clip_sizes = [sizes.get(p, 0) for p in clip_internals]
                self.titles.append(t)
                log_debug(
                    f"Built from MPLS: {ext_path.name}, "
                    f"duration={total_duration:.0f}s, "
                    f"{len(title_streams)} streams, "
                    f"{len(clip_internals)} clips"
                )
        if mpls_files and not self.titles:
            log_debug(
                f"Found {len(mpls_files)} MPLS file(s) in ISO but none produced a "
                f"valid title (parse failure, zero duration, no streams, "
                f"or missing M2TS clips)."
            )
        if not mpls_files and m2ts_files:
            for p in m2ts_files:
                if tmp := _extract_partial_7z(self.source, p):
                    if t := _create_title(self.titles, tmp, Path(p).stem):
                        t.source_file, t.iso_internal_paths = self.source, [p]
                        t.estimated_size_bytes = sizes.get(p, 0)
                        self.titles.append(t)
                    tmp.unlink(missing_ok=True)
        elif not mpls_files and not m2ts_files:
            # Check for DVD VIDEO_TS content (VOB/IFO files).
            vob_files = sorted(
                p
                for p in paths
                if p.lower().endswith(".vob") and p.upper().startswith("VIDEO_TS/")
            )
            ifo_files = sorted(
                p
                for p in paths
                if p.lower().endswith(".ifo") and p.upper().startswith("VIDEO_TS/")
            )
            if vob_files and ifo_files:
                log_info(tr("Detected DVD VIDEO_TS structure in ISO"))
                tmp_dir = Path(tempfile.mkdtemp(prefix="mkv_scan_"))
                _TEMP_DIRS.append(tmp_dir)

                # Extract all IFO files (small) for scanning.
                files_to_extract = list(ifo_files)
                # Map VTS number -> internal first-VOB path.
                vts_first_vob: dict[int, str] = {}
                for p in vob_files:
                    m = re.search(r"VTS_(\d+)_1\.VOB$", p, re.IGNORECASE)
                    if m:
                        vts = int(m.group(1))
                        if vts not in vts_first_vob:
                            vts_first_vob[vts] = p
                # Map VTS number -> all internal VOB paths (for muxing).
                # Part 0 (VTS_XX_0.VOB) is the VTSM menu VOB, not part of the
                # title's VOBU addressing space - the IFO's VOBU_ADMAP and
                # CellPlaybackInfo sector fields are relative to the title
                # VOB stream (parts 1+) only. Including part 0 here would
                # both mux DVD menu video into the output and shift every
                # sector-based calculation off by the menu VOB's size.
                vts_all_vobs: dict[int, list[str]] = {}
                for p in vob_files:
                    m = re.search(r"VTS_(\d+)_(\d+)\.VOB$", p, re.IGNORECASE)
                    if m and int(m.group(2)) >= 1:
                        vts = int(m.group(1))
                        vts_all_vobs.setdefault(vts, []).append(p)
                for parts in vts_all_vobs.values():
                    parts.sort(
                        key=lambda p: (
                            int(m.group(1))
                            if (m := re.search(r"_(\d+)\.VOB$", p))
                            else 0
                        )
                    )

                # Extract IFOs first so we can use them for parsing.
                extracted = _extract_with_7z(self.source, files_to_extract, tmp_dir)

                # Parse VMG IFO (VIDEO_TS.IFO) for disc name / barcode.
                vmg_path = next(
                    (e for e in extracted if e.name.upper() == "VIDEO_TS.IFO"),
                    None,
                )
                vmg_info: VmgInfo | None = None
                disc_barcode: str | None = None
                if vmg_path and vmg_path.exists():
                    try:
                        vmg_info = _parse_vmg_ifo(vmg_path)
                    except DvdIfoError as exc:
                        log_debug(f"VMG IFO parse failed: {exc}")
                    if vmg_info:
                        vmg_disc_name = vmg_info.get("disc_name")
                        if vmg_disc_name:
                            self.disc_name = vmg_disc_name
                        disc_barcode = vmg_info.get("barcode")

                # Build reverse lookup: VTS number -> first logical title number.
                vts_to_title_num: dict[int, int] = {}
                if vmg_info:
                    title_map = vmg_info.get("title_map")
                    if title_map:
                        for title_idx, (vts_num, _ttl_num) in title_map.items():
                            if vts_num not in vts_to_title_num:
                                vts_to_title_num[vts_num] = title_idx

                for ifo_path in sorted(extracted):
                    m = re.search(r"VTS_(\d+)_0\.IFO$", ifo_path.name, re.IGNORECASE)
                    if not m:
                        continue
                    vts = int(m.group(1))
                    first_vob_internal = vts_first_vob.get(vts)
                    if not first_vob_internal:
                        continue
                    # Extract the first VOB partially (up to 256 MB) for
                    # probing/fallback — _build_title_from_ifo reads the IFO,
                    # not the VOB, so a prefix is sufficient for scanning.
                    first_vob_extracted = _extract_partial_7z(
                        self.source, first_vob_internal
                    )
                    if not first_vob_extracted:
                        continue
                    if first_vob_extracted.suffix.lower() != ".vob":
                        # _extract_partial_7z creates a .tmp file; rename
                        # so VOB-dependent paths (mkvmerge probe, etc.) work.
                        vob_renamed = first_vob_extracted.with_suffix(".vob")
                        try:
                            first_vob_extracted.rename(vob_renamed)
                        except Exception:
                            pass
                        else:
                            first_vob_extracted = vob_renamed
                            _TEMP_FILES.append(first_vob_extracted)

                    logical_title = vts_to_title_num.get(vts)
                    title_name = f"Title {vts}"
                    if logical_title:
                        title_name = f"Title {logical_title} (VTS {vts})"
                        log_debug(f"TT_SRPT: VTS {vts} -> DVD Title {logical_title}")

                    t = _build_title_from_ifo(
                        self.titles,
                        first_vob_extracted,
                        ifo_path,
                        [first_vob_extracted],
                        vts,
                        title_name=title_name,
                    )
                    if t is None:
                        if t := _create_title(
                            self.titles, first_vob_extracted, title_name
                        ):
                            t.disc_name = self.disc_name
                            t.disc_barcode = disc_barcode
                            _apply_dvd_ifo_languages(t, ifo_path)
                            t.source_file = self.source
                            t.iso_internal_paths = vts_all_vobs.get(
                                vts, [first_vob_internal]
                            )
                            t.estimated_size_bytes = sum(
                                sizes.get(vp, 0) for vp in t.iso_internal_paths
                            )
                            self.titles.append(t)
                        continue
                    t.source_file = self.source
                    t.iso_internal_paths = vts_all_vobs.get(vts, [first_vob_internal])
                    t.estimated_size_bytes = sum(
                        sizes.get(vp, 0) for vp in t.iso_internal_paths
                    )
                    t.disc_name = self.disc_name
                    t.disc_barcode = disc_barcode
                    self.titles.append(t)

                    # Seamless-branching discs can hold multiple substantial
                    # PGCs within the same VTS (e.g. a theatrical cut plus one
                    # or more longer bonus/extended cuts sharing footage via
                    # interleaved cells). Expose each as its own separate,
                    # independently rippable title alongside the default one,
                    # matching how MakeMKV lists each edition separately
                    # rather than collapsing them into a single title.
                    try:
                        ifo_bytes = ifo_path.read_bytes()
                    except Exception as e:
                        log_debug(
                            f"Alternate-edition PGC scan skipped for {ifo_path.name}: {e}"
                        )
                        ifo_bytes = b""
                    extra_pgcs = (
                        _find_alternate_edition_pgcs(ifo_bytes, CONFIG.min_duration)
                        if ifo_bytes
                        else []
                    )
                    for edition_num, pgc_num in enumerate(extra_pgcs, start=2):
                        edition_name = f"{title_name} - Edition {edition_num}"
                        t_alt = _build_title_from_ifo(
                            self.titles,
                            first_vob_extracted,
                            ifo_path,
                            [first_vob_extracted],
                            vts,
                            title_name=edition_name,
                            pgc_number=pgc_num,
                        )
                        if t_alt is None:
                            continue
                        t_alt.source_file = self.source
                        t_alt.iso_internal_paths = vts_all_vobs.get(
                            vts, [first_vob_internal]
                        )
                        t_alt.estimated_size_bytes = sum(
                            sizes.get(vp, 0) for vp in t_alt.iso_internal_paths
                        )
                        t_alt.disc_name = self.disc_name
                        t_alt.disc_barcode = disc_barcode
                        t_alt.dvd_edition_label = f"Edition {edition_num}"
                        self.titles.append(t_alt)
                        log_debug(
                            f"  Alternate edition: PGC {pgc_num} "
                            f"({t_alt.duration_seconds:.0f}s) exposed as "
                            f"'{edition_name}'"
                        )
            else:
                log_debug(
                    "7z listed files from the ISO, but none matched BDMV/VIDEO_TS "
                    "paths (.mpls, .m2ts, or .vob). The ISO may not be a video disc."
                )

        if mpls_files:
            self.titles = _dedup_duplicate_playlists(self.titles)

    def _scan_iso_mount(self) -> None:
        """Mount the ISO via ``sudo mount -o loop,ro`` and scan the result."""
        from disc_reader import _try_direct_mount

        if CONFIG.no_sudo:
            log_info(tr("Skipping direct mount (--no-sudo is set)"))
            return
        mnt = _try_direct_mount(self.source)
        if not mnt:
            log_error("All ISO reading methods failed.")
            log_error(f"Try: sudo mount -o loop,ro '{self.source}' /mnt/iso")
            return
        log_info(f"Direct mount succeeded at {mnt}")
        if (mnt / "BDMV").is_dir():
            blu_titles, disc_name = _scan_bluray_source(mnt)
            self.titles.extend(blu_titles)
            if disc_name:
                self.disc_name = disc_name
        elif (mnt / "VIDEO_TS").is_dir():
            dvd_titles, disc_name = _scan_dvd_source(mnt)
            self.titles.extend(dvd_titles)
            if disc_name:
                self.disc_name = disc_name
        else:
            log_error(
                tr(
                    "Mounted {path} but found neither BDMV nor VIDEO_TS at the top level.",
                    path=mnt,
                )
            )
            # Unmount and clean up immediately instead of waiting for atexit.
            _ = subprocess.run(
                ["sudo", "umount", str(mnt)], capture_output=True, timeout=30
            )
            try:
                mnt.rmdir()
            except Exception:
                pass
            if mnt in _DIRECT_MOUNT_CLEANUP:
                _DIRECT_MOUNT_CLEANUP.remove(mnt)


# =============================================================================
# Title ranking (notable titles / main feature)
# =============================================================================


def _is_notable_title(title: Title) -> bool:
    """Determine if a title is likely actual content vs. menu/trailer/junk.

    Titles that fail this check are hidden from the default display and excluded
    from automatic main-feature detection. Pass ``--show-all`` to show every
    title regardless of its quality score.

    Heuristics (based on real-world Blu-ray & DVD behaviour):
      - Very short clips (<2 min) are almost always trailers / warnings / menus
      - Short clips (<5 min) with only 1 audio stream and no subs are likely junk
      - Titles with no audio streams are PiP / slideshow / interactive content
    """
    if not title.video_streams:
        return False

    dur = title.duration_seconds
    n_audio = len(title.audio_streams)
    n_sub = len(title.subtitle_streams)

    # Under 2 minutes: almost never the main feature.
    if dur < 120:
        return False

    # Under 5 minutes with minimal streams: likely a trailer/menu.
    if dur < 300:
        # Has multiple audio tracks or at least one subtitle -> might be a short featurette.
        if n_audio >= 2 or n_sub >= 1:
            return True
        # Single audio (especially without a real language code) -> junk.
        if n_audio == 0:
            return False
        if n_audio == 1 and all(s.language == "und" for s in title.audio_streams):
            return False
        # Could be a short extra with a named language track.
        return True

    # 5+ minutes: likely content.  Require at least one audio stream though.
    return n_audio > 0


def _get_notable_titles(titles: list[Title]) -> tuple[list[Title], int]:
    """Return (notable, hidden_count).

    Respects ``CONFIG.show_all`` — when set, all titles are returned as notable
    and hidden_count is always 0.
    """
    if CONFIG.show_all:
        return titles, 0
    notable = [t for t in titles if _is_notable_title(t)]
    return notable, len(titles) - len(notable)


def _main_feature_score(title: Title) -> tuple[int, int, float]:
    """Rank titles for "main feature" detection.

    DVDs put the real film in the title set with the richest audio/subtitle
    selection; extras are often longer but have one audio track and no subs.
    Primary key: number of audio + subtitle streams. This matches the
    heuristic MakeMKV uses to flag the main title.

    Secondary key: whether this is the disc's own designated default title
    (``dvd_pgc_number is None``) rather than an explicitly-tagged alternate
    seamless-branching edition. This must outrank duration: a bonus/extended
    cut can have nearly identical audio/subtitle richness to the default
    title but a longer duration (that's the whole reason it needs its own
    PGC), so a pure duration tiebreak would wrongly promote the alternate
    edition to "main feature" over the disc's actual default title.

    Final tiebreak: duration.

    Titles shorter than ``CONFIG.min_duration`` (default 60s) are excluded
    from main-feature consideration — they are almost always menus, trailers,
    or warning cards with unusually rich stream tables.
    """
    if title.duration_seconds < CONFIG.min_duration:
        return (-1, 0, 0.0)
    richness = len(title.audio_streams) + len(title.subtitle_streams)
    is_default_edition = 0 if title.dvd_pgc_number is not None else 1
    return (richness, is_default_edition, title.duration_seconds)


def pick_main_feature(titles: list[Title]) -> int:
    """Return the index of the best main-feature candidate, or -1 if empty."""
    if not titles:
        return -1
    best = max(titles, key=_main_feature_score)
    return best.index
