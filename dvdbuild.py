"""
DVD title building and VIDEO_TS scanning.

Extracted from main.py: turns VTS IFO data into Title objects (stream
attributes, PGC chapters/duration, seamless-branching editions, TV-episode
detection) and scans a DVD VIDEO_TS directory into a title list. The raw
IFO/PGC/VOBU binary parsing lives in dvdifo.py; VOB subpicture scanning in
vobsub.py; mkvmerge probing in probe.py.

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
from dataclasses import dataclass
from pathlib import Path

# IFO parsing helpers live in dvdifo.py (imported explicitly below).
import dvdifo
from dvdifo import (
    DvdIfoError,
    VmgInfo,
    _IFOAudioAttrs,
    _IFOSubpictureAttrs,
    _IFOVideoAttrs,
    _VTS_IFO_IDENT,
    _VTS_IFO_SUBP_COUNT,
    _VTS_IFO_SUBP_ATTR,
    _VTS_IFO_SUBP_ENTRY_LEN,
    _read_u16,
    _parse_vts_video_attrs,
    _parse_vts_audio_attrs,
    _parse_vts_ifo_languages,
    _parse_vts_subp_attrs,
    _ifo_audio_title,
    _get_active_pgc_streams,
    _parse_pgc_stream_languages,
    _parse_vts_pgc_info,
    _parse_vmg_ifo,
    _find_alternate_edition_pgcs,
    _detect_episode_pgcs,
    _default_pgc_number,
)
from models import Config, RUNTIME_STATE, StreamType, Stream, Title, log_debug, log_info
from probe import _probe_with_mkvmerge, _parse_mkvmerge_streams
from vobsub import _scan_vob_subpictures
from i18n import tr


@dataclass(frozen=True)
class _DvdDiscMetadata:
    disc_name: str | None
    disc_barcode: str | None
    vts_to_title_num: dict[int, int]


@dataclass(frozen=True)
class _DvdVtsLayout:
    vts: int
    ifo_path: Path
    first_vob: Path
    vob_parts: list[Path]
    title_name: str


def _read_dvd_disc_metadata(base: Path) -> _DvdDiscMetadata:
    """Read VMG metadata and map each VTS to its first logical DVD title."""
    vmg_path = base / "VIDEO_TS.IFO"
    vmg_info: VmgInfo | None = None
    disc_name: str | None = None
    disc_barcode: str | None = None
    if not vmg_path.exists():
        return _DvdDiscMetadata(None, None, {})

    try:
        vmg_info = _parse_vmg_ifo(vmg_path)
    except DvdIfoError as exc:
        log_debug(f"VMG IFO parse failed: {exc}")
        vmg_info = None

    vts_to_title_num: dict[int, int] = {}
    if not vmg_info:
        return _DvdDiscMetadata(disc_name, disc_barcode, vts_to_title_num)

    vmg_disc_name = vmg_info.get("disc_name")
    if vmg_disc_name:
        log_info(tr("VMG disc name: {name}", name=vmg_disc_name))
        disc_name = vmg_disc_name
    disc_barcode = vmg_info.get("barcode")
    if disc_barcode:
        log_debug(f"VMG barcode: {disc_barcode}")
    vmg_provider = vmg_info.get("provider_id", "")
    if vmg_provider:
        log_debug(f"VMG Provider ID: {vmg_provider}")

    title_map = vmg_info.get("title_map")
    if title_map:
        for title_idx, (vts_num, _ttl_num) in title_map.items():
            if vts_num not in vts_to_title_num:
                vts_to_title_num[vts_num] = title_idx

    return _DvdDiscMetadata(disc_name, disc_barcode, vts_to_title_num)


def _vob_sort_key(path: Path) -> int:
    match = re.search(r"_(\d+)\.VOB$", path.name)
    return int(match.group(1)) if match else 0


def _dvd_vts_layouts(
    base: Path, vts_to_title_num: dict[int, int]
) -> list[_DvdVtsLayout]:
    layouts: list[_DvdVtsLayout] = []
    for ifo_path in sorted(base.glob("VTS_*_0.IFO")):
        match = re.search(r"VTS_(\d+)_0\.IFO", ifo_path.name)
        if not match or int(match.group(1)) == 0:
            continue
        vts = int(match.group(1))
        first_vob = base / f"VTS_{vts:02d}_1.VOB"
        if not first_vob.exists():
            continue
        vob_parts = sorted(
            base.glob(f"VTS_{vts:02d}_[1-9].VOB"),
            key=_vob_sort_key,
        )
        logical_title = vts_to_title_num.get(vts)
        title_name = f"Title {vts}"
        if logical_title:
            title_name = f"Title {logical_title} (VTS {vts})"
            log_debug(f"TT_SRPT: VTS {vts} -> DVD Title {logical_title}")
        layouts.append(_DvdVtsLayout(vts, ifo_path, first_vob, vob_parts, title_name))
    return layouts


def _apply_dvd_disc_metadata(title: Title, metadata: _DvdDiscMetadata) -> None:
    title.disc_name = metadata.disc_name
    title.disc_barcode = metadata.disc_barcode


def _append_default_dvd_title(
    titles: list[Title],
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    config: Config | None = None,
) -> Title | None:
    title = _build_title_from_ifo(
        titles,
        layout.first_vob,
        layout.ifo_path,
        layout.vob_parts,
        layout.vts,
        title_name=layout.title_name,
        config=config,
    )
    if title is None:
        title = _create_title(
            titles, layout.first_vob, layout.title_name, config=config
        )
        if title is None:
            return None
        title.append_clips = layout.vob_parts[1:]
        _apply_dvd_ifo_languages(title, layout.ifo_path)

    _apply_dvd_disc_metadata(title, metadata)
    titles.append(title)
    return title


def _read_vts_ifo_bytes(layout: _DvdVtsLayout) -> bytes:
    try:
        return layout.ifo_path.read_bytes()
    except OSError as exc:
        log_debug(
            f"Alternate-edition PGC scan skipped for {layout.ifo_path.name}: {exc}"
        )
        return b""


def _build_dvd_pgc_title(
    titles: list[Title],
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    pgc_number: int,
    title_name: str,
    config: Config | None = None,
) -> Title | None:
    title = _build_title_from_ifo(
        titles,
        layout.first_vob,
        layout.ifo_path,
        layout.vob_parts,
        layout.vts,
        title_name=title_name,
        pgc_number=pgc_number,
        config=config,
    )
    if title is None:
        return None
    _apply_dvd_disc_metadata(title, metadata)
    return title


def _classify_default_episode_title(
    default_title: Title,
    episode_pgcs: list[int],
    play_all_pgc: int | None,
    default_pgc_num: int | None,
) -> None:
    if default_pgc_num in set(episode_pgcs):
        default_title.dvd_episode_number = (
            episode_pgcs.index(default_pgc_num) + 1
            if default_pgc_num is not None
            else None
        )
    elif play_all_pgc is not None and default_pgc_num == play_all_pgc:
        default_title.dvd_play_all = True


def _append_dvd_episode_pgcs(
    titles: list[Title],
    default_title: Title,
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    config: Config | None,
    episode_pgcs: list[int],
    default_pgc_num: int | None,
) -> None:
    for episode_index, pgc_num in enumerate(episode_pgcs, start=1):
        if pgc_num == default_pgc_num:
            log_debug(
                f"  Episode {episode_index}: PGC {pgc_num} "
                f"({default_title.duration_seconds:.0f}s) [default title]"
            )
            continue

        title = _build_dvd_pgc_title(
            titles,
            layout,
            metadata,
            pgc_num,
            f"{layout.title_name} - Episode {episode_index}",
            config=config,
        )
        if title is None:
            continue
        title.dvd_episode_number = episode_index
        titles.append(title)
        log_debug(
            f"  Episode {episode_index}: PGC {pgc_num} ({title.duration_seconds:.0f}s)"
        )


def _append_dvd_play_all(
    titles: list[Title],
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    config: Config | None,
    play_all_pgc: int | None,
    default_pgc_num: int | None,
) -> None:
    if play_all_pgc is None or play_all_pgc == default_pgc_num:
        return

    title = _build_dvd_pgc_title(
        titles,
        layout,
        metadata,
        play_all_pgc,
        f"{layout.title_name} - Play All",
        config=config,
    )
    if title is None:
        return
    title.dvd_play_all = True
    titles.append(title)
    log_debug(f"  Play all: PGC {play_all_pgc} ({title.duration_seconds:.0f}s)")


def _append_dvd_extras(
    titles: list[Title],
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    config: Config | None,
    episode_pgcs: list[int],
    play_all_pgc: int | None,
    default_pgc_num: int | None,
    ifo_bytes: bytes,
) -> None:
    episode_set = set(episode_pgcs)
    minimum_duration = (config or RUNTIME_STATE.config).min_duration
    for pgc_num, _start, duration, _cells in dvdifo._enumerate_vts_pgcs(ifo_bytes):
        if (
            pgc_num in episode_set
            or pgc_num == play_all_pgc
            or pgc_num == default_pgc_num
            or duration < minimum_duration
        ):
            continue

        title = _build_dvd_pgc_title(
            titles,
            layout,
            metadata,
            pgc_num,
            f"{layout.title_name} - Extra",
            config=config,
        )
        if title is None:
            continue
        titles.append(title)
        log_debug(f"  Extra: PGC {pgc_num} ({title.duration_seconds:.0f}s)")


def _log_dvd_episode_group(
    episode_count: int, vts: int, play_all_pgc: int | None
) -> None:
    log_info(
        tr(
            "Detected {n} episode(s) in VTS {vts}",
            n=episode_count,
            vts=vts,
        )
        + (tr(" (play-all PGC {pgc})", pgc=play_all_pgc) if play_all_pgc else "")
    )


def _append_dvd_episode_titles(
    titles: list[Title],
    default_title: Title,
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    config: Config | None,
    episode_pgcs: list[int],
    play_all_pgc: int | None,
    default_pgc_num: int | None,
    ifo_bytes: bytes,
) -> None:
    _classify_default_episode_title(
        default_title, episode_pgcs, play_all_pgc, default_pgc_num
    )
    _append_dvd_episode_pgcs(
        titles,
        default_title,
        layout,
        metadata,
        config,
        episode_pgcs,
        default_pgc_num,
    )
    _append_dvd_play_all(
        titles, layout, metadata, config, play_all_pgc, default_pgc_num
    )
    _append_dvd_extras(
        titles,
        layout,
        metadata,
        config,
        episode_pgcs,
        play_all_pgc,
        default_pgc_num,
        ifo_bytes,
    )
    _log_dvd_episode_group(len(episode_pgcs), layout.vts, play_all_pgc)


def _append_dvd_alternate_editions(
    titles: list[Title],
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    ifo_bytes: bytes,
    config: Config | None = None,
) -> None:
    extra_pgcs = (
        _find_alternate_edition_pgcs(
            ifo_bytes, (config or RUNTIME_STATE.config).min_duration
        )
        if ifo_bytes
        else []
    )
    for edition_offset, pgc_num in enumerate(extra_pgcs, start=2):
        title = _build_dvd_pgc_title(
            titles,
            layout,
            metadata,
            pgc_num,
            f"{layout.title_name} - Edition {edition_offset}",
            config=config,
        )
        if title is None:
            continue
        title.dvd_edition_label = f"Edition {edition_offset}"
        titles.append(title)
        log_debug(
            f"  Alternate edition: PGC {pgc_num} "
            f"({title.duration_seconds:.0f}s) exposed as '{title.name}'"
        )


def _append_dvd_pgc_titles(
    titles: list[Title],
    default_title: Title,
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    ifo_bytes: bytes,
    config: Config | None = None,
) -> None:
    # TV-series discs use multiple similar-duration PGCs as separate episodes.
    # Detect that pattern first; otherwise expose substantial alternate PGCs as
    # seamless-branching editions, matching MakeMKV's separate listings.
    episode_pgcs: list[int] = []
    play_all_pgc: int | None = None
    if ifo_bytes:
        episode_pgcs, play_all_pgc = _detect_episode_pgcs(
            ifo_bytes,
            (config or RUNTIME_STATE.config).min_duration,
        )
    default_pgc_num = _default_pgc_number(ifo_bytes) if ifo_bytes else None

    if len(episode_pgcs) >= 2:
        _append_dvd_episode_titles(
            titles,
            default_title,
            layout,
            metadata,
            config,
            episode_pgcs,
            play_all_pgc,
            default_pgc_num,
            ifo_bytes,
        )
    else:
        _append_dvd_alternate_editions(titles, layout, metadata, ifo_bytes, config)


def _ensure_dvd_subtitle_streams(title: "Title", sub_by_id: dict[int, str]) -> None:
    """Create VobSub stream entries from a VTS .IFO when stream probing missed them.

    DVD subpictures are sparse picture subtitles: they only emit packets while a
    line is on screen. mkvmerge probing a single VOB part can
    detects *zero* subtitle streams even though the disc has several (the scan
    then reports ``S:0`` and they are never muxed). The VTS .IFO's
    subpicture-attribute table is authoritative, so we synthesise a
    ``dvd_subtitle`` Stream per declared stream ID.

    These synthetic streams carry their MPEG sub-stream ``id`` (0x20+) so the
    muxer can match them against what mkvmerge discovers in the
    (possibly trimmed) input and label each one correctly.

    When the VTS subpicture attribute table (``title.dvd_subp_attrs``) marks
    a subtitle stream with code_extension 9 (forced), the stream's ``is_forced``
    flag is set so it is flagged as "forced" in the muxed output.
    """
    log_debug(
        f"_ensure_dvd_subtitle_streams: sub_by_id={sub_by_id}, "
        f"len(streams)={len(title.streams)}, "
        f"dvd_sub_lang_ref={id(title.dvd_sub_lang)} sub_by_id_ref={id(sub_by_id)}"
    )
    if not sub_by_id:
        return
    # Remove any probed subtitle streams (sparse VobSub detection
    # is unreliable) and rebuild from the authoritative IFO subpicture table.
    title.streams = [s for s in title.streams if s.stream_type != StreamType.SUBTITLE]
    base = (
        (max((s.index for s in title.streams), default=-1) + 1) if title.streams else 0
    )
    for i, sid in enumerate(sorted(sub_by_id)):
        is_forced = False
        if sid in title.dvd_subp_attrs:
            is_forced = title.dvd_subp_attrs[sid].is_forced
        title.streams.append(
            Stream(
                index=base + i,
                stream_type=StreamType.SUBTITLE,
                codec="dvd_subtitle",
                language=sub_by_id[sid] if sub_by_id[sid] != "und" else "und",
                type_index=i,
                sub_id=sid,
                is_forced=is_forced,
            )
        )
    log_debug(
        f"  Created {len(sub_by_id)} subtitle streams from IFO: "
        f"{[sid for sid in sorted(sub_by_id)]}"
    )


def _build_dvd_video_stream(ifo_data: bytes) -> Stream:
    video = Stream(
        0,
        StreamType.VIDEO,
        "mpeg2video",
        "und",
        "",
        False,
        False,
        type_index=0,
        sub_id=0x1E0,
    )
    video_attrs = _parse_vts_video_attrs(ifo_data)
    if not video_attrs:
        return video

    if video_attrs.resolution:
        video.width, video.height = video_attrs.resolution
    display_aspect = video_attrs.aspect_ratio
    standard = video_attrs.standard or "NTSC"
    if display_aspect and video.width and video.height:
        aspect_ratios = {"4:3": 4.0 / 3.0, "16:9": 16.0 / 9.0}
        if display_aspect in aspect_ratios:
            sample_aspect_ratio = (
                aspect_ratios[display_aspect] * video.height / video.width
            )
            video.sample_aspect_ratio = f"{sample_aspect_ratio:.6f}"

    if standard == "NTSC":
        video.color_primaries = "smpte170m"
        video.color_space = "smpte170m"
    else:
        video.color_primaries = "bt470bg"
        video.color_space = "bt470bg"
    video.color_transfer = "bt709"
    video.color_range = "limited"
    video.field_order = "progressive"
    return video


def _merged_dvd_stream_languages(
    ifo_data: bytes, pgc_number: int | None
) -> tuple[dict[int, str], dict[int, str]]:
    audio_languages, subtitle_languages = _parse_vts_ifo_languages(ifo_data)
    pgc_audio, pgc_subtitles = _parse_pgc_stream_languages(ifo_data, pgc_number)
    audio_languages.update(pgc_audio)
    subtitle_languages.update(pgc_subtitles)
    return audio_languages, subtitle_languages


def _build_dvd_audio_streams(
    ifo_data: bytes,
    active_audio: set[int],
    audio_languages: dict[int, str],
) -> list[Stream]:
    audio_attrs = _parse_vts_audio_attrs(ifo_data)
    if active_audio:
        audio_ids = sorted(active_audio)
    else:
        # Some discs do not mark all streams available in PGC control entries.
        audio_ids = sorted(audio_languages)
        if audio_ids:
            log_debug(f"PGC active_audio empty, using VTS audio IDs: {audio_ids}")

    streams: list[Stream] = []
    for type_index, stream_id in enumerate(audio_ids):
        attrs = audio_attrs.get(stream_id)
        codec_name = attrs.codec.lower() if attrs else "ac3"
        channels = attrs.channels if attrs else 2
        audio_label = _ifo_audio_title(attrs)
        stream = Stream(
            0,
            StreamType.AUDIO,
            codec_name,
            audio_languages.get(stream_id, "und"),
            "",
            False,
            False,
            type_index=type_index,
            sub_id=stream_id,
        )
        stream.channels = channels
        stream.sample_rate = "48000"
        if audio_label:
            stream.title = audio_label
        if attrs and attrs.bits_per_sample:
            stream.bits_per_sample = attrs.bits_per_sample
        if attrs and attrs.is_commentary:
            stream.is_commentary = True
        streams.append(stream)
    return streams


def _build_dvd_subtitle_streams(
    ifo_data: bytes,
    active_subtitles: set[int],
    subtitle_languages: dict[int, str],
) -> list[Stream]:
    subtitle_attrs = _parse_vts_subp_attrs(ifo_data)
    if active_subtitles:
        subtitle_ids = sorted(active_subtitles)
    else:
        # Some discs author subtitle streams without marking them available in
        # the PGC stream-control table.
        subtitle_ids = sorted(subtitle_languages)
        if subtitle_ids:
            log_debug(f"PGC active_sub empty, using VTS sub IDs: {subtitle_ids}")

    streams: list[Stream] = []
    for type_index, stream_id in enumerate(subtitle_ids):
        attrs = subtitle_attrs.get(stream_id)
        stream = Stream(
            0,
            StreamType.SUBTITLE,
            "dvd_subtitle",
            subtitle_languages.get(stream_id, "und"),
            "",
            False,
            attrs.is_forced if attrs else False,
            attrs.is_hearing_impaired if attrs else False,
            attrs.is_commentary if attrs else False,
            type_index=type_index,
            sub_id=stream_id,
        )
        streams.append(stream)
    return streams


def _build_dvd_streams_from_ifo(
    ifo_data: bytes,
    duration: float,
    pgc_number: int | None = None,
) -> list[Stream]:
    """Build authoritative DVD streams from a VTS IFO.

    Returns video, audio, and subpicture streams in mux order. Active PGC IDs
    are preferred; VTS attribute-table IDs are used when PGC control omits all
    streams. An invalid IFO returns an empty list so callers can probe instead.
    """
    if len(ifo_data) < 12 or ifo_data[:12] != _VTS_IFO_IDENT:
        return []

    active_audio, active_subtitles = _get_active_pgc_streams(ifo_data, pgc_number)
    audio_languages, subtitle_languages = _merged_dvd_stream_languages(
        ifo_data, pgc_number
    )
    return [
        _build_dvd_video_stream(ifo_data),
        *_build_dvd_audio_streams(ifo_data, active_audio, audio_languages),
        *_build_dvd_subtitle_streams(ifo_data, active_subtitles, subtitle_languages),
    ]


def _create_title(
    titles: list[Title],
    src: Path,
    name: str,
    override_duration: float | None = None,
    config: Config | None = None,
) -> Title | None:
    """Create a Title by probing *src* with mkvmerge -J.

    Returns None if probing produces no stream data.
    """
    pd = _probe_with_mkvmerge(src)
    if not pd or not pd.get("tracks"):
        return None
    dur = pd.get("duration", 0.0)
    if dur == 0 and override_duration is not None:
        dur = override_duration
    if dur < (config or RUNTIME_STATE.config).min_duration:
        return None
    t = Title(len(titles), src, name, dur)
    _parse_mkvmerge_streams(pd, t)
    return t


def _assemble_dvd_ifo_title(
    titles: list[Title],
    first_vob: Path,
    vob_parts: list[Path],
    name: str,
    ifo_data: bytes,
    chapters: list[float],
    duration: float,
    pgc_number: int | None,
) -> Title:
    title = Title(len(titles), first_vob, name, duration)
    title.streams = _build_dvd_streams_from_ifo(ifo_data, duration, pgc_number)
    title.chapters = chapters
    title.append_clips = vob_parts[1:]
    title.dvd_pgc_number = pgc_number
    _store_dvd_ifo_metadata(title, ifo_data, pgc_number)
    return title


def _extra_subpicture_attributes(
    ifo_data: bytes, stream_id: int
) -> _IFOSubpictureAttrs | None:
    subpicture_index = stream_id - 0x20
    if not 0 <= subpicture_index < 32:
        return None
    attribute_offset = _VTS_IFO_SUBP_ATTR + subpicture_index * _VTS_IFO_SUBP_ENTRY_LEN
    if attribute_offset + _VTS_IFO_SUBP_ENTRY_LEN > len(ifo_data):
        return None
    return _IFOSubpictureAttrs.from_bytes(ifo_data, attribute_offset)


def _undeclared_subpicture_ids(
    title: Title, vob_parts: list[Path], debug: bool
) -> list[int]:
    known_sub_ids = {
        stream.sub_id for stream in title.subtitle_streams if stream.sub_id is not None
    }
    scanned_subpictures = _scan_vob_subpictures(
        vob_parts,
        max_bytes=128 * 1024 * 1024,
        debug=debug,
    )
    return sorted(
        stream_id
        for stream_id, entries in scanned_subpictures.items()
        if stream_id != 0 and stream_id not in known_sub_ids and entries
    )


def _append_undeclared_subpicture(
    title: Title,
    ifo_data: bytes,
    stream_id: int,
    stream_index: int,
    type_index: int,
    declared_count: int,
) -> None:
    language = "und"
    is_forced = False
    is_hearing_impaired = False
    is_commentary = False
    attributes = _extra_subpicture_attributes(ifo_data, stream_id)
    if attributes is not None:
        language_code = attributes.lang_code
        if len(language_code) == 2 and all(
            character.isascii() and character.isalpha() for character in language_code
        ):
            language = language_code
            is_forced = attributes.is_forced
            is_hearing_impaired = attributes.is_hearing_impaired
            is_commentary = attributes.is_commentary
            title.dvd_sub_lang[stream_id] = language
            title.dvd_subp_attrs[stream_id] = attributes
            log_debug(
                f"    Recovered language '{language}' for stream "
                f"0x{stream_id:02x} from VTS SPST attr table "
                f"(index {stream_id - 0x20}, declared count {declared_count})"
            )

    log_debug(
        f"  Detected extra subpicture stream 0x{stream_id:02x} in VOB "
        "(not declared in IFO); adding to listing"
    )
    title.streams.append(
        Stream(
            index=stream_index,
            stream_type=StreamType.SUBTITLE,
            codec="dvd_subtitle",
            language=language,
            type_index=type_index,
            sub_id=stream_id,
            is_forced=is_forced,
            is_hearing_impaired=is_hearing_impaired,
            is_commentary=is_commentary,
        )
    )


def _append_undeclared_dvd_subpictures(
    title: Title,
    ifo_data: bytes,
    vob_parts: list[Path],
    duration: float,
    config: Config | None,
) -> None:
    effective_config = config or RUNTIME_STATE.config
    if duration < effective_config.min_duration or not vob_parts:
        return

    try:
        extra_ids = _undeclared_subpicture_ids(title, vob_parts, effective_config.debug)
        base_index = max((stream.index for stream in title.streams), default=-1) + 1
        base_type_index = len(title.subtitle_streams)
        declared_count = _read_u16(ifo_data, _VTS_IFO_SUBP_COUNT)
        for offset, stream_id in enumerate(extra_ids):
            _append_undeclared_subpicture(
                title,
                ifo_data,
                stream_id,
                base_index + offset,
                base_type_index + offset,
                declared_count,
            )
    except Exception as exc:
        log_debug(
            f"Extra subpicture detection failed ({exc}); "
            "listing may undercount subtitles"
        )


def _build_title_from_ifo(
    titles: list[Title],
    first_vob: Path,
    ifo_path: Path,
    vob_parts: list[Path],
    vts: int,
    title_name: str | None = None,
    pgc_number: int | None = None,
    config: Config | None = None,
) -> Title | None:
    """Build a Title from authoritative VTS IFO data.

    Falls back to mkvmerge when the IFO cannot be read, has an invalid identity,
    has no valid PGC duration, or produces no streams. Valid short PGCs are kept;
    display filtering happens separately through the notable-title ranking.
    """
    name = title_name or f"Title {vts}"
    try:
        ifo_data = ifo_path.read_bytes()
    except OSError as exc:
        log_debug(f"IFO read failed for {ifo_path.name}: {exc}")
        return _create_title(titles, first_vob, name, config=config)

    if len(ifo_data) < 12 or ifo_data[:12] != _VTS_IFO_IDENT:
        log_debug(
            f"Invalid IFO ident in {ifo_path.name}, falling back to mkvmerge probe"
        )
        return _create_title(titles, first_vob, name, config=config)

    chapters, duration = _parse_vts_pgc_info(ifo_data, pgc_number)
    if duration <= 0:
        log_debug(f"No valid PGC duration in {ifo_path.name}, using mkvmerge probe")
        return _create_title(titles, first_vob, name, config=config)

    title = _assemble_dvd_ifo_title(
        titles,
        first_vob,
        vob_parts,
        name,
        ifo_data,
        chapters,
        duration,
        pgc_number,
    )
    if not title.streams:
        log_debug(
            f"No streams from IFO for {ifo_path.name}, falling back to mkvmerge probe"
        )
        return _create_title(titles, first_vob, name, config=config)

    _append_undeclared_dvd_subpictures(title, ifo_data, vob_parts, duration, config)
    log_debug(f"Built from IFO: {ifo_path.name}, {len(chapters)} chapters, {duration}s")
    log_debug(
        f"  {len(title.video_streams)}v {len(title.audio_streams)}a"
        f"{len(title.subtitle_streams)}s"
    )
    return title


def _store_dvd_ifo_metadata(
    title: Title, ifo_data: bytes, pgc_number: int | None = None
) -> tuple[dict[int, str], dict[int, str]]:
    audio_languages, subtitle_languages = _parse_vts_ifo_languages(ifo_data)
    pgc_audio, pgc_subtitles = _parse_pgc_stream_languages(ifo_data, pgc_number)
    audio_languages.update(pgc_audio)
    subtitle_languages.update(pgc_subtitles)
    if pgc_audio or pgc_subtitles:
        log_debug(
            f"PGC stream control languages: audio={pgc_audio} sub={pgc_subtitles}"
        )

    title.dvd_audio_lang = audio_languages
    title.dvd_sub_lang = subtitle_languages
    title.dvd_audio_attrs = _parse_vts_audio_attrs(ifo_data)
    title.dvd_video_attrs = _parse_vts_video_attrs(ifo_data)
    title.dvd_subp_attrs = _parse_vts_subp_attrs(ifo_data)
    title.dvd_ifo_data = ifo_data
    return audio_languages, subtitle_languages


def _apply_dvd_audio_attributes(
    stream: Stream, audio_attrs: dict[int, _IFOAudioAttrs]
) -> None:
    attr = audio_attrs.get(stream.sub_id) if stream.sub_id is not None else None
    if attr and attr.bits_per_sample and stream.bits_per_sample is None:
        stream.bits_per_sample = attr.bits_per_sample


def _apply_dvd_video_attributes(
    stream: Stream, video_attrs: _IFOVideoAttrs | None
) -> None:
    if video_attrs is None:
        return
    if video_attrs.resolution and not stream.width:
        stream.width, stream.height = video_attrs.resolution
    if video_attrs.aspect_ratio and not stream.sample_aspect_ratio:
        log_debug(
            f"IFO video: {video_attrs.mpeg_version} {video_attrs.standard} "
            f"{video_attrs.resolution or '?'} AR={video_attrs.aspect_ratio}"
        )


def _apply_dvd_stream_metadata(
    stream: Stream,
    title: Title,
    audio_languages: dict[int, str],
    subtitle_languages: dict[int, str],
) -> None:
    if stream.sub_id is None:
        return

    language = (
        audio_languages.get(stream.sub_id)
        if stream.stream_type == StreamType.AUDIO
        else subtitle_languages.get(stream.sub_id)
    )
    if language and language != "und":
        stream.language = language
    if stream.stream_type == StreamType.AUDIO:
        _apply_dvd_audio_attributes(stream, title.dvd_audio_attrs)
    elif stream.stream_type == StreamType.VIDEO:
        _apply_dvd_video_attributes(stream, title.dvd_video_attrs)


def _apply_dvd_pgc_info(title: Title, ifo_data: bytes, ifo_name: str) -> None:
    chapters, duration = _parse_vts_pgc_info(ifo_data)
    if chapters:
        title.chapters = chapters
        log_debug(f"{ifo_name}: {len(chapters)} chapters from PGC")
    if duration > 0:
        title.duration_seconds = duration


def _apply_dvd_ifo_languages(title: Title, ifo_path: Path) -> None:
    """Read authoritative stream languages, attributes, chapters, and runtime."""
    try:
        ifo_data = ifo_path.read_bytes()
    except OSError as exc:
        log_debug(f"IFO read failed for {ifo_path.name}: {exc}")
        return

    audio_languages, subtitle_languages = _store_dvd_ifo_metadata(title, ifo_data)
    for stream in title.streams:
        _apply_dvd_stream_metadata(stream, title, audio_languages, subtitle_languages)

    _ensure_dvd_subtitle_streams(title, subtitle_languages)
    log_debug(
        f"After _ensure_dvd_subtitle_streams: {len(title.subtitle_streams)} subtitle"
        f" streams ({[stream.sub_id for stream in title.subtitle_streams]})"
    )
    _apply_dvd_pgc_info(title, ifo_data, ifo_path.name)


def _scan_dvd_source(
    source: Path, config: Config | None = None
) -> tuple[list[Title], str | None]:
    """Scan DVD VIDEO_TS structure and return (titles, disc_name)."""
    base = source / "VIDEO_TS" if (source / "VIDEO_TS").is_dir() else source
    metadata = _read_dvd_disc_metadata(base)
    titles: list[Title] = []

    for layout in _dvd_vts_layouts(base, metadata.vts_to_title_num):
        default_title = _append_default_dvd_title(titles, layout, metadata, config)
        if default_title is None:
            continue
        ifo_bytes = _read_vts_ifo_bytes(layout)
        _append_dvd_pgc_titles(
            titles, default_title, layout, metadata, ifo_bytes, config
        )

    return titles, metadata.disc_name
