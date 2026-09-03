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
from pathlib import Path

# IFO parsing helpers live in dvdifo.py (imported explicitly below).
import dvdifo
from dvdifo import (
    DvdIfoError,
    VmgInfo,
    _IFOSubpictureAttrs,
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
from models import CONFIG, StreamType, Stream, Title, log_debug, log_info
from probe import _probe_with_mkvmerge, _parse_mkvmerge_streams
from vobsub import _scan_vob_subpictures
from i18n import tr


def _ensure_dvd_subtitle_streams(title: "Title", sub_by_id: dict[int, str]) -> None:
    """Create VobSub stream entries from a VTS .IFO when ffprobe missed them.

    DVD subpictures are sparse picture subtitles: they only emit packets while a
    line is on screen. ffprobe probing a single VOB part therefore frequently
    detects *zero* subtitle streams even though the disc has several (the scan
    then reports ``S:0`` and they are never muxed). The VTS .IFO's
    subpicture-attribute table is authoritative, so we synthesise a
    ``dvd_subtitle`` Stream per declared stream ID.

    These synthetic streams carry their MPEG sub-stream ``id`` (0x20+) so the
    muxer can match them against what ffmpeg actually discovers in the
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
    # Remove any subtitle streams ffprobe may have found (sparse VobSub detection
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


def _build_dvd_streams_from_ifo(
    ifo_data: bytes,
    duration: float,
    pgc_number: int | None = None,
) -> list[Stream]:
    """Build Stream objects from VTS IFO data, replacing ffprobe for DVD sources.

    The IFO VTSI_MAT table contains authoritative stream attributes (codec,
    channels, language, resolution) that are more reliable than ffprobe's
    first-packet-based probing, which often misses sparse subpictures or
    misreports channel counts for certain audio configurations. Only streams
    active in the main PGC are included.

    Returns a list of Stream objects (video first, then audio, then subpics)
    with the correct sub-stream IDs (0x1E0 video, 0x80+ audio, 0x20+ subpic)
    and IFO-accurate attributes. Returns an empty list if the IFO data is
    invalid (caller should fall back to ffprobe).
    """
    if len(ifo_data) < 12 or ifo_data[:12] != _VTS_IFO_IDENT:
        return []

    streams: list[Stream] = []

    # Determine which streams are active in the main PGC (avoids including
    # audio/subpicture streams that belong to other PGCs in the same VTS).
    active_audio, active_sub = _get_active_pgc_streams(ifo_data, pgc_number)

    # --- Video stream ---
    # DVD video is always MPEG-2. Build a stream with IFO-derived attributes.
    vid_attrs = _parse_vts_video_attrs(ifo_data)
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
    if vid_attrs:
        if vid_attrs.resolution:
            video.width, video.height = vid_attrs.resolution
        # Derive sample aspect ratio from display aspect ratio.
        dar = vid_attrs.aspect_ratio
        standard = vid_attrs.standard or "NTSC"
        if dar and video.width and video.height:
            dar_map = {"4:3": 4.0 / 3.0, "16:9": 16.0 / 9.0}
            if dar in dar_map:
                sar = dar_map[dar] * video.height / video.width
                video.sample_aspect_ratio = f"{sar:.6f}"
        # Set standard colour signalling for SD MPEG-2.
        if standard == "NTSC":
            video.color_primaries = "smpte170m"
            video.color_transfer = "bt709"
            video.color_space = "smpte170m"
        else:  # PAL
            video.color_primaries = "bt470bg"
            video.color_transfer = "bt709"
            video.color_space = "bt470bg"
        video.color_range = "limited"
        video.field_order = "progressive"
    streams.append(video)

    # --- Audio streams (only those active in the main PGC) ---
    audio_lang, sub_lang = _parse_vts_ifo_languages(ifo_data)
    pgc_audio, pgc_sub = _parse_pgc_stream_languages(ifo_data, pgc_number)
    audio_lang.update(pgc_audio)
    sub_lang.update(pgc_sub)
    audio_attrs = _parse_vts_audio_attrs(ifo_data)

    # If PGC stream control returned no active streams, fall back to
    # VTS attribute table IDs (some discs don't mark all streams as
    # available in the PGC control entries).
    if active_audio:
        audio_ids = sorted(active_audio)
    else:
        audio_ids = sorted(audio_lang.keys())
        if audio_ids:
            log_debug(f"PGC active_audio empty, using VTS audio IDs: {audio_ids}")

    for i, sid in enumerate(audio_ids):
        attrs = audio_attrs.get(sid)
        codec_name = attrs.codec.lower() if attrs else "ac3"
        channels = attrs.channels if attrs else 2
        lang = audio_lang.get(sid, "und")
        audio_label = _ifo_audio_title(attrs)
        stream = Stream(
            0,
            StreamType.AUDIO,
            codec_name,
            lang,
            "",
            False,
            False,
            type_index=i,
            sub_id=sid,
        )
        stream.channels, stream.sample_rate = channels, "48000"
        if audio_label:
            stream.title = audio_label
        if attrs and attrs.bits_per_sample:
            stream.bits_per_sample = attrs.bits_per_sample
        if attrs and attrs.is_commentary:
            stream.is_commentary = True
        streams.append(stream)

    # --- Subpicture streams (only those active in the main PGC) ---
    subp_attrs = _parse_vts_subp_attrs(ifo_data)

    sub_ids: list[int] = []
    if active_sub:
        sub_ids = sorted(active_sub)
    else:
        # Fallback: some discs author subtitle streams without marking
        # them as available in the PGC stream control table.  Use the
        # VTS attribute table IDs when the PGC reports nothing.
        sub_ids = sorted(sub_lang.keys())
        if sub_ids:
            log_debug(f"PGC active_sub empty, using VTS sub IDs: {sub_ids}")

    if sub_ids:
        for i, sid in enumerate(sub_ids):
            lang = sub_lang.get(sid, "und")
            subp = subp_attrs.get(sid)
            is_forced = subp.is_forced if subp else False
            is_hi = subp.is_hearing_impaired if subp else False
            is_comm = subp.is_commentary if subp else False
            stream = Stream(
                0,
                StreamType.SUBTITLE,
                "dvd_subtitle",
                lang,
                "",
                False,
                is_forced,
                is_hi,
                is_comm,
                type_index=i,
                sub_id=sid,
            )
            streams.append(stream)

    return streams


def _create_title(
    titles: list[Title], src: Path, name: str, override_duration: float | None = None
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
    if dur < CONFIG.min_duration:
        return None
    t = Title(len(titles), src, name, dur)
    _parse_mkvmerge_streams(pd, t)
    return t


def _build_title_from_ifo(
    titles: list[Title],
    first_vob: Path,
    ifo_path: Path,
    vob_parts: list[Path],
    vts: int,
    title_name: str | None = None,
    pgc_number: int | None = None,
) -> Title | None:
    """Build a Title from VTS IFO data, skipping ffprobe for DVD sources.

    The IFO is authoritative for all stream attributes (codec, channels,
    language, resolution, chapters, duration) and avoids ffprobe's
    unreliable first-packet probing which can miss sparse DVD subpictures.

    ``title_name`` overrides the default ``f"Title {vts}"`` label.
    Falls back to ffprobe-based title creation if the IFO cannot be parsed.

    ``pgc_number`` selects a specific PGC (1-indexed, see
    ``_enumerate_vts_pgcs``) instead of the disc's default title designation
    - used to build an additional Title for an alternate seamless-branching
    edition within the same VTS.

    Note: PGC duration under ``CONFIG.min_duration`` (``--min-duration``) does
    NOT trigger a fallback — when the IFO has valid PGC data and stream
    attributes, the title is built from the IFO regardless of length. This is
    essential for discs with short easter-egg / hidden-content VTS domains
    (e.g., Freedom Downtime) whose PGC durations are perfectly valid but below
    the default 60-second threshold. The ``--show-all`` / ``_is_notable_title``
    mechanism separately controls which titles are displayed by default.
    """
    name = title_name or f"Title {vts}"
    try:
        ifo_data = ifo_path.read_bytes()
    except Exception as e:
        log_debug(f"IFO read failed for {ifo_path.name}: {e}")
        return _create_title(titles, first_vob, name)

    if len(ifo_data) < 12 or ifo_data[:12] != _VTS_IFO_IDENT:
        log_debug(
            f"Invalid IFO ident in {ifo_path.name}, falling back to mkvmerge probe"
        )
        return _create_title(titles, first_vob, name)

    # Get duration and chapters from PGC (authoritative).
    chapters, duration = _parse_vts_pgc_info(ifo_data, pgc_number)
    if duration <= 0:
        log_debug(f"No valid PGC duration in {ifo_path.name}, using mkvmerge probe")
        return _create_title(titles, first_vob, name)

    # Build stream list from IFO attributes (replaces ffprobe's parse_streams).
    streams = _build_dvd_streams_from_ifo(ifo_data, duration, pgc_number)
    if not streams:
        log_debug(
            f"No streams from IFO for {ifo_path.name}, falling back to mkvmerge probe"
        )
        return _create_title(titles, first_vob, name)

    t = Title(len(titles), first_vob, name, duration)
    t.streams = streams
    t.chapters = chapters
    t.append_clips = vob_parts[1:]
    t.dvd_pgc_number = pgc_number

    # Apply IFO language/attribute maps for the muxer.
    audio_lang, sub_lang = _parse_vts_ifo_languages(ifo_data)
    pgc_audio, pgc_sub = _parse_pgc_stream_languages(ifo_data, pgc_number)
    audio_lang.update(pgc_audio)
    sub_lang.update(pgc_sub)
    t.dvd_audio_lang = audio_lang
    t.dvd_sub_lang = sub_lang
    t.dvd_audio_attrs = _parse_vts_audio_attrs(ifo_data)
    t.dvd_video_attrs = _parse_vts_video_attrs(ifo_data)
    t.dvd_subp_attrs = _parse_vts_subp_attrs(ifo_data)
    t.dvd_ifo_data = ifo_data

    # Detect subpicture streams the IFO doesn't declare at all. This is a
    # real (if unusual) disc-authoring gap seen on some discs: a subtitle
    # language is physically present in the VOB bitstream but missing
    # entirely from the IFO's subpicture attribute table, so it can only be
    # found by scanning the actual bitstream. The rip-time VobSub fallback
    # already does a full scan and picks these up correctly, but without
    # this check the up-front title listing would undercount subtitles
    # compared to what a rip actually produces. Bounded to a modest prefix
    # and gated on notable duration so scanning many short menu/junk titles
    # on a disc doesn't get noticeably slower.
    if duration >= CONFIG.min_duration and vob_parts:
        try:
            known_sub_ids = {
                s.sub_id for s in t.subtitle_streams if s.sub_id is not None
            }
            _extra_spus = _scan_vob_subpictures(vob_parts, max_bytes=128 * 1024 * 1024)
            _extra_ids = sorted(
                sid
                for sid, entries in _extra_spus.items()
                if sid != 0 and sid not in known_sub_ids and entries
            )
            _base_index = max((s.index for s in t.streams), default=-1) + 1
            _base_type_index = len(t.subtitle_streams)
            # When a subpicture stream exists in the VOB bitstream but isn't
            # declared in the IFO's subpicture *count* field, the VTS
            # subpicture attribute table (VTS_SPST_ATRT at 0x0256) may still
            # have a valid 6-byte entry for it - a common disc-authoring gap
            # where the count is wrong but the attributes are filled in. Try
            # to recover the language from that table so the extra stream is
            # labelled correctly rather than always 'und'.
            _declared_sub = _read_u16(ifo_data, _VTS_IFO_SUBP_COUNT)
            for j, sid in enumerate(_extra_ids):
                _ext_lang = "und"
                _ext_forced = False
                _ext_hi = False
                _ext_comm = False
                _sub_idx = sid - 0x20
                if 0 <= _sub_idx < 32:
                    _attr_off = _VTS_IFO_SUBP_ATTR + _sub_idx * _VTS_IFO_SUBP_ENTRY_LEN
                    if _attr_off + _VTS_IFO_SUBP_ENTRY_LEN <= len(ifo_data):
                        _ext_attrs = _IFOSubpictureAttrs.from_bytes(ifo_data, _attr_off)
                        _lc = _ext_attrs.lang_code
                        if len(_lc) == 2 and all(
                            65 <= ord(c) <= 90 or 97 <= ord(c) <= 122 for c in _lc
                        ):
                            _ext_lang = _lc
                            _ext_forced = _ext_attrs.is_forced
                            _ext_hi = _ext_attrs.is_hearing_impaired
                            _ext_comm = _ext_attrs.is_commentary
                            sub_lang[sid] = _ext_lang
                            t.dvd_sub_lang[sid] = _ext_lang
                            t.dvd_subp_attrs[sid] = _ext_attrs
                            log_debug(
                                f"    Recovered language '{_ext_lang}' for stream "
                                f"0x{sid:02x} from VTS SPST attr table "
                                f"(index {_sub_idx}, declared count {_declared_sub})"
                            )
                log_debug(
                    f"  Detected extra subpicture stream 0x{sid:02x} in VOB "
                    "(not declared in IFO); adding to listing"
                )
                t.streams.append(
                    Stream(
                        index=_base_index + j,
                        stream_type=StreamType.SUBTITLE,
                        codec="dvd_subtitle",
                        language=_ext_lang,
                        type_index=_base_type_index + j,
                        sub_id=sid,
                        is_forced=_ext_forced,
                        is_hearing_impaired=_ext_hi,
                        is_commentary=_ext_comm,
                    )
                )
        except Exception as e:
            log_debug(
                f"Extra subpicture detection failed ({e}); "
                "listing may undercount subtitles"
            )

    log_debug(f"Built from IFO: {ifo_path.name}, {len(chapters)} chapters, {duration}s")
    log_debug(
        f"  {len(t.video_streams)}v {len(t.audio_streams)}a {len(t.subtitle_streams)}s"
    )
    return t


def _apply_dvd_ifo_languages(title: Title, ifo_path: Path) -> None:
    """Read audio/subtitle languages, PGC chapters and runtime from a VTS .IFO."""
    try:
        data = ifo_path.read_bytes()
    except Exception as e:
        log_debug(f"IFO read failed for {ifo_path.name}: {e}")
        return
    audio_by_id, sub_by_id = _parse_vts_ifo_languages(data)
    pgc_audio, pgc_sub = _parse_pgc_stream_languages(data)
    audio_by_id.update(pgc_audio)
    sub_by_id.update(pgc_sub)
    if pgc_audio or pgc_sub:
        log_debug(f"PGC stream control languages: audio={pgc_audio} sub={pgc_sub}")
    title.dvd_audio_lang = audio_by_id
    title.dvd_sub_lang = sub_by_id
    title.dvd_audio_attrs = _parse_vts_audio_attrs(data)
    title.dvd_video_attrs = _parse_vts_video_attrs(data)
    title.dvd_subp_attrs = _parse_vts_subp_attrs(data)
    title.dvd_ifo_data = data
    for s in title.streams:
        if s.sub_id is None:
            continue
        lang = (
            audio_by_id.get(s.sub_id)
            if s.stream_type == StreamType.AUDIO
            else sub_by_id.get(s.sub_id)
        )
        if lang and lang != "und":
            s.language = lang
        if s.stream_type == StreamType.AUDIO and title.dvd_audio_attrs:
            attr = title.dvd_audio_attrs.get(s.sub_id)
            if attr and attr.bits_per_sample and s.bits_per_sample is None:
                s.bits_per_sample = attr.bits_per_sample
        if s.stream_type == StreamType.VIDEO and title.dvd_video_attrs:
            va = title.dvd_video_attrs
            if va.resolution and not s.width:
                w, h = va.resolution
                s.width, s.height = w, h
            if va.aspect_ratio and not s.sample_aspect_ratio:
                log_debug(
                    f"IFO video: {va.mpeg_version} {va.standard} "
                    f"{va.resolution or '?'} AR={va.aspect_ratio}"
                )
    _ensure_dvd_subtitle_streams(title, sub_by_id)
    log_debug(
        f"After _ensure_dvd_subtitle_streams: {len(title.subtitle_streams)} subtitle"
        f" streams ({[s.sub_id for s in title.subtitle_streams]})"
    )
    chapters, duration = _parse_vts_pgc_info(data)
    if chapters:
        title.chapters = chapters
        log_debug(f"{ifo_path.name}: {len(chapters)} chapters from PGC")
    if duration > 0:
        title.duration_seconds = duration


def _scan_dvd_source(source: Path) -> tuple[list[Title], str | None]:
    """Scan DVD VIDEO_TS structure and return (titles, disc_name)."""
    base = source / "VIDEO_TS" if (source / "VIDEO_TS").is_dir() else source
    titles: list[Title] = []
    disc_name: str | None = None

    # Pre-scan VMG IFO (VIDEO_TS.IFO) for disc name, barcode, and title metadata.
    vmg_path = base / "VIDEO_TS.IFO"
    vmg_info: VmgInfo | None = None
    disc_barcode: str | None = None
    if vmg_path.exists():
        try:
            vmg_info = _parse_vmg_ifo(vmg_path)
        except DvdIfoError as exc:
            log_debug(f"VMG IFO parse failed: {exc}")
            vmg_info = None
        if vmg_info:
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

    # Build reverse lookup: VTS number -> first logical title number
    vts_to_title_num: dict[int, int] = {}
    if vmg_info:
        title_map = vmg_info.get("title_map")
        if title_map:
            for title_idx, (vts_num, _ttl_num) in title_map.items():
                if vts_num not in vts_to_title_num:
                    vts_to_title_num[vts_num] = title_idx

    for ifo in sorted(base.glob("VTS_*_0.IFO")):
        m = re.search(r"VTS_(\d+)_0\.IFO", ifo.name)
        if not m or int(m.group(1)) == 0:
            continue
        vts = int(m.group(1))
        first_vob = base / f"VTS_{vts:02d}_1.VOB"
        if not first_vob.exists():
            continue

        def _vob_sort_key(p: Path) -> int:
            vm = re.search(r"_(\d+)\.VOB$", p.name)
            return int(vm.group(1)) if vm else 0

        vob_parts = sorted(
            base.glob(f"VTS_{vts:02d}_[1-9].VOB"),
            key=_vob_sort_key,
        )
        logical_title = vts_to_title_num.get(vts)
        title_name = f"Title {vts}"
        if logical_title:
            title_name = f"Title {logical_title} (VTS {vts})"
            log_debug(f"TT_SRPT: VTS {vts} -> DVD Title {logical_title}")
        t = _build_title_from_ifo(
            titles, first_vob, ifo, vob_parts, vts, title_name=title_name
        )
        if t is None:
            if t := _create_title(titles, first_vob, title_name):
                t.append_clips = vob_parts[1:]
                t.disc_name = disc_name
                t.disc_barcode = disc_barcode
                _apply_dvd_ifo_languages(t, ifo)
                titles.append(t)
            continue
        t.disc_name = disc_name
        t.disc_barcode = disc_barcode
        titles.append(t)

        # Seamless-branching discs can hold multiple substantial PGCs within
        # the same VTS (e.g. a theatrical cut plus one or more longer bonus/
        # extended cuts sharing footage via interleaved cells). Expose each
        # as its own separate, independently rippable title alongside the
        # default one, matching how MakeMKV lists each edition separately.
        try:
            ifo_bytes = ifo.read_bytes()
        except Exception as e:
            log_debug(f"Alternate-edition PGC scan skipped for {ifo.name}: {e}")
            ifo_bytes = b""

        # --- TV-series episode detection ---
        # On TV-series discs, multiple PGCs of similar duration are separate
        # episodes (each with its own distinct cell table), not alternate cuts
        # of the same movie. Detect this first; fall back to the
        # seamless-branching heuristic only when no episode pattern is found.
        episode_pgcs: list[int] = []
        play_all_pgc: int | None = None
        if ifo_bytes:
            episode_pgcs, play_all_pgc = _detect_episode_pgcs(
                ifo_bytes,
                CONFIG.min_duration,
            )
        default_pgc_num = _default_pgc_number(ifo_bytes) if ifo_bytes else None

        if episode_pgcs and len(episode_pgcs) >= 2:
            # --- Episode mode ---
            ep_set = set(episode_pgcs)

            # The default title (already built as ``t``) may be one of the
            # episodes, the play-all chain, or (rarely) an unrelated PGC.
            if default_pgc_num in ep_set:
                t.dvd_episode_number = episode_pgcs.index(default_pgc_num) + 1
            elif play_all_pgc is not None and default_pgc_num == play_all_pgc:
                t.dvd_play_all = True

            for ep_idx, pgc_num in enumerate(episode_pgcs, start=1):
                if pgc_num == default_pgc_num:
                    log_debug(
                        f"  Episode {ep_idx}: PGC {pgc_num} "
                        f"({t.duration_seconds:.0f}s) [default title]"
                    )
                    continue
                t_ep = _build_title_from_ifo(
                    titles,
                    first_vob,
                    ifo,
                    vob_parts,
                    vts,
                    title_name=f"{title_name} - Episode {ep_idx}",
                    pgc_number=pgc_num,
                )
                if t_ep is None:
                    continue
                t_ep.disc_name = disc_name
                t_ep.disc_barcode = disc_barcode
                t_ep.dvd_episode_number = ep_idx
                titles.append(t_ep)
                log_debug(
                    f"  Episode {ep_idx}: PGC {pgc_num} ({t_ep.duration_seconds:.0f}s)"
                )

            # Build the "play all" chain (if any) as a demoted title so it
            # stays available but doesn't masquerade as an episode.
            if play_all_pgc is not None and play_all_pgc != default_pgc_num:
                t_pa = _build_title_from_ifo(
                    titles,
                    first_vob,
                    ifo,
                    vob_parts,
                    vts,
                    title_name=f"{title_name} - Play All",
                    pgc_number=play_all_pgc,
                )
                if t_pa is not None:
                    t_pa.disc_name = disc_name
                    t_pa.disc_barcode = disc_barcode
                    t_pa.dvd_play_all = True
                    titles.append(t_pa)
                    log_debug(
                        f"  Play all: PGC {play_all_pgc} ({t_pa.duration_seconds:.0f}s)"
                    )

            # Build remaining substantial PGCs (extras) as regular titles.
            for e_num, _e_abs, e_dur, _e_cells in dvdifo._enumerate_vts_pgcs(ifo_bytes):
                if e_num in ep_set or e_num == play_all_pgc:
                    continue
                if e_num == default_pgc_num:
                    continue
                if e_dur < CONFIG.min_duration:
                    continue
                t_extra = _build_title_from_ifo(
                    titles,
                    first_vob,
                    ifo,
                    vob_parts,
                    vts,
                    title_name=f"{title_name} - Extra",
                    pgc_number=e_num,
                )
                if t_extra is None:
                    continue
                t_extra.disc_name = disc_name
                t_extra.disc_barcode = disc_barcode
                titles.append(t_extra)
                log_debug(f"  Extra: PGC {e_num} ({t_extra.duration_seconds:.0f}s)")

            log_info(
                tr("Detected {n} episode(s) in VTS {vts}", n=len(episode_pgcs), vts=vts)
                + (
                    tr(" (play-all PGC {pgc})", pgc=play_all_pgc)
                    if play_all_pgc
                    else ""
                )
            )
        else:
            # --- Seamless-branching edition fallback ---
            extra_pgcs = (
                _find_alternate_edition_pgcs(ifo_bytes, CONFIG.min_duration)
                if ifo_bytes
                else []
            )
            for edition_num, pgc_num in enumerate(extra_pgcs, start=2):
                edition_name = f"{title_name} - Edition {edition_num}"
                t_alt = _build_title_from_ifo(
                    titles,
                    first_vob,
                    ifo,
                    vob_parts,
                    vts,
                    title_name=edition_name,
                    pgc_number=pgc_num,
                )
                if t_alt is None:
                    continue
                t_alt.disc_name = disc_name
                t_alt.disc_barcode = disc_barcode
                t_alt.dvd_edition_label = f"Edition {edition_num}"
                titles.append(t_alt)
                log_debug(
                    f"  Alternate edition: PGC {pgc_num} "
                    f"({t_alt.duration_seconds:.0f}s) exposed as '{edition_name}'"
                )

    return titles, disc_name
