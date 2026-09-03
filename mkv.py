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

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any, final

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
)
from models import (
    CONFIG,
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
    _TEMP_FILES,
    _HAS_MKVMERGE,
    register_active_muxer,
    unregister_active_muxer,
    register_active_output,
    unregister_active_output,
    finish_progress_line,
    set_progress_active,
)
from probe import _MKVMERGE_CODEC_MAP
from i18n import tr

if TYPE_CHECKING:
    from tagger import ArtAttachment, MovieMetadata


# =============================================================================
# Muxing
# =============================================================================


def select_streams(title: Title, force: list[str] | None = None) -> list[Stream]:
    if force:
        return _select_explicit(title, force)
    sel: list[Stream] = [title.video_streams[0]] if title.video_streams else []
    sel.extend(
        s
        for s in title.audio_streams
        if CONFIG.keep_all_audio or s.language in CONFIG.preferred_languages
    )
    sel.extend(
        s
        for s in title.subtitle_streams
        if CONFIG.keep_all_subtitles and (CONFIG.include_forced or not s.is_forced)
    )
    return sel


def _select_explicit(title: Title, sids: list[str]) -> list[Stream]:
    sel: list[Stream] = []
    type_map = {
        "v:all": StreamType.VIDEO,
        "a:all": StreamType.AUDIO,
        "s:all": StreamType.SUBTITLE,
    }
    for sid in sids:
        if sid in type_map:
            sel.extend([s for s in title.streams if s.stream_type == type_map[sid]])
        elif ":" in sid:
            p, i = sid.split(":", 1)
            st = {
                "v": StreamType.VIDEO,
                "a": StreamType.AUDIO,
                "s": StreamType.SUBTITLE,
            }.get(p)
            if st:
                if i.isalpha() and len(i) == 3:
                    sel.extend(
                        [
                            s
                            for s in title.streams
                            if s.stream_type == st and s.language == i
                        ]
                    )
                else:
                    try:
                        sel.extend(
                            [
                                s
                                for s in title.streams
                                if s.stream_type == st and s.type_index == int(i)
                            ]
                        )
                    except ValueError:
                        pass
    seen: set[int] = set()
    out: list[Stream] = []
    for s in sel:
        if s.index not in seen:
            seen.add(s.index)
            out.append(s)
    return out


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


def _resolve_video_color(stream: Stream) -> tuple[str, str, str, str] | None:
    """Return (primaries, transfer, matrix, range) to apply to a video stream.

    Uses the source's own signalling when present (scan-time CLPI/STN colour
    metadata for Blu-ray, IFO standard for DVD). When nothing is set, infers
    from the resolution: HD (720p/1080p) and unmarked UHD (2160p) are BT.709
    end-to-end (SDR — HDR is only inferred at scan time when the STN table
    marks the stream hdr10/dolby_vision, in which case the fields are already
    set and this fallback never runs), and SD (480/576-line) video falls back
    to the standard-definition defaults (BT.601 NTSC/PAL, limited range) -
    otherwise the muxer writes no colour tags at all and players guess.
    """
    primaries = stream.color_primaries
    transfer = stream.color_transfer
    matrix = stream.color_space
    rng = stream.color_range or "tv"

    if primaries is None and stream.height in (480, 576, 720, 1080, 2160):
        if stream.height in (720, 1080, 2160):
            # HD Blu-ray/AVC is virtually always BT.709 end-to-end, and UHD
            # without explicit HDR signalling defaults to SDR BT.709 as well.
            primaries = transfer = matrix = "bt709"
        else:
            # SDTV/DVD defaults per V4L2_COLORSPACE_SMPTE170M: BT.601
            # primaries + Y'CbCr matrix, but the BT.709 transfer function
            # (the SMPTE 170M and Rec.709 OETF curves are defined to be
            # identical; see the V4L2 detailed colorspace docs, sect. 2.6.1).
            transfer = "bt709"
            if stream.height == 576:  # PAL/SECAM
                primaries = matrix = "bt470bg"
            else:  # NTSC / 480-line
                primaries = matrix = "smpte170m"

    if primaries is None and transfer is None and matrix is None:
        return None
    return (
        primaries or "unknown",
        transfer or "unknown",
        matrix or "unknown",
        rng or "tv",
    )


# _AUDIO_CHANNEL_TITLES now lives in dvdifo.py (imported explicitly above).


def _audio_title(stream: Stream, channel_count: int | None = None) -> str | None:
    """Synthesise a track title (e.g. 'AC3 Surround 5.1') from codec and channel count.

    DVDs carry no audio title metadata; MakeMKV generates labels from the channel
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
    element handles that for mediainfo).  The MakeMKV-style prefix is unnecessary
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


@final
class MKVCreator:
    def __init__(self, out: Path, tag_opts: TagOptions | None = None):
        self.out = out
        self.tag_opts = tag_opts
        self.out.mkdir(parents=True, exist_ok=True)

    def create_mkv(self, title: Title, streams: list[Stream] | None = None) -> Path:
        from disc_reader import _extract_full_for_muxing, temp_base_for_title
        from tagger import _prepare_tagging, _write_tag_xml

        if not streams:
            streams = select_streams(title)
        if not streams:
            raise RipError(message="No streams selected", title=title, streams=streams)

        # Build a filesystem-safe filename: strip Windows path chars and any
        # non-alphanumeric/non-ASCII characters (e.g. ™, ©, newlines) from the
        # filename only — the movie name metadata keeps its original formatting.
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", title.name)
        safe_name = re.sub(r"[^\w\s\-.]", "", safe_name)
        safe_name = re.sub(r"\s+", " ", safe_name).strip()
        out_file = self.out / f"{safe_name}_t{title.index:02d}.mkv"

        # Ordered input files. ISO sources are extracted up front; a
        # seamless-branching playlist contributes its segments as appended inputs.
        is_iso = title.iso_internal_paths and title.source_file.suffix.lower() == ".iso"
        # Estimate the on-disk footprint of this title's raw streams so the
        # RAM-backed temp dir can spill oversized titles to disk. ISO sizes were
        # captured at scan time; for folder/device sources we stat the inputs.
        est = title.estimated_size_bytes
        if not est and not is_iso:
            try:
                est = sum(
                    f.stat().st_size
                    for f in (title.source_file, *title.append_clips)
                    if f.is_file()
                )
            except Exception:
                est = 0
        _extract_temp_base = temp_base_for_title(est)
        if is_iso:
            inputs = _extract_full_for_muxing(
                title.source_file,
                title.iso_internal_paths,
                temp_base=_extract_temp_base,
            )
            cleanup = list(inputs)
        else:
            inputs = [title.source_file, *title.append_clips]
            cleanup = []

        if not inputs:
            raise RipError(message="No source files", title=title, streams=streams)

        # DVD cell trimming: many discs store warning/intro PGCs interleaved
        # with the movie in the same VOBs, each resetting its PTS to ~0. Copying
        # the raw VOBs then splices those cards in front of the movie and breaks
        # A/V sync. When that is detected we extract only the movie's cells (the
        # longest PTS-continuous run) into a temp file and mux from that instead.
        video_stream_scan = next(
            (s for s in streams if s.stream_type == StreamType.VIDEO), None
        )
        is_dvd_vob = (
            video_stream_scan is not None
            and video_stream_scan.codec == "mpeg2video"
            and inputs[0].suffix.lower() == ".vob"
        )
        _vobu_trim_parts: list[Path] | None = None
        _vobu_part_sizes: list[int] | None = None
        if is_dvd_vob:
            rng: tuple[int, int] | None = None
            # Try IFO cell address table first (instant; no VOB scanning).
            if title.dvd_ifo_data is not None:
                try:
                    total_size = sum(f.stat().st_size for f in inputs)
                    rng = _lookup_main_feature_range(
                        title.dvd_ifo_data,
                        total_size,
                        title.dvd_pgc_number,
                    )
                    if rng:
                        log_debug(f"IFO cell trim: extracting bytes {rng[0]}-{rng[1]}")
                except Exception as e:
                    log_debug(f"IFO cell table failed ({e}); trying PTS scan")
                    rng = None
            # Fallback: PTS-based scanning (works on all discs).
            if rng is None:
                try:
                    rng = _dvd_main_content_range(inputs)
                except Exception as e:
                    log_debug(f"DVD PTS cell scan failed ({e}); muxing raw VOBs")
                    rng = None
            if rng is not None:
                start, end = rng
                _vob_dir = str(_extract_temp_base) if _extract_temp_base else None
                tmp = Path(
                    tempfile.NamedTemporaryFile(
                        suffix=".vob",
                        delete=False,
                        dir=_vob_dir,
                    ).name
                )
                _TEMP_FILES.append(tmp)
                cleanup.append(tmp)

                # Try VOBU-level extraction for seamless branching discs.
                # This scans each VOBU's own NAV pack to determine which
                # edition it truly belongs to (matching the main PGC's own
                # cells), skipping interleaved VOBUs from other editions
                # that a contiguous byte range would otherwise capture.
                _vobu_ranges: list[tuple[int, int]] | None = None
                if title.dvd_ifo_data is not None:
                    try:
                        _admap = _parse_vts_vobu_admap(title.dvd_ifo_data)
                        if _admap:
                            _vobu_ranges = _build_main_edition_vobu_ranges(
                                title.dvd_ifo_data,
                                _admap,
                                inputs,
                                title.dvd_pgc_number,
                            )
                    except Exception:
                        _vobu_ranges = None

                if _vobu_ranges:
                    _total_vobu = sum(e - s for s, e in _vobu_ranges)
                    log_info(
                        f"Trimming DVD main edition "
                        f"({len(_vobu_ranges)} VOBU run(s), "
                        f"{_total_vobu / 1e9:.1f} GB)..."
                    )
                    # Write each VOBU run to a separate temp file, then
                    # concatenate into the final trimmed VOB.
                    _parts: list[Path] = []
                    for vs, ve in _vobu_ranges:
                        _p = Path(
                            tempfile.NamedTemporaryFile(
                                suffix=".vob",
                                delete=False,
                                dir=_vob_dir,
                            ).name
                        )
                        _TEMP_FILES.append(_p)
                        cleanup.append(_p)
                        _extract_concat_range(inputs, vs, ve, _p)
                        _parts.append(_p)
                    with open(tmp, "wb") as _out:
                        for _p in _parts:
                            _out.write(_p.read_bytes())
                    inputs = [tmp]
                    # Keep the per-run parts for subtitle PTS remapping.
                    # On seamless-branching discs, the concatenated VOB has
                    # PTS discontinuities at each run boundary; subtitle
                    # extraction needs to remap timestamps per-run.
                    _vobu_trim_parts = _parts if len(_parts) > 1 else None
                    # Pass the total VOBU run byte sizes for proportional
                    # duration allocation (the PGC's declared playback time
                    # is authoritative, not raw PTS which can reset at cell
                    # boundaries in interleaved blocks).
                    _vobu_part_sizes = (
                        [e - s for s, e in _vobu_ranges] if _vobu_trim_parts else None
                    )
                else:
                    log_info(
                        f"Trimming DVD to main feature "
                        f"({(end - start) / 1e9:.1f} GB)..."
                    )
                    _extract_concat_range(inputs, start, end, tmp)
                    inputs = [tmp]
            else:
                log_warn(
                    "DVD cell trimming failed; muxing raw VOBs. "
                    "Output duration may be incorrect. "
                    "Run with --debug to see why trimming was skipped."
                )
        # mkvmerge enumerates DVD MPEG-PS streams by stream ID (not by first
        # packet appearance like ffmpeg), so the scanned per-type indices and
        # sub_ids from IFO data are authoritative.  No reconciliation step is
        # needed — we map selected streams to mkvmerge track IDs below by
        # matching sub_id (DVD) or pid (Blu-ray).
        # Identify the actual track layout from the first input so we can map
        # our selected Stream objects to the correct mkvmerge track IDs.
        ident_tracks = _identify_input_tracks(inputs[0])
        if not ident_tracks:
            log_debug(
                "mkvmerge -J returned no tracks; muxing will include all streams "
                "and per-stream properties may be incorrect."
            )

        # Map selected streams to mkvmerge input track IDs.
        # Strategy: first try matching by sub_id/pid (stream ID), then fall
        # back to matching by stream type + position within type.  This handles
        # cases where mkvmerge's -J output does not include a 'number' property
        # that matches our sub_id (e.g. DVD video: IFO uses 0x1E0 but
        # mkvmerge reports the raw PES stream ID 0xE0).
        mapped: list[dict[str, Any]] = []  # {input_id, type, stream}

        # Build a positional index of ident_tracks for fallback matching.
        # Maps (type, type_index) -> track dict.
        type_counter: dict[str, int] = {}
        type_position: dict[tuple[str, int], dict[str, Any]] = {}
        if ident_tracks:
            for t in ident_tracks:
                tt = t.get("type", "")
                idx = type_counter.get(tt, 0)
                type_position[(tt, idx)] = t
                type_counter[tt] = idx + 1

        for s in streams:
            match_id = s.pid or s.sub_id
            matched_track = None
            if match_id is not None and ident_tracks:
                # Pass 1: match by stream ID (sub_id / pid).
                for t in ident_tracks:
                    if t.get("properties", {}).get("number") == match_id:
                        matched_track = t
                        break

            if matched_track is None and ident_tracks:
                # Pass 2: match by stream type + position within type.
                tt = s.stream_type.value
                if tt == "subtitle":
                    tt = "subtitles"  # mkvmerge uses "subtitles"
                candidate = type_position.get((tt, s.type_index))
                if candidate is not None:
                    matched_track = candidate
                    # Distinguish ID-based from positional matching in debug.
                    if match_id is not None:
                        log_debug(
                            f"  Positional fallback for {s.display_id}: "
                            f"id=0x{match_id:x} matched track {candidate['id']} "
                            f"by type={tt} index={s.type_index}"
                        )

            if matched_track is not None:
                ident_channels = matched_track.get("properties", {}).get(
                    "audio_channels"
                )
                # Override the scan-time codec from mkvmerge's bitstream-level
                # identification. Both the MPLS STN table and CLPI can contain
                # authoring errors (e.g. some Disney discs label TrueHD as
                # DTS-HD HR in both metadata sources). mkvmerge -J reads the
                # actual codec from the M2TS bitstream, making it the only
                # authoritative source for the codec identity.
                ident_codec = matched_track.get("codec", "")
                mapped_codec = _MKVMERGE_CODEC_MAP.get(ident_codec)
                if mapped_codec and mapped_codec != s.codec:
                    log_debug(
                        f"  Codec override for {s.display_id}: "
                        f"{s.codec} -> {mapped_codec} (from mkvmerge -J)"
                    )
                    s.codec = mapped_codec
                mapped.append(
                    {
                        "input_id": matched_track["id"],
                        "type": matched_track["type"],
                        "stream": s,
                        "ident_channels": ident_channels,
                    }
                )
            else:
                # No match found — will be handled by positional fallback below.
                if match_id is not None:
                    log_debug(
                        f"  Dropping stream: {s.display_id} (id=0x{match_id:x}) not found in source"
                    )
                mapped.append(
                    {
                        "input_id": -1,
                        "type": s.stream_type.value,
                        "stream": s,
                        "ident_channels": None,
                    }
                )

        # If we have ident data, sort mapped by input_id for clean output track order.
        if ident_tracks:
            mapped.sort(key=lambda m: m["input_id"])

        # ---- DVD subtitle extraction fallback ----
        # mkvmerge's -J probe cannot detect DVD subpicture streams in VOB
        # files (they only appear in later VOB segments, and mkvmerge's
        # initial scan of the first VOB never finds them). We scan the
        # MPEG-PS bitstream directly for private_stream_1 (0xBD) packets
        # with sub_stream_id 0x20-0x3F, then write VobSub .idx/.sub files
        # that mkvmerge CAN read natively.
        _sub_fallback_mkv: Path | None = None
        _sub_fallback_tracks: list[dict[str, Any]] = []
        _unmatched_ifo_subs: list[Stream] = [
            m["stream"]
            for m in mapped
            if m["input_id"] < 0 and m["stream"].stream_type == StreamType.SUBTITLE
        ]
        if _unmatched_ifo_subs and is_dvd_vob:
            # Build language/forced maps from IFO subtitle stream attributes.
            _sub_lang_by_id: dict[int, str] = {}
            _sub_forced_by_id: dict[int, bool] = {}
            for s in _unmatched_ifo_subs:
                if s.sub_id is not None:
                    _sub_lang_by_id[s.sub_id] = s.language
                    _sub_forced_by_id[s.sub_id] = s.is_forced
            _vobs_to_scan: list[Path] = inputs
            log_debug(
                "DVD subtitle fallback: %d IFO subs (%s), scanning %d VOB(s)"
                % (
                    len(_unmatched_ifo_subs),
                    ", ".join(
                        "0x%02x=%s" % (sid, _sub_lang_by_id.get(sid, "?"))
                        for sid in sorted(_sub_lang_by_id)
                    ),
                    len(_vobs_to_scan),
                )
            )
            # Scan the SAME (already trimmed/cell-reordered) VOB that the
            # video and audio tracks are muxed from, via ``inputs`` - not the
            # original raw disc source. Subtitle SPU packets carry their own
            # embedded PTS timestamps, and on discs where cell trimming
            # reorders or drops content (seamless branching, menu/junk cell
            # removal, etc.) those timestamps only line up with the actual
            # muxed video/audio timeline if we re-derive them from that same
            # trimmed byte stream. Scanning the untrimmed original source
            # here produced subtitles timed against a completely different
            # (raw, pre-trim) timeline, causing them to appear at the wrong
            # time entirely (e.g. starting mid-sentence at the wrong spot).
            # Try to extract the IFO PGC palette (discs with real luminance
            # entries will use their intended colours; zeroed palettes will
            # fall through to the default greyscale + custom colors).
            _ifo_palette: list[tuple[int, int, int]] | None = None
            if title.dvd_ifo_data is not None:
                # Always use the default title PGC's palette (pgc_number=None)
                # for subtitle rendering, even for alternate editions. Different
                # PGCs may have different palettes, but subtitle colours should
                # be consistent across editions of the same movie.
                _ifo_palette = _extract_dvd_ifo_palette(
                    title.dvd_ifo_data,
                    None,
                )
            result = _extract_dvd_vobsubs(
                _vobs_to_scan,
                _sub_lang_by_id,
                _sub_forced_by_id,
                ifo_palette=_ifo_palette,
                vobu_parts=_vobu_trim_parts,
                vobu_part_sizes=_vobu_part_sizes if _vobu_trim_parts else None,
                total_duration=title.duration_seconds,
            )
            if result is not None:
                _sub_fallback_mkv, _sub_fallback_tracks = result
                log_debug(
                    "DVD subtitle fallback: %d extracted track(s) for %d IFO stream(s)"
                    % (len(_sub_fallback_tracks), len(_unmatched_ifo_subs))
                )

        # Metadata tagging (optional): fetch from TMDB up front so the tags and
        # any cover art can be embedded directly in the mux command below. A
        # tagging failure never aborts the rip; we just mux without tags.
        tag_md: MovieMetadata | None = None
        tag_art: list[ArtAttachment] = []
        if self.tag_opts is not None and self.tag_opts.enabled:
            try:
                tag_md, tag_art = _prepare_tagging(title.name, self.tag_opts)
            except Exception as e:
                log_warn(tr("Tagging failed (ripping without tags): {err}", err=e))
                tag_md, tag_art = None, []

        # Inject disc-level metadata tags into MovieMetadata so they are
        # written alongside TMDB tags in the Matroska global-tags XML.
        if tag_md is not None:
            tag_md.custom_properties["ENCODER"] = "mkvsmith"
            if title.disc_barcode:
                tag_md.custom_properties["BARCODE"] = title.disc_barcode
            # ORIGINAL_MEDIA_TYPE: infer from title source file extension or
            # ISO content path.
            src_name = title.source_file.name.lower()
            iso_paths = title.iso_internal_paths
            if any(p.upper().startswith("BDMV") for p in iso_paths):
                tag_md.custom_properties["ORIGINAL_MEDIA_TYPE"] = "Blu-ray"
            elif any(p.upper().startswith("VIDEO_TS") for p in iso_paths):
                tag_md.custom_properties["ORIGINAL_MEDIA_TYPE"] = "DVD"
            elif src_name.endswith(".vob"):
                tag_md.custom_properties["ORIGINAL_MEDIA_TYPE"] = "DVD"
            elif src_name.endswith(".m2ts"):
                tag_md.custom_properties["ORIGINAL_MEDIA_TYPE"] = "Blu-ray"

        chapters_file: Path | None = None
        tags_file: Path | None = None
        try:
            cmd = ["mkvmerge", "-o", str(out_file)]

            # Container-level title. Prefer the canonical TMDB title when we
            # have it; otherwise fall back to the disc name from disc metadata
            # (bdmt.xml / VMG IFO), and only then to the inferred title name.
            container_title = (
                tag_md.title
                if (tag_md and tag_md.title)
                else (title.disc_name or title.name)
            )
            cmd += ["--title", container_title]

            # Chapters as Matroska Chapters XML.
            if title.editions:
                # Multi-edition rip: one ordered edition per playlist cut.
                chapters_file = Path(
                    tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name
                )
                _TEMP_FILES.append(chapters_file)
                cleanup.append(chapters_file)
                _write_multi_edition_chapters_xml(title.editions, chapters_file)
                cmd += ["--chapters", str(chapters_file)]
                for ed in title.editions:
                    log_debug(
                        f"  Edition {ed.uid} '{ed.name}'"
                        f"{' (default)' if ed.is_default else ''}: "
                        f"{len(ed.atoms)} atoms, {ed.duration:.0f}s"
                    )
            elif title.chapters:
                chapters = list(title.chapters)
                # Drop trailing chapter if it matches the title duration
                # (MakeMKV convention: final chapter-at-end-of-movie is omitted).
                if len(chapters) > 1 and title.duration_seconds > 0:
                    dur = title.duration_seconds
                    if chapters[-1] >= dur - 0.5:
                        chapters = chapters[:-1]
                        log_debug(
                            f"Filtered trailing end-of-movie chapter; {len(chapters)} remain"
                        )
                if chapters:
                    chapters_file = Path(
                        tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name
                    )
                    _TEMP_FILES.append(chapters_file)
                    cleanup.append(chapters_file)
                    _write_chapters_xml(chapters, chapters_file)
                    cmd += ["--chapters", str(chapters_file)]
                    log_debug(f"Loaded {len(chapters)} chapters")

            # Track selection: only mux the streams the user chose.
            # Only include entries with valid (non-negative) input IDs.
            #
            # The filter is captured into ``track_filter_opts`` so it can be
            # repeated before each appended file (``+ clip``). Without this,
            # mkvmerge reads ALL tracks from appended files and the default
            # append mapping fails when an appended clip has a track that the
            # first clip lacks (common on BD: the opening clip may omit an
            # audio PID that later clips carry). MPEG-TS track IDs are
            # PID-based, so the same IDs select the same streams across all
            # clips in a playlist.
            track_filter_opts: list[str] = []
            if ident_tracks and mapped:
                video_ids = [
                    str(m["input_id"])
                    for m in mapped
                    if m["input_id"] >= 0 and m["type"] == "video"
                ]
                audio_ids = [
                    str(m["input_id"])
                    for m in mapped
                    if m["input_id"] >= 0 and m["type"] == "audio"
                ]
                sub_ids = [
                    str(m["input_id"])
                    for m in mapped
                    if m["input_id"] >= 0 and m["type"] in ("subtitle", "subtitles")
                ]
                if video_ids:
                    track_filter_opts += ["--video-tracks", ",".join(video_ids)]
                n_scan_audio = sum(1 for t in ident_tracks if t.get("type") == "audio")
                n_ifo_audio = len(title.audio_streams) if title.audio_streams else 0
                use_audio_filter = not (
                    n_ifo_audio > 0
                    and n_scan_audio < n_ifo_audio
                    and title.dvd_ifo_data is not None
                )
                if not use_audio_filter:
                    log_debug(
                        f"DVD audio stream fallback: mkvmerge -J found "
                        f"{n_scan_audio}/{n_ifo_audio} audio tracks, "
                        "omitting --audio-tracks filter"
                    )
                if use_audio_filter and audio_ids:
                    track_filter_opts += ["--audio-tracks", ",".join(audio_ids)]
                if sub_ids:
                    track_filter_opts += ["--subtitle-tracks", ",".join(sub_ids)]
            cmd += track_filter_opts

            # Fallback: no ident data — include all tracks and set language
            # per-stream using output renumbering (less precise).
            # mkvmerge will use all tracks from the source by default.
            need_positional_fallback = not (ident_tracks and mapped)

            # Per-stream language, default/forced, and track name.
            for m in mapped:
                s = m["stream"]
                input_id = m["input_id"]

                if need_positional_fallback or input_id < 0:
                    # Without ident data or for unmatched streams, skip
                    # per-track property setting (unreliable without IDs).
                    log_debug(
                        f"  No input track ID for {s.display_id}; "
                        "track properties may be misaligned."
                    )
                    continue

                cmd += ["--language", f"{input_id}:{s.language}"]

                # Explicitly set --default-track to match the source; passing
                # neither 'yes' nor 'no' lets mkvmerge apply its own defaults,
                # which often marks the first track of each type as default
                # even when the source had no such flag.
                if s.is_default:
                    cmd += ["--default-track", f"{input_id}:yes"]
                else:
                    cmd += ["--default-track", f"{input_id}:no"]

                if s.is_forced:
                    cmd += ["--forced-track", f"{input_id}:yes"]

                if s.is_hearing_impaired:
                    cmd += ["--hearing-impaired-flag", f"{input_id}:yes"]

                if s.is_commentary:
                    cmd += ["--commentary-flag", f"{input_id}:yes"]

                # Colour signalling: forward scan-time colour metadata
                # (CLPI/STN for Blu-ray, IFO standard for DVD) so the output
                # carries explicit primaries/transfer/matrix even when the
                # bitstream declares none (common for DVD MPEG-2 and some
                # BD AVC streams).
                if s.stream_type == StreamType.VIDEO:
                    color_info = _resolve_video_color(s)
                    if color_info is not None:
                        c_primaries, c_transfer, c_matrix, c_range = color_info
                        for opt, code in (
                            ("--color-primaries", _COLOR_CICP.get(c_primaries)),
                            (
                                "--color-transfer-characteristics",
                                _COLOR_CICP.get(c_transfer),
                            ),
                            (
                                "--color-matrix-coefficients",
                                _COLOR_CICP.get(c_matrix),
                            ),
                            ("--color-range", _COLOR_RANGE.get(c_range)),
                        ):
                            if code is not None:
                                cmd += [opt, f"{input_id}:{code}"]

                # Track name: prefer explicit title, otherwise synthesize
                # for audio from channel count + codec. Use the accurate channel
                # count from mkvmerge's identification when available (the
                # scan-time CLPI count can be wrong, e.g. 5.0 stored as 5.1).
                track_name = s.title or ""
                if not track_name and s.stream_type == StreamType.AUDIO:
                    track_name = _audio_title(s, m.get("ident_channels")) or ""
                if track_name:
                    cmd += ["--track-name", f"{input_id}:{track_name}"]

            # Log a warning when we have no ident data to match against.
            if need_positional_fallback:
                log_warn(
                    "mkvmerge track identification unavailable; "
                    "track properties (language, name) may not be applied correctly."
                )

            # File-level Matroska Tags (TMDB metadata and/or edition names).
            # Edition TITLE tags are required for multi-edition rips even when
            # TMDB tagging is off — they are how players name the editions.
            if tag_md is not None or title.editions:
                tags_file = Path(
                    tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name
                )
                _TEMP_FILES.append(tags_file)
                cleanup.append(tags_file)
                _write_tags_xml_mkvmerge(tags_file, md=tag_md, editions=title.editions)
                cmd += ["--global-tags", str(tags_file)]

            # Cover art as Matroska attachments.
            for art in tag_art:
                cmd += [
                    "--attachment-name",
                    art["filename"],
                    "--attachment-mime-type",
                    art["mime"],
                    "--attachment-description",
                    art["label"],
                    str(art["path"]),
                ]

            # NOTE: We deliberately do *not* force --default-duration based on
            # the IFO's declared video_attr_t.film_mode bit here. That bit is
            # a disc-authoring declaration, not a measurement, and is known to
            # be unreliable on real discs. Forcing a *wrong* constant frame
            # rate onto content that is actually soft-telecined/VFR (or vice
            # versa) doesn't just mislabel the output - it corrupts
            # mkvmerge's internal timing model and can silently truncate a
            # large fraction of the video with no warning (confirmed: forcing
            # 30000/1001fps onto genuine 23.976fps pulldown content dropped
            # ~20% of the video track while audio/container duration looked
            # unaffected). mkvmerge's own frame-rate autodetection from the
            # MPEG-2 sequence headers is reliable once the source is cleanly
            # trimmed to a single edition (see cell/VOBU trimming above) and
            # should be trusted instead.

            # Input files (with append syntax for seamless branching).
            # --append-mode track is essential for M2TS/VOB segments that are
            # parts of one continuous timeline (seamless-branching playlists,
            # multi-VOB DVD titles). The default 'file' mode offsets ALL tracks
            # in the appended file by the single highest timestamp across ALL
            # tracks in the previous file. Since audio (TrueHD especially) runs
            # slightly longer than video in each segment, 'file' mode offsets
            # the appended video by audio_end rather than video_end, creating a
            # tiny video gap at every segment boundary (~0.5ms each, compounding
            # to tens of ms over a heavily-branched title like Monsters
            # University with 130+ segments). 'track' mode gives each track its
            # own offset, keeping video and audio each continuous.
            #
            # track_filter_opts is repeated before each appended file so that
            # mkvmerge only reads the selected tracks from it. Without this,
            # clips that carry a PID absent from the first clip (e.g. an audio
            # track that starts later) cause the default append mapping to fail.
            if len(inputs) > 1:
                cmd += ["--append-mode", "track"]
            cmd.append(str(inputs[0]))
            for clip in inputs[1:]:
                cmd += ["+"]
                if track_filter_opts:
                    cmd += track_filter_opts
                cmd.append(str(clip))

            # DVD subtitle fallback (.idx): add as a separate input file
            # (not appended) so its tracks are included in the output, and apply
            # language/default/forced from the IFO subtitle stream attributes.
            #
            # IMPORTANT: Options for the .idx input must come AFTER the main VOB
            # files but BEFORE the .idx filename in the mkvmerge command.
            # mkvmerge applies options to the NEXT input file.  If we placed
            # --language 4:en before the VOB, mkvmerge would look for track 4
            # in the VOB (which only has tracks 0-3).  Instead, we use the .idx
            # file's internal track IDs (0, 1, 2) placed just before the .idx.
            if _sub_fallback_mkv is not None and _sub_fallback_tracks:
                # Collect subtitle fallback options (applied just before .idx).
                _sub_opts: list[str] = []
                if ident_tracks:
                    for i, ifo_stream in enumerate(_unmatched_ifo_subs):
                        if i >= len(_sub_fallback_tracks):
                            log_debug(
                                f"  Sub fallback: {len(_sub_fallback_tracks)} extracted tracks "
                                f"< {len(_unmatched_ifo_subs)} IFO streams; stopping"
                            )
                            break
                        # Use .idx internal track IDs (0, 1, 2) not global IDs.
                        idx_track_id = i
                        log_debug(
                            f"  Sub fallback: {ifo_stream.display_id} -> "
                            f".idx track {idx_track_id} ({ifo_stream.language})"
                        )
                        _sub_opts += [
                            "--language",
                            f"{idx_track_id}:{ifo_stream.language}",
                        ]
                        if ifo_stream.is_default:
                            _sub_opts += ["--default-track", f"{idx_track_id}:yes"]
                        else:
                            _sub_opts += ["--default-track", f"{idx_track_id}:no"]
                        if ifo_stream.is_forced:
                            _sub_opts += ["--forced-track", f"{idx_track_id}:yes"]
                        if ifo_stream.is_hearing_impaired:
                            _sub_opts += [
                                "--hearing-impaired-flag",
                                f"{idx_track_id}:yes",
                            ]
                        if ifo_stream.is_commentary:
                            _sub_opts += ["--commentary-flag", f"{idx_track_id}:yes"]
                        # Synthesise a track name from the IFO language.
                        track_name = ifo_stream.title or ""
                        if not track_name and ifo_stream.language != "und":
                            lang_name = get_language_name(ifo_stream.language)
                            if lang_name:
                                track_name = f"Subtitles ({lang_name})"
                        if track_name:
                            _sub_opts += [
                                "--track-name",
                                f"{idx_track_id}:{track_name}",
                            ]
                else:
                    log_debug(
                        "Cannot apply language tags to DVD subtitle fallback "
                        "(no mkvmerge track identification data)"
                    )

                # Add subtitle options (they apply to the .idx file which follows).
                cmd += _sub_opts
                cleanup.append(_sub_fallback_mkv)
                cmd.append(str(_sub_fallback_mkv))

            log_info(tr("Muxing: {name}...", name=out_file.name))
            # Track the in-progress output so Ctrl+C deletes the partial file
            # instead of leaving a truncated mkv next to completed rips.
            register_active_output(out_file)
            rc, output_text, timed_out = self._run_mkvmerge(
                cmd, out_file.name, title.duration_seconds
            )
            if timed_out:
                raise RipError(
                    message="mkvmerge timed out after 3600s",
                    command=cmd,
                    stderr=output_text,
                    title=title,
                    streams=streams,
                )
            if rc == 1:
                log_warn(
                    "mkvmerge completed with warnings; check the output for details"
                )
                # Show mkvmerge warnings to help debug subtitle issues
                if CONFIG.debug:
                    for _line in output_text.split("\n"):
                        stripped = _line.strip()
                        if "Warning" in stripped or "warning" in stripped:
                            if "%" not in stripped:  # skip progress lines
                                log_debug(f"  mkvmerge: {stripped}")
            elif rc != 0:
                raise RipError(
                    message=f"mkvmerge failed ({rc})",
                    command=cmd,
                    returncode=rc,
                    stderr=output_text,
                    title=title,
                    streams=streams,
                )
            if not out_file.exists():
                raise RipError(
                    message="Output missing", command=cmd, title=title, streams=streams
                )

            self._log_created(out_file)
            if (
                tag_md is not None
                and self.tag_opts is not None
                and self.tag_opts.save_xml
            ):
                try:
                    xml_path = out_file.with_suffix(".xml")
                    _write_tag_xml(tag_md, xml_path)
                    log_info(f"Tag XML written: {xml_path}")
                except Exception as e:
                    log_warn(tr("Could not write tag XML: {err}", err=e))
            if tag_art:
                labels = ", ".join(a["label"] for a in tag_art)
                log_info(f"Attached art: {labels}")
            return out_file
        finally:
            unregister_active_output(out_file)
            for f in cleanup:
                try:
                    f.unlink()
                except Exception:
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

    @staticmethod
    def _show_progress(label: str, pct: int) -> None:
        name = label if len(label) <= 24 else label[:21] + "..."
        filled = max(0, min(20, pct // 5))
        bar = "█" * filled + "░" * (20 - filled)
        set_progress_active(True)
        sys.stderr.write(f"\rMuxing {name} {bar} {pct:3d}%")
        sys.stderr.flush()

    def _run_mkvmerge(
        self, cmd: list[str], label: str, duration: float, timeout: int = 3600
    ) -> tuple[int, str, bool]:
        """Run mkvmerge, showing live progress parsed from its output.

        mkvmerge writes progress to stderr as ``Progress: N%`` (using ``\r``
        carriage returns, not newlines). We merge stderr into stdout and read
        in 512-byte chunks, searching for the ``Progress: N%`` pattern.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        # mkvmerge runs in its own session, so the terminal's Ctrl+C never
        # reaches it. Track its pgid (== its pid under start_new_session) so
        # the signal handler can kill it instead of relying on a broken-pipe
        # death after we exit.
        register_active_muxer(proc.pid)
        chunks: list[str] = []
        last_pct = -1
        carry = ""
        timed_out = False

        def _kill_tree() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()

        def _on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            _kill_tree()

        watchdog = threading.Timer(timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                chunks.append(chunk)
                # mkvmerge progress: "Progress: 42%" (uses \r not \n)
                data = carry + chunk
                m = re.search(r"Progress:\s*(\d+)%", data)
                if m:
                    pct = min(100, int(m.group(1)))
                    if pct != last_pct:
                        last_pct = pct
                        self._show_progress(label, pct)
                carry = data[-64:]
        except KeyboardInterrupt:
            _kill_tree()
            proc.wait()
            finish_progress_line()
            raise
        finally:
            watchdog.cancel()
            unregister_active_muxer(proc.pid)
        rc = proc.wait()
        finish_progress_line()
        return rc, "".join(chunks), timed_out
