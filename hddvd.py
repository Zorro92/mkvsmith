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
# "French Forced") is the fallback language source in XPL. Codes are ISO 639-2.
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

# ISO 639-1 -> 639-2 for TrackNavigationList langcodes ("en:01") and the
# TitleSet defaultLanguage. Only common DVD languages; unknown codes fall
# back to description parsing.
_LANG_2_TO_3 = {
    "en": "eng",
    "fr": "fra",
    "es": "spa",
    "de": "deu",
    "it": "ita",
    "pt": "por",
    "nl": "nld",
    "ja": "jpn",
    "ru": "rus",
    "zh": "zho",
    "ko": "kor",
    "sv": "swe",
    "da": "dan",
    "fi": "fin",
    "no": "nor",
    "pl": "pol",
    "cs": "ces",
    "hu": "hun",
    "el": "ell",
    "tr": "tur",
    "ar": "ara",
    "he": "heb",
    "hi": "hin",
    "th": "tha",
}

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
    # XPL id attribute ("MainMovie", "Trailer3") — the authorial token,
    # distinct from the human displayName.
    id: str = ""
    # Authoritative per-track languages from TrackNavigationList langcodes
    # ("en:01"), keyed by XPL track number. Missing entries fall back to
    # description parsing, then the disc default language (video only).
    audio_nav_langs: dict[int, str] = field(default_factory=dict)
    subtitle_nav_langs: dict[int, str] = field(default_factory=dict)

    @property
    def streams(self) -> list[HddvdStream]:
        """Track list of the first clip (clips repeat the same layout)."""
        return self.clips[0].streams if self.clips else []


@dataclass
class HddvdDisc:
    titles: list[HddvdTitle] = field(default_factory=list)
    provider: str | None = None
    default_language: str = "und"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _display_name(element: ET.Element, number: int) -> str:
    """Human title from displayName/description/id, with a kind suffix.

    Suffixes (currently just Trailer) are appended only when the name does
    not already contain them: "IronMan" becomes "IronMan (Trailer)" while
    "Teaser Trailer" is untouched. This intentionally diverges from the
    reference ripper's bare names; the XPL id is the only place the kind
    is recorded.
    """
    name = (
        element.get("displayName")
        or element.get("description")
        or element.get("id")
        or f"Title {number}"
    )
    lowered = name.lower()
    id_lower = (element.get("id") or "").lower()
    if "trailer" in id_lower and "trailer" not in lowered:
        name = f"{name} (Trailer)"
    return name


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


def _default_language_from_titleset(root: ET.Element) -> str:
    """TitleSet defaultLanguage ("en") as ISO 639-2, else ``und``."""
    for child in root:
        if _local(child.tag) == "TitleSet":
            return _LANG_2_TO_3.get((child.get("defaultLanguage") or "").lower(), "und")
    return "und"


def _nav_langs(title_el: ET.Element) -> tuple[dict[int, str], dict[int, str]]:
    """TrackNavigationList langcodes per XPL track number (audio, subtitle)."""
    audio: dict[int, str] = {}
    subs: dict[int, str] = {}
    for nav in title_el.iter():
        if _local(nav.tag) != "TrackNavigationList":
            continue
        for entry in nav:
            kind = _local(entry.tag)
            try:
                track = int(entry.get("track", "0"))
            except ValueError:
                continue
            code = (entry.get("langcode") or "").split(":")[0].lower()
            lang = _LANG_2_TO_3.get(code)
            if not lang:
                continue
            if kind == "AudioTrack":
                audio[track] = lang
            elif kind == "SubtitleTrack":
                subs[track] = lang
    return audio, subs


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


def _parse_stream(
    element: ET.Element,
    kind: str,
    nav_lang: str | None = None,
    default: str = "und",
) -> HddvdStream:
    description = element.get("description", "")
    desc_code, forced, commentary, sdh = describe_language(description)
    # TrackNavigationList langcodes are authoritative; description text is
    # the fallback; video falls back to the disc default language.
    code = nav_lang or desc_code
    if code == "und" and kind == "video":
        code = default
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
    # NOTE: probing lies on case-insensitive filesystems (Windows,
    # macOS) — every variant "exists". Match the directory listing so
    # the returned path carries the on-disc casing on every platform.
    try:
        entries = {
            entry.name.upper(): entry for entry in hvdvd_ts.iterdir() if entry.is_file()
        }
    except OSError:
        return hvdvd_ts / name
    return entries.get(name.upper(), hvdvd_ts / name)


def parse_xpl(xpl_path: Path, hvdvd_ts: Path) -> HddvdDisc:
    """Parse an XPL playlist into titles (clips, chapters, track lists)."""
    disc = HddvdDisc()
    try:
        root = ET.parse(xpl_path).getroot()
    except (OSError, ET.ParseError):
        return disc
    fps = _fps_from_titleset(root)
    disc.default_language = _default_language_from_titleset(root)
    for element in root.iter():
        if _local(element.tag) != "Title":
            continue
        try:
            number = int(element.get("titleNumber", "0"))
        except ValueError:
            continue
        name = _display_name(element, number)
        title = HddvdTitle(
            number=number,
            name=name,
            duration_seconds=parse_xpl_time(element.get("titleDuration"), fps),
            id=element.get("id") or "",
        )
        audio_nav, sub_nav = _nav_langs(element)
        title.audio_nav_langs = audio_nav
        title.subtitle_nav_langs = sub_nav
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
                        try:
                            track_no = int(track_el.get("track", "1"))
                        except ValueError:
                            track_no = 1
                        if kind == "audio":
                            nav_lang: str | None = audio_nav.get(track_no)
                        elif kind == "subtitle":
                            nav_lang = sub_nav.get(track_no)
                        else:
                            nav_lang = None
                        clip.streams.append(
                            _parse_stream(
                                track_el, kind, nav_lang, disc.default_language
                            )
                        )
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
