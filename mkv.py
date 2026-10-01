"""
mkvmerge muxing.

Extracted from main.py: the MKVCreator class (mux command construction,
DVD cell/VOBU trimming, DVD subtitle extraction fallback, progress + timeout
handling), mkvmerge track identification, Matroska chapters / tags XML
writers, and the stream-selection helpers used by the muxer and the CLI.

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

import bisect
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TypedDict, cast, final
from collections.abc import Callable

from dvdifo import (
    _AUDIO_CHANNEL_TITLES,
    _lookup_main_feature_range,
    _parse_vts_vobu_admap,
    _build_main_edition_vobu_ranges,
    _extract_dvd_ifo_palette,
)
from vobsub import (
    _dvd_main_content_range,
    _extract_concat_range,
    _extract_dvd_vobsubs,
    _scan_evo_video_stream_id,
    _vobsub_pts_offset,
)
from cc608 import (
    CC608_CODECS,
    extract_cc608_captions,
    write_cc608_ass,
    write_cc608_srt,
)
from models import (
    Config,
    DiscMetadata,
    EditionAtom,
    EditionSpec,
    StreamType,
    Stream,
    Title,
    TagOptions,
    RipError,
    get_language_name,
    log_info,
    log_warn,
    log_debug,
    _HAS_MKVMERGE,
    _kill_process_group,
    RuntimeState,
    RUNTIME_STATE,
    UserPrompts,
)
from m2ts import (
    is_bluray_audio_pid,
    null_packets_from,
    plan_seamless_trim,
    scan_clip_tail,
)
from probe import _MKVMERGE_CODEC_MAP
from i18n import tr

if TYPE_CHECKING:
    from tagger import ArtAttachment, MovieMetadata


# =============================================================================
# Muxing
# =============================================================================


def select_streams(
    title: Title,
    force: list[str] | None = None,
    config: Config | None = None,
) -> list[Stream]:
    effective_config = config or RUNTIME_STATE.config
    if force:
        return _select_explicit(title, force)
    sel: list[Stream] = [title.video_streams[0]] if title.video_streams else []
    sel.extend(
        s
        for s in title.audio_streams
        if s.is_muxable
        and (
            effective_config.keep_all_audio
            or s.language in effective_config.preferred_languages
        )
    )
    sel.extend(
        s
        for s in title.subtitle_streams
        if s.is_muxable
        and effective_config.keep_all_subtitles
        and (effective_config.include_forced or not s.is_forced)
    )
    return sel


_SELECTOR_STREAM_TYPES: dict[str, StreamType] = {
    "v": StreamType.VIDEO,
    "a": StreamType.AUDIO,
    "s": StreamType.SUBTITLE,
}

_ALL_SELECTOR_SUFFIX = "all"


def _streams_of_type(title: Title, stream_type: StreamType) -> list[Stream]:
    return [stream for stream in title.streams if stream.stream_type == stream_type]


def _streams_for_selector(
    title: Title, stream_type: StreamType, value: str
) -> list[Stream]:
    if value == _ALL_SELECTOR_SUFFIX:
        return _streams_of_type(title, stream_type)
    if value.isalpha() and len(value) == 3:
        return [
            stream
            for stream in title.streams
            if stream.stream_type == stream_type and stream.language == value
        ]
    try:
        type_index = int(value)
    except ValueError:
        return []
    return [
        stream
        for stream in title.streams
        if stream.stream_type == stream_type and stream.type_index == type_index
    ]


def _select_streams_for_selector(title: Title, selector: str) -> list[Stream]:
    prefix, separator, value = selector.partition(":")
    if not separator:
        return []
    stream_type = _SELECTOR_STREAM_TYPES.get(prefix)
    if stream_type is None:
        return []
    return _streams_for_selector(title, stream_type, value)


def _deduplicate_streams(streams: list[Stream]) -> list[Stream]:
    seen_indexes: set[int] = set()
    unique: list[Stream] = []
    for stream in streams:
        if stream.index not in seen_indexes:
            seen_indexes.add(stream.index)
            unique.append(stream)
    return unique


def _select_explicit(title: Title, selectors: list[str]) -> list[Stream]:
    selected: list[Stream] = []
    for selector in selectors:
        selected.extend(_select_streams_for_selector(title, selector))
    return _deduplicate_streams(selected)


# CICP (ISO/IEC 23001-8) numeric codes for mkvmerge's --color-* options.
# "unknown" maps to 2 (unspecified) so partially-signalled streams still get
# an explicit value rather than the option being skipped.
_COLOR_CICP: dict[str, int] = {
    "unknown": 2,
    "bt709": 1,
    "bt470bg": 5,
    "smpte170m": 6,
    "bt2020": 9,
    "bt2020nc": 9,
    "smpte2084": 16,
}

# Matroska Colour/Range element: 1 = limited (broadcast), 2 = full.
_COLOR_RANGE: dict[str, int] = {
    "tv": 1,
    "limited": 1,
    "pc": 2,
    "full": 2,
}


def _chroma_siting_for_codec(codec: str) -> str | None:
    """Return "hori,vert" chroma siting for codecs that cannot signal it.

    MPEG-2 has no bitstream field for chroma siting: the position of the
    4:2:0 chroma planes is fixed by the standard (collocated with the left
    luma column, vertically midway between rows).  Matroska expresses that
    as ChromaSitingHorz=1 (left collocated) / ChromaSitingVert=2 (half) —
    the same mapping FFmpeg's libavformat/matroskaenc.c uses for
    AVCHROMA_LOC_LEFT ("MPEG-2 style").  Signalling it explicitly completes
    the track header; players already assume this convention when the
    element is absent.

    Other codecs (H.264/HEVC VUI chroma_sample_position) *can* signal siting
    in the bitstream, which mkvsmith does not parse — passing a guess would
    risk overriding a real value, so they get None.
    """
    if codec == "mpeg2video":
        return "1,2"
    return None


_SDTV_COLOR_HEIGHTS = (480, 576)
_HD_COLOR_HEIGHTS = (720, 1080, 2160)


def _infer_video_color_fields(height: int | None) -> tuple[str, str, str] | None:
    if height in _HD_COLOR_HEIGHTS:
        # HD Blu-ray/AVC is virtually always BT.709 end-to-end, and UHD without
        # explicit HDR signalling defaults to SDR BT.709 as well.
        return "bt709", "bt709", "bt709"
    if height in _SDTV_COLOR_HEIGHTS:
        # SDTV/DVD defaults use BT.601 primaries/matrix with the BT.709 transfer
        # function; SMPTE 170M and Rec.709 define identical OETF curves.
        if height == 576:  # PAL/SECAM
            return "bt470bg", "bt709", "bt470bg"
        return "smpte170m", "bt709", "smpte170m"  # NTSC / 480-line
    return None


def _finalize_video_color(
    primaries: str | None,
    transfer: str | None,
    matrix: str | None,
    color_range: str,
) -> tuple[str, str, str, str] | None:
    if primaries is None and transfer is None and matrix is None:
        return None
    return (
        primaries or "unknown",
        transfer or "unknown",
        matrix or "unknown",
        color_range,
    )


def _resolve_video_color(stream: Stream) -> tuple[str, str, str, str] | None:
    """Return colour fields to apply to a video stream.

    Source signalling is preferred. When primaries are absent and resolution is
    known, standard DVD/Blu-ray defaults are inferred; partial signalling is
    padded with ``unknown`` so the muxer still writes explicit fields.
    """
    primaries = stream.color_primaries
    transfer = stream.color_transfer
    matrix = stream.color_space
    color_range = stream.color_range or "tv"

    if primaries is None:
        inferred = _infer_video_color_fields(stream.height)
        if inferred is not None:
            primaries, transfer, matrix = inferred

    return _finalize_video_color(primaries, transfer, matrix, color_range)


# _AUDIO_CHANNEL_TITLES now lives in dvdifo.py (imported explicitly above).


def _audio_title(stream: Stream, channel_count: int | None = None) -> str | None:
    """Synthesise a track title (e.g. 'AC3 Surround 5.1') from codec and channel count.

    DVDs carry no audio title metadata; the reference ripper generates labels from the channel
    configuration. We extend this with the codec name (from ``codec_display``) so
    that titles are informative even when IFO attribute parsing is unavailable.

    ``channel_count`` overrides ``stream.channels`` when supplied. This is used at
    mux time to pass the accurate channel count from ``mkvmerge -J`` (which reads
    the codec bitstream), since the scan-time CLPI channel count can be wrong —
    e.g. BD's CLPI has no 5.0 config code, so a 5.0 mix is stored as the 5.1
    config and over-counted to 6 channels. Falling back to ``stream.channels``
    keeps the old behaviour when no accurate count is available.
    """
    n = channel_count if channel_count else stream.channels
    ch_title = _AUDIO_CHANNEL_TITLES.get(n) if n else None
    if ch_title is None:
        return None
    codec_label = stream.codec_display
    return f"{codec_label} {ch_title}" if codec_label else ch_title


# =============================================================================
# Metadata tagging (TMDB) — extracted to ``tagger.py``
# =============================================================================
# TMDB metadata fetching and Matroska tag/artwork generation lives in the
# separate ``tagger`` module.  Functions are imported lazily (at call site) to
# keep the import graph acyclic: ``tagger`` imports a few low-level symbols
# from ``models``, so this module must not import ``tagger`` at module level
# (only type-checking imports above).


# =============================================================================
# New helpers for mkvmerge (MKVToolNix) muxing
# =============================================================================


def _identify_input_tracks(path: Path) -> list[dict[str, Any]]:
    """Run ``mkvmerge -J`` on *path* and return the track list.

    Returns a list of dicts with keys:
      id         — global 0-based track ID within the input
      type       — "video", "audio", or "subtitles"
      codec      — human-readable codec string (e.g. "AVC/H.264")
      properties — dict with ``number`` (stream ID / PID decimal),
                   ``language``, ``default_track``, ``forced_track``, etc.

    Returns an empty list on any failure (mkvmerge not found, bad file, etc.).
    """
    if not _HAS_MKVMERGE:
        return []
    try:
        proc = subprocess.run(
            ["mkvmerge", "-J", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            log_debug(
                f"mkvmerge -J failed (rc={proc.returncode}): {(proc.stderr or '').strip()}"
            )
            return []
        data = json.loads(proc.stdout)
        return data.get("tracks", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log_debug(f"mkvmerge -J exception: {exc}")
        return []


def _ns_to_ts(ns: int) -> str:
    """Format nanoseconds as HH:MM:SS.nnnnnnnnn (mkvmerge chapter format)."""
    hh, rem = divmod(ns, 3_600_000_000_000)
    mm, rem = divmod(rem, 60_000_000_000)
    ss, frac = divmod(rem, 1_000_000_000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{frac:09d}"


def _write_multi_edition_chapters_xml(
    editions: list[EditionSpec], out: Path, lang: str = "eng"
) -> None:
    """Write a multi-edition ordered-chapters XML for mkvmerge's ``--chapters``.

    One ``EditionEntry`` per edition (all ``EditionFlagOrdered``; the first is
    the default). Each atom carries explicit start/end times on the combined
    timeline; hidden atoms are segment-boundary continuations of a chapter
    that spans a branch point and get no ``ChapterDisplay`` element. This is
    the "magic chapter file" multi-edition tools hand to mkvmerge — validated
    against mkvmerge v96 (editions, ordered flags, out-of-order atoms and
    hidden flags all round-trip; see tests/test_editions.py).
    """
    root = ET.Element("Chapters")
    uid_counter = 1000  # deterministic atom UIDs, disjoint from edition UIDs
    for ed in editions:
        edition = ET.SubElement(root, "EditionEntry")
        ET.SubElement(edition, "EditionUID").text = str(ed.uid)
        ET.SubElement(edition, "EditionFlagHidden").text = "0"
        ET.SubElement(edition, "EditionFlagDefault").text = (
            "1" if ed.is_default else "0"
        )
        ET.SubElement(edition, "EditionFlagOrdered").text = "1"
        for atom in ed.atoms:
            uid_counter += 1
            entry = ET.SubElement(edition, "ChapterAtom")
            ET.SubElement(entry, "ChapterUID").text = str(uid_counter)
            ET.SubElement(entry, "ChapterTimeStart").text = _ns_to_ts(
                int(round(atom.start * 1_000_000_000))
            )
            ET.SubElement(entry, "ChapterTimeEnd").text = _ns_to_ts(
                int(round(atom.end * 1_000_000_000))
            )
            ET.SubElement(entry, "ChapterFlagHidden").text = "1" if atom.hidden else "0"
            ET.SubElement(entry, "ChapterFlagEnabled").text = "1"
            if not atom.hidden:
                display = ET.SubElement(entry, "ChapterDisplay")
                ET.SubElement(display, "ChapterString").text = atom.name or ""
                ET.SubElement(display, "ChapterLanguage").text = lang
    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    out.write_bytes(xml_bytes)


def _write_chapters_xml(times: list[float], out: Path, lang: str = "eng") -> None:
    """Write a Matroska Chapters XML file for mkvmerge's ``--chapters`` option.

    Chapter names are not prefixed with the language code (the ``ChapterLanguage``
    element handles that for mediainfo).  A language-code prefix is unnecessary
    and would produce a doubled language label like ``en:eng:Chapter 01``.
    Timestamps use nanosecond precision as mkvmerge expects.
    """
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")
    ET.SubElement(edition, "EditionUID").text = "1"
    ET.SubElement(edition, "EditionFlagHidden").text = "0"
    ET.SubElement(edition, "EditionFlagDefault").text = "1"
    ET.SubElement(edition, "EditionFlagOrdered").text = "0"

    for i, t in enumerate(times):
        start_ns = int(round(t * 1_000_000_000))
        end_ns = (
            int(round(times[i + 1] * 1_000_000_000)) if i + 1 < len(times) else start_ns
        )

        atom = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterUID").text = str(i + 1)

        ET.SubElement(atom, "ChapterTimeStart").text = _ns_to_ts(start_ns)
        ET.SubElement(atom, "ChapterTimeEnd").text = _ns_to_ts(end_ns)

        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text = f"Chapter {i + 1:02d}"
        ET.SubElement(display, "ChapterLanguage").text = lang

    # Pretty-print the XML for readability, then write.
    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    out.write_bytes(xml_bytes)


def _write_tags_xml_mkvmerge(
    out_path: Path,
    md: MovieMetadata | None = None,
    editions: list[EditionSpec] | None = None,
) -> None:
    """Write a Matroska Tags XML file for ``--global-tags``.

    Adds TMDB metadata fields (if *md* is provided) and, for multi-edition
    rips, one ``TITLE`` tag per edition targeting its ``EditionUID`` — the
    mechanism players use to name the cuts in their edition picker
    (mirrors xin1generator's TagsGenerator). ``--global-tags`` preserves
    ``<Targets>`` elements including ``EditionUID`` (verified against
    mkvmerge v96), so the edition linkage survives the mux.
    """
    from tagger import _TAG_FIELDS

    root = ET.Element("Tags")

    def add_simple(parent: ET.Element, name: str, value: str) -> None:
        simple = ET.SubElement(parent, "Simple")
        ET.SubElement(simple, "Name").text = name
        ET.SubElement(simple, "String").text = value

    if md is not None:
        tag = ET.SubElement(root, "Tag")
        targets = ET.SubElement(tag, "Targets")
        ET.SubElement(targets, "TargetTypeValue").text = "50"
        for attr, name, fmt in _TAG_FIELDS:
            value = getattr(md, attr)
            if not value:
                continue
            add_simple(tag, name, fmt(value) if fmt else str(value))
        for name, value in md.custom_properties.items():
            add_simple(tag, name.upper(), str(value))

    for ed in editions or []:
        tag = ET.SubElement(root, "Tag")
        targets = ET.SubElement(tag, "Targets")
        ET.SubElement(targets, "EditionUID").text = str(ed.uid)
        add_simple(tag, "TITLE", ed.name)

    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    out_path.write_bytes(xml_bytes)


class MappedStream(TypedDict):
    input_id: int
    type: str
    stream: Stream
    ident_channels: int | None


def _ident_tracks_by_position(
    ident_tracks: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    type_counter: dict[str, int] = {}
    tracks_by_position: dict[tuple[str, int], dict[str, Any]] = {}
    for track in ident_tracks:
        track_type = track.get("type", "")
        type_index = type_counter.get(track_type, 0)
        tracks_by_position[(track_type, type_index)] = track
        type_counter[track_type] = type_index + 1
    return tracks_by_position


def _ident_track_for_source_id(
    stream: Stream, ident_tracks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    source_id = stream.pid or stream.sub_id
    if source_id is None:
        return None
    return next(
        (
            track
            for track in ident_tracks
            if track.get("properties", {}).get("number") == source_id
        ),
        None,
    )


def _positional_ident_track(
    stream: Stream,
    tracks_by_position: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any] | None:
    track_type = stream.stream_type.value
    if track_type == "subtitle":
        track_type = "subtitles"
    candidate = tracks_by_position.get((track_type, stream.type_index))
    if candidate is not None and (stream.pid or stream.sub_id) is not None:
        source_id = stream.pid or stream.sub_id
        assert source_id is not None
        log_debug(
            f"  Positional fallback for {stream.display_id}: "
            f"id=0x{source_id:x} matched track {candidate['id']} "
            f"by type={track_type} index={stream.type_index}"
        )
    return candidate


def _unmapped_stream(stream: Stream) -> MappedStream:
    source_id = stream.pid or stream.sub_id
    if source_id is not None:
        log_debug(
            f"  Dropping stream: {stream.display_id} "
            f"(id=0x{source_id:x}) not found in source"
        )
    return {
        "input_id": -1,
        "type": stream.stream_type.value,
        "stream": stream,
        "ident_channels": None,
    }


def _refine_stream_codec(stream: Stream, ident_track: dict[str, Any]) -> None:
    ident_codec = ident_track.get("codec", "")
    mapped_codec = _MKVMERGE_CODEC_MAP.get(ident_codec)
    if mapped_codec and mapped_codec != stream.codec:
        log_debug(
            f"  Codec override for {stream.display_id}: "
            f"{stream.codec} -> {mapped_codec} (from mkvmerge -J)"
        )
        stream.codec = mapped_codec


def _mapped_stream(stream: Stream, ident_track: dict[str, Any]) -> MappedStream:
    _refine_stream_codec(stream, ident_track)
    properties = ident_track.get("properties", {})
    ident_channel_count = properties.get("audio_channels")
    return {
        "input_id": int(ident_track["id"]),
        "type": str(ident_track["type"]),
        "stream": stream,
        "ident_channels": (
            int(ident_channel_count) if ident_channel_count is not None else None
        ),
    }


def _map_streams_to_ident_tracks(
    streams: list[Stream], ident_tracks: list[dict[str, Any]]
) -> list[MappedStream]:
    tracks_by_position = _ident_tracks_by_position(ident_tracks)
    mapped: list[MappedStream] = []
    for stream in streams:
        if not stream.is_muxable:
            # A SubPath stream's PID belongs to another clip; matching it by
            # PID or position would pair it with an unrelated main-clip track.
            log_warn(
                tr(
                    "Skipping {stream}: it is stored in a Blu-ray sub-path "
                    "clip, which can't be muxed",
                    stream=stream.display_id,
                )
            )
            mapped.append(_unmapped_stream(stream))
            continue
        ident_track = _ident_track_for_source_id(stream, ident_tracks)
        if ident_track is None:
            ident_track = _positional_ident_track(stream, tracks_by_position)
        if ident_track is None:
            mapped.append(_unmapped_stream(stream))
        else:
            mapped.append(_mapped_stream(stream, ident_track))

    if ident_tracks:
        mapped.sort(key=lambda entry: entry["input_id"])
    return mapped


def _verify_edition_append_pids(
    title: Title,
    streams: list[Stream],
    inputs: list[Path],
    mapped: list[MappedStream],
) -> None:
    """Fail loud when a muxed PID is missing from any appended edition clip.

    Appended inputs are selected by mkvmerge track ID, so a clip physically
    lacking a union PID would silently shift every later TID and append the
    wrong tracks. Multi-edition titles carry the union of their editions'
    track lists, which is only safe when every staged clip actually contains
    every muxed PID — ``mkvmerge -J`` reads headers only, so probing each
    input is cheap. Raises ``RipError`` naming the clip and PID otherwise.
    """
    if not title.editions or len(inputs) < 2:
        return
    wanted: dict[int, str] = {}
    for entry in mapped:
        stream = entry["stream"]
        if entry["input_id"] >= 0 and stream.pid is not None:
            wanted.setdefault(stream.pid, stream.display_id)
    for path in inputs:
        probed = _identify_input_tracks(path)
        if not probed:
            raise RipError(
                message=f"Cannot verify track layout of {path.name}; "
                "aborting multi-edition mux",
                title=title,
                streams=streams,
            )
        numbers = {track.get("properties", {}).get("number") for track in probed}
        for pid, display_id in wanted.items():
            if pid not in numbers:
                raise RipError(
                    message=f"Clip {path.name} lacks {display_id} "
                    f"(PID 0x{pid:x}); editions with different tracks "
                    "cannot be combined",
                    title=title,
                    streams=streams,
                )


def _prepare_mux_tags(
    title: Title,
    tag_opts: TagOptions | None,
    temp_files: list[Path],
    disc_metadata: DiscMetadata | None = None,
    prompts: UserPrompts | None = None,
) -> tuple[MovieMetadata | None, list[ArtAttachment]]:
    if tag_opts is None or not tag_opts.enabled:
        return None, []

    from tagger import _prepare_tagging

    try:
        metadata, art = _prepare_tagging(title.name, tag_opts, temp_files, prompts)
    except Exception as exc:
        log_warn(tr("Tagging failed (ripping without tags): {err}", err=exc))
        return None, []

    if metadata is None:
        return None, art

    metadata.custom_properties["ENCODER"] = "mkvsmith"
    if disc_metadata and disc_metadata.upc_ean:
        metadata.custom_properties["BARCODE"] = disc_metadata.upc_ean
    if disc_metadata and disc_metadata.dvd_disc_id:
        metadata.custom_properties["DVD_DISC_ID"] = disc_metadata.dvd_disc_id
    if disc_metadata and disc_metadata.matrix256_fingerprint:
        metadata.custom_properties["MATRIX256_FINGERPRINT"] = (
            disc_metadata.matrix256_fingerprint
        )
    if disc_metadata and disc_metadata.disc_hash:
        metadata.custom_properties["THEDISCDB_DISC_HASH"] = disc_metadata.disc_hash

    source_name = title.source_file.name.lower()
    iso_paths = title.iso_internal_paths
    if any(path.upper().startswith("BDMV") for path in iso_paths):
        metadata.custom_properties["ORIGINAL_MEDIA_TYPE"] = "Blu-ray"
    elif any(path.upper().startswith("VIDEO_TS") for path in iso_paths):
        metadata.custom_properties["ORIGINAL_MEDIA_TYPE"] = "DVD"
    elif source_name.endswith(".vob"):
        metadata.custom_properties["ORIGINAL_MEDIA_TYPE"] = "DVD"
    elif source_name.endswith(".m2ts"):
        metadata.custom_properties["ORIGINAL_MEDIA_TYPE"] = "Blu-ray"
    return metadata, art


def _mapped_input_ids(mapped: list[MappedStream], *track_types: str) -> list[str]:
    return [
        str(entry["input_id"])
        for entry in mapped
        if entry["input_id"] >= 0 and entry["type"] in track_types
    ]


def _should_use_audio_filter(ident_tracks: list[dict[str, Any]], title: Title) -> bool:
    scanned_audio_count = sum(
        1 for track in ident_tracks if track.get("type") == "audio"
    )
    ifo_audio_count = len(title.audio_streams)
    use_audio_filter = not (
        ifo_audio_count > 0
        and scanned_audio_count < ifo_audio_count
        and title.dvd_ifo_data is not None
    )
    if not use_audio_filter:
        log_debug(
            f"DVD audio stream fallback: mkvmerge -J found "
            f"{scanned_audio_count}/{ifo_audio_count} audio tracks, "
            "omitting --audio-tracks filter"
        )
    return use_audio_filter


def _track_filter_options(
    ident_tracks: list[dict[str, Any]],
    mapped: list[MappedStream],
    title: Title,
) -> list[str]:
    if not ident_tracks or not mapped:
        return []

    video_ids = _mapped_input_ids(mapped, "video")
    audio_ids = _mapped_input_ids(mapped, "audio")
    subtitle_ids = _mapped_input_ids(mapped, "subtitle", "subtitles")
    use_audio_filter = _should_use_audio_filter(ident_tracks, title)

    options: list[str] = []
    if video_ids:
        options += ["--video-tracks", ",".join(video_ids)]
    if use_audio_filter and audio_ids:
        options += ["--audio-tracks", ",".join(audio_ids)]
    if subtitle_ids:
        options += ["--subtitle-tracks", ",".join(subtitle_ids)]
    return options


def _append_track_state_options(cmd: list[str], input_id: int, stream: Stream) -> None:
    cmd += ["--language", f"{input_id}:{stream.language}"]
    cmd += [
        "--default-track",
        f"{input_id}:{'yes' if stream.is_default else 'no'}",
    ]
    if stream.is_forced:
        cmd += ["--forced-track", f"{input_id}:yes"]
    if stream.is_hearing_impaired:
        cmd += ["--hearing-impaired-flag", f"{input_id}:yes"]
    if stream.is_commentary:
        cmd += ["--commentary-flag", f"{input_id}:yes"]


def _append_video_track_options(cmd: list[str], input_id: int, stream: Stream) -> None:
    color_info = _resolve_video_color(stream)
    if color_info is not None:
        primaries, transfer, matrix, range_ = color_info
        color_options = (
            ("--color-primaries", _COLOR_CICP.get(primaries)),
            (
                "--color-transfer-characteristics",
                _COLOR_CICP.get(transfer),
            ),
            ("--color-matrix-coefficients", _COLOR_CICP.get(matrix)),
            ("--color-range", _COLOR_RANGE.get(range_)),
        )
        for option, code in color_options:
            if code is not None:
                cmd += [option, f"{input_id}:{code}"]

    siting = _chroma_siting_for_codec(stream.codec)
    if siting is not None:
        cmd += ["--chroma-siting", f"{input_id}:{siting}"]


def _track_name_for_stream(entry: MappedStream) -> str:
    stream = entry["stream"]
    track_name = stream.title
    if not track_name and stream.stream_type == StreamType.AUDIO:
        track_name = _audio_title(stream, entry.get("ident_channels")) or ""
    return track_name


def _dvd_vts_number(title: Title) -> int | None:
    """VTS number parsed from the title's source VOB, or None.

    Only DVD titles (IFO/PGC evidence on the Title) qualify: a lone
    ``VTS_01_1.VOB`` opened as a plain video file must not gain a fabricated
    source id.
    """
    if (
        title.dvd_ifo_data is None
        and title.dvd_pgc_number is None
        and title.dvd_title_id is None
        and not title.dvd_subp_attrs
        and not title.dvd_audio_attrs
    ):
        return None
    match = re.search(r"VTS_(\d+)_", title.source_file.name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _source_id_for_stream(
    stream: Stream, title: Title, *, hddvd_video_id: int | None = None
) -> str | None:
    """SOURCE_ID source-medium tag for a track, or None when it can't be known.

    Blu-ray is the file index plus PID hex (``001011`` for PID 0x1011),
    decoded from a reference BD-sourced rip; the multi-clip prefix
    is unverified, so first-file ``00`` is assumed. DVD is the VTS number
    plus sub-stream plus pack stream id (``0100E0`` video, ``0180BD``
    AC-3, ``0120BD`` subpicture 0), decoded from a reference
    DVD-sourced rip. HD DVD shares the DVD shape (``0100E2`` video,
    ``01C0BD`` DD+, decoded from a reference HD DVD rip): the ``01`` prefix
    is the first-file index, audio/subs use XPL order (0xC0+/0x20+), and
    the video pack id comes from an EVO packet scan since it is not always
    0xE0. Raw files and anything else yield None (no tag).
    """
    if title.hddvd_title_number is not None:
        # Checked before pid: probed EVO streams carry mkvmerge's internal
        # track numbers in pid/sub_id, which are not source medium ids.
        if stream.stream_type == StreamType.VIDEO:
            if hddvd_video_id is None:
                return None
            return f"0100{hddvd_video_id:02X}"
        if stream.stream_type == StreamType.AUDIO:
            sub_id = 0xC0 + stream.type_index
        else:
            sub_id = 0x20 + stream.type_index
        return f"01{sub_id:02X}BD"
    if stream.pid is not None:
        return f"00{stream.pid:04X}"
    vts = _dvd_vts_number(title)
    if vts is None:
        return None
    pack_id = 0xE0 if stream.stream_type == StreamType.VIDEO else 0xBD
    if stream.stream_type == StreamType.VIDEO:
        sub_id = 0x00
    else:
        sub_id = stream.sub_id if stream.sub_id is not None else 0x00
    return f"{vts:02d}{sub_id:02X}{pack_id:02X}"


def _create_source_id_tags_file(
    source_id: str, cleanup: list[Path], temp_files: list[Path]
) -> Path:
    """Write a single-Simple ``SOURCE_ID`` tags XML for ``--tags TID:file``.

    ``<Targets>`` is deliberately omitted: mkvmerge inserts the UID it
    generates for the track named by the TID part of ``--tags`` itself
    (verified against mkvmerge v96). ``TagLanguage eng`` matches the reference tag layout,
    which sets it on every Simple (mkvmerge's own statistics tags omit it).
    """
    tags_file = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    temp_files.append(tags_file)
    cleanup.append(tags_file)
    root = ET.Element("Tags")
    tag = ET.SubElement(root, "Tag")
    simple = ET.SubElement(tag, "Simple")
    ET.SubElement(simple, "Name").text = "SOURCE_ID"
    ET.SubElement(simple, "TagLanguage").text = "eng"
    ET.SubElement(simple, "String").text = source_id
    ET.indent(root, space="  ")
    tags_file.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return tags_file


def _normalize_tags_for_source_id(tags_xml: bytes) -> bytes | None:
    """Make track SOURCE_ID tags visible to players.

    Two things stop players from surfacing a track's SOURCE_ID tag (as
    OriginalSourceMedium_ID):
    - mkvmerge regenerates each tag block's _STATISTICS_TAGS list at mux
      time from its own statistics tags, dropping SOURCE_ID from it, and
      players only honour names on that list;
    - mkvmerge appends the UID it generates for a mux-time ``--tags``
      file after its Simple elements, but players attach leading
      Simples to no track, so Targets must come first.
    Returns the rewritten XML, or None when nothing needs changing.
    """
    try:
        root = ET.fromstring(tags_xml)
    except ET.ParseError:
        return None
    changed = False
    for tag in root.findall("Tag"):
        simples = tag.findall("Simple")
        if not any(simple.findtext("Name") == "SOURCE_ID" for simple in simples):
            continue
        children = list(tag)
        first_simple = next(
            (i for i, child in enumerate(children) if child.tag == "Simple"), None
        )
        first_targets = next(
            (i for i, child in enumerate(children) if child.tag == "Targets"), None
        )
        if (
            first_simple is not None
            and first_targets is not None
            and first_simple < first_targets
        ):
            for child in children:
                tag.remove(child)
            for child in children:
                if child.tag == "Targets":
                    tag.append(child)
            for child in children:
                if child.tag != "Targets":
                    tag.append(child)
            changed = True
        for simple in simples:
            if simple.findtext("Name") != "_STATISTICS_TAGS":
                continue
            string_el = simple.find("String")
            if string_el is None:
                continue
            listed = (string_el.text or "").split()
            if "SOURCE_ID" not in listed:
                string_el.text = " ".join([*listed, "SOURCE_ID"])
                changed = True
    if not changed:
        return None
    ET.indent(root, space="  ")
    result = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    assert isinstance(result, bytes)
    return result


def _apply_source_id_statistics_fix(out_file: Path, temp_files: list[Path]) -> bool:
    """List each track's SOURCE_ID in its _STATISTICS_TAGS after muxing.

    Reads the muxed tags back, applies _statistics_tags_with_source_id,
    and writes them back when anything changed. Returns True when the
    file was rewritten. Never raises: tag cosmetics must not fail a rip.
    """
    if shutil.which("mkvextract") is None or shutil.which("mkvpropedit") is None:
        return False
    try:
        extract = _run_mkvtoolnix(["mkvextract", str(out_file), "tags"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_debug(f"SOURCE_ID statistics fix: mkvextract failed: {exc}")
        return False
    if extract.returncode != 0:
        log_debug(f"SOURCE_ID statistics fix: mkvextract failed ({extract.returncode})")
        return False
    fixed = _normalize_tags_for_source_id(extract.stdout.encode("utf-8", "replace"))
    if fixed is None:
        return False
    tags_file = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    temp_files.append(tags_file)
    try:
        tags_file.write_bytes(fixed)
        rewrite = _run_mkvtoolnix(
            ["mkvpropedit", str(out_file), "--tags", f"all:{tags_file}"]
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_warn(
            tr("Could not update tags in {name}: {err}", name=out_file.name, err=exc)
        )
        return False
    if rewrite.returncode not in (0, 1):
        log_warn(
            tr(
                "Could not update tags in {name} (mkvpropedit failed)",
                name=out_file.name,
            )
        )
        return False
    log_debug(f"SOURCE_ID listed in _STATISTICS_TAGS of {out_file.name}")
    return True


def _audio_channels_by_track_id(
    ident_tracks: list[dict[str, Any]],
) -> dict[int, int | None]:
    """Audio channel counts keyed by mkvmerge track id."""
    channels: dict[int, int | None] = {}
    for track in ident_tracks:
        if track.get("type") != "audio":
            continue
        try:
            track_id = int(track.get("id", -1))
        except (TypeError, ValueError):
            continue
        props = track.get("properties", {})
        value = (
            cast(dict[str, Any], props).get("audio_channels")
            if isinstance(props, dict)
            else None
        )
        channels[track_id] = int(value) if isinstance(value, int) else None
    return channels


def _drop_incompatible_append_inputs(
    inputs: list[Path],
    mapped: list[MappedStream],
    ident_tracks: list[dict[str, Any]],
) -> tuple[list[Path], list[int]]:
    """Drop appended clips whose audio layouts differ from the first clip.

    mkvmerge refuses to append when a mapped audio track's channel count
    differs (e.g. a stereo credits clip after 5.1 episode clips). The first
    clip always stays; incompatible followers are excluded with a warning.
    Returns (kept_inputs, dropped_indices).
    """
    if len(inputs) < 2:
        return inputs, []
    expected = _audio_channels_by_track_id(ident_tracks)
    wanted_ids = {
        entry["input_id"]
        for entry in mapped
        if entry["type"] == "audio" and entry["input_id"] >= 0
    }
    if not wanted_ids:
        return inputs, []
    kept = [inputs[0]]
    dropped: list[int] = []
    for index in range(1, len(inputs)):
        probed = _identify_input_tracks(inputs[index])
        if not probed:
            # Unprobable clip: keep it and let mkvmerge decide.
            kept.append(inputs[index])
            continue
        actual = _audio_channels_by_track_id(probed)
        mismatch = False
        for track_id in wanted_ids:
            want = expected.get(track_id)
            got = actual.get(track_id)
            if want is not None and got is not None and want != got:
                mismatch = True
                break
        if mismatch:
            dropped.append(index)
            log_warn(
                tr(
                    "Skipping incompatible clip for append: {name} "
                    "(audio layout differs)",
                    name=inputs[index].name,
                )
            )
        else:
            kept.append(inputs[index])
    return kept, dropped


def _append_track_options(
    cmd: list[str],
    mapped: list[MappedStream],
    *,
    title: Title | None = None,
    cleanup: list[Path] | None = None,
    temp_files: list[Path] | None = None,
    hddvd_video_id: int | None = None,
) -> None:
    for entry in mapped:
        stream = entry["stream"]
        input_id = entry["input_id"]
        if input_id < 0:
            log_debug(
                f"  No input track ID for {stream.display_id}; "
                "track properties may be misaligned."
            )
            continue

        _append_track_state_options(cmd, input_id, stream)
        if stream.stream_type == StreamType.VIDEO:
            _append_video_track_options(cmd, input_id, stream)

        track_name = _track_name_for_stream(entry)
        if track_name:
            cmd += ["--track-name", f"{input_id}:{track_name}"]

        if title is not None and cleanup is not None and temp_files is not None:
            source_id = _source_id_for_stream(
                stream, title, hddvd_video_id=hddvd_video_id
            )
            if source_id is not None:
                tags_file = _create_source_id_tags_file(source_id, cleanup, temp_files)
                cmd += ["--tags", f"{input_id}:{tags_file}"]


@dataclass
class _DvdTrimResult:
    inputs: list[Path]
    vobu_parts: list[Path] | None
    vobu_part_sizes: list[int] | None


def _is_dvd_vob_input(streams: list[Stream], inputs: list[Path]) -> bool:
    video_stream = next(
        (stream for stream in streams if stream.stream_type == StreamType.VIDEO),
        None,
    )
    return (
        video_stream is not None
        and video_stream.codec == "mpeg2video"
        and bool(inputs)
        and inputs[0].suffix.lower() == ".vob"
    )


@dataclass
class _MuxInputPlan:
    inputs: list[Path]
    cleanup: list[Path]
    is_dvd_vob: bool
    vobu_parts: list[Path] | None
    vobu_part_sizes: list[int] | None


@dataclass
class _PreparedMuxTracks:
    ident_tracks: list[dict[str, Any]]
    mapped: list[MappedStream]
    subtitle_fallback: Path | None
    fallback_tracks: list[dict[str, Any]]
    unmatched_ifo_subs: list[Stream]
    cc608_srt: Path | None


def _confirm_overwrite(out_file: Path, prompts: UserPrompts | None = None) -> bool:
    """Ask before overwriting an existing output file (default No)."""
    hooks = prompts or RUNTIME_STATE.prompts
    return hooks.confirm(
        tr("'{name}' already exists. Overwrite? [y/N]:", name=out_file.name)
    )


def _output_file_for_title(output_dir: Path, title: Title) -> Path:
    filename_name = title.name
    title_suffix = f" - Title {title.index + 1}"
    if filename_name.endswith(title_suffix):
        filename_name = filename_name[: -len(title_suffix)]
    filename_name = re.sub(
        r"\s+-\s+Blu-ray(?:\s*[™℠])?\s*$",
        "",
        filename_name,
        flags=re.IGNORECASE,
    )
    # Strip Windows-reserved path characters and unusual Unicode symbols from
    # the filename only; container title metadata keeps the original formatting.
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename_name or title.name)
    safe_name = re.sub(r"[^\w\s\-.]", "", safe_name)
    safe_name = re.sub(r"\s+", " ", safe_name).strip()
    return output_dir / f"{safe_name}_t{title.index:02d}.mkv"


def _find_dvd_trim_range(title: Title, inputs: list[Path]) -> tuple[int, int] | None:
    trim_range: tuple[int, int] | None = None
    if title.dvd_ifo_data is not None:
        try:
            total_size = sum(path.stat().st_size for path in inputs)
            trim_range = _lookup_main_feature_range(
                title.dvd_ifo_data,
                total_size,
                title.dvd_pgc_number,
            )
            if trim_range:
                log_debug(
                    f"IFO cell trim: extracting bytes {trim_range[0]}-{trim_range[1]}"
                )
        except Exception as exc:
            log_debug(f"IFO cell table failed ({exc}); trying PTS scan")
            trim_range = None

    if trim_range is None:
        try:
            trim_range = _dvd_main_content_range(inputs)
        except Exception as exc:
            log_debug(f"DVD PTS cell scan failed ({exc}); muxing raw VOBs")
            trim_range = None
    return trim_range


def _temp_vob_path(directory: Path | None) -> Path:
    return Path(
        tempfile.NamedTemporaryFile(
            suffix=".vob",
            delete=False,
            dir=str(directory) if directory else None,
        ).name
    )


def _register_temp_file(
    path: Path, cleanup: list[Path], temp_files: list[Path]
) -> None:
    temp_files.append(path)
    cleanup.append(path)


def _main_edition_vobu_ranges(
    title: Title, inputs: list[Path]
) -> list[tuple[int, int]] | None:
    if title.dvd_ifo_data is None:
        return None
    try:
        admap = _parse_vts_vobu_admap(title.dvd_ifo_data)
        if not admap:
            return None
        return _build_main_edition_vobu_ranges(
            title.dvd_ifo_data,
            admap,
            inputs,
            title.dvd_pgc_number,
        )
    except Exception:
        return None


def _write_vobu_trim(
    inputs: list[Path],
    ranges: list[tuple[int, int]],
    output: Path,
    temp_directory: Path | None,
    cleanup: list[Path],
    temp_files: list[Path],
) -> tuple[list[Path], list[int]]:
    total_vobu = sum(end - start for start, end in ranges)
    log_debug(
        f"Trimming DVD title ({len(ranges)} VOBU run(s), {total_vobu / 1e9:.1f} GB)..."
    )
    parts: list[Path] = []
    for start, end in ranges:
        part = _temp_vob_path(temp_directory)
        _register_temp_file(part, cleanup, temp_files)
        _extract_concat_range(inputs, start, end, part)
        parts.append(part)
    with output.open("wb") as output_file:
        for part in parts:
            output_file.write(part.read_bytes())
    return parts, [end - start for start, end in ranges]


def _prepare_dvd_inputs(
    title: Title,
    inputs: list[Path],
    temp_base: Path | None,
    cleanup: list[Path],
    temp_files: list[Path],
) -> _DvdTrimResult:
    trim_range = _find_dvd_trim_range(title, inputs)
    if trim_range is None:
        log_warn(
            "DVD cell trimming failed; muxing raw VOBs. "
            "Output duration may be incorrect. "
            "Run with --debug to see why trimming was skipped."
        )
        return _DvdTrimResult(inputs, None, None)

    start, end = trim_range
    temp_directory = temp_base
    output = _temp_vob_path(temp_directory)
    _register_temp_file(output, cleanup, temp_files)

    vobu_ranges = _main_edition_vobu_ranges(title, inputs)
    if vobu_ranges:
        parts, part_sizes = _write_vobu_trim(
            inputs, vobu_ranges, output, temp_directory, cleanup, temp_files
        )
        vobu_parts = parts if len(parts) > 1 else None
        return _DvdTrimResult(
            [output],
            vobu_parts,
            part_sizes if vobu_parts else None,
        )

    log_debug(f"Trimming DVD title ({(end - start) / 1e9:.1f} GB)...")
    _extract_concat_range(inputs, start, end, output)
    return _DvdTrimResult([output], None, None)


def _is_bluray_clip_append(inputs: list[Path]) -> bool:
    """True when every appended input is a Blu-ray clip (.m2ts).

    ISO-extracted clips keep their on-disc filenames, so this holds for
    folder/device and ISO sources alike.
    """
    return bool(inputs) and all(
        input_path.suffix.lower() == ".m2ts" for input_path in inputs
    )


def _seamless_audio_pids(mapped: list[MappedStream]) -> set[int] | None:
    """Blu-ray PIDs of the muxed audio tracks, or None if they're ambiguous.

    Each muxed audio track must map to its own BD audio PID; a PID shared by
    two muxed tracks (a TrueHD stream and its embedded AC-3 core) can't be
    trimmed per track.
    """
    pids: list[int] = []
    for entry in mapped:
        if entry["type"] != "audio" or entry["input_id"] < 0:
            continue
        pid = entry["stream"].pid
        if pid is None or not is_bluray_audio_pid(pid):
            return None
        pids.append(pid)
    if not pids or len(set(pids)) != len(pids):
        return None
    return set(pids)


def _clone_or_copy(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, as a copy-on-write clone where supported.

    FICLONE (btrfs, XFS, ...) shares the data blocks, so only the trimmed
    tail is ever duplicated; any failure (other filesystems, cross-device,
    non-Linux) falls back to a regular copy.
    """
    try:
        import fcntl

        ioctl = getattr(fcntl, "ioctl", None)
        if ioctl is None:
            raise OSError("FICLONE not available on this platform")
        ficlone = 0x40049409  # _IOW(0x94, 9, int), linux/fs.h
        with src.open("rb") as source, dst.open("wb") as target:
            ioctl(target.fileno(), ficlone, source.fileno())
        return
    except (ImportError, OSError):
        pass
    shutil.copyfile(src, dst)


def _prepare_seamless_append(
    title: Title,
    inputs: list[Path],
    mapped: list[MappedStream],
    owned: set[Path],
    cleanup: list[Path],
    temp_dirs: list[Path],
    config: Config | None = None,
) -> list[Path] | None:
    """Trim clip tails so a seamless title can be appended in track mode.

    Returns the inputs to mux (trimmed private copies substituted where a
    clip needed trimming), or None to append in the default file mode. Clips
    in *owned* are already private copies (extracted from an ISO) and are
    trimmed in place; others are copied into a temp dir first so source
    files are never modified. Folder, drive, and ISO sources therefore mux
    identically.
    """
    if not title.seamless_connections or len(inputs) < 2:
        return None
    if not _is_bluray_clip_append(inputs):
        return None
    pids = _seamless_audio_pids(mapped)
    if pids is None:
        log_debug("Seamless append: muxed audio tracks lack unique BD PIDs")
        return None
    try:
        tails = [scan_clip_tail(path, pids) for path in inputs]
    except OSError as exc:
        log_debug(f"Seamless append: clip scan failed: {exc}")
        return None
    plan = plan_seamless_trim(tails, pids)
    if plan is None:
        log_debug(
            "Seamless append: audio can't be kept within half a frame of the "
            "video; using file append mode"
        )
        return None

    from disc_reader import temp_base_for_title

    copy_bytes = sum(
        path.stat().st_size
        for path, cuts in zip(inputs, plan)
        if cuts and path not in owned
    )
    copy_dir: Path | None = None
    result: list[Path] = []
    nulled = 0
    for path, cuts in zip(inputs, plan):
        if not cuts:
            result.append(path)
            continue
        target = path
        if path not in owned:
            if copy_dir is None:
                base = temp_base_for_title(copy_bytes, config)
                copy_dir = Path(
                    tempfile.mkdtemp(
                        prefix="mkv_seamless_", dir=str(base) if base else None
                    )
                )
                temp_dirs.append(copy_dir)
            target = copy_dir / path.name
            _clone_or_copy(path, target)
            cleanup.append(target)
        nulled += null_packets_from(target, cuts)
        result.append(target)
    trimmed = sum(1 for cuts in plan if cuts)
    log_debug(
        f"Seamless append: trimmed {trimmed}/{len(inputs)} clip tails "
        f"({nulled} audio packets nulled, {len(pids)} track(s)); track mode"
    )
    return result


def _append_input_files(
    cmd: list[str],
    inputs: list[Path],
    track_filter_opts: list[str],
    track_mode: bool = False,
) -> None:
    # Blu-ray clips use mkvmerge's default ``file`` append mode: each appended
    # clip is offset by the previous file's end, so audio and video stay
    # mutually synchronised. ``track`` mode instead offsets every track by its
    # own end; when a clip's audio ends earlier than its video the audio creeps
    # forward at every join and the error accumulates (Sgt Frog: ~0.6 s per
    # join, ~3 s by 6 h). VOB/EVO inputs keep ``track`` mode per the mkvmerge
    # manual (split parts of one recording, where per-track continuity wins).
    # *track_mode* forces ``track`` mode for Blu-ray clips whose tails were
    # trimmed by _prepare_seamless_append, which bounds that creep.
    # The filter is repeated before each appended input so clips carrying a
    # later-starting PID remain valid append sources.
    if len(inputs) > 1 and (track_mode or not _is_bluray_clip_append(inputs)):
        cmd += ["--append-mode", "track"]
    cmd.append(str(inputs[0]))
    for clip in inputs[1:]:
        cmd += ["+"]
        cmd += track_filter_opts
        cmd.append(str(clip))


def _create_mux_tags_file(
    title: Title,
    metadata: MovieMetadata | None,
    cleanup: list[Path],
    temp_files: list[Path],
) -> Path | None:
    if metadata is None and not title.editions:
        return None
    tags_file = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    temp_files.append(tags_file)
    cleanup.append(tags_file)
    _write_tags_xml_mkvmerge(tags_file, md=metadata, editions=title.editions)
    return tags_file


def _append_art_attachments(
    cmd: list[str], art_attachments: list[ArtAttachment]
) -> None:
    for art in art_attachments:
        cmd += [
            "--attachment-name",
            art["filename"],
            "--attachment-mime-type",
            art["mime"],
            "--attachment-description",
            art["label"],
            str(art["path"]),
        ]


def _build_mkvmerge_command(
    title: Title,
    out_file: Path,
    inputs: list[Path],
    mapped: list[MappedStream],
    ident_tracks: list[dict[str, Any]],
    metadata: MovieMetadata | None,
    art_attachments: list[ArtAttachment],
    subtitle_fallback: Path | None,
    fallback_tracks: list[dict[str, Any]],
    unmatched_ifo_subs: list[Stream],
    cc608_srt: Path | None,
    cleanup: list[Path],
    temp_files: list[Path],
    verbose: bool = False,
    track_append: bool = False,
) -> list[str]:
    cmd: list[str] = (
        ["mkvmerge"]
        + (["-v"] if verbose else [])
        + [
            "-o",
            str(out_file),
        ]
    )
    container_title = (
        metadata.title
        if metadata and metadata.title
        else (title.disc_name or title.name)
    )
    cmd += ["--title", container_title]

    if _uses_retimed_edition_chapters(title, inputs):
        # Placeholder chapters mark where mkvmerge actually put each appended
        # clip; the real edition chapters are written after the mux (see
        # _apply_retimed_edition_chapters).
        cmd += ["--generate-chapters", "when-appending"]
    else:
        chapters_file = _create_chapters_file(title, cleanup, temp_files)
        if chapters_file is not None:
            cmd += ["--chapters", str(chapters_file)]

    track_filter_opts = _track_filter_options(ident_tracks, mapped, title)
    cmd += track_filter_opts
    need_positional_fallback = not (ident_tracks and mapped)
    hddvd_video_id: int | None = None
    if title.hddvd_title_number is not None and inputs:
        # EVO video does not always ride 0xE0: read the pack id once for
        # the SOURCE_ID tags (bounded prefix scan).
        hddvd_video_id = _scan_evo_video_stream_id([inputs[0]])
    _append_track_options(
        cmd,
        mapped,
        title=title,
        cleanup=cleanup,
        temp_files=temp_files,
        hddvd_video_id=hddvd_video_id,
    )
    if need_positional_fallback:
        log_warn(
            "mkvmerge track identification unavailable; "
            "track properties (language, name) may not be applied correctly."
        )

    tags_file = _create_mux_tags_file(title, metadata, cleanup, temp_files)
    if tags_file is not None:
        cmd += ["--global-tags", str(tags_file)]
    _append_art_attachments(cmd, art_attachments)
    _append_input_files(cmd, inputs, track_filter_opts, track_mode=track_append)

    if subtitle_fallback is not None and fallback_tracks:
        cmd += _dvd_subtitle_fallback_options(
            unmatched_ifo_subs,
            fallback_tracks,
            ident_tracks,
            title=title,
            cleanup=cleanup,
            temp_files=temp_files,
            hddvd_video_id=hddvd_video_id,
        )
        cleanup.append(subtitle_fallback)
        cmd.append(str(subtitle_fallback))

    if cc608_srt is not None:
        cc_stream = next(
            stream for stream in title.subtitle_streams if stream.codec in CC608_CODECS
        )
        # Options apply to the next file argument; the sidecar's single
        # text track is track 0 within its own file.
        cc_options: list[str] = []
        _append_track_state_options(cc_options, 0, cc_stream)
        track_name = cc_stream.title or "Closed Captions"
        cc_options += ["--track-name", f"0:{track_name}"]
        cmd += cc_options
        cleanup.append(cc608_srt)
        cmd.append(str(cc608_srt))
    return cmd


def _subtitle_fallback_track_name(ifo_stream: Stream) -> str:
    track_name = ifo_stream.title
    if not track_name and ifo_stream.language != "und":
        language_name = get_language_name(ifo_stream.language)
        if language_name:
            track_name = f"Subtitles ({language_name})"
    return track_name


def _dvd_subtitle_fallback_options_for_track(
    track_id: int,
    ifo_stream: Stream,
    *,
    title: Title | None = None,
    cleanup: list[Path] | None = None,
    temp_files: list[Path] | None = None,
    hddvd_video_id: int | None = None,
) -> list[str]:
    options: list[str] = []
    _append_track_state_options(options, track_id, ifo_stream)
    track_name = _subtitle_fallback_track_name(ifo_stream)
    if track_name:
        options += ["--track-name", f"{track_id}:{track_name}"]
    if title is not None and cleanup is not None and temp_files is not None:
        source_id = _source_id_for_stream(
            ifo_stream, title, hddvd_video_id=hddvd_video_id
        )
        if source_id is not None:
            tags_file = _create_source_id_tags_file(source_id, cleanup, temp_files)
            options += ["--tags", f"{track_id}:{tags_file}"]
    return options


def _dvd_subtitle_fallback_options(
    unmatched_ifo_subs: list[Stream],
    fallback_tracks: list[dict[str, Any]],
    ident_tracks: list[dict[str, Any]],
    *,
    title: Title | None = None,
    cleanup: list[Path] | None = None,
    temp_files: list[Path] | None = None,
    hddvd_video_id: int | None = None,
) -> list[str]:
    options: list[str] = []
    if not ident_tracks:
        log_debug(
            "Cannot apply language tags to DVD subtitle fallback "
            "(no mkvmerge track identification data)"
        )
        return options

    for track_id, ifo_stream in enumerate(unmatched_ifo_subs):
        if track_id >= len(fallback_tracks):
            log_debug(
                f"  Sub fallback: {len(fallback_tracks)} extracted tracks "
                f"< {len(unmatched_ifo_subs)} IFO streams; stopping"
            )
            break
        log_debug(
            f"  Sub fallback: {ifo_stream.display_id} -> "
            f".idx track {track_id} ({ifo_stream.language})"
        )
        options += _dvd_subtitle_fallback_options_for_track(
            track_id,
            ifo_stream,
            title=title,
            cleanup=cleanup,
            temp_files=temp_files,
            hddvd_video_id=hddvd_video_id,
        )
    return options


def _extract_dvd_subtitle_fallback(
    title: Title,
    mapped: list[MappedStream],
    inputs: list[Path],
    vobu_parts: list[Path] | None,
    vobu_part_sizes: list[int] | None,
    temp_files: list[Path],
    debug: bool = False,
) -> tuple[Path | None, list[dict[str, Any]], list[Stream]]:
    unmatched_subs = [
        entry["stream"]
        for entry in mapped
        if entry["input_id"] < 0
        and entry["stream"].stream_type == StreamType.SUBTITLE
        and entry["stream"].codec not in CC608_CODECS
    ]
    if not unmatched_subs:
        return None, [], unmatched_subs

    language_by_id: dict[int, str] = {}
    forced_by_id: dict[int, bool] = {}
    for stream in unmatched_subs:
        if stream.sub_id is not None:
            language_by_id[stream.sub_id] = stream.language
            forced_by_id[stream.sub_id] = stream.is_forced

    log_debug(
        "DVD subtitle fallback: %d IFO subs (%s), scanning %d VOB(s)"
        % (
            len(unmatched_subs),
            ", ".join(
                "0x%02x=%s" % (sub_id, language_by_id.get(sub_id, "?"))
                for sub_id in sorted(language_by_id)
            ),
            len(inputs),
        )
    )
    palette: list[tuple[int, int, int]] | None = None
    if title.dvd_ifo_data is not None:
        palette = _extract_dvd_ifo_palette(title.dvd_ifo_data, None)

    result = _extract_dvd_vobsubs(
        inputs,
        language_by_id,
        forced_by_id,
        ifo_palette=palette,
        vobu_parts=vobu_parts,
        vobu_part_sizes=vobu_part_sizes if vobu_parts else None,
        total_duration=title.duration_seconds,
        temp_files=temp_files,
        debug=debug,
    )
    if result is None:
        return None, [], unmatched_subs

    fallback_path, fallback_tracks = result
    log_debug(
        "DVD subtitle fallback: %d extracted track(s) for %d IFO stream(s)"
        % (len(fallback_tracks), len(unmatched_subs))
    )
    return fallback_path, fallback_tracks, unmatched_subs


def _extract_dvd_cc608_sidecar(
    title: Title,
    inputs: list[Path],
    temp_files: list[Path],
    config: Config | None = None,
) -> Path | None:
    """Extract EIA-608 closed captions from the mux inputs as a text sidecar.

    The declared CC stream (``CC608_CODECS``, synthesised at scan time from
    the IFO's Line 21 flag plus verified VOB user data) has no MPEG
    sub-stream, so it cannot come from the VOB input itself; the captions
    are decoded from the video user data and muxed from a sidecar file —
    SubRip (portable plain text) or ASS (preserves the CC grid's speaker
    positioning and italics) per ``Config.cc608_format``.
    """
    if not any(stream.codec in CC608_CODECS for stream in title.subtitle_streams):
        return None
    events = extract_cc608_captions(inputs)
    if not any(event.rows for event in events):
        log_debug("CC608: no caption events in mux range; skipping track")
        return None
    effective_config = config or RUNTIME_STATE.config
    pts_offset = _vobsub_pts_offset(inputs)
    use_ass = effective_config.cc608_format == "ass"
    srt_path = Path(
        tempfile.NamedTemporaryFile(
            suffix=".ass" if use_ass else ".srt",
            delete=False,
            dir=str(tempfile.gettempdir()),
        ).name
    )
    if use_ass:
        resolution = (720, 480)
        if title.dvd_video_attrs is not None and title.dvd_video_attrs.resolution:
            resolution = title.dvd_video_attrs.resolution
        write_cc608_ass(events, srt_path, pts_offset=pts_offset, resolution=resolution)
    else:
        write_cc608_srt(events, srt_path, pts_offset=pts_offset)
    temp_files.append(srt_path)
    log_info(
        f"CC608: extracted {sum(1 for e in events if e.rows)} caption(s) "
        f"to {srt_path.name}"
    )
    return srt_path


def _create_chapters_file(
    title: Title, cleanup: list[Path], temp_files: list[Path]
) -> Path | None:
    if title.editions:
        chapters_file = Path(
            tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name
        )
        temp_files.append(chapters_file)
        cleanup.append(chapters_file)
        _write_multi_edition_chapters_xml(
            _pull_back_jump_atom_ends(title.editions, sum(title.clip_durations)),
            chapters_file,
        )
        for edition in title.editions:
            log_debug(
                f"  Edition {edition.uid} '{edition.name}'"
                f"{' (default)' if edition.is_default else ''}: "
                f"{len(edition.atoms)} atoms, {edition.duration:.0f}s"
            )
        return chapters_file

    if not title.chapters:
        return None

    chapters = list(title.chapters)
    if len(chapters) > 1 and title.duration_seconds > 0:
        if chapters[-1] >= title.duration_seconds - 0.5:
            chapters = chapters[:-1]
            log_debug(f"Filtered trailing end-of-movie chapter; {len(chapters)} remain")
    if not chapters:
        return None

    chapters_file = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    temp_files.append(chapters_file)
    cleanup.append(chapters_file)
    _write_chapters_xml(chapters, chapters_file)
    log_debug(f"Loaded {len(chapters)} chapters")
    return chapters_file


# Multi-edition atoms are planned on a timeline built from MPLS play-item
# durations, but mkvmerge's file append mode offsets each clip by the previous
# clip's real end (its last audio/video frame), which runs a few ms past the
# play item's OUT time. Over a 100+ clip seamless-branching disc the drift
# reaches seconds (Monsters University: ~1.9 s over 132 clips), so an alternate
# edition's jump into a later clip lands in the tail of the preceding one and
# shows black. The mux therefore records the true append offsets as
# placeholder chapters, and the edition atoms are remapped onto them.
_RETIME_EPS = 1e-6


def _uses_retimed_edition_chapters(title: Title, inputs: list[Path]) -> bool:
    return (
        bool(title.editions)
        and len(inputs) > 1
        and shutil.which("mkvextract") is not None
        and shutil.which("mkvpropedit") is not None
    )


def _parse_chapter_timestamp(text: str) -> float:
    hours, minutes, seconds = text.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _read_chapter_starts(chapters_xml: Path) -> list[float]:
    """Start times (seconds) of the first edition's atoms in file order."""
    root = ET.parse(chapters_xml).getroot()
    edition = root.find("EditionEntry")
    if edition is None:
        return []
    return [
        _parse_chapter_timestamp(start)
        for atom in edition.findall("ChapterAtom")
        if (start := atom.findtext("ChapterTimeStart"))
    ]


def _append_clip_bounds(
    clip_count: int,
    dropped: list[int],
    append_starts: list[float],
    total_end: float,
) -> list[tuple[float, float]] | None:
    """Real ``(start, end)`` of every union clip in the muxed file.

    *append_starts* holds one start per muxed clip (``when-appending``
    placeholder chapters). Dropped clips collapse to zero length at the start
    of the next muxed clip. Returns None when the counts don't line up.
    """
    dropped_set = set(dropped)
    kept = [index for index in range(clip_count) if index not in dropped_set]
    if len(kept) != len(append_starts) or not kept:
        return None
    kept_bounds: dict[int, tuple[float, float]] = {}
    for position, index in enumerate(kept):
        end = (
            append_starts[position + 1]
            if position + 1 < len(append_starts)
            else total_end
        )
        kept_bounds[index] = (append_starts[position], end)
    bounds: list[tuple[float, float]] = []
    for index in reversed(range(clip_count)):
        if index in kept_bounds:
            bounds.append(kept_bounds[index])
        else:
            anchor = bounds[-1][0] if bounds else total_end
            bounds.append((anchor, anchor))
    bounds.reverse()
    return bounds


def _retime_editions(
    editions: list[EditionSpec],
    planned_durations: list[float],
    bounds: list[tuple[float, float]],
) -> list[EditionSpec]:
    """Map edition atoms from the planned clip timeline onto *bounds*.

    Atoms never span a clip boundary, so each one is shifted by its clip's
    real start; an atom ending at its clip's planned end ends at the clip's
    real end, absorbing the per-clip overrun. Zero-length atoms (dropped
    clips) are removed; a removed visible atom hands its name to the next
    hidden one so chapter numbering stays intact.
    """
    planned_starts: list[float] = []
    running = 0.0
    for duration in planned_durations:
        planned_starts.append(running)
        running += duration

    retimed: list[EditionSpec] = []
    for edition in editions:
        atoms: list[EditionAtom] = []
        pending_name: str | None = None
        for atom in edition.atoms:
            clip = max(
                0, bisect.bisect_right(planned_starts, atom.start + _RETIME_EPS) - 1
            )
            clip = min(clip, len(bounds) - 1)
            real_start, real_end = bounds[clip]
            real_len = real_end - real_start
            planned_end = planned_starts[clip] + planned_durations[clip]
            start = real_start + min(
                max(atom.start - planned_starts[clip], 0.0), real_len
            )
            if atom.end >= planned_end - _RETIME_EPS:
                end = real_end
            else:
                end = real_start + min(atom.end - planned_starts[clip], real_len)
            hidden, name = atom.hidden, atom.name
            if end - start <= _RETIME_EPS:
                if not hidden and pending_name is None:
                    pending_name = name
                continue
            if hidden and pending_name is not None:
                hidden, name = False, pending_name
            if not hidden:
                pending_name = None
            atoms.append(EditionAtom(start, end, hidden=hidden, name=name))
        retimed.append(
            EditionSpec(
                uid=edition.uid,
                name=edition.name,
                is_default=edition.is_default,
                atoms=atoms,
            )
        )
    return retimed


# How far a jump atom's end is pulled back from the next clip's first frame.
# Matroska chapter ends are exclusive, but mpv's ordered-chapter timeline
# stops a segment at the first keyframe with ``pts >= end`` and lets later
# non-keyframes through (demux_timeline.c). An end sitting exactly on the
# following clip's IDR can round either way in its timeline arithmetic, so
# that frame and the next clip's following P/B frames may leak into the
# seam before the jump. 1 ms also clears players that compare whole ms,
# and stays far below the clip's own last frame (one frame duration back).
_JUMP_ATOM_END_MARGIN = 0.001


def _pull_back_jump_atom_ends(
    editions: list[EditionSpec], file_end: float
) -> list[EditionSpec]:
    """Pull back atom ends that are followed by a jump (or end the edition).

    Only atoms whose successor doesn't start exactly where they end are
    changed; contiguous atoms play straight through and stay exact. An
    edition's final atom is left alone when it runs to the end of the file.
    """
    adjusted: list[EditionSpec] = []
    for edition in editions:
        atoms: list[EditionAtom] = []
        for index, atom in enumerate(edition.atoms):
            following = (
                edition.atoms[index + 1] if index + 1 < len(edition.atoms) else None
            )
            end = atom.end
            contiguous = (
                following is not None and abs(following.start - atom.end) <= _RETIME_EPS
            )
            at_file_end = following is None and atom.end >= file_end - _RETIME_EPS
            if (
                not contiguous
                and not at_file_end
                and atom.end - atom.start > 2 * _JUMP_ATOM_END_MARGIN
            ):
                end = atom.end - _JUMP_ATOM_END_MARGIN
            atoms.append(EditionAtom(atom.start, end, atom.hidden, atom.name))
        adjusted.append(
            EditionSpec(
                uid=edition.uid,
                name=edition.name,
                is_default=edition.is_default,
                atoms=atoms,
            )
        )
    return adjusted


def _run_mkvtoolnix(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def _muxed_duration_seconds(out_file: Path) -> float | None:
    try:
        proc = _run_mkvtoolnix(["mkvmerge", "-J", str(out_file)])
        duration = json.loads(proc.stdout)["container"]["properties"]["duration"]
        return int(duration) / 1_000_000_000
    except (
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def _apply_retimed_edition_chapters(
    out_file: Path, title: Title, dropped: list[int], temp_files: list[Path]
) -> None:
    """Replace the mux's placeholder chapters with retimed edition chapters.

    Reads the ``when-appending`` chapters (the true clip offsets) back with
    mkvextract, remaps the edition atoms onto them, and writes the result
    with mkvpropedit. Falls back to the planned atoms if the offsets can't be
    matched to the clip list. Raises RipError if the chapters can't be written,
    since the file would otherwise carry only the placeholders.
    """
    extracted = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    chapters_file = Path(tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name)
    temp_files += [extracted, chapters_file]

    bounds: list[tuple[float, float]] | None = None
    try:
        proc = _run_mkvtoolnix(
            ["mkvextract", str(out_file), "chapters", str(extracted)]
        )
        total_end = _muxed_duration_seconds(out_file)
        if proc.returncode == 0 and total_end is not None:
            bounds = _append_clip_bounds(
                len(title.clip_durations),
                dropped,
                _read_chapter_starts(extracted),
                total_end,
            )
    except (subprocess.TimeoutExpired, OSError, ET.ParseError, ValueError) as exc:
        log_debug(f"Reading append offsets failed: {exc}")

    if bounds is None:
        log_warn(
            tr(
                "Could not read clip offsets from {name}; edition chapters "
                "may be misaligned at branch points",
                name=out_file.name,
            )
        )
        editions = _pull_back_jump_atom_ends(title.editions, sum(title.clip_durations))
    else:
        editions = _pull_back_jump_atom_ends(
            _retime_editions(title.editions, title.clip_durations, bounds),
            bounds[-1][1],
        )
        drift = bounds[-1][1] - sum(title.clip_durations)
        log_debug(f"Retimed edition chapters onto mux offsets (drift {drift:+.3f}s)")
    for edition in editions:
        log_debug(
            f"  Edition {edition.uid} '{edition.name}'"
            f"{' (default)' if edition.is_default else ''}: "
            f"{len(edition.atoms)} atoms, {edition.duration:.0f}s"
        )

    _write_multi_edition_chapters_xml(editions, chapters_file)
    try:
        proc = _run_mkvtoolnix(
            ["mkvpropedit", str(out_file), "--chapters", str(chapters_file)]
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RipError(message=f"mkvpropedit failed: {exc}", title=title) from exc
    if proc.returncode not in (0, 1):
        raise RipError(
            message=f"mkvpropedit failed ({proc.returncode})",
            stderr=(proc.stdout or "") + (proc.stderr or ""),
            title=title,
        )


@dataclass
class _MkvmergeTimeoutState:
    timed_out: bool = False


def _kill_mkvmerge_process(process: subprocess.Popen[str]) -> None:
    _kill_process_group(process.pid)


def _start_mkvmerge_watchdog(
    process: subprocess.Popen[str],
    timeout_state: _MkvmergeTimeoutState,
    timeout: int,
) -> threading.Timer:
    def on_timeout() -> None:
        timeout_state.timed_out = True
        _kill_mkvmerge_process(process)

    watchdog = threading.Timer(timeout, on_timeout)
    watchdog.daemon = True
    watchdog.start()
    return watchdog


def _parse_mkvmerge_progress(data: str) -> int | None:
    matches = re.findall(r"Progress:\s*(\d+)%", data)
    if not matches:
        return None
    return min(100, int(matches[-1]))


def _read_mkvmerge_output(stdout: IO[str], on_progress: Callable[[int], None]) -> str:
    # mkvmerge frames progress as \r-terminated updates ("Progress: N%\r",
    # flushed promptly). readline() returns each update as soon as it
    # arrives; the previous read(n) blocked until n chars accumulated, which
    # collapsed a whole mux into 1-2 jumps.
    chunks: list[str] = []
    last_percentage = -1
    while True:
        line = stdout.readline()
        if not line:
            break
        chunks.append(line)
        percentage = _parse_mkvmerge_progress(line)
        if percentage is not None and percentage != last_percentage:
            last_percentage = percentage
            on_progress(percentage)
    return "".join(chunks)


@final
class MKVCreator:
    def __init__(
        self,
        out: Path,
        tag_opts: TagOptions | None = None,
        config: Config | None = None,
        runtime_state: RuntimeState | None = None,
    ):
        state = runtime_state or RUNTIME_STATE
        self.out = out
        self.tag_opts = tag_opts
        self.disc_metadata = state.disc_metadata
        self.config = config or state.config
        self.prompts = state.prompts
        state.logger.configure(self.config)
        self.logger = state.logger
        self.cleanup = state.cleanup
        self.active_processes = state.active_processes
        self.out.mkdir(parents=True, exist_ok=True)

    def select_streams(
        self, title: Title, force: list[str] | None = None
    ) -> list[Stream]:
        return select_streams(title, force, self.config)

    def _prepare_inputs(self, title: Title, streams: list[Stream]) -> _MuxInputPlan:
        from disc_reader import _extract_full_for_muxing, temp_base_for_title

        is_iso = bool(
            title.iso_internal_paths and title.source_file.suffix.lower() == ".iso"
        )
        estimated_size = title.estimated_size_bytes
        if not estimated_size and not is_iso:
            try:
                estimated_size = sum(
                    path.stat().st_size
                    for path in (title.source_file, *title.append_clips)
                    if path.is_file()
                )
            except OSError:
                estimated_size = 0

        extract_temp_base = temp_base_for_title(estimated_size, self.config)
        if is_iso:
            inputs = _extract_full_for_muxing(
                title.source_file,
                title.iso_internal_paths,
                temp_base=extract_temp_base,
                temp_dirs=self.cleanup.temp_dirs,
                symlinks=self.cleanup.symlinks,
                expected_bytes=estimated_size,
            )
            cleanup = list(inputs)
        else:
            inputs = [title.source_file, *title.append_clips]
            cleanup = []

        if not inputs:
            raise RipError(message="No source files", title=title, streams=streams)

        is_dvd_vob = _is_dvd_vob_input(streams, inputs)
        dvd_trim = (
            _prepare_dvd_inputs(
                title,
                inputs,
                extract_temp_base,
                cleanup,
                self.cleanup.temp_files,
            )
            if is_dvd_vob
            else _DvdTrimResult(inputs, None, None)
        )
        return _MuxInputPlan(
            inputs=dvd_trim.inputs,
            cleanup=cleanup,
            is_dvd_vob=is_dvd_vob,
            vobu_parts=dvd_trim.vobu_parts,
            vobu_part_sizes=dvd_trim.vobu_part_sizes,
        )

    def _prepare_tracks(
        self,
        title: Title,
        streams: list[Stream],
        input_plan: _MuxInputPlan,
    ) -> _PreparedMuxTracks:
        # mkvmerge enumerates DVD MPEG-PS streams by stream ID rather than by
        # first packet appearance. IFO sub_ids therefore remain authoritative;
        # matching by sub_id or PID happens immediately after identification.
        ident_tracks = _identify_input_tracks(input_plan.inputs[0])
        if not ident_tracks:
            log_debug(
                "mkvmerge -J returned no tracks; muxing will include all streams "
                "and per-stream properties may be incorrect."
            )
        mapped = _map_streams_to_ident_tracks(streams, ident_tracks)
        _verify_edition_append_pids(title, streams, input_plan.inputs, mapped)

        # mkvmerge cannot detect sparse DVD subpictures from the first VOB,
        # and misses HD DVD EVO subpictures the same way (0xBD private
        # stream). Scan the MPEG-PS bitstream directly and emit a VobSub
        # fallback input.
        if input_plan.is_dvd_vob or title.hddvd_title_number is not None:
            fallback_path, fallback_tracks, unmatched_ifo_subs = (
                _extract_dvd_subtitle_fallback(
                    title,
                    mapped,
                    input_plan.inputs,
                    input_plan.vobu_parts,
                    input_plan.vobu_part_sizes,
                    self.cleanup.temp_files,
                    self.logger.debug_enabled,
                )
            )
        else:
            fallback_path = None
            fallback_tracks = []
            unmatched_ifo_subs = []

        cc608_srt = None
        if input_plan.is_dvd_vob:
            cc608_srt = _extract_dvd_cc608_sidecar(
                title, input_plan.inputs, self.cleanup.temp_files, self.config
            )

        return _PreparedMuxTracks(
            ident_tracks=ident_tracks,
            mapped=mapped,
            subtitle_fallback=fallback_path,
            fallback_tracks=fallback_tracks,
            unmatched_ifo_subs=unmatched_ifo_subs,
            cc608_srt=cc608_srt,
        )

    def _validate_mux_result(
        self,
        title: Title,
        streams: list[Stream],
        command: list[str],
        out_file: Path,
        returncode: int,
        output_text: str,
        timed_out: bool,
    ) -> None:
        if timed_out:
            raise RipError(
                message="mkvmerge timed out after 3600s",
                command=command,
                stderr=output_text,
                title=title,
                streams=streams,
            )
        if returncode == 1:
            warnings = [
                stripped
                for line in output_text.split("\n")
                if (stripped := line.strip())
                and ("Warning" in stripped or "warning" in stripped)
                and "%" not in stripped
            ]
            if not warnings:
                log_warn(
                    "mkvmerge completed with warnings, but no warning lines "
                    "were captured; re-run with --debug for its full output"
                )
            else:
                log_warn("mkvmerge completed with warnings:")
                shown = warnings if self.logger.debug_enabled else warnings[:10]
                for line in shown:
                    log_warn(f"  mkvmerge: {line}")
                if len(shown) < len(warnings):
                    log_warn(
                        f"  ... and {len(warnings) - len(shown)} more "
                        "(re-run with --debug for all)"
                    )
        elif returncode != 0:
            raise RipError(
                message=f"mkvmerge failed ({returncode})",
                command=command,
                returncode=returncode,
                stderr=output_text,
                title=title,
                streams=streams,
            )
        if not out_file.exists():
            raise RipError(
                message="Output missing",
                command=command,
                title=title,
                streams=streams,
            )

    def _ensure_overwrite_allowed(self, out_file: Path) -> None:
        """Raise RipError when *out_file* exists and overwriting is declined.

        Existing outputs are never silently clobbered: unless ``--force`` is
        set, the user must confirm (a declined or interrupted prompt skips the
        title, which batch flows report via the usual RipError channel).
        """
        if not out_file.exists():
            return
        if self.config.force_overwrite:
            log_info(f"Overwriting existing output: {out_file.name}")
            return
        if not _confirm_overwrite(out_file, self.prompts):
            raise RipError(message=f"Output exists, not overwriting: {out_file.name}")

    def _finish_created_output(
        self,
        out_file: Path,
        metadata: MovieMetadata | None,
        art_attachments: list[ArtAttachment],
    ) -> None:
        from tagger import _write_tag_xml

        self._log_created(out_file)
        if (
            metadata is not None
            and self.tag_opts is not None
            and self.tag_opts.save_xml
        ):
            try:
                xml_path = out_file.with_suffix(".xml")
                _write_tag_xml(metadata, xml_path)
                log_info(f"Tag XML written: {xml_path}")
            except OSError as exc:
                log_warn(tr("Could not write tag XML: {err}", err=exc))
        if art_attachments:
            labels = ", ".join(art["label"] for art in art_attachments)
            log_info(f"Attached art: {labels}")

    def _execute_mux(
        self,
        title: Title,
        streams: list[Stream],
        command: list[str],
        out_file: Path,
        metadata: MovieMetadata | None,
        art_attachments: list[ArtAttachment],
        retime_dropped_inputs: list[int] | None = None,
        fix_source_id_statistics: bool = False,
    ) -> Path:
        """Run the mux and finalise the output.

        *retime_dropped_inputs* is set (possibly empty) when the command used
        ``--generate-chapters when-appending`` for a multi-edition title; the
        placeholder chapters are then replaced by the retimed editions. It
        lists the union clip indices that were not muxed. *fix_source_id_statistics*
        lists each track's SOURCE_ID in its _STATISTICS_TAGS afterwards so
        players surface it (see _apply_source_id_statistics_fix).
        """
        log_info(tr("Muxing: {name}...", name=out_file.name))
        # Track the in-progress output so Ctrl+C deletes the partial file
        # instead of leaving a truncated mkv next to completed rips.
        self.active_processes.register_output(out_file)
        returncode, output_text, timed_out = self._run_mkvmerge(
            command, out_file.name, title.duration_seconds
        )
        self._validate_mux_result(
            title,
            streams,
            command,
            out_file,
            returncode,
            output_text,
            timed_out,
        )
        if retime_dropped_inputs is not None:
            _apply_retimed_edition_chapters(
                out_file, title, retime_dropped_inputs, self.cleanup.temp_files
            )
        if fix_source_id_statistics:
            _apply_source_id_statistics_fix(out_file, self.cleanup.temp_files)
        self._finish_created_output(out_file, metadata, art_attachments)
        return out_file

    def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path:
        if not streams:
            streams = self.select_streams(title)
        if not streams:
            raise RipError(message="No streams selected", title=title, streams=streams)

        out_file = _output_file_for_title(self.out, title)
        self._ensure_overwrite_allowed(out_file)
        input_plan = self._prepare_inputs(title, streams)
        prepared_tracks = self._prepare_tracks(title, streams, input_plan)
        tag_md, tag_art = _prepare_mux_tags(
            title,
            self.tag_opts,
            self.cleanup.temp_files,
            self.disc_metadata,
            self.prompts,
        )

        kept_inputs, dropped_inputs = _drop_incompatible_append_inputs(
            input_plan.inputs, prepared_tracks.mapped, prepared_tracks.ident_tracks
        )
        try:
            seamless_inputs = _prepare_seamless_append(
                title,
                kept_inputs,
                prepared_tracks.mapped,
                owned=set(input_plan.cleanup),
                cleanup=input_plan.cleanup,
                temp_dirs=self.cleanup.temp_dirs,
                config=self.config,
            )
            if seamless_inputs is not None:
                kept_inputs = seamless_inputs
            command = _build_mkvmerge_command(
                title,
                out_file,
                kept_inputs,
                prepared_tracks.mapped,
                prepared_tracks.ident_tracks,
                tag_md,
                tag_art,
                prepared_tracks.subtitle_fallback,
                prepared_tracks.fallback_tracks,
                prepared_tracks.unmatched_ifo_subs,
                prepared_tracks.cc608_srt,
                input_plan.cleanup,
                self.cleanup.temp_files,
                verbose=self.config.debug,
                track_append=seamless_inputs is not None,
            )
            return self._execute_mux(
                title,
                streams,
                command,
                out_file,
                tag_md,
                tag_art,
                retime_dropped_inputs=(
                    dropped_inputs
                    if _uses_retimed_edition_chapters(title, kept_inputs)
                    else None
                ),
                fix_source_id_statistics=any(
                    _source_id_for_stream(stream, title) is not None
                    for stream in streams
                ),
            )
        finally:
            self.active_processes.unregister_output(out_file)
            for path in input_plan.cleanup:
                try:
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _log_created(out_file: Path) -> None:
        size = out_file.stat().st_size
        units = ["B", "KB", "MB", "GB", "TB"]
        display = float(size)
        for u in units:
            if display < 1024 or u == units[-1]:
                log_info(
                    tr(
                        "Created: {name} ({size:.1f} {unit})",
                        name=out_file.name,
                        size=display,
                        unit=u,
                    )
                )
                return
            display /= 1024

    def _show_progress(self, label: str, pct: int) -> None:
        name = label if len(label) <= 24 else label[:21] + "..."
        filled = max(0, min(20, pct // 5))
        bar = "█" * filled + "░" * (20 - filled)
        self.active_processes.set_progress_active(True)
        sys.stderr.write(f"\rMuxing {name} {bar} {pct:3d}%")
        sys.stderr.flush()

    def _run_mkvmerge(
        self, cmd: list[str], label: str, duration: float, timeout: int = 3600
    ) -> tuple[int, str, bool]:
        """Run mkvmerge, showing live progress parsed from its output."""
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        # mkvmerge runs in its own session, so the terminal's Ctrl+C never
        # reaches it. Track its pgid (== its pid under start_new_session) so
        # the signal handler can kill it instead of relying on broken-pipe
        # death after we exit.
        self.active_processes.register_muxer(process.pid)
        timeout_state = _MkvmergeTimeoutState()
        watchdog = _start_mkvmerge_watchdog(process, timeout_state, timeout)
        try:
            assert process.stdout is not None
            output = _read_mkvmerge_output(
                process.stdout,
                lambda percentage: self._show_progress(label, percentage),
            )
        except BaseException as exc:
            _kill_mkvmerge_process(process)
            if isinstance(exc, KeyboardInterrupt):
                process.wait()
                self.active_processes.finish_progress_line()
            raise
        finally:
            watchdog.cancel()
            self.active_processes.unregister_muxer(process.pid)

        returncode = process.wait()
        self.active_processes.finish_progress_line()
        return returncode, output, timeout_state.timed_out
