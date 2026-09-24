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
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    _compute_dvd_disc_id,
    _compute_libdvdread_disc_id,
    _effective_pgc_durations,
    _episode_part_labels,
    _EPISODE_DURATION_TOL,
    _EPISODE_DWARF_RATIO,
)
from models import (
    Config,
    DiscMetadata,
    RUNTIME_STATE,
    StreamType,
    Stream,
    Title,
    log_debug,
    log_info,
)
from cc608 import (
    CC608_CODEC_ASS,
    CC608_CODEC_SRT,
    _has_cc608_data,
)
from probe import _probe_with_mkvmerge, _parse_mkvmerge_streams
from vobsub import _scan_vob_subpictures
from i18n import tr


@dataclass(frozen=True)
class _DvdDiscMetadata:
    disc: DiscMetadata
    vts_to_title_num: dict[int, int]


@dataclass(frozen=True)
class _DvdVtsLayout:
    vts: int
    ifo_path: Path
    first_vob: Path
    vob_parts: list[Path]
    title_name: str
    logical_title: int | None


def _read_dvd_disc_metadata(base: Path) -> _DvdDiscMetadata:
    """Read VMG metadata and map each VTS to its first logical DVD title."""
    vmg_path = base / "VIDEO_TS.IFO"
    vmg_info: VmgInfo | None = None
    disc = DiscMetadata()
    if not vmg_path.exists():
        return _DvdDiscMetadata(disc, {})

    try:
        vmg_info = _parse_vmg_ifo(vmg_path)
    except DvdIfoError as exc:
        log_debug(f"VMG IFO parse failed: {exc}")
        vmg_info = None

    vts_to_title_num: dict[int, int] = {}
    if not vmg_info:
        return _DvdDiscMetadata(disc, vts_to_title_num)

    vmg_disc_name = vmg_info.get("disc_name")
    if vmg_disc_name:
        log_info(tr("VMG disc name: {name}", name=vmg_disc_name))
    vmg_barcode = vmg_info.get("barcode")
    if vmg_barcode:
        log_debug(f"VMG UPC/EAN: {vmg_barcode}")
    vmg_provider = vmg_info.get("provider_id", "")
    if vmg_provider:
        log_debug(f"VMG Provider ID: {vmg_provider}")
    disc = DiscMetadata(
        name=vmg_disc_name,
        upc_ean=vmg_barcode,
        provider_id=vmg_provider or None,
    )

    title_map = vmg_info.get("title_map")
    if title_map:
        for title_idx, (vts_num, _ttl_num) in title_map.items():
            if vts_num not in vts_to_title_num:
                vts_to_title_num[vts_num] = title_idx

    try:
        disc = replace(disc, dvd_disc_id=_compute_dvd_disc_id(base))
    except (OSError, FileNotFoundError) as exc:
        log_debug(f"DVD disc ID unavailable: {exc}")
    try:
        disc = replace(disc, libdvdread_disc_id=_compute_libdvdread_disc_id(base))
    except (OSError, ValueError, FileNotFoundError) as exc:
        log_debug(f"libdvdread DVD Disc ID unavailable: {exc}")
    return _DvdDiscMetadata(disc, vts_to_title_num)


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
        layouts.append(
            _DvdVtsLayout(
                vts,
                ifo_path,
                first_vob,
                vob_parts,
                title_name,
                logical_title,
            )
        )
    return layouts


def _apply_dvd_disc_metadata(title: Title, metadata: _DvdDiscMetadata) -> None:
    title.disc_name = metadata.disc.name


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
    title.dvd_title_id = layout.logical_title
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
    title.dvd_title_id = layout.logical_title
    return title


@dataclass(frozen=True)
class _DvdPgcPlan:
    """Per-VTS classification of which substantial PGCs to expose as titles.

    Produced by ``_plan_dvd_pgc_titles`` — the single decision tree shared by
    every DVD source mode (extracted ``VIDEO_TS`` folders and ISO images
    alike), so both produce identical title layouts for identical content.
    """

    # 1-indexed PGC numbers of detected TV episodes. Empty for non-episodic
    # VTSs; two or more entries mean the VTS is treated as an episode group.
    episode_pgcs: list[int]
    # PGC number of the "play all" chain (duration ~= sum of episodes).
    play_all_pgc: int | None
    # PGC number used by the disc's default title (VTS_TTN 1 designation).
    default_pgc_num: int | None
    # Episode groups only: substantial PGCs outside the episode / play-all /
    # default set, exposed as "Extra" titles.
    extras: list[int]
    # Non-episode VTSs only: substantial PGCs other than the default title's,
    # as ``(pgc_number, is_edition)`` — editions re-cut the default title's
    # footage, plain PGCs are unrelated programs (e.g. bonus features).
    editions: list[tuple[int, bool]]
    # Episode groups whose episodes are authored in two alternating parts
    # (Superman 1988: long PGC + short PGC per episode): PGC number to
    # ``(episode_number, part)``. None when episodes are single programs.
    episode_parts: dict[int, tuple[int, str]] | None = None


def _episode_part_map(
    ifo_bytes: bytes, episode_pgcs: list[int], min_duration: float
) -> dict[int, tuple[int, str]] | None:
    """Derive a/b part labels for split-episode PGCs (see dvdifo)."""
    all_pgcs = _effective_pgc_durations(
        ifo_bytes, dvdifo._enumerate_vts_pgcs(ifo_bytes)
    )
    by_number = {pgc[0]: pgc for pgc in all_pgcs}
    members = [by_number[num] for num in episode_pgcs if num in by_number]
    return _episode_part_labels(members) if len(members) == len(episode_pgcs) else None


def _plan_dvd_pgc_titles(ifo_bytes: bytes, config: Config | None = None) -> _DvdPgcPlan:
    """Classify a VTS's substantial PGCs into the titles to expose.

    TV-episode VTSs (two or more similar-duration PGCs with distinct cell
    tables, see ``_detect_episode_pgcs``) expose one title per episode plus
    an optional "Play All" chain and "Extra" titles; every other VTS exposes
    alternate editions and plain PGCs (see ``_find_alternate_edition_pgcs``).
    Returns an empty plan for a missing or unparseable IFO.
    """
    if not ifo_bytes:
        return _DvdPgcPlan([], None, None, [], [])
    minimum_duration = (config or RUNTIME_STATE.config).min_duration

    episode_pgcs, play_all_pgc = _detect_episode_pgcs(ifo_bytes, minimum_duration)
    default_pgc_num = _default_pgc_number(ifo_bytes)
    if len(episode_pgcs) >= 2:
        episode_set = set(episode_pgcs)
        episode_parts = _episode_part_map(ifo_bytes, episode_pgcs, minimum_duration)
        extras = [
            num
            for num, _pgc_abs, duration, _cells in dvdifo._effective_pgc_durations(
                ifo_bytes, dvdifo._enumerate_vts_pgcs(ifo_bytes)
            )
            if num not in episode_set
            and num != play_all_pgc
            and num != default_pgc_num
            and duration >= minimum_duration
        ]
        return _DvdPgcPlan(
            episode_pgcs, play_all_pgc, default_pgc_num, extras, [], episode_parts
        )

    editions = _find_alternate_edition_pgcs(ifo_bytes, minimum_duration)
    return _DvdPgcPlan([], None, default_pgc_num, [], editions)


def _classify_default_episode_title(default_title: Title, plan: _DvdPgcPlan) -> None:
    if plan.default_pgc_num is not None and plan.default_pgc_num in set(
        plan.episode_pgcs
    ):
        default_title.dvd_episode_number = (
            plan.episode_pgcs.index(plan.default_pgc_num) + 1
        )
        if plan.episode_parts is not None:
            part = plan.episode_parts.get(plan.default_pgc_num)
            if part is not None:
                default_title.dvd_episode_number, default_title.dvd_episode_part = part
    elif plan.play_all_pgc is not None and plan.default_pgc_num == plan.play_all_pgc:
        default_title.dvd_play_all = True


def _demote_dwarfed_episode_groups(
    titles: list[Title], config: Config | None = None
) -> None:
    """Strip within-VTS episode labels when the disc's content dwarfs them.

    Episode groups are detected per VTS, where a movie in another VTS
    cannot protect the heuristic: Treasure Planet's VTS 11 holds seven
    ~2-minute featurettes plus their 730s compilation — structurally an
    anthology, exactly like Tex Avery's cartoon VTS — but the disc is a
    95-minute movie and the featurettes are bonus content. The disc-level
    rule is the one the groups themselves use: nothing on the disc (outside
    play-all chains) may run ``_EPISODE_DWARF_RATIO`` times the episodes'
    longest member. Series discs pass this (Sonic, Superman, Peanuts,
    Tex Avery: their longest non-play-all titles are episodes or extras
    shorter than the episodes).
    """
    episode_titles = [
        title
        for title in titles
        if title.dvd_episode_number is not None and not title.dvd_play_all
    ]
    if not episode_titles:
        return
    group_max = max(title.duration_seconds for title in episode_titles)
    for title in titles:
        if (
            title.dvd_episode_number is not None
            or title.dvd_play_all
            or title.duration_seconds < group_max * _EPISODE_DWARF_RATIO
        ):
            continue
        for episode in episode_titles:
            episode.dvd_episode_number = None
            episode.dvd_episode_part = None
        log_info(
            tr(
                "Dropping {n} episode label(s): dwarfed by a "
                "{dur} title — bonus content on a movie disc",
                n=len(episode_titles),
                dur=title.duration_display,
            )
        )
        return


def _label_cross_vts_episodes(
    titles: list[Title], config: Config | None = None
) -> None:
    """Label one-episode-per-VTS series titles as episodes.

    Some TV-series authoring puts each episode in its own video title set —
    Tales from the Cryptkeeper S1 holds seven VTSs of ~21-minute episodes —
    which the within-VTS PGC clustering can never see: each VTS has a single
    substantial PGC. This pass clusters the disc's substantial DVD titles by
    duration (the same tolerance the PGC detection uses) and labels the
    cluster's titles ``Episode 1..N`` in DVD title order.

    At least three distinct titles are required: two similar-duration titles
    are the classic widescreen/fullscreen pair of one movie, while a series
    is three or more. Titles already episode-labelled by the within-VTS
    detection, play-all chains, and alternate editions never join a cluster.
    """
    minimum_duration = (config or RUNTIME_STATE.config).min_duration
    candidates: dict[int, Title] = {}
    for title in titles:
        if (
            title.dvd_title_id is None
            or title.dvd_episode_number is not None
            or title.dvd_play_all
            or title.dvd_edition_label is not None
            or title.duration_seconds < minimum_duration
        ):
            continue
        # One title per DVD title number: the default title represents its
        # VTS (editions of the same movie are excluded above).
        candidates.setdefault(title.dvd_title_id, title)
    if len(candidates) < 3:
        return

    cluster_values = list(candidates.values())
    best_group: list[Title] = []
    for title in cluster_values:
        duration = title.duration_seconds
        neighbours = [
            candidate
            for candidate in cluster_values
            if abs(candidate.duration_seconds - duration)
            / max(candidate.duration_seconds, duration, 1.0)
            <= _EPISODE_DURATION_TOL
        ]
        if len(neighbours) > len(best_group):
            best_group = neighbours
    if len(best_group) < 3:
        return

    # Episodes are the disc's content: no substantial title may dwarf the
    # cluster (the same principle as the within-VTS check). Peanuts' Emmy
    # disc carries three ~100s intro clips in their own VTSs beside
    # 24-minute specials — a cluster, but extras, not episodes.
    group_max = max(title.duration_seconds for title in best_group)
    group_ids = {title.dvd_title_id for title in best_group}
    for title in titles:
        if (
            title.dvd_title_id in group_ids
            or title.dvd_play_all
            or title.duration_seconds < minimum_duration
        ):
            continue
        if title.duration_seconds >= group_max * _EPISODE_DWARF_RATIO:
            return

    for episode_index, title in enumerate(
        sorted(best_group, key=lambda t: t.dvd_title_id or 0), start=1
    ):
        title.dvd_episode_number = episode_index
    log_info(
        tr(
            "Detected {n} episode(s) across {m} title(s)",
            n=len(best_group),
            m=len(cluster_values),
        )
    )


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
    plan: _DvdPgcPlan,
    default_title: Title,
    title_name: str,
    vts: int,
    build_title: Callable[[int, str], Title | None],
) -> None:
    """Append one title per episode PGC, plus play-all and extra titles.

    Shared driver for every DVD source mode: ``build_title`` builds a title
    for a PGC under its source mode's own bookkeeping (its arguments are the
    1-indexed PGC number and the final title name) and must not append it;
    this driver owns appending, numbering, flags, and logging.
    """
    _classify_default_episode_title(default_title, plan)
    for episode_index, pgc_num in enumerate(plan.episode_pgcs, start=1):
        # Split-episode discs (Superman 1988) pair two PGCs per episode:
        # the part map renumbers them "1a"/"1b", "2a"/"2b", ... in disc
        # order; single-program episodes number sequentially.
        part = plan.episode_parts.get(pgc_num) if plan.episode_parts else None
        if part is not None:
            part_number, part_letter = part
            label = f"Episode {part_number}{part_letter}"
        else:
            part_number, part_letter = episode_index, None
            label = f"Episode {episode_index}"
        if pgc_num == plan.default_pgc_num:
            log_debug(
                f"  {label}: PGC {pgc_num} "
                f"({default_title.duration_seconds:.0f}s) [default title]"
            )
            continue

        title = build_title(pgc_num, f"{title_name} - {label}")
        if title is None:
            continue
        title.dvd_episode_number = part_number
        title.dvd_episode_part = part_letter
        titles.append(title)
        log_debug(f"  {label}: PGC {pgc_num} ({title.duration_seconds:.0f}s)")

    if plan.play_all_pgc is not None and plan.play_all_pgc != plan.default_pgc_num:
        title = build_title(plan.play_all_pgc, f"{title_name} - Play All")
        if title is not None:
            title.dvd_play_all = True
            titles.append(title)
            log_debug(
                f"  Play all: PGC {plan.play_all_pgc} ({title.duration_seconds:.0f}s)"
            )

    for pgc_num in plan.extras:
        title = build_title(pgc_num, f"{title_name} - Extra")
        if title is None:
            continue
        titles.append(title)
        log_debug(f"  Extra: PGC {pgc_num} ({title.duration_seconds:.0f}s)")

    _log_dvd_episode_group(len(plan.episode_pgcs), vts, plan.play_all_pgc)


def _append_dvd_alternate_editions(
    titles: list[Title],
    title_name: str,
    plan: _DvdPgcPlan,
    build_title: Callable[[int, str], Title | None],
) -> None:
    """Append titles for a non-episodic VTS's alternate PGCs.

    Shared driver for every DVD source mode (see
    ``_append_dvd_episode_titles``): genuine re-cuts of the default title are
    labelled "Edition N" (numbered by editions only); unrelated substantial
    PGCs such as bonus features sharing the VTS get a neutral "PGC N" label.
    """
    edition_offset = 2
    for pgc_num, is_edition in plan.editions:
        label = f"Edition {edition_offset}" if is_edition else f"PGC {pgc_num}"
        title = build_title(pgc_num, f"{title_name} - {label}")
        if title is None:
            continue
        title.dvd_edition_label = label
        titles.append(title)
        if is_edition:
            edition_offset += 1
        log_debug(
            f"  {'Alternate edition' if is_edition else 'Additional PGC'}: "
            f"PGC {pgc_num} ({title.duration_seconds:.0f}s) "
            f"exposed as '{title.name}'"
        )


def _append_dvd_pgc_titles(
    titles: list[Title],
    default_title: Title,
    layout: _DvdVtsLayout,
    metadata: _DvdDiscMetadata,
    ifo_bytes: bytes,
    config: Config | None = None,
) -> None:
    # One shared classification drives every DVD source mode (extracted
    # VIDEO_TS folders and ISO images alike), so both label identical
    # content identically; see `_plan_dvd_pgc_titles`.
    plan = _plan_dvd_pgc_titles(ifo_bytes, config)
    if not (plan.episode_pgcs or plan.editions):
        return

    def build_title(pgc_number: int, name: str) -> Title | None:
        return _build_dvd_pgc_title(
            titles, layout, metadata, pgc_number, name, config=config
        )

    if plan.episode_pgcs:
        _append_dvd_episode_titles(
            titles, plan, default_title, layout.title_name, layout.vts, build_title
        )
    else:
        _append_dvd_alternate_editions(titles, layout.title_name, plan, build_title)


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
    # The VTS attribute-table set (via the merged language map) is
    # authoritative, matching MakeMKV: a PGC's stream-control table may mark
    # streams unavailable for its presentation (commentary tracks enabled
    # only in an alternate PGC), but the streams exist in the VOBs and
    # rippers list them. The PGC active set is only a fallback for discs
    # whose attribute tables yield no stream IDs at all.
    audio_ids = sorted(audio_languages)
    if not audio_ids:
        audio_ids = sorted(active_audio)
        if audio_ids:
            log_debug(f"VTS audio IDs empty, using PGC active IDs: {audio_ids}")

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
    # Attribute-table set first, PGC active set as fallback — same MakeMKV
    # rationale as the audio path above.
    subtitle_ids = sorted(subtitle_languages)
    if not subtitle_ids:
        subtitle_ids = sorted(active_subtitles)
        if subtitle_ids:
            log_debug(f"VTS sub IDs empty, using PGC active IDs: {subtitle_ids}")

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

    Returns video, audio, and subpicture streams in mux order. The VTS
    attribute-table stream set is authoritative — matching MakeMKV, which
    lists every stream a VTS declares even when a PGC's stream-control
    table marks it unavailable (e.g. Treasure Planet's 2002 R1 DVD9: the
    commentary track is disabled in the default movie PGC and enabled only
    in its alternate, yet MakeMKV lists all four audio streams for both).
    PGC control entries supply per-stream language overrides; the PGC
    active set is only a fallback when the attribute table yields nothing.
    An invalid IFO returns an empty list so callers can probe instead.
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


def _append_dvd_closed_captions(
    title: Title, vob_parts: list[Path], config: Config | None = None
) -> None:
    """Declare a text closed-caption track when the disc carries Line 21 CC.

    The VTS IFO's video attributes flag Line 21 field 1/2 support, but some
    discs declare the flag without carrying caption user data, so the first
    VOB part is scanned for DVD-CC GOP user data before the track is
    listed. The track is realised at mux time as an extracted text sidecar
    (mkv.py ``_extract_dvd_cc608_sidecar``): SRT (plain text) or ASS
    (preserves the CC grid's speaker positioning and italics), selected by
    ``Config.cc608_format``. Opt-in via ``Config.extract_cc608``
    (``--cc-srt``) — the captions usually duplicate the VobSub tracks.
    """
    effective_config = config or RUNTIME_STATE.config
    if not effective_config.extract_cc608:
        return
    attrs = title.dvd_video_attrs
    if attrs is None or not (attrs.cc_field_1 or attrs.cc_field_2):
        return
    if not vob_parts:
        return
    if not _has_cc608_data(vob_parts[0]):
        log_debug("IFO declares Line 21 captions; no CC user data found in VOB")
        return
    language = next(
        (stream.language for stream in title.audio_streams if stream.language != "und"),
        "und",
    )
    codec = (
        CC608_CODEC_ASS if effective_config.cc608_format == "ass" else CC608_CODEC_SRT
    )
    stream = Stream(
        index=len(title.streams),
        stream_type=StreamType.SUBTITLE,
        codec=codec,
        language=language,
        is_hearing_impaired=True,
        type_index=len(title.subtitle_streams),
    )
    stream.title = "Closed Captions"
    title.streams.append(stream)
    log_debug(
        "Detected EIA-608 closed captions in video user data; "
        f"adding CC track ({effective_config.cc608_format})"
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
    _append_dvd_closed_captions(title, vob_parts, config)
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
) -> tuple[list[Title], DiscMetadata]:
    """Scan DVD VIDEO_TS structure and return (titles, disc metadata)."""
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

    return titles, metadata.disc
