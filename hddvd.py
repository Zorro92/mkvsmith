"""
HD DVD structure parsing (XPL playlists, DISCID.DAT).

An HD DVD exposes ``HVDVD_TS/*.EVO`` (MPEG-PS-based Enhanced VOBs, readable
by mkvmerge directly) plus ``ADV_OBJ/*.XPL`` playlists — plain XML in the
DVD Forum playlist namespace. Compared to Blu-ray's MPLS/CLPI pair this is
straightforward: one XPL ``Title`` carries its clips (``.MAP`` sidecars
naming the ``.EVO``), its chapters (title-relative timestamps), and its
track list with free-text descriptions that carry the languages.

Copyright (C) 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

# Licensed under GPL-3.0-or-later

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# HD DVD playlist namespace (DVD Forum). Elements are matched by local name
# so minor schema revisions without this namespace still parse.
_XPL_TIME_RE = re.compile(r"(\d+):(\d\d):(\d\d):(\d\d)")

# Description text (e.g. "English DD+ 5.1", "Brazilian Portuguese",
# "French Forced") is the only language source in XPL. Codes are ISO 639-2.
_DESCRIPTION_LANGUAGES = (
    ("brazilian portuguese", "por"),
    ("portuguese", "por"),
    ("english", "eng"),
    ("french", "fra"),
    ("spanish", "spa"),
    ("german", "deu"),
    ("italian", "ita"),
    ("japanese", "jpn"),
    ("dutch", "nld"),
    ("russian", "rus"),
    ("chinese", "zho"),
    ("korean", "kor"),
)

_DISCID_MAGIC = b"HDDVD-V_CONF"


@dataclass
class HddvdStream:
    """One XPL-declared track (video, audio, subtitle, or PiP secondary)."""

    kind: str  # "video" | "audio" | "subtitle" | "subvideo" | "subaudio"
    track: int
    stream_number: int
    language: str = "und"
    description: str = ""
    is_forced: bool = False
    is_commentary: bool = False
    is_hearing_impaired: bool = False


@dataclass
class HddvdClip:
    """One PrimaryAudioVideoClip: an EVO file plus title-relative bounds."""

    evo_path: Path
    begin_seconds: float = 0.0
    end_seconds: float = 0.0
    streams: list[HddvdStream] = field(default_factory=list)


@dataclass
class HddvdTitle:
    number: int
    name: str
    duration_seconds: float = 0.0
    clips: list[HddvdClip] = field(default_factory=list)
    chapters: list[float] = field(default_factory=list)

    @property
    def streams(self) -> list[HddvdStream]:
        """Track list of the first clip (clips repeat the same layout)."""
        return self.clips[0].streams if self.clips else []


@dataclass
class HddvdDisc:
    titles: list[HddvdTitle] = field(default_factory=list)
    provider: str | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xpl_time(text: str | None, fps: float = 60.0) -> float:
    """Parse an XPL ``HH:MM:SS:FF`` timestamp (frames at *fps*) to seconds."""
    if not text:
        return 0.0
    match = _XPL_TIME_RE.fullmatch(text.strip())
    if not match:
        return 0.0
    hours, minutes, seconds, frames = (int(part) for part in match.groups())
    return hours * 3600.0 + minutes * 60.0 + seconds + frames / fps


def _fps_from_titleset(root: ET.Element) -> float:
    for child in root:
        if _local(child.tag) == "TitleSet":
            base = child.get("timeBase", "60fps")
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*fps", base.strip())
            if match:
                return float(match.group(1))
    return 60.0


def describe_language(description: str) -> tuple[str, bool, bool, bool]:
    """Map an XPL track description to (code, forced, commentary, sdh)."""
    lowered = description.lower()
    code = "und"
    for needle, lang in _DESCRIPTION_LANGUAGES:
        if needle in lowered:
            code = lang
            break
    return (
        code,
        "forced" in lowered,
        "commentary" in lowered,
        "sdh" in lowered or "hard of hearing" in lowered,
    )


def _parse_stream(element: ET.Element, kind: str) -> HddvdStream:
    description = element.get("description", "")
    code, forced, commentary, sdh = describe_language(description)
    try:
        track = int(element.get("track", "1"))
    except ValueError:
        track = 1
    try:
        stream_number = int(element.get("streamNumber", str(track)))
    except ValueError:
        stream_number = track
    return HddvdStream(
        kind=kind,
        track=track,
        stream_number=stream_number,
        language=code,
        description=description,
        is_forced=forced,
        is_commentary=commentary,
        is_hearing_impaired=sdh,
    )


def _clip_evo_path(hvdvd_ts: Path, src: str | None) -> Path | None:
    if not src:
        return None
    name = src.rsplit("/", 1)[-1]
    if name.lower().endswith(".map"):
        name = name[: -len(".map")] + ".evo"
    else:
        name = name + ".evo" if not name.lower().endswith(".evo") else name
    for candidate in (
        hvdvd_ts / name,
        hvdvd_ts / name.upper(),
        hvdvd_ts / name.lower(),
    ):
        if candidate.is_file():
            return candidate
    return hvdvd_ts / name


def parse_xpl(xpl_path: Path, hvdvd_ts: Path) -> HddvdDisc:
    """Parse an XPL playlist into titles (clips, chapters, track lists)."""
    disc = HddvdDisc()
    try:
        root = ET.parse(xpl_path).getroot()
    except (OSError, ET.ParseError):
        return disc
    fps = _fps_from_titleset(root)
    for element in root.iter():
        if _local(element.tag) != "Title":
            continue
        try:
            number = int(element.get("titleNumber", "0"))
        except ValueError:
            continue
        name = (
            element.get("displayName")
            or element.get("description")
            or element.get("id")
            or f"Title {number}"
        )
        title = HddvdTitle(
            number=number,
            name=name,
            duration_seconds=parse_xpl_time(element.get("titleDuration"), fps),
        )
        for child in element:
            local = _local(child.tag)
            if local == "PrimaryAudioVideoClip":
                evo = _clip_evo_path(hvdvd_ts, child.get("src"))
                if evo is None:
                    continue
                clip = HddvdClip(
                    evo_path=evo,
                    begin_seconds=parse_xpl_time(child.get("titleTimeBegin"), fps),
                    end_seconds=parse_xpl_time(child.get("titleTimeEnd"), fps),
                )
                for track_el in child:
                    kind = _local(track_el.tag).lower()
                    if kind in ("video", "audio", "subtitle", "subvideo", "subaudio"):
                        clip.streams.append(_parse_stream(track_el, kind))
                title.clips.append(clip)
            elif local == "ChapterList":
                for chapter in child:
                    if _local(chapter.tag) != "Chapter":
                        continue
                    title.chapters.append(
                        parse_xpl_time(chapter.get("titleTimeBegin"), fps)
                    )
        title.chapters.sort()
        disc.titles.append(title)
    disc.titles.sort(key=lambda t: t.number)
    return disc


def find_playlist(adv_obj: Path) -> Path | None:
    """First ``VPLST*.XPL`` playlist (``*.BAK`` backups never match)."""
    candidates = sorted(adv_obj.glob("VPLST*.XPL"))
    return candidates[0] if candidates else None


def parse_discid(discid_path: Path) -> str | None:
    """Provider string from DISCID.DAT, or None when absent/unrecognised.

    The 128-byte file starts with the ``HDDVD-V_CONF`` magic followed by
    binary data and a NUL-padded ASCII provider name (e.g. PARAMOUNT_HD-SLY).
    """
    try:
        data = discid_path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_DISCID_MAGIC):
        return None
    # The provider name is the longest printable run past the magic; shorter
    # runs are binary accidents, not names.
    tokens = [
        token.decode("ascii", "ignore").strip("\x00 ").strip()
        for token in re.findall(rb"[ -~]{4,}", data[len(_DISCID_MAGIC) :])
    ]
    names: list[str] = [text for text in tokens if text and len(text) >= 8]
    if not names:
        return None
    return max(names, key=len)
