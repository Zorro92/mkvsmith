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
from dataclasses import dataclass
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, final

from bluray import (
    MplsStreamInfo,
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
    Config,
    RuntimeState,
    RUNTIME_STATE,
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


def _streams_from_mpls(mpls_streams: list[MplsStreamInfo]) -> list[Stream]:
    type_counts = {
        StreamType.VIDEO: 0,
        StreamType.AUDIO: 0,
        StreamType.SUBTITLE: 0,
    }
    title_streams: list[Stream] = []
    for stream_info in mpls_streams:
        stream_type = stream_info["type"]
        if stream_type == StreamType.VIDEO:
            stream = Stream(
                0,
                StreamType.VIDEO,
                stream_info["codec"],
                "und",
                "",
                False,
                False,
                type_index=0,
                pid=stream_info.get("pid"),
            )
            _set_video_color_from_info(stream, stream_info)
            title_streams.append(stream)
            type_counts[StreamType.VIDEO] += 1
        elif stream_type == StreamType.AUDIO:
            stream = Stream(
                0,
                StreamType.AUDIO,
                stream_info["codec"],
                stream_info["lang"],
                "",
                False,
                False,
                type_index=type_counts[StreamType.AUDIO],
                pid=stream_info.get("pid"),
            )
            stream.channels = stream_info.get("channels")
            title_streams.append(stream)
            type_counts[StreamType.AUDIO] += 1
        elif stream_type == StreamType.SUBTITLE:
            stream = Stream(
                0,
                StreamType.SUBTITLE,
                stream_info["codec"],
                stream_info["lang"],
                "",
                False,
                False,
                type_index=type_counts[StreamType.SUBTITLE],
                pid=stream_info.get("pid"),
            )
            title_streams.append(stream)
            type_counts[StreamType.SUBTITLE] += 1
    return title_streams


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


_RELEASE_TITLE_SMALL_WORDS = {
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


def _release_name_tokens(name: str) -> list[str]:
    base = re.sub(r"\.(mkv|mp4|avi|iso|m2ts|vob|ts|m4v)$", "", name, flags=re.I)
    normalized = re.sub(r"[\._\-]+", " ", base).strip()
    normalized = re.sub(r"[\[\]\(\)]", " ", normalized)
    return normalized.split()


def _is_release_year(token: str) -> bool:
    return re.fullmatch(r"(19|20)\d{2}", token) is not None


def _is_episode_tag(token: str) -> bool:
    return re.fullmatch(r"s\d{1,2}e\d{1,3}", token) is not None


def _is_release_metadata(token: str) -> bool:
    if token in _RELEASE_NAME_TAGS:
        return True
    return (
        re.fullmatch(r"\d{3,4}p", token) is not None
        or re.fullmatch(r"\d{3,4}x\d{3,4}", token) is not None
    )


def _release_title_tokens(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    for token in tokens:
        normalized = token.lower().strip(":,;!?")
        if _is_release_year(normalized):
            kept.append(normalized)
            break
        if _is_episode_tag(normalized):
            kept.append(token.upper())
            break
        if _is_release_metadata(normalized):
            break
        kept.append(token)
    return kept


def _title_case_release_name(title: str) -> str:
    words = title.split()
    titled: list[str] = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if _is_episode_tag(lowered):
            titled.append(word.upper())
        elif index > 0 and lowered in _RELEASE_TITLE_SMALL_WORDS:
            titled.append(lowered)
        elif word[:1].isalpha():
            titled.append(word[:1].upper() + word[1:].lower())
        else:
            titled.append(word)
    return " ".join(titled)


def _clean_release_name(name: str) -> str:
    """Turn a release-style folder/file name into a human title."""
    tokens = _release_name_tokens(name)
    title = " ".join(_release_title_tokens(tokens)).strip(" -,.;:!?")
    if not title:
        return ""
    return _title_case_release_name(title)


_PlaylistDedupKey = tuple[tuple[str, ...], int]


def _playlist_clip_ids(title: Title) -> tuple[str, ...]:
    if title.iso_internal_paths:
        return tuple(Path(path).name for path in title.iso_internal_paths)
    return (title.source_file.name,) + tuple(
        Path(path).name for path in title.append_clips
    )


def _playlist_dedup_key(title: Title) -> _PlaylistDedupKey:
    return (_playlist_clip_ids(title), round(title.duration_seconds))


def _group_duplicate_playlists(
    titles: list[Title],
) -> tuple[dict[_PlaylistDedupKey, list[Title]], list[_PlaylistDedupKey]]:
    groups: dict[_PlaylistDedupKey, list[Title]] = {}
    order: list[_PlaylistDedupKey] = []
    for title in titles:
        key = _playlist_dedup_key(title)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(title)
    return groups, order


def _playlist_rank(title: Title) -> tuple[int, int, int]:
    return (
        len(title.chapters),
        len(title.streams),
        len(title.audio_streams) + len(title.subtitle_streams),
    )


def _best_duplicate_playlist(group: list[Title]) -> Title:
    best = group[0]
    best_rank = _playlist_rank(best)
    for title in group[1:]:
        rank = _playlist_rank(title)
        if rank > best_rank:
            best, best_rank = title, rank
    return best


def _collapse_duplicate_group(group: list[Title]) -> Title:
    best = _best_duplicate_playlist(group)
    for title in group:
        if title is not best:
            log_debug(
                f"Collapsed duplicate playlist {title.name} "
                f"(same clips+duration as {best.name})"
            )
    return best


def _dedup_duplicate_playlists(titles: list[Title]) -> list[Title]:
    """Collapse Blu-ray titles that resolve to the same clip sequence.

    Titles are grouped by clip sequence and rounded duration; only the richest
    representative is kept. First-built titles win ties, preserving the lowest
    sorted MPLS number. Different clip sets or durations remain separate.
    """
    groups, order = _group_duplicate_playlists(titles)
    if all(len(groups[key]) == 1 for key in order):
        return titles

    deduplicated: list[Title] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            deduplicated.extend(group)
        else:
            deduplicated.append(_collapse_duplicate_group(group))
    return deduplicated


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


@dataclass
class _EditionClipUnion:
    keys: list[str]
    index: dict[str, int]
    durations: list[float]
    sizes: list[int]
    starts: list[float]
    total_duration: float


def _validate_edition_titles(edition_titles: list[Title]) -> tuple[Title, bool]:
    if len(edition_titles) < 2:
        raise ValueError("multi-edition needs at least two titles")
    first = edition_titles[0]
    for title in edition_titles:
        if not title.playlist_name:
            raise ValueError(
                f"'{title.name}' is not a Blu-ray playlist title; "
                "multi-edition MKVs can only combine playlists"
            )
        if _stream_signature(title) != _stream_signature(first):
            raise ValueError(
                f"'{title.name}' has a different stream layout than '{first.name}'; "
                "editions combined into one MKV must share the same tracks"
            )

    is_iso = bool(first.iso_internal_paths)
    if any(bool(title.iso_internal_paths) != is_iso for title in edition_titles):
        raise ValueError("cannot mix ISO and folder sources in one multi-edition title")
    return first, is_iso


def _union_edition_clips(edition_titles: list[Title]) -> _EditionClipUnion:
    """Build the first-appearance clip union and its combined timeline."""
    clip_index: dict[str, int] = {}
    clip_keys: list[str] = []
    clip_durations: list[float] = []
    clip_sizes: list[int] = []

    for title in edition_titles:
        keys = _title_clip_keys(title)
        durations = title.clip_durations
        sizes = title.clip_sizes
        if len(durations) != len(keys):
            log_debug(
                f"{title.name}: clip_durations mismatch "
                f"({len(durations)} vs {len(keys)}); multi-edition atoms "
                "may be approximate"
            )
            durations = (durations + [0.0] * len(keys))[: len(keys)]
            sizes = (sizes + [0] * len(keys))[: len(keys)]

        for position, key in enumerate(keys):
            if key in clip_index:
                previous_duration = clip_durations[clip_index[key]]
                if (
                    durations[position]
                    and abs(durations[position] - previous_duration) > _EDITION_EPS
                ):
                    log_debug(
                        f"clip {Path(key).name}: duration differs between "
                        f"playlists ({previous_duration:.3f}s vs "
                        f"{durations[position]:.3f}s); using the first"
                    )
                continue

            clip_index[key] = len(clip_keys)
            clip_keys.append(key)
            clip_durations.append(durations[position])
            clip_sizes.append(sizes[position] if position < len(sizes) else 0)

    clip_starts: list[float] = []
    running_duration = 0.0
    for duration in clip_durations:
        clip_starts.append(running_duration)
        running_duration += duration

    return _EditionClipUnion(
        keys=clip_keys,
        index=clip_index,
        durations=clip_durations,
        sizes=clip_sizes,
        starts=clip_starts,
        total_duration=running_duration,
    )


def _edition_name(
    title: Title,
    first: Title,
    edition_index: int,
    edition_names: list[str] | None,
) -> str:
    if edition_names is not None:
        return edition_names[edition_index]
    if edition_index == 0:
        # The default edition carries the movie/disc name, not the scanner's
        # generic " - Title N" list label.
        return first.disc_name or first.name
    return f"Playlist {title.playlist_name}"


def _build_edition_specs(
    edition_titles: list[Title],
    first: Title,
    clip_union: _EditionClipUnion,
    edition_names: list[str] | None,
) -> list[EditionSpec]:
    if edition_names is not None and len(edition_names) != len(edition_titles):
        raise ValueError("edition name count does not match title count")

    editions: list[EditionSpec] = []
    for edition_index, title in enumerate(edition_titles):
        keys = _title_clip_keys(title)
        indices = [clip_union.index[key] for key in keys if key in clip_union.index]
        chapters = list(title.chapters)
        # Re-apply the trailing end-chapter strip relative to this edition's
        # own duration; scanners normally already did this.
        if chapters and chapters[-1] >= title.duration_seconds - 0.5:
            chapters = chapters[:-1]

        atoms = _edition_atoms(
            indices,
            clip_union.starts,
            clip_union.durations,
            chapters,
        )
        visible_count = 0
        for atom in atoms:
            if not atom.hidden:
                visible_count += 1
                atom.name = f"Chapter {visible_count:02d}"

        editions.append(
            EditionSpec(
                uid=edition_index + 1,
                name=_edition_name(title, first, edition_index, edition_names),
                is_default=(edition_index == 0),
                atoms=atoms,
            )
        )
    return editions


def _build_combined_edition_title(
    first: Title,
    clip_union: _EditionClipUnion,
    is_iso: bool,
    editions: list[EditionSpec],
) -> Title:
    base_name = first.disc_name or first.name
    if is_iso:
        combined = Title(
            first.index,
            first.source_file,
            base_name,
            clip_union.total_duration,
        )
        combined.iso_internal_paths = clip_union.keys
    else:
        combined = Title(
            first.index,
            Path(clip_union.keys[0]),
            base_name,
            clip_union.total_duration,
        )
        combined.append_clips = [Path(key) for key in clip_union.keys[1:]]

    combined.streams = [Stream(**vars(stream)) for stream in first.streams]
    combined.disc_name = first.disc_name
    combined.disc_barcode = first.disc_barcode
    combined.playlist_name = first.playlist_name
    combined.clip_durations = clip_union.durations
    combined.clip_sizes = clip_union.sizes
    combined.estimated_size_bytes = sum(clip_union.sizes)
    combined.editions = editions
    log_debug(
        f"Multi-edition title: {len(clip_union.keys)} unique clips "
        f"({clip_union.total_duration:.0f}s total), {len(editions)} editions "
        f"({', '.join(edition.name for edition in editions)})"
    )
    return combined


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
    first, is_iso = _validate_edition_titles(edition_titles)
    clip_union = _union_edition_clips(edition_titles)
    editions = _build_edition_specs(edition_titles, first, clip_union, edition_names)
    return _build_combined_edition_title(first, clip_union, is_iso, editions)


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


def _build_bluray_title_from_mpls(
    titles: list[Title],
    mpls: Path,
    clip_paths: list[Path],
    info: dict[str, Any],
    disc_name: str | None,
    disc_barcode: str | None,
) -> Title | None:
    total_duration = sum(play_item["duration"] for play_item in info["play_items"])
    title_streams = _streams_from_mpls(info.get("streams", []))
    if title_streams:
        title = Title(
            len(titles),
            clip_paths[0],
            f"Playlist {mpls.stem}",
            total_duration,
        )
        title.streams = title_streams
    else:
        log_debug(f"No STN streams in {mpls.stem}, falling back to mkvmerge")
        title = _create_title(
            titles,
            clip_paths[0],
            f"Playlist {mpls.stem}",
            override_duration=total_duration,
        )
        if title is None:
            return None
        _apply_stn_languages(title, info["audio_langs"], info["subtitle_langs"])

    title.duration_seconds = total_duration
    title.append_clips = clip_paths[1:]
    title.chapters = info.get("chapter_times", [])
    title.disc_name = disc_name
    title.disc_barcode = disc_barcode
    title.playlist_name = mpls.stem
    title.clip_durations = [play_item["duration"] for play_item in info["play_items"]]
    try:
        title.clip_sizes = [clip_path.stat().st_size for clip_path in clip_paths]
    except OSError:
        title.clip_sizes = []
    if title.chapters and title.chapters[-1] >= total_duration:
        title.chapters = title.chapters[:-1]
    if len(clip_paths) > 1:
        log_debug(f"{mpls.stem}: {len(clip_paths)} clips will be appended")
    return title


def _log_bluray_subpaths(
    playlist_stem: str, info: dict[str, Any], stream_dir: Path
) -> None:
    subpath_entries = info.get("subpath_entries", [])
    if not subpath_entries:
        return
    log_debug(f"{playlist_stem}: {len(subpath_entries)} SubPath entries")
    for subpath in subpath_entries:
        subpath_type = subpath.get("type", 0)
        subpath_clips = subpath.get("clips", [])
        if subpath_type not in (4, 6) or not subpath_clips:
            continue
        log_debug(f"  SubPath type {subpath_type}: clips {subpath_clips}")
        for subpath_clip in subpath_clips:
            clip_path = stream_dir / f"{subpath_clip}.m2ts"
            if clip_path.exists():
                log_debug(
                    f"    SubPath clip found: {subpath_clip}.m2ts "
                    f"({clip_path.stat().st_size / 1e6:.1f} MB)"
                )


def _find_bdmv_directory(source: Path) -> Path:
    return source / "BDMV" if (source / "BDMV").is_dir() else source / "bdmv"


def _read_bdmv_metadata(bdmv: Path) -> tuple[str | None, str | None]:
    disc_name = _parse_bdmv_disc_name(bdmv)
    disc_barcode = _parse_bdmv_catalog_number(bdmv)
    if disc_name:
        log_info(tr("Disc name: {name}", name=disc_name))
    if disc_barcode:
        log_debug(f"BD catalog number: {disc_barcode}")
    return disc_name, disc_barcode


def _playlist_clip_paths(
    play_items: list[dict[str, Any]], stream_dir: Path
) -> list[Path] | None:
    clip_paths: list[Path] = []
    for play_item in play_items:
        clip_path = stream_dir / f"{play_item['clip']}.m2ts"
        if not clip_path.exists():
            return None
        clip_paths.append(clip_path)
    return clip_paths or None


def _scan_playlist_titles(
    titles: list[Title],
    playlist_dir: Path,
    clpi_dir: Path | None,
    stream_dir: Path,
    disc_name: str | None,
    disc_barcode: str | None,
    config: Config | None,
) -> None:
    minimum_duration = (config or RUNTIME_STATE.config).min_duration
    for playlist in sorted(playlist_dir.glob("*.mpls")):
        playlist_info = _parse_mpls(playlist, clpi_dir=clpi_dir)
        if not playlist_info:
            continue

        play_items = playlist_info["play_items"]
        total_duration = sum(play_item["duration"] for play_item in play_items)
        if total_duration < minimum_duration:
            continue

        clip_paths = _playlist_clip_paths(play_items, stream_dir)
        if clip_paths is None:
            continue

        title = _build_bluray_title_from_mpls(
            titles,
            playlist,
            clip_paths,
            playlist_info,
            disc_name,
            disc_barcode,
        )
        if title is None:
            continue

        _log_bluray_subpaths(playlist.stem, playlist_info, stream_dir)
        titles.append(title)
        log_debug(
            f"Built from MPLS (native): {playlist.stem}, "
            f"duration={total_duration:.0f}s, {len(title.streams)} streams"
        )


def _fallback_raw_m2ts_dir(bdmv: Path) -> Path | None:
    stream_dir = bdmv / "STREAM"
    return stream_dir if stream_dir.is_dir() else None


def _scan_bluray_source(
    source: Path, config: Config | None = None
) -> tuple[list[Title], str | None]:
    """Scan a Blu-ray BDMV directory and return (titles, disc_name)."""
    bdmv = _find_bdmv_directory(source)
    titles: list[Title] = []
    disc_name, disc_barcode = _read_bdmv_metadata(bdmv)

    clpi_dir = bdmv / "CLIPINF"
    playlist_dir = bdmv / "PLAYLIST"
    stream_dir = bdmv / "STREAM"
    if playlist_dir.is_dir():
        _scan_playlist_titles(
            titles,
            playlist_dir,
            clpi_dir if clpi_dir.is_dir() else None,
            stream_dir,
            disc_name,
            disc_barcode,
            config,
        )

    if not titles:
        raw_stream_dir = _fallback_raw_m2ts_dir(bdmv)
        if raw_stream_dir is not None:
            _scan_m2ts_dir(raw_stream_dir, titles)

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


def _first_iso_playlist_clpi(
    playlist_path: Path, extracted_clpi: dict[str, Path]
) -> Path | None:
    """Read the first play-item clip name from a raw MPLS blob."""
    try:
        data = playlist_path.read_bytes()
        if len(data) < 40 or data[0:4] != b"MPLS":
            return None
        play_items_position = _read_u32(data, 8) + 10
        if play_items_position + 2 > len(data):
            return None
        play_item_length = _read_u16(data, play_items_position)
        play_item = data[
            play_items_position + 2 : play_items_position + 2 + play_item_length
        ]
        if len(play_item) < 32:
            return None
        clip_name = play_item[0:5].decode("ascii", "ignore")
        return extracted_clpi.get(clip_name)
    except (OSError, IndexError, ValueError):
        return None


def _iso_clip_internals(
    play_items: list[dict[str, Any]], m2ts_by_clip: dict[str, str]
) -> list[str]:
    clip_internals: list[str] = []
    for play_item in play_items:
        internal_path = m2ts_by_clip.get(play_item["clip"])
        if internal_path:
            clip_internals.append(internal_path)
        else:
            log_debug(f"  Clip {play_item['clip']}.m2ts not found in ISO")
    return clip_internals


def _build_iso_bluray_playlist_title(
    *,
    playlist_path: Path,
    index: int,
    source_file: Path,
    extracted_clpi: dict[str, Path],
    m2ts_by_clip: dict[str, str],
    sizes: dict[str, int],
    minimum_duration: float,
) -> Title | None:
    first_clip_clpi = _first_iso_playlist_clpi(playlist_path, extracted_clpi)
    clpi_dir = (
        first_clip_clpi.parent
        if first_clip_clpi is not None and first_clip_clpi.suffix.lower() == ".clpi"
        else None
    )

    playlist_info = _parse_mpls(playlist_path, clpi_dir=clpi_dir)
    if not playlist_info:
        log_debug(f"MPLS parse failed for {playlist_path.name}")
        return None

    play_items = playlist_info["play_items"]
    total_duration = sum(play_item["duration"] for play_item in play_items)
    if total_duration < minimum_duration:
        return None

    clip_internals = _iso_clip_internals(play_items, m2ts_by_clip)
    if not clip_internals:
        log_debug(f"  No M2TS files found for {playlist_path.name}, skipping")
        return None

    title_streams = _streams_from_mpls(playlist_info.get("streams", []))
    if not title_streams:
        log_debug(f"  No usable streams from {playlist_path.name}, skipping")
        return None

    title = Title(
        index,
        source_file,
        f"Playlist {playlist_path.stem}",
        total_duration,
    )
    title.streams = title_streams
    title.chapters = playlist_info.get("chapter_times", [])
    if title.chapters and title.chapters[-1] >= total_duration:
        title.chapters = title.chapters[:-1]
    title.iso_internal_paths = clip_internals
    title.estimated_size_bytes = sum(
        sizes.get(internal_path, 0) for internal_path in clip_internals
    )
    title.playlist_name = playlist_path.stem
    title.clip_durations = [play_item["duration"] for play_item in play_items]
    title.clip_sizes = [sizes.get(internal_path, 0) for internal_path in clip_internals]
    return title


def _scanned_title_sort_key(title: Title) -> tuple[int, int, float]:
    # Episodes first, other titles by duration, then the play-all chain last.
    if title.dvd_play_all:
        group = 2
    elif title.dvd_episode_number is not None:
        group = 0
    else:
        group = 1
    return (
        group,
        title.dvd_episode_number if title.dvd_episode_number is not None else 0,
        -title.duration_seconds,
    )


def _sort_and_reindex_titles(titles: list[Title]) -> None:
    titles.sort(key=_scanned_title_sort_key)
    for index, title in enumerate(titles):
        title.index = index


def _resolve_iso_source(source: Path) -> Path:
    if not source.is_dir():
        return source

    isos = sorted(source.glob("*.iso"))
    if not isos:
        log_warn(tr("No ISO file found in {path}", path=source))
        return source

    selected = isos[0]
    log_info(tr("Using ISO file in directory: {name}", name=selected.name))
    return selected


@final
class Scanner:
    def __init__(
        self,
        source: Path,
        config: Config | None = None,
        runtime_state: RuntimeState | None = None,
    ):
        state = runtime_state or RUNTIME_STATE
        self.source = source
        self.config = config if config is not None else state.config
        self.cleanup = state.cleanup
        self.titles: list[Title] = []
        self.disc_name: str | None = None

    def _scan_source_type(self, source_type) -> None:
        from disc_reader import SourceType

        if source_type in (SourceType.DVD, SourceType.DVD_RAW):
            self.titles, self.disc_name = _scan_dvd_source(self.source, self.config)
        elif source_type == SourceType.BLURAY:
            self.titles, self.disc_name = _scan_bluray_source(self.source, self.config)
        elif source_type == SourceType.BLURAY_RAW:
            self.titles, self.disc_name = _scan_bluray_raw_source(self.source)
        elif source_type == SourceType.VIDEO_FILE:
            self.titles = _scan_video_source(self.source)
        elif source_type == SourceType.DEVICE:
            self.titles = _scan_device_source(self.source)

    def scan(self) -> list[Title]:
        from disc_reader import SourceType, detect_source_type

        source_type = detect_source_type(self.source)
        log_info(tr("Source type: {type}", type=source_type.value))
        if source_type == SourceType.ISO_UNKNOWN:
            self.source = _resolve_iso_source(self.source)
            self._scan_iso()
        else:
            self._scan_source_type(source_type)

        _sort_and_reindex_titles(self.titles)
        if self.titles:
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
        main_idx = pick_main_feature(self.titles, self.config)
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
        from disc_reader import _list_iso_files_7z

        log_info(tr("Scanning ISO with 7z..."))
        paths, sizes = _list_iso_files_7z(self.source, self.cleanup.symlinks)
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
            self._scan_iso_bluray(paths, sizes, mpls_files, m2ts_files)
        if not mpls_files and m2ts_files:
            self._scan_iso_raw_m2ts(m2ts_files, sizes)
        elif not mpls_files and not m2ts_files:
            self._scan_iso_dvd(paths, sizes)

        if mpls_files:
            self.titles = _dedup_duplicate_playlists(self.titles)

    def _scan_iso_raw_m2ts(self, m2ts_files: list[str], sizes: dict[str, int]) -> None:
        from disc_reader import _extract_partial_7z

        for internal_path in m2ts_files:
            if tmp := _extract_partial_7z(
                self.source,
                internal_path,
                temp_files=self.cleanup.temp_files,
                symlinks=self.cleanup.symlinks,
            ):
                if title := _create_title(self.titles, tmp, Path(internal_path).stem):
                    title.source_file = self.source
                    title.iso_internal_paths = [internal_path]
                    title.estimated_size_bytes = sizes.get(internal_path, 0)
                    self.titles.append(title)
                tmp.unlink(missing_ok=True)

    def _scan_iso_bluray(
        self,
        paths: list[str],
        sizes: dict[str, int],
        mpls_files: list[str],
        m2ts_files: list[str],
    ) -> None:
        from disc_reader import _extract_with_7z

        tmp_dir = Path(tempfile.mkdtemp(prefix="mkv_scan_"))
        self.cleanup.register_temp_dir(tmp_dir)
        m2ts_by_clip = {Path(path).stem: path for path in m2ts_files}
        clpi_internal = {
            Path(path).stem: path for path in paths if path.lower().endswith(".clpi")
        }
        bdmt_files = [
            path
            for path in paths
            if path.lower().endswith(".xml")
            and "meta" in path.lower()
            and Path(path).stem.startswith("bdmt")
        ]

        files_to_extract = list(mpls_files)
        files_to_extract.extend(clpi_internal.values())
        files_to_extract.extend(bdmt_files)
        extracted_paths = _extract_with_7z(
            self.source, files_to_extract, tmp_dir, self.cleanup.symlinks
        )
        extracted_clpi = {
            path.stem: path
            for path in extracted_paths
            if path.suffix.lower() == ".clpi"
        }
        if bdmt_files and not self.disc_name:
            self._read_iso_disc_name(extracted_paths)

        for ext_path in extracted_paths:
            if ext_path.suffix.lower() == ".mpls":
                self._build_iso_bluray_title(
                    ext_path, extracted_clpi, m2ts_by_clip, sizes
                )

        if not self.titles:
            log_debug(
                f"Found {len(mpls_files)} MPLS file(s) in ISO but none produced a "
                f"valid title (parse failure, zero duration, no streams, "
                "or missing M2TS clips)."
            )

    def _read_iso_disc_name(self, extracted_paths: list[Path]) -> None:
        for ext_path in extracted_paths:
            if ext_path.suffix.lower() != ".xml" or not ext_path.stem.startswith(
                "bdmt"
            ):
                continue
            try:
                for elem in ET.parse(ext_path).iter():
                    if elem.tag.endswith("name") and elem.text and elem.text.strip():
                        raw = elem.text.strip()
                        self.disc_name = (
                            raw.replace("\r\n", " - ")
                            .replace("\r", " - ")
                            .replace("\n", " - ")
                        )
                        log_info(
                            tr("Disc name from bdmt.xml: {name}", name=self.disc_name)
                        )
                        break
            except (ET.ParseError, OSError, UnicodeError, LookupError) as exc:
                log_debug(f"Failed to parse bdmt.xml: {exc}")
            break

    def _scan_iso_dvd(self, paths: list[str], sizes: dict[str, int]) -> None:
        from disc_reader import _extract_with_7z

        vob_files = sorted(
            path
            for path in paths
            if path.lower().endswith(".vob") and path.upper().startswith("VIDEO_TS/")
        )
        ifo_files = sorted(
            path
            for path in paths
            if path.lower().endswith(".ifo") and path.upper().startswith("VIDEO_TS/")
        )
        if not vob_files or not ifo_files:
            log_debug(
                "7z listed files from the ISO, but none matched BDMV/VIDEO_TS "
                "paths (.mpls, .m2ts, or .vob). The ISO may not be a video disc."
            )
            return

        log_info(tr("Detected DVD VIDEO_TS structure in ISO"))
        tmp_dir = Path(tempfile.mkdtemp(prefix="mkv_scan_"))
        self.cleanup.register_temp_dir(tmp_dir)
        vts_first_vob, vts_all_vobs = self._dvd_iso_vob_maps(vob_files)
        extracted = _extract_with_7z(
            self.source, ifo_files, tmp_dir, self.cleanup.symlinks
        )
        vmg_info, disc_barcode = self._parse_iso_vmg(extracted)
        self._scan_iso_dvd_vts(
            extracted,
            vts_first_vob,
            vts_all_vobs,
            self._vts_title_numbers(vmg_info),
            disc_barcode,
            sizes,
        )

    @staticmethod
    def _dvd_iso_vob_maps(
        vob_files: list[str],
    ) -> tuple[dict[int, str], dict[int, list[str]]]:
        vts_first_vob: dict[int, str] = {}
        for internal_path in vob_files:
            match = re.search(r"VTS_(\d+)_1\.VOB$", internal_path, re.IGNORECASE)
            if match:
                vts_first_vob.setdefault(int(match.group(1)), internal_path)

        vts_all_vobs: dict[int, list[str]] = {}
        for internal_path in vob_files:
            match = re.search(r"VTS_(\d+)_(\d+)\.VOB$", internal_path, re.IGNORECASE)
            if match and int(match.group(2)) >= 1:
                vts_all_vobs.setdefault(int(match.group(1)), []).append(internal_path)

        def part_number(internal_path: str) -> int:
            match = re.search(r"_(\d+)\.VOB$", internal_path)
            return int(match.group(1)) if match else 0

        for parts in vts_all_vobs.values():
            parts.sort(key=part_number)
        return vts_first_vob, vts_all_vobs

    def _parse_iso_vmg(
        self, extracted: list[Path]
    ) -> tuple[VmgInfo | None, str | None]:
        vmg_path = next(
            (path for path in extracted if path.name.upper() == "VIDEO_TS.IFO"),
            None,
        )
        if not vmg_path or not vmg_path.exists():
            return None, None

        vmg_info: VmgInfo | None = None
        try:
            vmg_info = _parse_vmg_ifo(vmg_path)
        except DvdIfoError as exc:
            log_debug(f"VMG IFO parse failed: {exc}")
        if not vmg_info:
            return None, None

        disc_name = vmg_info.get("disc_name")
        if disc_name:
            self.disc_name = disc_name
        return vmg_info, vmg_info.get("barcode")

    @staticmethod
    def _vts_title_numbers(vmg_info: VmgInfo | None) -> dict[int, int]:
        title_map = vmg_info.get("title_map") if vmg_info else None
        if not title_map:
            return {}

        vts_to_title_num: dict[int, int] = {}
        for title_idx, (vts_num, _ttl_num) in title_map.items():
            vts_to_title_num.setdefault(vts_num, title_idx)
        return vts_to_title_num

    def _scan_iso_dvd_vts(
        self,
        extracted: list[Path],
        vts_first_vob: dict[int, str],
        vts_all_vobs: dict[int, list[str]],
        vts_to_title_num: dict[int, int],
        disc_barcode: str | None,
        sizes: dict[str, int],
    ) -> None:
        from disc_reader import _extract_partial_7z

        for ifo_path in sorted(extracted):
            match = re.search(r"VTS_(\d+)_0\.IFO$", ifo_path.name, re.IGNORECASE)
            if not match:
                continue
            vts = int(match.group(1))
            first_vob_internal = vts_first_vob.get(vts)
            if not first_vob_internal:
                continue

            first_vob_extracted = _extract_partial_7z(
                self.source,
                first_vob_internal,
                temp_files=self.cleanup.temp_files,
                symlinks=self.cleanup.symlinks,
            )
            if not first_vob_extracted:
                continue
            first_vob_extracted = self._ensure_vob_suffix(
                first_vob_extracted, self.cleanup.temp_files
            )

            logical_title = vts_to_title_num.get(vts)
            title_name = f"Title {vts}"
            if logical_title:
                title_name = f"Title {logical_title} (VTS {vts})"
                log_debug(f"TT_SRPT: VTS {vts} -> DVD Title {logical_title}")

            title = _build_title_from_ifo(
                self.titles,
                first_vob_extracted,
                ifo_path,
                [first_vob_extracted],
                vts,
                title_name=title_name,
                config=self.config,
            )
            if title is None:
                if title := _create_title(self.titles, first_vob_extracted, title_name):
                    title.disc_name = self.disc_name
                    title.disc_barcode = disc_barcode
                    _apply_dvd_ifo_languages(title, ifo_path)
                    self._apply_iso_dvd_source(
                        title,
                        vts,
                        first_vob_internal,
                        vts_all_vobs,
                        disc_barcode,
                        sizes,
                    )
                    self.titles.append(title)
                continue

            self._apply_iso_dvd_source(
                title,
                vts,
                first_vob_internal,
                vts_all_vobs,
                disc_barcode,
                sizes,
            )
            self.titles.append(title)
            self._scan_iso_dvd_alternate_editions(
                ifo_path,
                first_vob_extracted,
                title_name,
                vts,
                first_vob_internal,
                vts_all_vobs,
                disc_barcode,
                sizes,
            )

    @staticmethod
    def _ensure_vob_suffix(extracted: Path, temp_files: list[Path]) -> Path:
        if extracted.suffix.lower() == ".vob":
            return extracted
        renamed = extracted.with_suffix(".vob")
        try:
            extracted.rename(renamed)
        except OSError:
            return extracted
        temp_files.append(renamed)
        return renamed

    def _apply_iso_dvd_source(
        self,
        title: Title,
        vts: int,
        first_vob_internal: str,
        vts_all_vobs: dict[int, list[str]],
        disc_barcode: str | None,
        sizes: dict[str, int],
    ) -> None:
        title.source_file = self.source
        title.iso_internal_paths = vts_all_vobs.get(vts, [first_vob_internal])
        title.estimated_size_bytes = sum(
            sizes.get(internal_path, 0) for internal_path in title.iso_internal_paths
        )
        title.disc_name = self.disc_name
        title.disc_barcode = disc_barcode

    def _scan_iso_dvd_alternate_editions(
        self,
        ifo_path: Path,
        first_vob_extracted: Path,
        title_name: str,
        vts: int,
        first_vob_internal: str,
        vts_all_vobs: dict[int, list[str]],
        disc_barcode: str | None,
        sizes: dict[str, int],
    ) -> None:
        try:
            ifo_bytes = ifo_path.read_bytes()
        except OSError as exc:
            log_debug(f"Alternate-edition PGC scan skipped for {ifo_path.name}: {exc}")
            ifo_bytes = b""

        extra_pgcs = (
            _find_alternate_edition_pgcs(ifo_bytes, self.config.min_duration)
            if ifo_bytes
            else []
        )
        for edition_num, pgc_num in enumerate(extra_pgcs, start=2):
            edition_name = f"{title_name} - Edition {edition_num}"
            alternate = _build_title_from_ifo(
                self.titles,
                first_vob_extracted,
                ifo_path,
                [first_vob_extracted],
                vts,
                title_name=edition_name,
                pgc_number=pgc_num,
                config=self.config,
            )
            if alternate is None:
                continue
            self._apply_iso_dvd_source(
                alternate,
                vts,
                first_vob_internal,
                vts_all_vobs,
                disc_barcode,
                sizes,
            )
            alternate.dvd_edition_label = f"Edition {edition_num}"
            self.titles.append(alternate)
            log_debug(
                f"  Alternate edition: PGC {pgc_num} "
                f"({alternate.duration_seconds:.0f}s) exposed as "
                f"'{edition_name}'"
            )

    def _build_iso_bluray_title(
        self,
        ext_path: Path,
        extracted_clpi: dict[str, Path],
        m2ts_by_clip: dict[str, str],
        sizes: dict[str, int],
    ) -> None:
        title = _build_iso_bluray_playlist_title(
            playlist_path=ext_path,
            index=len(self.titles),
            source_file=self.source,
            extracted_clpi=extracted_clpi,
            m2ts_by_clip=m2ts_by_clip,
            sizes=sizes,
            minimum_duration=self.config.min_duration,
        )
        if title is None:
            return
        self.titles.append(title)
        log_debug(
            f"Built from MPLS: {ext_path.name}, "
            f"duration={title.duration_seconds:.0f}s, "
            f"{len(title.streams)} streams, "
            f"{len(title.iso_internal_paths)} clips"
        )

    def _scan_iso_mount(self) -> None:
        """Mount the ISO via ``sudo mount -o loop,ro`` and scan the result."""
        from disc_reader import _try_direct_mount

        if self.config.no_sudo:
            log_info(tr("Skipping direct mount (--no-sudo is set)"))
            return
        mnt = _try_direct_mount(
            self.source,
            self.config,
            direct_mounts=self.cleanup.direct_mounts,
        )
        if not mnt:
            log_error("All ISO reading methods failed.")
            log_error(f"Try: sudo mount -o loop,ro '{self.source}' /mnt/iso")
            return
        log_info(f"Direct mount succeeded at {mnt}")
        if (mnt / "BDMV").is_dir():
            blu_titles, disc_name = _scan_bluray_source(mnt, self.config)
            self.titles.extend(blu_titles)
            if disc_name:
                self.disc_name = disc_name
        elif (mnt / "VIDEO_TS").is_dir():
            dvd_titles, disc_name = _scan_dvd_source(mnt, self.config)
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
            except OSError:
                pass
            self.cleanup.unregister_direct_mount(mnt)


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


def _get_notable_titles(
    titles: list[Title], config: Config | None = None
) -> tuple[list[Title], int]:
    """Return (notable, hidden_count).

    Respects ``config.show_all`` — when set, all titles are returned as notable
    and hidden_count is always 0.
    """
    if (config or RUNTIME_STATE.config).show_all:
        return titles, 0
    notable = [t for t in titles if _is_notable_title(t)]
    return notable, len(titles) - len(notable)


def _main_feature_score(
    title: Title, config: Config | None = None
) -> tuple[int, int, float]:
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

    Titles shorter than ``config.min_duration`` (default 60s) are excluded
    from main-feature consideration — they are almost always menus, trailers,
    or warning cards with unusually rich stream tables.
    """
    if title.duration_seconds < (config or RUNTIME_STATE.config).min_duration:
        return (-1, 0, 0.0)
    richness = len(title.audio_streams) + len(title.subtitle_streams)
    is_default_edition = 0 if title.dvd_pgc_number is not None else 1
    return (richness, is_default_edition, title.duration_seconds)


def pick_main_feature(titles: list[Title], config: Config | None = None) -> int:
    """Return the index of the best main-feature candidate, or -1 if empty."""
    if not titles:
        return -1
    best = max(titles, key=lambda title: _main_feature_score(title, config))
    return best.index
