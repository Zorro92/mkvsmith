"""
Blu-ray (BDMV) binary metadata parsing.

Extracted from main.py: pure-Python parsers for Blu-ray disc structures —
CLPI clip-info stream attributes, MPLS playlists (STN tables, playitems,
SubPaths, chapter marks), and BDMV bdmt_*.xml metadata (disc name, catalog
number). Imports the Stream / Title / StreamType model classes from
models.py; there is no dependency back on main.py.

Contains portions inspired by:
- pyparsebluray (MIT) https://github.com/Ichunjo/pyparsebluray (STN table parsing,
  CHARACTER_CODE, HEVC HDR metadata)
- bluinfo (GPL-3.0) https://github.com/SavSanta/bluinfo (CLPI format layout,
  stream attribute constants)
- libbluray (GPL-2.0) https://code.videolan.org/videolan/libbluray (BD-ROM
  structure reference, CLPI/MPLS parsing implementation)
- ace20022/libbluray (GPL-2.0) https://github.com/ace20022/libbluray
  (clean Pythonic CLPI/MPLS parsing reference)
- Blu-ray Disc Read-Only Format specifications (BD-ROM)

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
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from dvdifo import _read_u16, _read_u32
from models import StreamType, Stream, Title, log_debug


# =============================================================================
# STN table language parsing
# =============================================================================
_AUDIO_CODING_TYPES = {0x03, 0x04, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2}
_PG_IG_CODING_TYPES = {0x90, 0x91}
_TEXT_SUB_CODING_TYPE = 0x92


def _parse_stn_table_languages(stn_bytes: bytes) -> tuple[list[str], list[str]]:
    if len(stn_bytes) < 16:
        return [], []
    n_video, n_audio, n_pg = stn_bytes[4], stn_bytes[5], stn_bytes[6]
    pos = 16

    def _skip_entry() -> bytes | None:
        nonlocal pos
        if pos >= len(stn_bytes):
            return None
        elen = stn_bytes[pos]
        pos += 1 + elen
        if pos > len(stn_bytes):
            return None
        if pos >= len(stn_bytes):
            return b""
        alen = stn_bytes[pos]
        pos += 1
        attr = stn_bytes[pos : pos + alen]
        pos += alen
        return attr

    def _lang_from_attr(attr: bytes | None) -> str:
        if not attr or len(attr) < 2:
            return "und"
        coding_type = attr[0]
        try:
            if coding_type in _AUDIO_CODING_TYPES:
                raw = attr[2:5]
            elif coding_type in _PG_IG_CODING_TYPES:
                raw = attr[1:4]
            elif coding_type == _TEXT_SUB_CODING_TYPE:
                raw = attr[2:5]
            else:
                return "und"
        except Exception:
            return "und"
        if len(raw) == 3 and all(32 <= c < 127 for c in raw):
            return raw.decode("ascii", "ignore")
        return "und"

    for _ in range(n_video):
        _skip_entry()
    audio_langs = [_lang_from_attr(_skip_entry()) for _ in range(n_audio)]
    subtitle_langs = [_lang_from_attr(_skip_entry()) for _ in range(n_pg)]
    return audio_langs, subtitle_langs


# Blu-ray (BDMV) stream coding types used in MPLS STN table entries.
# See Blu-ray Disc Read-Only Format spec and dvdutils / python-dvdvideo references.
_BD_VIDEO_CODING_MAP: dict[int, str] = {
    0x01: "mpeg1video",
    0x02: "mpeg2video",
    0x14: "h264",  # Secondary video (H.264 MVC for 3D)
    0x1B: "h264",
    0x24: "hevc",
    0xEA: "vc1",
}

_BD_AUDIO_CODING_MAP: dict[int, tuple[str, str]] = {
    0x80: ("lpcm", "pcm_s16le"),
    0x81: ("ac3", "ac3"),
    0x82: ("dts", "dts"),
    0x83: ("truehd", "truehd"),  # Dolby TrueHD
    0x84: ("eac3", "eac3"),  # Dolby Digital Plus (E-AC-3)
    0x85: ("dts_hd_hr", "dts"),  # DTS-HD High Resolution
    0x86: ("dts_hd_ma", "dts"),  # DTS-HD Master Audio
    0xA1: ("eac3", "eac3"),  # E-AC3 (Dolby Digital Plus alternate)
    0xA2: ("dts", "dts"),  # DTS (alternate)
}

_BD_SUB_CODING_MAP: dict[int, str] = {
    0x90: "hdmv_pgs_subtitle",
    0x91: "hdmv_pgs_subtitle",  # Interactive Graphics (also PGS)
    0x92: "dvd_subtitle",  # Text subtitle stream
}

# CHARACTER_CODE mapping for text subtitles (coding_type 0x92).
# Text subtitle language bytes are encoded using this character set.
# Based on pyparsebluray's CHARACTER_CODE dict and the BD spec.
_BD_CHARACTER_CODE: dict[int, str] = {
    0x01: "utf-8",
    0x02: "utf-16-be",
    0x03: "shift-jis",
    0x04: "euc-kr",
    0x05: "gb18030",
    0x06: "gb2312",
    0x07: "big5",
}

# CLPI video format ID -> (height, interlaced) mapping.
# From bluinfo/ts_attrconst.py VideoFormat and BD spec.
_BD_VIDEO_FORMAT_MAP: dict[int, tuple[int, bool]] = {
    1: (480, True),  # 480i
    2: (576, True),  # 576i
    3: (480, False),  # 480p
    4: (1080, True),  # 1080i
    5: (720, False),  # 720p
    6: (1080, False),  # 1080p
    7: (576, False),  # 576p
    8: (2160, False),  # 2160p
}

# CLPI frame rate ID -> frame rate as a float.
# From bluinfo/ts_attrconst.py FrameRate and BD spec.
_BD_FRAMERATE_MAP: dict[int, Fraction] = {
    1: Fraction(24000, 1001),  # 23.976
    2: Fraction(24, 1),  # 24
    3: Fraction(25, 1),  # 25
    4: Fraction(30000, 1001),  # 29.97
    5: Fraction(30, 1),  # 30
    6: Fraction(50, 1),  # 50
    7: Fraction(60000, 1001),  # 59.94
}

# CLPI audio channel layout ID -> channel count.
# These are the standard BD channel layout values from the spec.
_BD_CHANNEL_LAYOUT_MAP: dict[int, int] = {
    1: 1,  # Mono
    2: 2,  # Dual mono
    3: 2,  # Stereo
    4: 3,  # 3ch
    5: 4,  # 4ch
    6: 6,  # 5.1ch
    7: 7,  # 6.1ch
    8: 8,  # 7.1ch
    9: 8,  # 7.1ch (alternative)
}

# CLPI sample rate ID -> sample rate in Hz.
_BD_SAMPLE_RATE_MAP: dict[int, int] = {
    1: 48000,
    2: 96000,
    3: 192000,
    4: 96000,  # 96kHz (alternative encoding)
    5: 192000,  # 192kHz (alternative encoding)
    12: 48000,  # 48/192 combo
    14: 48000,  # 48/96 combo
}

# CLPI aspect ratio ID -> display aspect ratio string.
_BD_ASPECT_RATIO_MAP: dict[int, str] = {
    2: "4:3",
    3: "16:9",
    4: ">2.20:1",
}

# Dynamic range type for HEVC video (coding_type 0x24 in MPLS stream attributes).
# From pyparsebluray's DYNAMIC_RANGE_TYPE.
_BD_DYNAMIC_RANGE_MAP: dict[int, str] = {
    0: "SDR",
    1: "hdr10",
    2: "dolby_vision",
}

# Color space for HEVC video (coding_type 0x24 in MPLS stream attributes).
# From pyparsebluray's COLOR_SPACE.
_BD_COLOR_SPACE_MAP: dict[int, str] = {
    0: "reserved",
    1: "bt709",
    2: "bt2020",
}


# =============================================================================
# CLPI (Clip Information) parser
#
# .clpi files live in BDMV/CLIPINF/ alongside the .m2ts streams in
# BDMV/STREAM/.  Each CLPI contains authoritative stream attributes for its
# M2TS clip — audio channel counts, video resolution, framerate, codecs, and
# languages — all without touching the M2TS itself.
#
# Previously, channel counts were obtained by probing the M2TS via ffprobe
# (or 7z partial extraction for ISOs).  CLPI parsing removes this dependency,
# making scanning order(s) of magnitude faster and more reliable.
#
# Based on bluinfo/ts_scanner.py's clipfilescan() and the BD-ROM spec.
# =============================================================================


def _parse_clpi(data: bytes) -> dict[int, dict[str, Any]]:
    """Parse a CLPI file's ProgramInfo stream entries.

    Returns a dict mapping ``PID -> stream_info``, where each stream_info
    is a dict with fields like ``coding_type``, ``codec``, ``channels``,
    ``sample_rate``, ``language``, ``height``, ``framerate``, etc.

    Returns an empty dict on failure.
    """
    result: dict[int, dict[str, Any]] = {}
    if len(data) < 40:
        return result
    magic = data[:8]
    if magic not in (
        b"HDMV0100",
        b"HDMV0200",
        b"HDMV0300",
        b"HDBD0100",
        b"HDBD0200",
        b"HDBD0300",
    ):
        log_debug(f"CLPI: unknown magic {magic}")
        return result
    try:
        # Offset to clip info block is at byte 12 (4 bytes, big-endian).
        clip_index = _read_u32(data, 12)
        if clip_index + 4 >= len(data):
            return result
        # Clip info: [length(4)] [reserved(4)] [num_streams(1)] ...
        clip_pos = clip_index + 4
        if clip_pos + 12 > len(data):
            return result
        num_streams = data[clip_pos + 8]
        pos = clip_pos + 10  # Start of first stream entry

        for _ in range(num_streams):
            if pos + 3 > len(data):
                break
            pid = _read_u16(data, pos)
            pos += 2
            if pos >= len(data):
                break
            stream_info_len = data[pos]
            pos += 1
            if pos + stream_info_len > len(data):
                break
            info: dict[str, Any] = {"pid": pid, "coding_type": 0, "codec": "unknown"}
            info_start = pos
            if stream_info_len < 1:
                # No info, move to next entry
                pos += stream_info_len
                continue
            coding_type = data[pos]
            info["coding_type"] = coding_type
            attr_pos = pos + 1
            remaining = stream_info_len - 1

            # Determine stream type by coding_type.
            if coding_type in _BD_VIDEO_CODING_MAP:
                info["codec"] = _BD_VIDEO_CODING_MAP[coding_type]
                if remaining >= 1:
                    video_byte = data[attr_pos]
                    video_format = (video_byte >> 4) & 0x0F
                    frame_rate = video_byte & 0x0F
                    info["video_format"] = video_format
                    info["framerate"] = _BD_FRAMERATE_MAP.get(frame_rate)
                    if info["framerate"] is not None:
                        info["framerate"] = float(info["framerate"])
                    h_ilaced = _BD_VIDEO_FORMAT_MAP.get(video_format)
                    if h_ilaced:
                        info["height"] = h_ilaced[0]
                        info["interlaced"] = h_ilaced[1]
                    if remaining >= 2:
                        aspect_byte = data[attr_pos + 1]
                        aspect_id = (aspect_byte >> 4) & 0x0F
                        info["aspect"] = _BD_ASPECT_RATIO_MAP.get(aspect_id)

            elif coding_type in _BD_AUDIO_CODING_MAP:
                info["codec"] = _BD_AUDIO_CODING_MAP[coding_type][0]
                if remaining >= 1:
                    audio_byte = data[attr_pos]
                    ch_layout = (audio_byte >> 4) & 0x0F
                    sr_id = audio_byte & 0x0F
                    info["channels"] = _BD_CHANNEL_LAYOUT_MAP.get(ch_layout)
                    info["sample_rate"] = _BD_SAMPLE_RATE_MAP.get(sr_id)
                if remaining >= 4:
                    raw_lang = data[attr_pos + 1 : attr_pos + 4]
                    if all(32 <= c < 127 for c in raw_lang):
                        info["language"] = raw_lang.decode("ascii", "ignore")

            elif coding_type in _BD_SUB_CODING_MAP:
                info["codec"] = _BD_SUB_CODING_MAP[coding_type]
                if coding_type == 0x92:  # Text subtitle with character code
                    if remaining >= 1:
                        char_code = data[attr_pos]
                        info["character_code"] = char_code
                        enc = _BD_CHARACTER_CODE.get(char_code, "utf-8")
                    else:
                        enc = "utf-8"
                    if remaining >= 4:
                        raw_lang = data[attr_pos + 1 : attr_pos + 4]
                        info["language"] = raw_lang.decode(enc, "ignore")
                elif coding_type in (0x90, 0x91):  # PG/IG
                    if remaining >= 3:
                        raw_lang = data[attr_pos : attr_pos + 3]
                        if all(32 <= c < 127 for c in raw_lang):
                            info["language"] = raw_lang.decode("ascii", "ignore")

            result[pid] = info
            # Advance past this stream entry: already at info_start, skip info_len bytes.
            pos = info_start + stream_info_len

    except Exception as e:
        log_debug(f"CLPI parse error: {e}")
        return {}

    return result


def _parse_clpi_file(clpi_path: Path) -> dict[int, dict[str, Any]]:
    """Read and parse a .clpi file on disk.  Returns PID->info dict."""
    try:
        data = clpi_path.read_bytes()
        return _parse_clpi(data)
    except Exception as e:
        log_debug(f"CLPI read/parse failed for {clpi_path.name}: {e}")
        return {}


def _merge_clpi_into_mpls(
    mpls_streams: list[MplsStreamInfo],
    clpi_streams: dict[int, dict[str, Any]],
) -> None:
    """Merge CLPI stream attributes (channels, resolution) into MPLS stream info.

    Updates ``mpls_streams`` in-place, filling ``channels``, ``sample_rate``,
    ``width``/``height``, and ``framerate`` from CLPI data where available.

    The ``codec`` field is **overridden** from CLPI when the two disagree.
    The CLPI is the authoritative per-clip metadata (generated from the actual
    M2TS stream), while the MPLS STN table is a per-playitem view that can
    contain authoring errors — e.g. some Disney discs declare TrueHD (0x85)
    as DTS-HD HR (0x83) in the MPLS STN table.
    """
    for s in mpls_streams:
        pid = s.get("pid")
        if pid is None:
            continue
        cinfo = clpi_streams.get(pid)
        if cinfo is None:
            continue
        # Override codec from CLPI (authoritative per-clip metadata).
        clpi_codec = cinfo.get("codec")
        if clpi_codec and clpi_codec != "unknown" and s.get("codec") != clpi_codec:
            s["codec"] = clpi_codec
        if s.get("channels") is None:
            s["channels"] = cinfo.get("channels")
        if s.get("sample_rate") is None:
            s["sample_rate"] = cinfo.get("sample_rate")
        if s.get("height") is None:
            s["height"] = cinfo.get("height")
        if s.get("framerate") is None:
            s["framerate"] = cinfo.get("framerate")
        if s.get("interlaced") is None:
            s["interlaced"] = cinfo.get("interlaced")
        if s.get("aspect") is None:
            s["aspect"] = cinfo.get("aspect")


# BD/H.264 colour metadata heuristics — used when ffprobe probing is skipped
# (MPLS+CLPI scan path).  Virtually all HD Blu-ray (720p/1080p/i) uses BT.709.
# UHD (2160p) is handled separately in _set_video_color_from_info: the STN
# table's dynamic_range_type marks HDR (hdr10/dolby_vision) vs SDR, and only
# HDR gets BT.2020 primaries + PQ transfer — SDR UHD defaults to BT.709.
_BD_HEIGHT_COLOR_MAP: dict[int, tuple[str, str, str]] = {
    # height -> (primaries, transfer, matrix)
    480: ("smpte170m", "bt709", "smpte170m"),
    576: ("bt470bg", "bt709", "bt470bg"),
    720: ("bt709", "bt709", "bt709"),
    1080: ("bt709", "bt709", "bt709"),
}
_BD_HEVC_COLOR_MAP: dict[str, tuple[str, str, str]] = {
    # colorspace -> (primaries, transfer, matrix)
    "bt709": ("bt709", "bt709", "bt709"),
    "bt2020": ("bt2020", "smpte2084", "bt2020nc"),
}


def _set_video_color_from_info(s: Stream, si: dict[str, Any]) -> None:
    """Populate colour metadata on a video Stream from scan info dict.

    Used when scanning via MPLS+CLPI without ffprobe.  For HEVC streams the
    STN table contains explicit ``colorspace`` and ``dynamic_range_type``
    attributes (byte 5 of the stream attributes).  For other codecs we infer
    HD vs SD from the CLPI-provided ``height``.

    Does nothing when colour fields are already set (e.g. after ffprobe).
    """
    if s.color_primaries is not None:
        return  # Already populated by ffprobe.

    cs = si.get("colorspace")
    if cs is not None and cs in _BD_HEVC_COLOR_MAP:
        primaries, transfer, matrix = _BD_HEVC_COLOR_MAP[cs]
        s.color_primaries = primaries
        s.color_transfer = transfer
        s.color_space = matrix
        s.color_range = "limited"
        return

    height = si.get("height")
    if height is not None:
        if height == 2160:
            # UHD: the STN's dynamic_range_type is the authoritative HDR
            # indicator. Only hdr10 / dolby_vision get BT.2020 + PQ; SDR
            # UHD (or an unparseable DR byte) defaults to BT.709.
            if si.get("dynamic_range_type") in ("hdr10", "dolby_vision"):
                info = ("bt2020", "smpte2084", "bt2020nc")
            else:
                info = ("bt709", "bt709", "bt709")
        else:
            info = _BD_HEIGHT_COLOR_MAP.get(height)
        if info is not None:
            primaries, transfer, matrix = info
            s.color_primaries = primaries
            s.color_transfer = transfer
            s.color_space = matrix
            s.color_range = "limited"
            return

    # Last resort: guess from codec.  BD AVC/MPEG-2 is virtually always HD.
    if si.get("codec") in ("h264", "mpeg2video", "vc1", "hevc"):
        s.color_primaries = "bt709"
        s.color_transfer = "bt709"
        s.color_space = "bt709"
        s.color_range = "limited"


class MplsStreamInfo(TypedDict):
    """Stream entry parsed from an MPLS STN table.

    ``sample_rate``/``height``/``framerate``/``interlaced``/``aspect`` are
    filled in later by ``_merge_clpi_into_mpls`` from CLPI metadata.
    """

    type: StreamType | None
    codec: str
    lang: str
    channels: int | None
    pid: int | None
    coding_type: NotRequired[int]
    dynamic_range_type: NotRequired[str]
    colorspace: NotRequired[str]
    sample_rate: NotRequired[int | None]
    height: NotRequired[int | None]
    framerate: NotRequired[str | None]
    interlaced: NotRequired[bool | None]
    aspect: NotRequired[str | None]


class MplsPlayItem(TypedDict):
    """One PlayItem from a .mpls PlayList block."""

    clip: str
    duration: float
    in_time: int
    out_time: int


def _parse_stn_table_streams(stn_bytes: bytes) -> list[MplsStreamInfo]:
    """Extract full stream info from an MPLS STN table, not just languages.

    Reads all 8 stream category counts (primary video/audio/PG/IG, secondary
    audio/video/PG, and Dolby Vision) per the BD-ROM specification. Handles
    HEVC (0x24) with dynamic_range_type/colorspace extraction, CHARACTER_CODE
    encoding for text subtitles (0x92), and secondary video (0x14).

    Returns a list of dicts, each with:
      type          : StreamType
      coding_type   : int
      codec         : str
      lang          : str  (ISO 639-2 3-char, "und" when unavailable)
      channels      : int | None  (only for audio)
      pid           : int | None  (the MPEG-2 TS PID, where present)
      dynamic_range_type : str | None  (HEVC only: SDR/hdr10/dolby_vision)
      colorspace    : str | None       (HEVC only: bt709/bt2020)

    Returns an empty list when the STN table is unreadable.
    """
    result: list[MplsStreamInfo] = []
    if len(stn_bytes) < 16:
        return result

    # BD spec / pyparsebluray: 8 stream category counts at bytes 4-11.
    counts: dict[str, int] = {
        "prim_video": stn_bytes[4],
        "prim_audio": stn_bytes[5],
        "prim_pg": stn_bytes[6],
        "prim_ig": stn_bytes[7],
        "seco_audio": stn_bytes[8],
        "seco_video": stn_bytes[9],
        "seco_pg": stn_bytes[10],
        "dv": stn_bytes[11],
    }
    pos = 16

    # Default placeholder dicts for malformed entries (backward compat).
    _PLACEHOLDER_VIDEO: MplsStreamInfo = {
        "type": StreamType.VIDEO,
        "coding_type": 0,
        "codec": "mpeg2video",
        "lang": "und",
        "channels": None,
        "pid": None,
    }
    _PLACEHOLDER_AUDIO: MplsStreamInfo = {
        "type": StreamType.AUDIO,
        "coding_type": 0,
        "codec": "ac3",
        "lang": "und",
        "channels": None,
        "pid": None,
    }
    _PLACEHOLDER_SUB: MplsStreamInfo = {
        "type": StreamType.SUBTITLE,
        "coding_type": 0,
        "codec": "hdmv_pgs_subtitle",
        "lang": "und",
        "channels": None,
        "pid": None,
    }

    def _read_entry() -> MplsStreamInfo | None:
        nonlocal pos
        if pos >= len(stn_bytes):
            return None
        elen = stn_bytes[pos]
        pos += 1
        if pos + elen > len(stn_bytes):
            return None
        entry_data = stn_bytes[pos : pos + elen]
        pos += elen
        if pos >= len(stn_bytes):
            return None
        alen = stn_bytes[pos]
        pos += 1
        if pos + alen > len(stn_bytes):
            return None
        attr = stn_bytes[pos : pos + alen]
        pos += alen
        if len(attr) < 2:
            return None
        coding_type = attr[0]
        pid: int | None = None
        lang = "und"
        channels: int | None = None
        codec = "unknown"
        st: StreamType | None = None
        dynamic_range_type: str | None = None
        colorspace: str | None = None

        # PID is at entry_data[1:3] (big-endian) for primary streams.
        # entry_data[0] is a stream-index / ref byte (0x01 for primary, 0x02 for secondary).
        if elen >= 3:
            pid = (entry_data[1] << 8) | entry_data[2]

        # --- Determine stream type and extract fields ---
        if coding_type in _BD_VIDEO_CODING_MAP:
            st = StreamType.VIDEO
            codec = _BD_VIDEO_CODING_MAP[coding_type]
            # HEVC (0x24): dynamic_range_type and colorspace at attr[5]
            if coding_type == 0x24 and len(attr) >= 6:
                dr_byte = attr[5]
                dynamic_range_type = _BD_DYNAMIC_RANGE_MAP.get(dr_byte >> 4, "unknown")
                cspace = dr_byte & 0x0F
                colorspace = _BD_COLOR_SPACE_MAP.get(cspace, "unknown")
        elif coding_type in _BD_AUDIO_CODING_MAP:
            st = StreamType.AUDIO
            codec = _BD_AUDIO_CODING_MAP[coding_type][0]
            if len(attr) >= 5:
                raw_lang = attr[2:5]
                if all(32 <= c < 127 for c in raw_lang):
                    lang = raw_lang.decode("ascii", "ignore")
        elif coding_type in _BD_SUB_CODING_MAP:
            st = StreamType.SUBTITLE
            codec = _BD_SUB_CODING_MAP[coding_type]
            if coding_type == 0x92:  # Text subtitle with CHARACTER_CODE
                if len(attr) >= 5:
                    char_code = attr[1]
                    enc = _BD_CHARACTER_CODE.get(char_code, "utf-8")
                    raw_lang = attr[2:5]
                    decoded = raw_lang.decode(enc, "ignore")
                    # Fall back to ASCII if the encoded decode produces nonsense.
                    # Check for any non-ASCII character via ord() since decoded is a str.
                    if decoded.strip() and any(ord(c) > 127 for c in decoded):
                        lang = decoded
                    elif all(32 <= c < 127 for c in raw_lang):
                        lang = raw_lang.decode("ascii", "ignore")
                    else:
                        lang = decoded if decoded.strip() else "und"
            else:  # PG (0x90) / IG (0x91)
                if len(attr) >= 4:
                    raw_lang = attr[1:4]
                    if all(32 <= c < 127 for c in raw_lang):
                        lang = raw_lang.decode("ascii", "ignore")

        if st is None:
            return {
                "type": None,
                "codec": "unknown",
                "lang": "und",
                "channels": None,
                "pid": None,
            }

        entry: MplsStreamInfo = {
            "type": st,
            "coding_type": coding_type,
            "codec": codec,
            "lang": lang,
            "channels": channels,
            "pid": pid,
        }
        if dynamic_range_type is not None:
            entry["dynamic_range_type"] = dynamic_range_type
        if colorspace is not None:
            entry["colorspace"] = colorspace
        return entry

    # Category iteration order (per BD spec):
    category_order = [
        ("prim_video", "video"),
        ("prim_audio", "audio"),
        ("prim_pg", "sub"),
        ("prim_ig", "sub"),
        ("seco_audio", "audio"),
        ("seco_video", "video"),
        ("seco_pg", "sub"),
        ("dv", "video"),
    ]
    placeholder_map: dict[str, MplsStreamInfo] = {
        "video": _PLACEHOLDER_VIDEO,
        "audio": _PLACEHOLDER_AUDIO,
        "sub": _PLACEHOLDER_SUB,
    }

    for cat_name, cat_kind in category_order:
        count = counts.get(cat_name, 0)
        for _ in range(count):
            info = _read_entry()
            if info and info["type"] is not None:
                result.append(info)
            else:
                result.append(placeholder_map[cat_kind].copy())

    return result


def _parse_mpls(path: Path, clpi_dir: Path | None = None) -> dict[str, Any] | None:
    """Parse a .mpls playlist file and return stream/chapter data.

    If *clpi_dir* is provided (a ``BDMV/CLIPINF`` directory), the first
    playitem's .clpi file is parsed and its attributes (audio channel counts,
    video resolution, framerate) are merged into the STN stream info. This
    eliminates the need for ffprobe/M2TS probing on directory-based Blu-rays.
    """
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if len(data) < 40 or data[0:4] != b"MPLS":
        return None
    try:
        playlist_start = _read_u32(data, 8)
        if playlist_start + 10 > len(data):
            return None
        num_playitems = _read_u16(data, playlist_start + 6)
        pos = playlist_start + 10
        play_items: list[MplsPlayItem] = []
        audio_langs: list[str] = []
        subtitle_langs: list[str] = []
        stn_data = b""
        for i in range(num_playitems):
            if pos + 2 > len(data):
                break
            item_len = _read_u16(data, pos)
            pos += 2
            item = data[pos : pos + item_len]
            pos += item_len
            if len(item) < 32:
                continue
            clip_name = item[0:5].decode("ascii", "ignore")
            in_time, out_time = _read_u32(item, 12), _read_u32(item, 16)
            duration = max(0.0, (out_time - in_time) / 45000.0)
            is_multi_angle = (_read_u16(item, 9) >> 4) & 1
            stn_off = 32
            if is_multi_angle:
                stn_off = 32 + 2 + max(0, item[32] - 1) * 10
            play_items.append(
                {
                    "clip": clip_name,
                    "duration": duration,
                    "in_time": in_time,
                    "out_time": out_time,
                }
            )
            if i == 0:
                stn_data = item[stn_off:]
                audio_langs, subtitle_langs = _parse_stn_table_languages(stn_data)
        if not play_items:
            return None

        # --- SubPath entries (after PlayItems, before PlayListMark) ---
        # SubPath count is at playlist_start + 8 (2 bytes).
        num_subpaths = _read_u16(data, playlist_start + 8)
        subpath_entries: list[dict[str, Any]] = []
        for _sp_idx in range(num_subpaths):
            if pos + 4 > len(data):
                break
            sp_len = _read_u32(data, pos)
            pos += 4
            if pos + sp_len > len(data):
                break
            sp_data = data[pos : pos + sp_len]
            pos += sp_len
            if len(sp_data) < 4:
                continue
            sp_type = sp_data[0]
            num_sp_items = sp_data[3]
            sp_clips: list[str] = []
            spi_pos = 4
            # Out-of-mux SubPath types (1, 4, 6) have sync fields
            # (sync_PlayItem_id + sync_start_PTS = 5 extra bytes) appended
            # to each SubPlayItem. In-mux types don't.
            has_sync = sp_type in (1, 4, 6)
            sp_item_size = 19 + (5 if has_sync else 0)
            for _spi in range(num_sp_items):
                if spi_pos + sp_item_size > len(sp_data):
                    break
                clip_name = sp_data[spi_pos : spi_pos + 5].decode("ascii", "ignore")
                sp_clips.append(clip_name)
                spi_pos += sp_item_size
            if sp_clips:
                sp_entry: dict[str, Any] = {
                    "type": sp_type,
                    "clips": sp_clips,
                }
                subpath_entries.append(sp_entry)
        if subpath_entries:
            log_debug(
                f"{path.stem}: {len(subpath_entries)} SubPath entries "
                f"({', '.join(f'type={e["type"]}' for e in subpath_entries)})"
            )

        chapter_times = _parse_mpls_chapters(data, play_items)
        # Parse full stream info (codecs, channels) from the STN table of the
        # first playitem. This lets us build a Title without probing any M2TS.
        streams: list[MplsStreamInfo] = (
            _parse_stn_table_streams(stn_data) if play_items else []
        )

        # Merge CLPI attributes (audio channels, resolution) into STN streams.
        if clpi_dir is not None and play_items:
            first_clip_name = play_items[0]["clip"]
            clpi_path = clpi_dir / f"{first_clip_name}.clpi"
            if not clpi_path.exists():
                # Case-insensitive fallback (ISO 7z extraction preserves original
                # case, which may be .CLPI on some discs).
                for f in clpi_dir.iterdir():
                    if f.stem == first_clip_name and f.suffix.lower() == ".clpi":
                        clpi_path = f
                        break
                else:
                    clpi_path = None
            if clpi_path and clpi_path.exists():
                clpi_streams = _parse_clpi_file(clpi_path)
                if clpi_streams:
                    _merge_clpi_into_mpls(streams, clpi_streams)
                    log_debug(
                        f"Merged CLPI for {first_clip_name}: {len(clpi_streams)} streams from {clpi_path.name}"
                    )

        return {
            "play_items": play_items,
            "audio_langs": audio_langs,
            "subtitle_langs": subtitle_langs,
            "chapter_times": chapter_times,
            "streams": streams,
            "subpath_entries": subpath_entries,
        }
    except Exception as e:
        log_debug(f"MPLS parse failed for {path.name}: {e}")
        return None


def _parse_mpls_chapters(
    data: bytes, play_items: list[MplsPlayItem] | None = None
) -> list[float]:
    """Extract ENTRY_MARK chapter start times (seconds) from a .mpls PlayListMark section.

    MPLS mark ``time`` is a PTS **within the referenced PlayItem's clip** (45 kHz,
    relative to the clip's own STC), NOT a playlist-absolute timestamp. To get
    the playlist-absolute chapter time we must add the cumulative duration of all
    preceding PlayItems and subtract the referenced clip's ``in_time`` offset:

        absolute = sum(duration_i for i < ref) + (mark_time - in_time_ref) / 45000

    For single-clip playlists (or when ``play_items`` is None) the per-clip PTS
    happens to equal the playlist-absolute time, so the old behaviour (treating
    ``time`` as absolute) is preserved as a fallback.
    """
    if len(data) < 16:
        return []
    mark_start = _read_u32(data, 12)
    if mark_start == 0 or mark_start + 6 > len(data):
        return []
    num_marks = _read_u16(data, mark_start + 4)
    times: list[float] = []
    mp = mark_start + 6
    # Precompute cumulative durations for playlist-absolute conversion.
    # cum_dur[i] = sum of durations of PlayItems 0..i-1
    cum_dur: list[float] = []
    running = 0.0
    if play_items:
        for pi in play_items:
            cum_dur.append(running)
            running += pi.get("duration", 0.0)
    for _ in range(num_marks):
        if mp + 14 > len(data):
            break
        mark_type = data[mp + 1]
        play_item_ref = _read_u16(data, mp + 2)
        mark_time = _read_u32(data, mp + 4)  # 45 kHz PTS within the referenced clip
        if mark_type == 0x01:  # ENTRY_MARK == a chapter
            if play_items and play_item_ref < len(play_items):
                # Convert per-clip PTS to playlist-absolute time.
                in_time_ref = play_items[play_item_ref].get("in_time", 0)
                offset_within_clip = max(0, (mark_time - in_time_ref) / 45000.0)
                absolute = cum_dur[play_item_ref] + offset_within_clip
                times.append(absolute)
            else:
                # Fallback: treat as playlist-absolute (correct for
                # single-clip playlists or when PlayItem data is unavailable).
                times.append(mark_time / 45000.0)
        mp += 14
    times.sort()
    # Drop near-duplicate chapter boundaries (within 1s of the previous one).
    deduped: list[float] = []
    for t in times:
        if not deduped or t - deduped[-1] >= 1.0:
            deduped.append(t)
    # Normalise so the first chapter starts at 0 (matches MakeMKV).
    if deduped:
        offset = deduped[0]
        deduped = [t - offset for t in deduped]
    return deduped


def _apply_stn_languages(
    title: Title, audio_langs: list[str], subtitle_langs: list[str]
) -> None:
    a_idx = s_idx = 0
    for s in title.streams:
        if s.stream_type == StreamType.AUDIO:
            if a_idx < len(audio_langs) and audio_langs[a_idx] != "und":
                s.language = audio_langs[a_idx]
            a_idx += 1
        elif s.stream_type == StreamType.SUBTITLE:
            if s_idx < len(subtitle_langs) and subtitle_langs[s_idx] != "und":
                s.language = subtitle_langs[s_idx]
            s_idx += 1


# =============================================================================
# BDMV metadata (bdmt_*.xml)
# =============================================================================


def _parse_bdmv_disc_name(bdmv_root: Path) -> str | None:
    meta_dir = bdmv_root / "META" / "DL"
    if not meta_dir.is_dir():
        return None
    for xml_file in sorted(meta_dir.glob("bdmt_*.xml")):
        try:
            for elem in ET.parse(xml_file).iter():
                if elem.tag.endswith("name") and elem.text and elem.text.strip():
                    raw = elem.text.strip()
                    # Preserve spaces but convert newlines to " - "
                    return (
                        raw.replace("\r\n", " - ")
                        .replace("\r", " - ")
                        .replace("\n", " - ")
                    )
        except Exception:
            continue
    return None


def _parse_bdmv_catalog_number(bdmv_root: Path) -> str | None:
    """Extract the catalog number / EAN from bdmt_eng.xml.

    The BD-J metadata file (META/DL/bdmt_*.xml) often contains a
    ``<catalogNumber>`` element with the disc's UPC/EAN code.
    """
    meta_dir = bdmv_root / "META" / "DL"
    if not meta_dir.is_dir():
        return None
    for xml_file in sorted(meta_dir.glob("bdmt_*.xml")):
        try:
            for elem in ET.parse(xml_file).iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "catalogNumber" and elem.text and elem.text.strip():
                    raw = elem.text.strip()
                    digits_only = re.sub(r"[^0-9]", "", raw)
                    if len(digits_only) in (12, 13):
                        return digits_only
                    return raw
        except Exception:
            continue
    return None
