"""
DVD IFO/PGC/VOBU binary parsing.

Extracted from main.py: pure-Python parsers for DVD-Video IFO structures
(VTSI_MAT, VTS_PGCIT, VTS_C_ADT, VTS_VOBU_ADMAP, VMG_TXTDT_MG, PGC command
tables). This module has no dependency on the Stream / Title / Config classes
defined in main.py.

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
import struct
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, TypedDict


# =============================================================================
# Debug logging
# =============================================================================
#
# main.py calls set_debug(CONFIG.debug) at startup so this module emits the same
# [DEBUG] lines that it did before extraction. Until that call is made, debug
# output is suppressed.

_DEBUG = False


def set_debug(enabled: bool) -> None:
    global _DEBUG
    _DEBUG = enabled


def log_debug(msg: str) -> None:
    if _DEBUG:
        print(f"[DEBUG] {msg}", file=sys.stderr)


# =============================================================================
# Shared binary-read helpers
# =============================================================================


def _read_u16(b: bytes, off: int) -> int:
    return int.from_bytes(b[off : off + 2], "big")


def _read_u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off : off + 4], "big")


def _concat_file_layout(inputs: list[Path]) -> list[tuple[Path, int, int]]:
    """Return [(path, start_byte, end_byte)] for each input laid out end to end."""
    layout: list[tuple[Path, int, int]] = []
    cursor = 0
    for f in inputs:
        size = f.stat().st_size
        layout.append((f, cursor, cursor + size))
        cursor += size
    return layout


# =============================================================================
# PGC offset constants (inspired by pyparsedvd's PGCOffset enum)
# =============================================================================


class _PGCOffset:
    """Offsets within a PGC structure (relative to PGC start).

    Based on pyparsedvd's PGCOffset enum and http://www.mpucoder.com/DVD/pgc.html
    """

    NB_PROGRAMS = 0x002  # 1 byte
    NB_CELLS = 0x003  # 1 byte
    PLAYBACK_TIME = 0x004  # 4 bytes (BCD: hh:mm:ss.ff)
    PROHIBITED_USER_OPS = 0x008  # 4 bytes
    # Stream control table pointers/offsets
    AST_CTL = 0x00C  # 2 bytes (simplified) or 2B offset
    SPST_CTL = 0x01C  # 2 bytes (simplified) or 2B offset
    # Navigation
    NEXT_PGC = 0x09C  # 2 bytes
    PREV_PGC = 0x09E  # 2 bytes
    GOUP_PGC = 0x0A0  # 2 bytes
    STILL_TIME = 0x0A2  # 1 byte
    PG_PLAYBACK_MODE = 0x0A3  # 1 byte
    PALETTE = 0x0A4  # 16x4 = 64 bytes
    # Sub-table offsets (2 bytes each)
    COMMANDS_OFFSET = 0x0E4
    PROGRAM_MAP_OFFSET = 0x0E6
    CELL_PLAYBACK_INFO_TABLE_OFFSET = 0x0E8
    CELL_POSITION_INFO_TABLE_OFFSET = 0x0EA

    # Constants
    CELL_PLAYBACK_INFO_LEN = 0x18  # 24 bytes per entry
    CELL_DURATION_OFFSET = 0x04  # Playback time within cell info
    NUM_AST_ENTRIES = 8
    NUM_SPST_ENTRIES = 32
    AST_SIMPLIFIED_ENTRY_LEN = 2
    AST_NORMAL_ENTRY_LEN = 8
    SPST_ENTRY_LEN = 4


# =============================================================================
# Structured IFO parsing helpers (inspired by dvdutils)
# =============================================================================


@dataclass(slots=True)
class _IFOVideoAttrs:
    """DVD VTS video attributes (2 bytes from VTS_V_ATR).

    Based on dvdutils' VideoAttrs and the DVD spec (mpucoder.com).
    """

    mpeg_version: str
    standard: str
    aspect_ratio: str
    resolution: tuple[int, int] | None
    letterboxed: bool
    film_mode: bool
    cc_field_1: bool
    cc_field_2: bool

    MPEG_MAP: ClassVar[dict[int, str]] = {
        0: "MPEG-1",
        1: "MPEG-2",
        2: "reserved",
        3: "unknown",
    }
    STANDARD_MAP: ClassVar[dict[int, str]] = {
        0: "NTSC",
        1: "PAL",
        2: "reserved",
        3: "unknown",
    }
    ASPECT_MAP: ClassVar[dict[int, str]] = {
        0: "4:3",
        1: "16:9",
        2: "reserved",
        3: "unknown",
    }
    RES_MAP: ClassVar[dict[tuple[str, int], tuple[int, int]]] = {
        ("NTSC", 0): (720, 480),
        ("NTSC", 1): (704, 480),
        ("NTSC", 2): (352, 480),
        ("NTSC", 3): (352, 240),
        ("NTSC", 4): (544, 480),
        ("NTSC", 5): (480, 480),
        ("PAL", 0): (720, 576),
        ("PAL", 1): (704, 576),
        ("PAL", 2): (352, 576),
        ("PAL", 3): (352, 288),
        ("PAL", 4): (544, 576),
        ("PAL", 5): (480, 576),
    }

    @classmethod
    def from_bytes(cls, data: bytes, offset: int = 0) -> _IFOVideoAttrs:
        b0 = data[offset]
        b1 = data[offset + 1] if offset + 1 < len(data) else 0
        mpeg_version = cls.MPEG_MAP.get((b0 >> 6) & 3, "unknown")
        standard = cls.STANDARD_MAP.get((b0 >> 4) & 3, "unknown")
        aspect_ratio = cls.ASPECT_MAP.get((b0 >> 2) & 3, "unknown")
        letterboxed = bool(b0 & 0b10)
        film_mode = bool(b1 & 0b1)
        res_idx = (b1 >> 3) & 0b111
        resolution = cls.RES_MAP.get((standard, res_idx))
        cc_field_1 = bool((b1 >> 7) & 0b1)
        cc_field_2 = bool((b1 >> 6) & 0b1)
        return cls(
            mpeg_version=mpeg_version,
            standard=standard,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            letterboxed=letterboxed,
            film_mode=film_mode,
            cc_field_1=cc_field_1,
            cc_field_2=cc_field_2,
        )


@dataclass(slots=True)
class _IFOAudioAttrs:
    """DVD VTS audio attributes (8 bytes per entry from VTS_A_ATR).

    Based on dvdutils' AudioAttrs.
    """

    codec: str
    channels: int
    sample_rate: str
    quantization: str
    bits_per_sample: int | None
    dsur: bool
    code_extension: int
    lang_code: str

    @property
    def is_commentary(self) -> bool:
        """Whether this audio track is a commentary track.

        Checked via the code_extension / application byte (byte 5 in the
        VTS_A_ATR entry).  Value ranges vary by codec; common indicators
        are values >= 2 (visually impaired, director's comments, etc.).
        """
        return self.code_extension >= 2

    CODEC_MAP: ClassVar[dict[int, tuple[str, str]]] = {
        0: ("AC3", "ac3"),
        1: ("MPEG Audio", "mp2"),
        2: ("MPEG Audio", "mp2"),
        3: ("LPCM", "pcm_s16be"),
        4: ("DTS", "dts"),
        5: ("SDDS", "pcm_s16be"),
        6: ("DTS", "dts"),
        7: ("DTS", "dts"),
    }
    CHANNEL_MAP: ClassVar[dict[int, int]] = {
        0: 1,
        1: 2,
        2: 3,
        3: 4,
        4: 5,
        5: 6,
        6: 7,
        7: 8,
    }

    @classmethod
    def from_bytes(cls, data: bytes, offset: int = 0) -> _IFOAudioAttrs:
        b0 = data[offset] if offset < len(data) else 0
        b1 = data[offset + 1] if offset + 1 < len(data) else 0
        b5 = data[offset + 5] if offset + 5 < len(data) else 0
        lang_raw = data[offset + 2 : offset + 4] if offset + 4 < len(data) else b""

        codec_idx = (b0 >> 5) & 7
        codec_name, _ = cls.CODEC_MAP.get(codec_idx, ("Unknown", ""))
        channels = cls.CHANNEL_MAP.get(b1 & 7, 0)
        dsur = bool(b1 & 0x08)
        bits_val = (b1 >> 4) & 3
        quantization = {0: "16-bit", 1: "20-bit", 2: "24-bit", 3: "DRC"}.get(
            bits_val, "unknown"
        )
        bits_per_sample = {0: 16, 1: 20, 2: 24}.get(bits_val)
        code_extension = b5
        sample_rate = "48 kHz"  # DVD audio is always 48 kHz
        try:
            lang_code = lang_raw.decode("ascii", errors="replace")
        except Exception:
            lang_code = ""
        return cls(
            codec=codec_name,
            channels=channels,
            sample_rate=sample_rate,
            quantization=quantization,
            bits_per_sample=bits_per_sample,
            dsur=dsur,
            code_extension=code_extension,
            lang_code=lang_code,
        )


@dataclass(slots=True)
class _IFOSubpictureAttrs:
    """DVD VTS subpicture attributes (6 bytes per entry from VTS_SPST_ATRT).

    Based on dvdutils' SubpictureAttrs.
    """

    coding_mode: str
    code_extension: int
    lang_code: str
    is_hearing_impaired: bool

    @property
    def code_extension_label(self) -> str:
        return _IFO_SUBP_CODE_EXTENSION_MAP.get(
            self.code_extension, f"unknown ({self.code_extension})"
        )

    @property
    def is_forced(self) -> bool:
        return self.code_extension == 9

    @property
    def is_commentary(self) -> bool:
        """Director's comments / commentary (code_extension 13-15)."""
        return self.code_extension in (13, 14, 15)

    @classmethod
    def from_bytes(cls, data: bytes, offset: int = 0) -> _IFOSubpictureAttrs:
        b0 = data[offset] if offset < len(data) else 0
        lang_raw = data[offset + 2 : offset + 4] if offset + 4 < len(data) else b""
        coding_idx = (b0 >> 5) & 7
        coding_mode = {
            0: "run-length",
            1: "extended",
            2: "reserved",
            3: "line-21 CC",
        }.get(coding_idx, "unknown")
        # Byte 0, bit 0: subpicture type (0=normal, 1=hearing impaired)
        is_hi = bool(b0 & 0x01)
        code_extension = data[offset + 5] if offset + 5 < len(data) else 0
        try:
            lang_code = lang_raw.decode("ascii", errors="replace")
        except Exception:
            lang_code = ""
        return cls(
            coding_mode=coding_mode,
            code_extension=code_extension,
            lang_code=lang_code,
            is_hearing_impaired=is_hi,
        )


class DvdIfoError(Exception):
    """Raised when DVD IFO data is structurally invalid (wrong ident, truncated, corrupt table)."""


# =============================================================================
# Language maps and code-extension tables
# =============================================================================

_LANG_MAP_3_TO_2 = {
    "eng": "en",
    "fra": "fr",
    "fre": "fr",
    "spa": "es",
    "ger": "de",
    "deu": "de",
    "ita": "it",
    "por": "pt",
    "jpn": "ja",
    "kor": "ko",
    "zho": "zh",
    "chi": "zh",
    "ara": "ar",
    "rus": "ru",
    "nld": "nl",
    "dut": "nl",
    "swe": "sv",
    "dan": "da",
    "nor": "no",
    "fin": "fi",
    "pol": "pl",
    "ces": "cs",
    "cze": "cs",
    "hun": "hu",
    "tur": "tr",
    "tha": "th",
    "vie": "vi",
    "ind": "id",
}


# Subpicture code extension — byte 5 of each 6-byte SPST entry at VTSI_MAT+0x256.
# See dvdutils SubpictureCodeExtension and the DVD-Video specification.
# Value 9 means the subtitle stream should be flagged as "forced".
_IFO_SUBP_CODE_EXTENSION_MAP: dict[int, str] = {
    0: "unspecified",
    1: "normal",
    2: "large",
    3: "children",
    5: "captions",
    6: "large captions",
    7: "children captions",
    9: "forced",
    13: "director comments",
    14: "large director comments",
    15: "director comments for children",
}

# Audio code extension — byte 5 of each 8-byte AST entry at VTSI_MAT+0x204.
# See dvdutils AudioCodeExtension and the DVD-Video specification.
# Value 3 means the audio track is a Director's Commentary.
_IFO_AUDIO_CODE_EXTENSION_MAP: dict[int, str] = {
    0: "unspecified",
    1: "normal",
    2: "for visually impaired",
    3: "commentary",
    4: "alternate commentary",
}

# Channel count -> human label, matching what MakeMKV writes for audio.
#
# BD's CLPI channelConfiguration field uses a coarse discrete set
# (mono / dual-mono / 2.0 / 3.0 / 4.0 / 5.1 / 6.1 / 7.1) with NO 5.0 code, so a
# genuine 5.0 mix gets rounded up to the 5.1 config and an over-counted
# channel count. These labels therefore key on the ACTUAL stream channel count
# (as reported by mkvmerge -J, which decodes the codec headers), not the CLPI
# config. For BD's standard configurations the count is unambiguous: 5 channels
# can only be 5.0 (BD defines no 4.1 config), 6 -> 5.1, 7 -> 6.1, 8 -> 7.1.
_AUDIO_CHANNEL_TITLES = {
    1: "Mono",
    2: "Stereo",
    3: "Surround 3.0",
    4: "Surround 4.0",
    5: "Surround 5.0",
    6: "Surround 5.1",
    7: "Surround 6.1",
    8: "Surround 7.1",
}


# =============================================================================
# VTSI_MAT offsets / sector pointers
# =============================================================================
#
# VTSI_MAT offsets for DVD .IFO stream-attribute tables and sector pointers.
# DVDs store audio/subtitle languages and codec info here; ffprobe on a raw
# .VOB cannot recover them, so we read the matching VTS_*_0.IFO instead.
#
# VTS_C_ADT (Cell Address Table) at sector pointer 0xE0 maps each cell's
# VOB_ID/Cell_ID to its sector range, enabling IFO-based trimming without
# scanning the VOBs via ffprobe.
_VTS_IFO_IDENT = b"DVDVIDEO-VTS"
# VTS_V_ATR (video attributes) at 0x0200 (2 bytes)
_VTS_IFO_VIDEO_ATTR = 0x0200
# VTS_AST_Ns (audio stream count) at 0x0202-0x0203 (u16)
_VTS_IFO_AUDIO_COUNT = 0x0202
_VTS_IFO_AUDIO_COUNT_BYTE = (
    0x0203  # Low byte of the u16 count (fallback on malformed high byte)
)
_VTS_IFO_AUDIO_ATTR = 0x0204  # up to 8 entries, 8 bytes each
_VTS_IFO_AUDIO_ENTRY_LEN = 8
_VTS_IFO_SUBP_COUNT = 0x0254
_VTS_IFO_SUBP_ATTR = 0x0256  # up to 32 entries, 6 bytes each
_VTS_IFO_SUBP_ENTRY_LEN = 6
# Sector pointers in VTSI_MAT (4-byte sector numbers)
_VTS_PTR_PTT_SRPT = 0xC8  # VTS_PTT_SRPT (Title/Chapter -> PGC map)
_VTS_PTR_VOB_START = 0xC4  # VTS title VOB start sector (relative to IFO)
_VTS_PTR_PGCIT = 0xCC  # VTS_PGCIT
_VTS_PTR_C_ADT = 0xE0  # VTS_C_ADT (Cell Address Table)
_VTS_PTR_VOBU_ADMAP = 0xE4  # VTS_VOBU_ADMAP (VOBU Address Map)


# VTS VOBU Address Map entry: 4 bytes per VOBU start sector
_VTS_VOBU_ADMAP_ENTRY_LEN = 4


# VTS_C_ADT entry: 12 bytes × n cells
# VOB_ID(2) + Reserved(1) + Cell_ID(1) + StartSector(4) + EndSector(4)
_VTS_C_ADT_ENTRY_LEN = 12


# =============================================================================
# VMG IFO (VIDEO_TS.IFO) constants
# =============================================================================
#
# the VMG (Video Manager) carries
# the Title Search Pointer Table (TT_SRPT), Provider ID, and Text Data
# (VMG_TXTDT_MG) containing the disc volume name in multiple languages.
# See http://www.mpucoder.com/DVD/ifo.html and the DVD-Video spec.
_VMG_IFO_IDENT = b"DVDVIDEO-VMG"
# Provider ID at offset 0x0040 (32 bytes, null-padded ISO 646 string).
# This identifies the authoring tool or studio.
_VMG_PROVIDER_ID = 0x0040
_VMG_PROVIDER_ID_LEN = 32
# Sector pointers in VMGI_MAT (4-byte sector numbers, relative to IFO start)
_VMG_PTR_TT_SRPT = 0xC4  # Title Search Pointer Table
_VMG_PTR_TXTDT_MG = 0xD4  # Text Data Management Area (disc name)
# Each TT_SRPT entry: 2 B title_type + 2 B VTS_TTN + 8 B reserved (12 B total)
_VMG_TT_SRPT_ENTRY_LEN = 12
_VMG_TT_SRPT_ENTRY = struct.Struct(">HH8x")  # title_type(2) + vts_ttn(2) + reserved(8)

# Character coding values for VMG text data entries.
_VMG_CHAR_ISO_8859_1 = 0x00
_VMG_CHAR_UNICODE = 0x01


# =============================================================================
# Cell playback / position constants
# =============================================================================

# DVD Program Chain (PGC) chapter parsing.
#
# DVD chapters == PGC programs. The VTS Program Chain Information Table
# (VTS_PGCIT) sits at the sector given by the 4-byte sector offset stored at
# VTSI_MAT+0xCC. Inside, each PGC records how many programs/cells it has, a
# program->cell map, and a per-cell playback-time table. Summing cell durations
# up to each program boundary yields the chapter start times. All PGC constant
# values are defined as class attributes on ``_PGCOffset``.
#
# Cell type in bits 6-7 of the first byte of each CellPlaybackInfo entry.
# 0 = normal (sequential), 1 = first of angle block, 2 = middle of angle block,
# 3 = last of angle block. Only normal cells and first-of-angle-block cells
# contribute to cumulative playback time; middle/last angle-block cells are
# alternative camera angles playing concurrently with the first cell.
# See http://www.mpucoder.com/DVD/cell-pbi.html and dvdutils CellPlaybackInfo.
_CELL_TYPE_NORMAL = 0
_CELL_TYPE_FIRST_ANGLE = 1


# VTS VOBU Address Map struct: 4-byte VOBU start sectors (big-endian, packed).
_VTS_VOBU_ADMAP_ENTRY = struct.Struct(">I")


# Cell position info entry in a PGC: VOB_ID(2:16-bit BE) + Reserved(1) + Cell_ID(1)
_CELL_POS_ENTRY = struct.Struct(">HxB")

# Cell playback info sector fields, per libdvdread's cell_playback_t:
#   +0x08: first_sector             (first VOBU of the cell)
#   +0x0C: first_ilvu_end_sector    (end of first interleaved unit only —
#                                     NOT the cell's end; only meaningful for
#                                     interleaved/seamless-branching cells)
#   +0x10: last_vobu_start_sector
#   +0x14: last_sector              (the actual end sector of the cell)
#
# We want first_sector and last_sector (NOT first_ilvu_end_sector, which a
# previous version of this code mistakenly read as "last VOBU" — it produced
# wrong/misleading values specifically on interleaved seamless-branching
# cells since first_ilvu_end_sector is unrelated to the cell's true end).
_CELL_PB_FIRST_SECTOR_OFF = 0x08
_CELL_PB_LAST_SECTOR_OFF = 0x14


# =============================================================================
# BCD decode helpers
# =============================================================================


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


def _bcd_playback_seconds(data: bytes, off: int) -> float:
    """Decode a DVD 4-byte BCD playback time (HH MM SS FF) into seconds.

    The high two bits of the last byte select the frame rate: 1 = 25 fps
    (PAL), 3 = 30000/1001 fps (NTSC). Returns 0.0 if the bytes are not a
    plausible time.
    """
    if off + 4 > len(data):
        return 0.0
    hh = _bcd(data[off])
    mm = _bcd(data[off + 1])
    ss = _bcd(data[off + 2])
    frame_byte = data[off + 3]
    fps_code = (frame_byte >> 6) & 0x03
    fps = {0x01: Fraction(25, 1), 0x03: Fraction(30000, 1001)}.get(
        fps_code, Fraction(0, 1)
    )
    if hh > 23 or mm > 59 or ss > 59:
        return 0.0
    secs = Fraction(hh * 3600 + mm * 60 + ss, 1)
    if fps:
        secs += Fraction(_bcd(frame_byte & 0x3F), 1) / fps
    return float(secs)


# =============================================================================
# DVD colour / palette
# =============================================================================


def _ycbcr_to_rgb(y: int, cb: int, cr: int) -> tuple[int, int, int]:
    """Convert YCbCr (CCIR-601 studio swing) to RGB using BT.601.

    DVD PGC palette entries are stored in CCIR-601 range where luma Y is
    [16, 235] and chroma Cb/Cr is [16, 240].  We subtract the footroom
    (16) and scale by the inverse of the active range (255/219 for Y,
    255/224 for Cb/Cr) to recover full [0, 255] RGB.
    """
    # Remove studio-black footroom and scale CCIR range to full range.
    # Integer coefficients: Y*298/256 ≈ Y*(255/219), Cr*409/256 ≈ Cr*(255/224)*1.402, etc.
    y = max(0, y - 16)
    cb, cr = cb - 128, cr - 128
    r = (y * 298 + cr * 409 + 128) // 256
    g = (y * 298 - cb * 100 - cr * 208 + 128) // 256
    b = (y * 298 + cb * 516 + 128) // 256
    return (
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
    )


# =============================================================================
# VMG IFO parsing
# =============================================================================


def _dvd_lang_code_to_str(raw: bytes) -> str:
    """Convert a 2-byte DVD compressed language code to ISO 639-1 string.

    The DVD format stores languages as two bytes where each byte equals
    0x60 + offset from 'a'.  A value of 0x00 means 'no language'.
    Returns "??" on unrecognised input.
    """
    if len(raw) < 2 or raw == b"\x00\x00":
        return "??"
    b1, b2 = raw[0] - 0x60, raw[1] - 0x60
    if 1 <= b1 <= 26 and 1 <= b2 <= 26:
        return chr(ord("a") + b1 - 1) + chr(ord("a") + b2 - 1)
    return "??"


def _decode_vmg_text(data: bytes, char_code: int) -> str | None:
    """Decode a VMG text data entry into a Python string.

    Supports ISO 8859-1 (char_code=0x00) and Unicode UTF-16 BE (char_code=0x01).
    Returns None for unsupported encodings or empty results.
    Strips trailing null bytes and leading/trailing whitespace.
    """
    if not data:
        return None
    try:
        if char_code == _VMG_CHAR_ISO_8859_1:
            s = data.decode("latin-1", "replace")
        elif char_code == _VMG_CHAR_UNICODE:
            s = data.decode("utf-16-be", "replace")
        else:
            return None
    except Exception:
        return None
    s = s.rstrip("\x00").strip()
    return s if s else None


class VmgInfo(TypedDict, total=False):
    """Parsed VMG IFO metadata (see ``_parse_vmg_ifo``)."""

    provider_id: str
    disc_name: str
    barcode: str
    title_map: dict[int, tuple[int, int]]


def _parse_vmg_ifo(vmg_path: Path) -> VmgInfo:
    """Parse VIDEO_TS.IFO (VMG) to extract disc metadata.

    Reads the VMG IFO's Provider ID, VMG_TXTDT_MG disc name, and
    Title Search Pointer Table (TT_SRPT) to map logical titles to
    their VTS numbers.

    Returns a dict with keys:
      provider_id : str  (trimmed Provider ID string, or "")
      disc_name   : str | None  from VMG_TXTDT_MG (first non-empty text)
      barcode     : str | None  UPC/EAN barcode from VMG_TXTDT_MG
      title_map   : dict[int, tuple[int,int]]  title_idx -> (vts_num, ttl_num)

    Returns an empty dict on any error (caller should fall through
    without failing).
    """
    try:
        ifo_data = vmg_path.read_bytes()
    except Exception:
        log_debug("VMG IFO read failed")
        return {}
    if len(ifo_data) < 0x100 or ifo_data[:12] != _VMG_IFO_IDENT:
        log_debug(f"VMG IFO ident mismatch: got {ifo_data[:12]!r}")
        raise DvdIfoError(f"VMG IFO ident mismatch: got {ifo_data[:12]!r}")

    result: VmgInfo = {}

    # --- VMG_TXTDT_MG: Text Data Management Area (sector pointer at 0xD4) ---
    # Extracts both the disc name (first text entry) and UPC/EAN barcode
    # (subsequent text entries with 12-13 digit patterns).
    txtdt_sector = _read_u32(ifo_data, _VMG_PTR_TXTDT_MG)
    disc_name: str | None = None
    if txtdt_sector:
        base_off = txtdt_sector * 2048
        disc_name = _extract_vmg_disc_name(ifo_data, base_off)
        if disc_name:
            result["disc_name"] = disc_name
        barcode = _extract_vmg_barcode(ifo_data, base_off)
        if barcode:
            result["barcode"] = barcode

    # --- Provider ID (32 bytes at offset 0x0040) ---
    raw_pid = ifo_data[_VMG_PROVIDER_ID : _VMG_PROVIDER_ID + _VMG_PROVIDER_ID_LEN]
    pid = raw_pid.split(b"\x00")[0].decode("ascii", "ignore").strip()
    result["provider_id"] = pid

    # --- TT_SRPT: Title Search Pointer Table (sector pointer at 0xC4) ---
    tt_srpt_sector = _read_u32(ifo_data, _VMG_PTR_TT_SRPT)
    title_map: dict[int, tuple[int, int]] = {}
    if tt_srpt_sector:
        tt_base = tt_srpt_sector * 2048
        if tt_base + 4 <= len(ifo_data):
            n_titles = _read_u16(ifo_data, tt_base)
            for i in range(n_titles):
                entry_off = tt_base + 4 + i * _VMG_TT_SRPT_ENTRY_LEN
                if entry_off + _VMG_TT_SRPT_ENTRY_LEN > len(ifo_data):
                    break
                title_type, vts_ttn = _VMG_TT_SRPT_ENTRY.unpack_from(
                    ifo_data, entry_off
                )
                vts_num = vts_ttn >> 8  # bits 15-8 = VTS number (1-99)
                if vts_num > 0:
                    title_map[i + 1] = (vts_num, vts_ttn & 0xFF)
    result["title_map"] = title_map

    log_debug(
        f"VMG IFO: provider='{pid}' disc_name={disc_name} titles={len(title_map)}"
    )
    return result


def _extract_vmg_text_strings(ifo_data: bytes, base_off: int) -> list[str]:
    """Extract all text strings from the VMG TXTDT area.

    Returns every non-empty text string found across all language blocks,
    ordered by (priority, appearance). The first entry is typically the
    disc/volume name; subsequent entries may include a UPC/EAN barcode.
    """
    if base_off + 2 > len(ifo_data):
        return []
    n_lang = _read_u16(ifo_data, base_off)
    if n_lang == 0:
        return []

    off = base_off + 2
    all_strings: list[tuple[int, str]] = []  # (priority, text)

    for _ in range(n_lang):
        if off + 4 > len(ifo_data):
            break
        lang_code = _dvd_lang_code_to_str(ifo_data[off : off + 2])
        n_str = _read_u16(ifo_data, off + 2)
        off += 4
        for _ in range(n_str):
            if off + 4 > len(ifo_data):
                break
            char_code = _read_u16(ifo_data, off)
            str_len = _read_u16(ifo_data, off + 2)
            off += 4
            if off + str_len > len(ifo_data):
                break
            raw = ifo_data[off : off + str_len]
            off += str_len
            text = _decode_vmg_text(raw, char_code)
            if text:
                priority = (2 if lang_code == "en" else 0) + (
                    1 if char_code == _VMG_CHAR_UNICODE else 0
                )
                all_strings.append((priority, text))

    if not all_strings:
        return []
    all_strings.sort(key=lambda x: -x[0])
    return [t for _, t in all_strings]


def _extract_vmg_disc_name(ifo_data: bytes, base_off: int) -> str | None:
    """Extract the disc volume name from VMG_TXTDT_MG.

    Uses ``_extract_vmg_text_strings`` and returns the first non-empty
    text entry (the disc/volume name), preferring Unicode entries over
    ISO 8859-1 and English over other languages.
    """
    strings = _extract_vmg_text_strings(ifo_data, base_off)
    return strings[0] if strings else None


def _extract_vmg_barcode(ifo_data: bytes, base_off: int) -> str | None:
    """Extract a UPC/EAN barcode from VMG_TXTDT_MG.

    The barcode is typically the second text entry in each language block
    (after the disc name).  Returns the first text string that looks like
    a numeric barcode (12-13 digits, optionally with dashes).
    """
    strings = _extract_vmg_text_strings(ifo_data, base_off)
    # Disc name is index 0; other entries may include the barcode.
    for s in strings[1:]:
        digits_only = re.sub(r"[^0-9]", "", s)
        if len(digits_only) in (12, 13):
            return digits_only
    return None


# =============================================================================
# VTS IFO attribute parsing
# =============================================================================


def _parse_vts_video_attrs(ifo_data: bytes) -> _IFOVideoAttrs | None:
    """Parse VTS_V_ATR (2 bytes at VTSI_MAT+0x200).

    Returns ``_IFOVideoAttrs`` or None if the data is too short.
    """
    if len(ifo_data) < _VTS_IFO_VIDEO_ATTR + 2:
        return None
    return _IFOVideoAttrs.from_bytes(ifo_data, _VTS_IFO_VIDEO_ATTR)


def _parse_vts_subp_attrs(ifo_data: bytes) -> dict[int, _IFOSubpictureAttrs]:
    """Parse subpicture stream attributes from a VTS .IFO, keyed by stream ID.

    Returns a dict like ``{0x20: _IFOSubpictureAttrs(...), ...}``.
    Use ``attrs[sid].is_forced`` and ``attrs[sid].code_extension_label``
    for commonly needed derived values.
    Returns an empty dict on errors or invalid data.
    """
    if len(ifo_data) < _VTS_IFO_SUBP_ATTR + _VTS_IFO_SUBP_ENTRY_LEN:
        return {}
    n_sub = _read_u16(ifo_data, _VTS_IFO_SUBP_COUNT)
    if n_sub == 0 or n_sub > 32:
        return {}
    attrs: dict[int, _IFOSubpictureAttrs] = {}
    for i in range(n_sub):
        off = _VTS_IFO_SUBP_ATTR + i * _VTS_IFO_SUBP_ENTRY_LEN
        if off + _VTS_IFO_SUBP_ENTRY_LEN > len(ifo_data):
            break
        attrs[0x20 + i] = _IFOSubpictureAttrs.from_bytes(ifo_data, off)
    return attrs


def _read_vts_audio_count(ifo_data: bytes) -> int:
    """Read the VTS audio stream count from the IFO.

    Per the DVD-Video specification (and libdvdread / mpucoder), the audio
    stream count is a u16 at offset ``0x0202`` in VTSI_MAT (``VTS_AST_Ns``), with
    the 8-byte attribute entries starting at ``0x0204``. Offset ``0x0200`` is
    actually ``VTS_V_ATR`` (video attributes), NOT the audio count.

    However, some authoring tools write the count at ``0x0202`` as a u16 whose
    high byte is zero (or garbage) and low byte is the actual count. We also
    check the single byte at ``0x0203`` (the loop count low byte) as a fallback
    for discs where the high byte is nonsensical.

    Returns 0 on any error or invalid data.
    """
    if len(ifo_data) < _VTS_IFO_AUDIO_ATTR + 8:
        return 0
    raw = _read_u16(ifo_data, _VTS_IFO_AUDIO_COUNT)
    if 1 <= raw <= 8:
        log_debug(f"Audio count at 0x{_VTS_IFO_AUDIO_COUNT:04X} (u16): {raw}")
        return raw
    # Some authoring tools place the count at 0x0202 with a corrupted high byte,
    # but the low byte at 0x0203 is correct.
    byte_val = (
        ifo_data[_VTS_IFO_AUDIO_COUNT_BYTE]
        if _VTS_IFO_AUDIO_COUNT_BYTE < len(ifo_data)
        else 0
    )
    if 1 <= byte_val <= 8:
        log_debug(
            f"Audio count at 0x{_VTS_IFO_AUDIO_COUNT_BYTE:04X} (byte): {byte_val} (u16 at 0x{_VTS_IFO_AUDIO_COUNT:04X} was {raw})"
        )
        return byte_val
    log_debug(
        f"Audio count not found at 0x{_VTS_IFO_AUDIO_COUNT:04X} (u16: {raw}) or 0x{_VTS_IFO_AUDIO_COUNT_BYTE:04X} (byte: {byte_val})"
    )
    return 0


def _parse_vts_ifo_languages(
    ifo_data: bytes,
) -> tuple[dict[int, str], dict[int, str]]:
    """Extract audio/subtitle languages from a VTS .IFO, keyed by stream ID.

    Returns ``(audio_by_id, sub_by_id)`` mapping the MPEG program-stream
    sub-stream ID (audio ``0x80``-``0x87``, subpicture ``0x20``-``0x3F``) to a
    2-char ISO 639-1 language code. Keying by ID is essential because ffmpeg
    enumerates PS streams by first packet appearance, not by ID, so positional
    order is unreliable. Language codes that are unset (all-zero) or non-ASCII
    are reported as "und". Returns empty dicts for an invalid VTS IFO.
    """
    if len(ifo_data) < 4 or ifo_data[0:12] != _VTS_IFO_IDENT:
        return {}, {}

    def _lang(off: int) -> str:
        if off + 2 > len(ifo_data):
            return "und"
        raw = ifo_data[off : off + 2]
        if raw == b"\x00\x00" or not all(97 <= b <= 122 or 65 <= b <= 90 for b in raw):
            return "und"
        return raw.decode("ascii", "ignore")

    audio_by_id: dict[int, str] = {}
    n_audio = min(_read_vts_audio_count(ifo_data), 8)
    for i in range(n_audio):
        audio_by_id[0x80 + i] = _lang(
            _VTS_IFO_AUDIO_ATTR + i * _VTS_IFO_AUDIO_ENTRY_LEN + 2
        )

    sub_by_id: dict[int, str] = {}
    n_sub = min(_read_u16(ifo_data, _VTS_IFO_SUBP_COUNT), 32)
    for i in range(n_sub):
        sub_by_id[0x20 + i] = _lang(
            _VTS_IFO_SUBP_ATTR + i * _VTS_IFO_SUBP_ENTRY_LEN + 2
        )

    return audio_by_id, sub_by_id


def _parse_vts_audio_attrs(ifo_data: bytes) -> dict[int, _IFOAudioAttrs]:
    """Parse VTS IFO audio stream attributes, keyed by sub-stream ID.

    Returns a dict like ``{0x80: _IFOAudioAttrs(...), ...}``.
    Returns empty dict on errors.
    """
    if len(ifo_data) < _VTS_IFO_AUDIO_ATTR + 8:
        return {}
    n_audio = _read_vts_audio_count(ifo_data)
    if n_audio == 0:
        log_debug("Skipping IFO audio attributes (no valid count found)")
        return {}
    attrs: dict[int, _IFOAudioAttrs] = {}
    for i in range(n_audio):
        off = _VTS_IFO_AUDIO_ATTR + i * _VTS_IFO_AUDIO_ENTRY_LEN
        if off + 8 > len(ifo_data):
            break
        parsed = _IFOAudioAttrs.from_bytes(ifo_data, off)
        attrs[0x80 + i] = parsed
        bps_str = f"{parsed.bits_per_sample}bps" if parsed.bits_per_sample else "DRC"
        ext_label = _IFO_AUDIO_CODE_EXTENSION_MAP.get(
            parsed.code_extension, f"unknown ({parsed.code_extension})"
        )
        log_debug(
            f"    Audio {0x80 + i:#x}: {parsed.codec} {parsed.channels}ch {bps_str} ext={ext_label}"
        )
    log_debug(f"_parse_vts_audio_attrs result: {attrs}")
    return attrs


def _ifo_audio_title(attrs: _IFOAudioAttrs | None) -> str | None:
    """Build a human-readable audio track title from IFO attributes.

    Examples: ``"AC3 5.1"``, ``"DTS Dolby Surround"``, ``"LPCM 2.0"``.
    Returns None when the channel count is unknown or zero.
    """
    if attrs is None or attrs.channels <= 0:
        return None
    codec = attrs.codec
    channels = attrs.channels
    dsur = attrs.dsur
    if channels == 1:
        ch_str = "1.0"
    elif channels == 2:
        ch_str = "Dolby Surround" if dsur else "2.0"
    elif channels == 6:
        ch_str = "5.1"
    elif channels % 2 == 0:
        ch_str = f"{channels - 1}.1"
    else:
        ch_str = f"{channels}.0"
    title = f"{codec} {ch_str}"
    # Append code extension label (e.g. "(commentary)") when it adds value.
    ext_label = _IFO_AUDIO_CODE_EXTENSION_MAP.get(attrs.code_extension, "")
    if ext_label not in ("", "unspecified", "normal"):
        title += f" ({ext_label})"
    return title


# =============================================================================
# PGC enumeration and selection
# =============================================================================


def _vts_ttn1_pgc_abs(ifo_data: bytes) -> int | None:
    """Return the absolute offset of the PGC used by VTS_TTN 1 (chapter 1).

    Parses VTS_PTT_SRPT (Title/Chapter -> PGC map) to find the PGC number
    that VTS_TTN 1's first PTT (chapter) uses. This is the DVD's own
    authoritative, spec-defined "default title" designation for a VTS -
    exactly what real DVD players and MakeMKV use to pick the primary
    edition for a title set. This must take priority over any
    duration-based heuristic: on discs with multiple seamless-branching
    editions (e.g. a theatrical cut plus a longer "Special Edition" cut
    sharing footage via interleaved cells), the bonus/extended edition can
    have a *longer* declared PGC duration than the actual default/theatrical
    title, which would cause a "pick the longest PGC" heuristic to silently
    select the wrong edition.

    Returns None on any parse failure (callers fall back to the
    duration-based heuristic, which is still needed for menu PGCs and other
    non-title contexts that have no VTS_TTN of their own).
    """
    if len(ifo_data) < _VTS_PTR_PTT_SRPT + 4:
        return None
    srpt_sector = _read_u32(ifo_data, _VTS_PTR_PTT_SRPT)
    if srpt_sector == 0:
        return None
    srpt_base = srpt_sector * 2048
    if srpt_base + 8 > len(ifo_data):
        return None
    nr_of_srpts = _read_u16(ifo_data, srpt_base)
    if nr_of_srpts < 1:
        return None
    # First 4-byte TTU offset (relative to srpt_base) is VTS_TTN 1.
    ttu_off_pos = srpt_base + 8
    if ttu_off_pos + 4 > len(ifo_data):
        return None
    ttu_off = _read_u32(ifo_data, ttu_off_pos)
    ttu_abs = srpt_base + ttu_off
    if ttu_abs + 4 > len(ifo_data):
        return None
    nr_of_ptts = _read_u16(ifo_data, ttu_abs)
    if nr_of_ptts < 1:
        return None
    # First PTT (chapter 1): ptt_info_t { pgcn: u16, pgn: u16 }.
    ptt_off = ttu_abs + 2
    if ptt_off + 4 > len(ifo_data):
        return None
    pgcn = _read_u16(ifo_data, ptt_off)
    if pgcn < 1:
        return None

    pgcit_sector = _read_u32(ifo_data, _VTS_PTR_PGCIT)
    if pgcit_sector == 0:
        return None
    pgcit_base = pgcit_sector * 2048
    entry = pgcit_base + 8 + (pgcn - 1) * 8
    if entry + 8 > len(ifo_data):
        return None
    pgc_off = _read_u32(ifo_data, entry + 4) & 0x7FFFFFFF
    pgc_abs = pgcit_base + pgc_off
    if pgc_abs + 8 > len(ifo_data):
        return None
    return pgc_abs


def _enumerate_vts_pgcs(ifo_data: bytes) -> list[tuple[int, int, float, int]]:
    """Enumerate every Program Chain in a VTS's VTS_PGCIT.

    Returns a list of ``(pgc_number, pgc_abs_offset, duration_seconds,
    num_cells)`` tuples, one per PGC, in VTS_PGCIT order. ``pgc_number`` is
    the 1-indexed PGC number as referenced by VTS_PTT_SRPT's ``pgcn`` field
    (i.e. ``i + 1`` for the i-th entry). Returns an empty list on any parse
    failure.

    This is the shared enumeration used both to pick a single "main" PGC
    (see ``_find_main_pgc``) and to discover *other* substantial PGCs on
    seamless-branching discs, so each alternate edition can be exposed as
    its own rippable title.
    """
    if len(ifo_data) < 0x200 or ifo_data[:12] != _VTS_IFO_IDENT:
        return []
    pgcit_sector = _read_u32(ifo_data, _VTS_PTR_PGCIT)
    if pgcit_sector == 0:
        return []
    pgcit_base = pgcit_sector * 2048
    if pgcit_base + 8 > len(ifo_data):
        return []
    nb_pgci = _read_u16(ifo_data, pgcit_base)
    if nb_pgci < 1:
        return []

    result: list[tuple[int, int, float, int]] = []
    for i in range(nb_pgci):
        entry = pgcit_base + 8 + i * 8
        if entry + 8 > len(ifo_data):
            break
        pgc_off = _read_u32(ifo_data, entry + 4) & 0x7FFFFFFF
        pgc_abs = pgcit_base + pgc_off
        if pgc_abs + 8 > len(ifo_data):
            continue
        duration = _bcd_playback_seconds(ifo_data, pgc_abs + _PGCOffset.PLAYBACK_TIME)
        n_cells = (
            ifo_data[pgc_abs + _PGCOffset.NB_CELLS]
            if pgc_abs + 4 <= len(ifo_data)
            else 0
        )
        result.append((i + 1, pgc_abs, duration, n_cells))
    return result


def _find_main_pgc(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[int, float, int] | None:
    """Find a Program Chain in a VTS IFO.

    When ``pgc_number`` is given (1-indexed, matching VTS_PTT_SRPT's
    ``pgcn`` field), that exact PGC is returned directly - used to rip a
    specific alternate edition on seamless-branching discs (see
    ``_enumerate_vts_pgcs``).

    Otherwise, prefers the PGC that VTS_TTN 1 (the disc's own default title
    designation) actually uses, per VTS_PTT_SRPT - this is the
    spec-authoritative way to identify "the movie" for a title set and
    correctly handles discs with multiple seamless-branching editions where
    a bonus/extended cut has a longer declared duration than the default
    title (a pure "longest PGC" heuristic would pick the wrong one there).

    Falls back to scanning every PGC in the VTS_PGCIT and returning the one
    with the longest playback duration (cell count as tiebreaker) when the
    VTS_TTN 1 lookup fails - e.g. for VMGM/menu-domain IFOs that have no
    VTS_PTT_SRPT of their own.

    Returns ``(pgc_abs_offset, duration_seconds, num_cells)`` or None when the
    IFO is malformed, the PGCIT is missing, or no valid PGC is found.

    This helper eliminates duplicated PGC-selection logic across chapter parsing,
    active-stream detection, PGC language extraction, and byte-range lookup,
    ensuring all four pick the *same* PGC.
    """
    if len(ifo_data) < 0x200 or ifo_data[:12] != _VTS_IFO_IDENT:
        return None

    if pgc_number is not None:
        for num, pgc_abs, duration, n_cells in _enumerate_vts_pgcs(ifo_data):
            if num == pgc_number:
                return pgc_abs, duration, n_cells
        return None

    ttn1_pgc_abs = _vts_ttn1_pgc_abs(ifo_data)
    if ttn1_pgc_abs is not None and ttn1_pgc_abs + 4 <= len(ifo_data):
        duration = _bcd_playback_seconds(
            ifo_data, ttn1_pgc_abs + _PGCOffset.PLAYBACK_TIME
        )
        n_cells = ifo_data[ttn1_pgc_abs + _PGCOffset.NB_CELLS]
        if n_cells > 0:
            return ttn1_pgc_abs, duration, n_cells

    best_pgc_abs: int | None = None
    best_duration = 0.0
    best_cells = 0
    for _num, pgc_abs, duration, n_cells in _enumerate_vts_pgcs(ifo_data):
        # Primary criterion: duration. Tiebreaker: cell count.
        if duration > best_duration or (
            duration == best_duration and n_cells > best_cells
        ):
            best_duration = duration
            best_cells = n_cells
            best_pgc_abs = pgc_abs

    if best_pgc_abs is None:
        return None
    return best_pgc_abs, best_duration, best_cells


def _pgc_cell_position_signature(
    ifo_data: bytes,
    pgc_abs: int,
    n_cells: int,
) -> tuple[tuple[int, int], ...] | None:
    """Build a comparable signature of a PGC's cell position info table.

    Returns a tuple of (vob_id, cell_id) pairs in cell-table order, or None
    if the table can't be parsed. Used to detect duplicate PGCs that
    reference the same cells (common on discs using GPRM-based branching
    where multiple PGCs share the same cell table).
    """
    if pgc_abs + _PGCOffset.CELL_POSITION_INFO_TABLE_OFFSET + 2 > len(ifo_data):
        return None
    pos_off = _read_u16(ifo_data, pgc_abs + _PGCOffset.CELL_POSITION_INFO_TABLE_OFFSET)
    if not pos_off:
        return None
    pos_base = pgc_abs + pos_off
    sig: list[tuple[int, int]] = []
    for i in range(n_cells):
        off = pos_base + i * _CELL_POS_ENTRY.size
        if off + _CELL_POS_ENTRY.size > len(ifo_data):
            break
        vob_id, cell_id = _CELL_POS_ENTRY.unpack_from(ifo_data, off)
        sig.append((vob_id, cell_id))
    return tuple(sig)


def _find_alternate_edition_pgcs(
    ifo_data: bytes,
    min_duration: float = 60.0,
) -> list[int]:
    """Return 1-indexed PGC numbers for substantial PGCs other than the
    disc's default title PGC (VTS_TTN 1).

    Used to expose additional editions on seamless-branching discs (e.g. a
    theatrical cut plus one or more longer bonus/extended cuts sharing
    footage) as their own separate, independently rippable titles - matching
    how MakeMKV lists each edition as its own title rather than collapsing
    them into one.

    A PGC qualifies as an "alternate edition" when its own declared playback
    duration is at least ``min_duration`` (so menu loops, thumbnail/
    link PGCs, etc. are excluded) and it isn't the same PGC already used by
    the default title.

    PGCs are not de-duplicated even if they have identical cell position
    info — they may use different angles (via SetSTN pre-commands) which
    select different cells within the same interleaved blocks.

    Returns an empty list if the IFO has only one substantial PGC (the
    common, non-branching case) or cannot be parsed. Results are sorted by
    PGC number.
    """
    default = _find_main_pgc(ifo_data)
    default_abs = default[0] if default else None
    extras: list[int] = []
    for num, pgc_abs, duration, n_cells in _enumerate_vts_pgcs(ifo_data):
        if pgc_abs == default_abs:
            continue
        if duration < min_duration or n_cells < 1:
            continue
        extras.append(num)
    return sorted(extras)


def _default_pgc_number(ifo_data: bytes) -> int | None:
    """Return the 1-indexed PGC number used by the disc's default title.

    Maps the PGC offset returned by ``_find_main_pgc`` (which prefers
    VTS_TTN 1) back to a PGC number in the VTS_PGCIT. Returns None on any
    parse failure.
    """
    main = _find_main_pgc(ifo_data)
    if main is None:
        return None
    default_abs = main[0]
    for num, pgc_abs, _dur, _cells in _enumerate_vts_pgcs(ifo_data):
        if pgc_abs == default_abs:
            return num
    return None


# Relative duration tolerance for grouping PGCs into an episode cluster.
# Episodes of a TV series are typically within a few percent of each other;
# 15% is generous enough to absorb intro/outro variation while still
# separating a 22-minute episode from a 40-minute documentary on the same disc.
_EPISODE_DURATION_TOL = 0.15


def _detect_episode_pgcs(
    ifo_data: bytes,
    min_duration: float = 60.0,
) -> tuple[list[int], int | None]:
    """Detect TV-series episode PGCs within a single VTS.

    Returns ``(episode_pgc_numbers, play_all_pgc_number)``:
    - ``episode_pgc_numbers``: sorted list of 1-indexed PGC numbers for the
      detected episodes, or ``[]`` when no episode pattern is found.
    - ``play_all_pgc_number``: 1-indexed PGC number of the "play all" chain
      (a PGC whose duration ≈ the sum of all episodes), or ``None``.

    Two signals are required:

    1. **Duration clustering** — at least two substantial PGCs whose playback
       durations fall within ``_EPISODE_DURATION_TOL`` (15 %) of each other.
       Episodes on a TV-series disc are authored to a near-constant length
       (e.g. 8 × ~22 min), so a tight cluster of same-length PGCs is a strong
       series signal.
    2. **Distinct cell tables** — every episode PGC must reference a different
       set of ``(vob_id, cell_id)`` pairs. This distinguishes episodes from
       seamless-branching *editions* of the same movie, which share the same
       cell table (the PGCs differ only in angle commands or cell ordering, not
       in which physical cells they point at). Without this check a
       multi-angle disc (e.g. Beauty and the Beast SE) would be misdetected as
       a "series".

    The "play all" PGC — common on TV-series discs — is then identified by
    matching its duration against the sum of episode durations (within 5 %).
    """
    all_pgcs = _enumerate_vts_pgcs(ifo_data)
    substantial = [p for p in all_pgcs if p[2] >= min_duration and p[3] >= 1]
    if len(substantial) < 2:
        return [], None

    # --- Duration clustering ---
    # Pick the PGC whose duration neighbourhood is the largest; all PGCs
    # within tolerance of it form the candidate episode set. Ties favour the
    # shorter centre so that, if equal-size clusters exist at different
    # lengths, the episode-length group wins over a group of long extras.
    best_cluster: list[tuple[int, int, float, int]] = []
    for p in substantial:
        dur = p[2]
        neighbours = [
            q
            for q in substantial
            if abs(q[2] - dur) / max(q[2], dur, 1.0) <= _EPISODE_DURATION_TOL
        ]
        if len(neighbours) > len(best_cluster) or (
            len(neighbours) == len(best_cluster)
            and best_cluster
            and dur < best_cluster[0][2]
        ):
            best_cluster = neighbours

    if len(best_cluster) < 2:
        return [], None

    # --- Cell-table verification ---
    # Episode PGCs must have distinct cell-position signatures. Identical
    # signatures mean the PGCs share the same physical cells (seamless
    # branching / multi-angle), not separate episode footage.
    sigs: list[tuple[tuple[int, int], ...]] = []
    for num, pgc_abs, _dur, n_cells in best_cluster:
        sig = _pgc_cell_position_signature(ifo_data, pgc_abs, n_cells)
        if sig is None:
            # Can't verify — be conservative and keep duration as the sole
            # signal for this PGC by skipping its signature.
            continue
        sigs.append(sig)
    if sigs and len(set(sigs)) != len(sigs):
        # At least two PGCs share identical cell tables → branching, not series.
        return [], None

    episode_nums = sorted(p[0] for p in best_cluster)

    # --- Play-all detection ---
    # A non-episode PGC whose duration is close to the sum of all episodes.
    ep_total = sum(p[2] for p in best_cluster)
    play_all: int | None = None
    ep_set = set(episode_nums)
    for num, _pgc_abs, dur, _cells in all_pgcs:
        if num in ep_set:
            continue
        if dur < min_duration:
            continue
        if ep_total > 0 and abs(dur - ep_total) / ep_total <= 0.05:
            play_all = num
            break

    return episode_nums, play_all


# =============================================================================
# Chapter / duration parsing
# =============================================================================


def _pgc_chapters_and_duration(
    ifo_data: bytes, pgc_abs: int
) -> tuple[list[float], float]:
    """Return (chapter start times, total duration) for one PGC.

    Uses the per-program cell iteration approach (inspired by pyparsedvd's
    VTS_PGCI parser): for each program, read the program map to find the entry
    cell and exit cell, then sum the durations of the cells in that range.
    This is more spec-compliant than iterating all cells linearly and matching
    against a set of program-start cells, because it correctly skips orphan
    cells that don't belong to any program and handles degenerate program maps.

    Only normal cells (cell_type 0) and first-of-angle-block cells (cell_type 1)
    advance the timeline. Both represent sequentially-playing cells: type 0 is a
    standard sequential cell, while type 1 is the default view of a multi-angle
    block. Middle/last angle-block cells (types 2/3) are alternative camera
    angles that play concurrently rather than sequentially and are excluded.

    Returns ([], 0.0) if the PGC structure is malformed.
    """
    if pgc_abs + 0xEA > len(ifo_data):
        return [], 0.0
    n_programs = ifo_data[pgc_abs + _PGCOffset.NB_PROGRAMS]
    n_cells = ifo_data[pgc_abs + _PGCOffset.NB_CELLS]
    if not (0 < n_programs <= n_cells <= 255):
        return [], 0.0
    prog_map = pgc_abs + _read_u16(ifo_data, pgc_abs + _PGCOffset.PROGRAM_MAP_OFFSET)
    cell_table = pgc_abs + _read_u16(
        ifo_data, pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET
    )
    if prog_map + n_programs > len(ifo_data):
        return [], 0.0
    if cell_table + n_cells * _PGCOffset.CELL_PLAYBACK_INFO_LEN > len(ifo_data):
        return [], 0.0

    # Detect angle from pre-commands for angle-aware cell duration filtering.
    pgc_angle = _pgc_angle_from_commands(ifo_data, pgc_abs)
    angle_idx = pgc_angle - 1  # 0-based position within block

    chapters: list[float] = []
    prog_durations: list[float] = []
    cumulative = 0.0
    # Iterate per-program: each program maps to a cell range [entry, exit].
    # entry_cell = program_map[program]; exit_cell = program_map[program+1] - 1
    # (or n_cells for the last program). A chapter boundary is placed at the
    # cumulative time at the start of each program.
    for program in range(n_programs):
        entry_cell = ifo_data[prog_map + program]
        if program < n_programs - 1:
            exit_cell = ifo_data[prog_map + program + 1] - 1
        else:
            exit_cell = n_cells
        if not (1 <= entry_cell <= exit_cell <= n_cells):
            continue
        chapters.append(round(cumulative, 3))
        prog_start = cumulative
        # Walk cells, tracking interleaved blocks for angle selection.
        _chap_block: list[int] | None = None
        for cell in range(entry_cell, exit_cell + 1):
            cell_base = cell_table + (cell - 1) * _PGCOffset.CELL_PLAYBACK_INFO_LEN
            cell_type = (ifo_data[cell_base] >> 6) & 0x03
            if cell_type == 0:
                # Normal cell
                _chap_block = None
                cumulative += _bcd_playback_seconds(
                    ifo_data, cell_base + _PGCOffset.CELL_DURATION_OFFSET
                )
            elif cell_type == 1:
                _chap_block = [cell]
            elif cell_type in (2, 3):
                if _chap_block is None:
                    _chap_block = []
                _chap_block.append(cell)
                if cell_type == 3:
                    # End of block - select angle-appropriate cell
                    sel = (
                        _chap_block[min(angle_idx, len(_chap_block) - 1)]
                        if _chap_block
                        else cell
                    )
                    sel_base = (
                        cell_table + (sel - 1) * _PGCOffset.CELL_PLAYBACK_INFO_LEN
                    )
                    cumulative += _bcd_playback_seconds(
                        ifo_data, sel_base + _PGCOffset.CELL_DURATION_OFFSET
                    )
                    _chap_block = None
        prog_durations.append(cumulative - prog_start)
    # Drop degenerate trailing chapters (sub-second programs, e.g. per-chapter
    # thumbnail/keyframe cells used by a scene-selection menu that technically
    # belong to this PGC but aren't meant to be played as movie content - see
    # the matching trim in _build_main_edition_vobu_ranges). Keep popping from
    # the end while the trailing program is tiny, then snap the reported total
    # duration back to the last real chapter's end so it matches the actual
    # (trimmed) muxed content instead of including the dropped tail.
    _MIN_REAL_PROGRAM_SECONDS = 10.0
    while len(chapters) > 1 and prog_durations[-1] < _MIN_REAL_PROGRAM_SECONDS:
        chapters.pop()
        prog_durations.pop()
        cumulative = chapters[-1] + prog_durations[-1] if prog_durations else cumulative
    return chapters, cumulative


def _parse_vts_pgc_chapters(ifo_data: bytes) -> list[float]:
    """Extract chapter start times from a VTS .IFO's main PGC.

    A VTS can hold several Program Chains (a real title plus short menu/filler
    PGCs); we pick the longest one - the actual content - rather than blindly
    using PGC 0. Returns [] if no usable PGC is found.
    """
    chapters, _ = _parse_vts_pgc_info(ifo_data)
    return chapters


def _parse_vts_pgc_info(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[list[float], float]:
    """Return (chapters, total duration) for a PGC in a VTS .IFO.

    Used to recover authoritative chapter timings and runtime - ffprobe on raw
    VOBs reports wrong durations once timestamps wrap across VOB parts, and only
    sees the first VOB. Returns ([], 0.0) on any parse failure.

    ``pgc_number`` selects a specific PGC (1-indexed, see
    ``_enumerate_vts_pgcs``) instead of the default title's PGC - used to
    rip an alternate edition on seamless-branching discs.
    """
    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return [], 0.0
    pgc_abs, pgc_dur, _ = main
    chapters, dur = _pgc_chapters_and_duration(ifo_data, pgc_abs)
    if dur <= 0 and pgc_dur > 0:
        # Fallback: use PGC playback time when cell-based computation fails.
        # This can happen on seamless branching discs where angle blocks
        # cause the cell-type filtering to drop too many cells, or when
        # the program/cell count structure is misread by the parser.
        return [], pgc_dur
    return chapters, dur


# =============================================================================
# PGC stream control
# =============================================================================


def _get_active_pgc_streams(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[set[int], set[int]]:
    """Determine which audio/subpicture stream IDs are active in the longest PGC.

    Reads the PGC's stream control tables to find which audio (0x80+) and
    subpicture (0x20+) streams are actually used by the main PGC. Streams that
    exist in the VTS attribute table but aren't active in the PGC belong to
    other PGCs (menus, extras) and should not be included in the title.

    Per the DVD-Video specification (http://www.mpucoder.com/DVD/pgc.html),
    the PGC header always has 8 audio-stream-control and 32 subpicture-stream-
    control entries. In simplified mode (category bit 1 = 0) the entries are
    inline; in offset mode the fields are u16 pointers to external tables.

    Returns ``(audio_ids, sub_ids)`` as sets of sub-stream IDs.
    Returns empty sets on any error (caller falls back to all VTS streams).
    """
    audio_active: set[int] = set()
    sub_active: set[int] = set()

    if len(ifo_data) < 0x200 or ifo_data[:12] != _VTS_IFO_IDENT:
        return audio_active, sub_active

    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return audio_active, sub_active
    pgc_abs = main[0]

    pgc_category = _read_u16(ifo_data, pgc_abs)
    offset_mode = bool(pgc_category & 0x0002)

    # --- Audio Stream Control ---
    if offset_mode:
        # Offset mode: PGC+0x0C holds a u16 pointer to the ASCT.
        # Each ASCT entry is 8 bytes: stream_num (u16), lang (2B), type (2B), ext (2B).
        # stream_num bit 15 = available flag, low bits = stream number (0-7).
        asct_off = (
            _read_u16(ifo_data, pgc_abs + _PGCOffset.AST_CTL)
            if pgc_abs + _PGCOffset.AST_CTL + 2 <= len(ifo_data)
            else 0
        )
        if asct_off:
            asct_base = pgc_abs + asct_off
            n_vts_audio = min(
                _read_vts_audio_count(ifo_data), _PGCOffset.NUM_AST_ENTRIES
            )
            for i in range(n_vts_audio):
                off = asct_base + i * _PGCOffset.AST_NORMAL_ENTRY_LEN
                if off + 6 > len(ifo_data):
                    break
                stream_num = _read_u16(ifo_data, off)
                # In offset mode bit 15 indicates availability; 0xFFFF = no stream
                if stream_num != 0xFFFF:
                    audio_active.add(0x80 + (stream_num & 0x7FFF))
    else:
        # Simplified mode: inline AST at PGC+0x0C, 8 entries of 2 bytes each.
        # First byte: bits 0-2 = stream number, bit 7 = available flag.
        ast_base = pgc_abs + _PGCOffset.AST_CTL
        for i in range(_PGCOffset.NUM_AST_ENTRIES):
            off = ast_base + i * 2
            if off + 2 > len(ifo_data):
                break
            b0 = ifo_data[off]
            available = bool(b0 & 0x80)
            stream_num = b0 & 0x07
            if available and stream_num != 0x07:
                audio_active.add(0x80 + stream_num)

    # --- Subpicture Stream Control ---
    # Always 32 entries of 4 bytes. Each entry's first byte:
    #   bits 0-4 = stream number (for 4:3 display)
    #   bit 7    = stream available flag
    # In simplified mode the entries are inline at PGC+0x1C.
    # In offset mode PGC+0x1C is a u16 pointer to the SPST base.
    if offset_mode:
        spst_off = (
            _read_u16(ifo_data, pgc_abs + _PGCOffset.SPST_CTL)
            if pgc_abs + _PGCOffset.SPST_CTL + 2 <= len(ifo_data)
            else 0
        )
        spst_base = pgc_abs + spst_off if spst_off else 0
    else:
        spst_base = pgc_abs + _PGCOffset.SPST_CTL  # inline at PGC+0x1C

    if spst_base:
        for i in range(_PGCOffset.NUM_SPST_ENTRIES):
            entry_off = spst_base + i * _PGCOffset.SPST_ENTRY_LEN
            if entry_off + 4 > len(ifo_data):
                break
            b0 = ifo_data[entry_off]
            available = bool(b0 & 0x80)
            stream_num = b0 & 0x1F  # bits 0-4
            if available and stream_num != 0x1F:
                sub_active.add(0x20 + stream_num)

    return audio_active, sub_active


def _parse_pgc_stream_languages(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Extract audio/subpicture language codes from the longest PGC's stream control tables.

    The PGC's Subpicture Stream Control Table (SPSCT) and (in offset mode) Audio
    Stream Control Table (ASCT) can carry per-PGC language codes that differ from
    the global VTS attribute table. Some discs store correct per-PGC language
    metadata here even when the VTS attribute table has placeholder values.

    When a language code is non-zero, it overrides the VTS attribute table value.
    Returns ``(audio_by_id, sub_by_id)`` keyed by MPEG sub-stream ID
    (audio 0x80-0x87, subpicture 0x20-0x3F). Empty dicts on failure.
    """
    if len(ifo_data) < 0x200 or ifo_data[0:12] != _VTS_IFO_IDENT:
        return {}, {}
    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return {}, {}
    pgc_abs = main[0]

    # Determine stream control table mode from PGC category bit 1.
    pgc_category = _read_u16(ifo_data, pgc_abs)
    offset_mode = bool(pgc_category & 0x0002)

    audio_by_id: dict[int, str] = {}
    sub_by_id: dict[int, str] = {}

    def _extract_lang(off: int) -> str | None:
        if off + 2 > len(ifo_data):
            return None
        raw = ifo_data[off : off + 2]
        if raw == b"\x00\x00" or not all(97 <= b <= 122 or 65 <= b <= 90 for b in raw):
            return None
        return raw.decode("ascii", "ignore")

    # --- Audio Stream Control Table (language codes only in offset mode) ---
    if offset_mode:
        asct_off = (
            _read_u16(ifo_data, pgc_abs + _PGCOffset.AST_CTL)
            if pgc_abs + _PGCOffset.AST_CTL + 2 <= len(ifo_data)
            else 0
        )
        if asct_off:
            asct_base = pgc_abs + asct_off
            n_audio = min(_read_vts_audio_count(ifo_data), _PGCOffset.NUM_AST_ENTRIES)
            for i in range(n_audio):
                off = asct_base + i * _PGCOffset.AST_NORMAL_ENTRY_LEN
                if off + 6 > len(ifo_data):
                    break
                stream_num = _read_u16(ifo_data, off)
                if stream_num == 0xFFFF or not (stream_num & 0x8000):
                    continue
                # Bit 15 = available; low bits = stream number (0-7)
                actual_stream = stream_num & 0x7FFF
                lang = _extract_lang(off + 2)
                if lang:
                    audio_by_id[0x80 + actual_stream] = lang

    # --- Subpicture Stream Control Table ---
    # Language codes at offset+2 are only valid in offset mode. In
    # simplified mode the 4-byte inline entries at PGC+0x1C have NO
    # language codes — byte2 is the stream number for letterbox display
    # and byte3 for pan/scan. See dvdutils_vts SubpictureStreamControl
    # and http://www.mpucoder.com/DVD/pgc.html
    if offset_mode:
        spst_off = (
            _read_u16(ifo_data, pgc_abs + _PGCOffset.SPST_CTL)
            if pgc_abs + _PGCOffset.SPST_CTL + 2 <= len(ifo_data)
            else 0
        )
        if spst_off:
            spst_base = pgc_abs + spst_off
            for i in range(_PGCOffset.NUM_SPST_ENTRIES):
                entry_off = spst_base + i * _PGCOffset.SPST_ENTRY_LEN
                if entry_off + 4 > len(ifo_data):
                    break
                stream_num = _read_u16(ifo_data, entry_off)
                # Bit 15 = available flag. 0xFFFF = no stream.
                if stream_num == 0xFFFF or not (stream_num & 0x8000):
                    continue
                actual_stream = stream_num & 0x7FFF
                if actual_stream > 0x1F:
                    continue
                lang = _extract_lang(entry_off + 2)
                if lang:
                    sub_by_id[0x20 + actual_stream] = lang
    # In simplified mode the inline SPST entries carry only display-
    # format stream number assignments (4:3/wide/letterbox/pan_scan)
    # with no language codes. VTS attribute table languages are used.

    return audio_by_id, sub_by_id


# =============================================================================
# VM command parsing (angle detection)
# =============================================================================


def _pgc_angle_from_commands(ifo_data: bytes, pgc_abs: int) -> int:
    """Detect which angle a PGC selects by parsing its pre-commands.

    On seamless-branching discs that use multi-angle blocks (e.g. Beauty and
    the Beast SE), different editions share the same PGC cell table but set
    different angles via SetSTN pre-commands. Angle 1 selects block_mode=1
    cells, Angle 2 selects block_mode=2 cells, etc.

    Returns the angle number (1-based), or 1 if no SetSTN angle command is
    found (the default/standard angle).
    """
    if pgc_abs + _PGCOffset.COMMANDS_OFFSET + 2 > len(ifo_data):
        log_debug(f"    _pgc_angle: PGC offset {pgc_abs:#x} out of bounds for commands")
        return 1
    cmd_tbl_off = _read_u16(ifo_data, pgc_abs + _PGCOffset.COMMANDS_OFFSET)
    log_debug(
        f"    _pgc_angle: pgc_abs={pgc_abs:#x} cmd_tbl_off=0x{cmd_tbl_off:x} (at PGC+0x{_PGCOffset.COMMANDS_OFFSET:X})"
    )
    if cmd_tbl_off == 0:
        log_debug("    _pgc_angle: command table offset is 0, no commands")
        return 1
    cmd_base = pgc_abs + cmd_tbl_off
    if cmd_base + 6 > len(ifo_data):
        log_debug(f"    _pgc_angle: cmd_base {cmd_base:#x} out of bounds")
        return 1
    nr_pre = _read_u16(ifo_data, cmd_base) & 0x3F
    nr_post = _read_u16(ifo_data, cmd_base + 2) & 0x3F
    nr_cell = _read_u16(ifo_data, cmd_base + 4) & 0x3F
    log_debug(f"    _pgc_angle: nr_pre={nr_pre} nr_post={nr_post} nr_cell={nr_cell}")
    # Pre-commands start after the 8-byte header (nr_pre, nr_post,
    # nr_cell as 3x u16, plus a 2-byte reserved/next-command field).
    pre_start = cmd_base + 8
    for i in range(nr_pre):
        off = pre_start + i * 8
        if off + 8 > len(ifo_data):
            break
        cmd = ifo_data[off]
        # Dump first 8 and any SetSTN commands
        if i < 8 or cmd in (0x51, 0x41):
            b = ifo_data[off : off + 8]
            log_debug(
                f"    _pgc_angle: pre[{i}] off=0x{off:x} "
                f"cmd=0x{cmd:02x} bytes={b.hex(' ')}"
            )
        # SetSTN command: byte 0 = 0x51 (direct) or 0x41 (via GPRM)
        if cmd in (0x51, 0x41):
            angle_byte = ifo_data[off + 5]
            log_debug(
                f"    _pgc_angle: pre[{i}] cmd=0x{cmd:02x} "
                f"bytes=[{ifo_data[off]:02x} {ifo_data[off + 1]:02x} "
                f"{ifo_data[off + 2]:02x} {ifo_data[off + 3]:02x} "
                f"{ifo_data[off + 4]:02x} {ifo_data[off + 5]:02x} "
                f"{ifo_data[off + 6]:02x} {ifo_data[off + 7]:02x}] "
                f"angle_byte=0x{angle_byte:02x}"
            )
            if angle_byte & 0x80:
                angle = angle_byte & 0x7F
                if angle > 0:
                    log_debug(f"    _pgc_angle: detected Angle {angle}")
                    return angle
    log_debug("    _pgc_angle: no SetSTN angle command found, returning 1")
    return 1


# =============================================================================
# Cell / VOBU range computation
# =============================================================================


class CadtCell(TypedDict):
    """One VTS_C_ADT cell entry (libdvdread c_adt_t / mpucoder)."""

    vob_id: int
    cell_id: int
    start_sector: int
    end_sector: int


def _parse_vts_c_adt(ifo_data: bytes) -> list[CadtCell]:
    """Parse the VTS Cell Address Table into a list of cell entries.

    Each entry from VTS_C_ADT (at sector pointer 0xE0 in VTSI_MAT):
      - vob_id: VOB identifier (1-based within the VTS)
      - cell_id: cell number within that VOB
      - start_sector: first LBA of the cell (relative to VTS VOB area start)
      - end_sector: last LBA of the cell

    Returns an empty list if the C_ADT is missing, truncated, or the sector
    pointer is zero. Callers should fall back to PTS-based trimming.
    """
    if len(ifo_data) < _VTS_PTR_C_ADT + 4:
        return []
    sect_ptr = _read_u32(ifo_data, _VTS_PTR_C_ADT)
    if sect_ptr == 0:
        return []
    base = sect_ptr * 2048
    if base + 8 > len(ifo_data):
        return []
    # VTS_C_ADT starts with a 4-byte end address (relative to IFO start).
    # On some discs end_addr is corrupt and points far past the real table.
    # Strategy: scan forward from the entry area and stop at the first
    # all-zero 12-byte entry (the table is always zero-padded to its end).
    end_addr = _read_u32(ifo_data, base)
    log_debug(
        f"VTS_C_ADT: sect_ptr={sect_ptr} base={base:#x} end_addr=0x{end_addr:08x}"
    )

    cells: list[CadtCell] = []
    entry_base = base + 4
    max_bytes = len(ifo_data) - entry_base
    max_slots = max_bytes // _VTS_C_ADT_ENTRY_LEN
    if max_slots <= 0:
        return []

    # Determine candidate slot count from end_addr if available.
    n_addr_slots = 0
    if end_addr > base + 4:
        n_addr_slots = (end_addr - base - 4) // _VTS_C_ADT_ENTRY_LEN
    n_entries = min(max_slots, max(n_addr_slots, max_slots))

    # Scan for the first all-zero entry (real end of C_ADT).
    real_n = 0
    for i in range(n_entries):
        off = entry_base + i * _VTS_C_ADT_ENTRY_LEN
        if off + _VTS_C_ADT_ENTRY_LEN > len(ifo_data):
            break
        # Check if this 12-byte entry is entirely zero-filled.
        is_zero = True
        for b in ifo_data[off : off + _VTS_C_ADT_ENTRY_LEN]:
            if b != 0:
                is_zero = False
                break
        if is_zero:
            break
        real_n = i + 1

    log_debug(
        f"VTS_C_ADT: {real_n} raw entries (end_addr suggests {n_addr_slots}, IFO caps at {max_slots})"
    )
    if real_n == 0:
        return []

    for i in range(real_n):
        off = entry_base + i * _VTS_C_ADT_ENTRY_LEN
        if off + _VTS_C_ADT_ENTRY_LEN > len(ifo_data):
            break
        vob_id = _read_u16(ifo_data, off)
        cell_id = ifo_data[off + 2]  # byte 2 = Cell ID (per libdvdread / mpucoder)
        start_sector = _read_u32(ifo_data, off + 4)
        end_sector = _read_u32(ifo_data, off + 8)
        # Skip clearly invalid entries (common when end_addr is corrupt).
        if vob_id == 0 or cell_id == 0:
            continue
        if start_sector >= end_sector:
            continue
        cells.append(
            {
                "vob_id": vob_id,
                "cell_id": cell_id,
                "start_sector": start_sector,
                "end_sector": end_sector,
            }
        )
    log_debug(f"VTS_C_ADT: {len(cells)} valid cell entries (out of {real_n} raw)")
    if cells:
        log_debug(
            "  C_ADT sample: first 3 entries: "
            + ", ".join(
                f"VOB={c['vob_id']} Cell={c['cell_id']} sectors={c['start_sector']}-{c['end_sector']}"
                for c in cells[:3]
            )
        )
        log_debug(
            "  C_ADT sample: last 3 entries: "
            + ", ".join(
                f"VOB={c['vob_id']} Cell={c['cell_id']} sectors={c['start_sector']}-{c['end_sector']}"
                for c in cells[-3:]
            )
        )
    return cells


def _parse_vts_vobu_admap(ifo_data: bytes) -> list[int] | None:
    """Parse the VTS VOBU Address Map into a sorted list of VOBU start sectors.

    VOBU_ADMAP provides the starting sector (relative to VTS VOB area) of every
    VOBU in the VTS. The table sits at the sector given by the 4-byte offset at
    VTSI_MAT+0xE4.  Each entry is a 4-byte big-endian VOBU start sector.

    Returns a sorted list of VOBU start sectors, or None on any error.
    """
    if len(ifo_data) < _VTS_PTR_VOBU_ADMAP + 4:
        return None
    sect_ptr = _read_u32(ifo_data, _VTS_PTR_VOBU_ADMAP)
    if sect_ptr == 0:
        return None
    base = sect_ptr * 2048
    if base + 8 > len(ifo_data):
        return None
    # VTS_VOBU_ADMAP starts with a 4-byte end address (relative to IFO start).
    end_addr = _read_u32(ifo_data, base)
    log_debug(
        f"VTS_VOBU_ADMAP: sect_ptr={sect_ptr} base={base:#x} end_addr=0x{end_addr:08x}"
    )

    entry_base = base + 4
    max_bytes = len(ifo_data) - entry_base
    max_entries = max_bytes // _VTS_VOBU_ADMAP_ENTRY_LEN
    if max_entries <= 0:
        return None

    # Determine entry count from end_addr if available.
    n_addr_entries = 0
    if end_addr > base + 4:
        n_addr_entries = (end_addr - base - 4) // _VTS_VOBU_ADMAP_ENTRY_LEN
    n_entries = min(max_entries, max(n_addr_entries, max_entries))

    # Scan for the first all-zero entry (real end of VOBU_ADMAP).
    vobus: list[int] = []
    for i in range(n_entries):
        off = entry_base + i * _VTS_VOBU_ADMAP_ENTRY_LEN
        if off + _VTS_VOBU_ADMAP_ENTRY_LEN > len(ifo_data):
            break
        (vobu_start,) = _VTS_VOBU_ADMAP_ENTRY.unpack_from(ifo_data, off)
        if vobu_start == 0:
            # VOBU_ADMAP should not contain zero entries, but some discs
            # pad with zeros at the end.  A leading zero is valid when the
            # first VOBU starts at sector 0 (relative to VTS VOB area).
            # Peek at the next entry to distinguish leading-zero from
            # trailing-padding: if the next entry is non-zero, the current
            # zero entry is a legitimate VOBU at sector 0.
            if i == 0:
                peek_off = off + _VTS_VOBU_ADMAP_ENTRY_LEN
                nbytes = peek_off + _VTS_VOBU_ADMAP_ENTRY_LEN
                if nbytes <= len(ifo_data):
                    (next_vobu,) = _VTS_VOBU_ADMAP_ENTRY.unpack_from(ifo_data, peek_off)
                    if next_vobu != 0:
                        vobus.append(vobu_start)
                        continue
            break
        vobus.append(vobu_start)

    log_debug(f"VTS_VOBU_ADMAP: {len(vobus)} VOBU entries")
    if vobus:
        log_debug(f"  VOBU_ADMAP range: sectors {vobus[0]}-{vobus[-1]}")
    return vobus or None


def _vobu_end_byte(vobu_sector: int, vobu_admap: list[int]) -> int:
    """Return the byte offset after the last sector of the given VOBU.

    Uses the next VOBU start sector in the admap as the boundary, or
    falls back to (vobu_sector + 1) * 2048 for the last entry.
    """
    for i, vs in enumerate(vobu_admap):
        if vs == vobu_sector:
            if i + 1 < len(vobu_admap):
                return vobu_admap[i + 1] * 2048
            break
    return (vobu_sector + 1) * 2048


def _read_nav_ids_from_sector(sector: bytes) -> tuple[int, int] | None:
    """Parse (vob_id, cell_id) from a NAV pack's DSI packet.

    Every VOBU begins with a NAV pack containing a PCI (Presentation Control
    Info) and a DSI (Data Search Information) private_stream_2 (0xBF) PES
    packet. The DSI's dsi_gi_t always records vobu_vob_idn/vobu_c_idn - the
    VOB_ID and Cell_ID that this VOBU's own data actually belongs to.

    On seamless-branching (interleaved) discs, this is the only reliable way
    to tell which edition a given VOBU belongs to: an interleaved cell's
    IFO-reported first_sector/last_sector spans the *entire* interleaved
    block shared by all editions, not just the portion belonging to one
    PGC. This mirrors what a real DVD player (and MakeMKV's "complex
    multiplex" scan) does at playback time.

    Returns ``(vob_id, cell_id)``, or None if no DSI packet is found (e.g. a
    corrupt or non-NAV sector).
    """
    idx = 0
    n = len(sector)
    while idx + 7 <= n:
        pos = sector.find(b"\x00\x00\x01\xbf", idx)
        if pos == -1 or pos + 7 > n:
            return None
        pes_len = (sector[pos + 4] << 8) | sector[pos + 5]
        substream_id = sector[pos + 6]
        if substream_id == 0x01:
            # dsi_gi_t: nv_pck_scr(4) + nv_pck_lbn(4) + vobu_ea(4) +
            # vobu_1stref_ea(4) + vobu_2ndref_ea(4) + vobu_3rdref_ea(4) = 24,
            # then vobu_vob_idn(u16) at +24, zero1(u8) at +26,
            # vobu_c_idn(u8) at +27.
            dsi_start = pos + 7
            if dsi_start + 28 > n:
                return None
            vob_idn = (sector[dsi_start + 24] << 8) | sector[dsi_start + 25]
            cell_idn = sector[dsi_start + 27]
            return vob_idn, cell_idn
        idx = pos + 6 + max(pes_len, 1)
    return None


def _scan_vobu_cell_ids(
    inputs: list[Path], vobu_sectors: list[int]
) -> dict[int, tuple[int, int]]:
    """Read the NAV pack (vob_id, cell_id) identity for each given VOBU sector.

    Opens each backing file once and seeks per sector rather than reopening
    per VOBU, since this may run for thousands of VOBUs on discs with
    seamless branching.
    """
    results: dict[int, tuple[int, int]] = {}
    layout = _concat_file_layout(inputs)
    handles: dict[Path, Any] = {}
    try:
        for sector in vobu_sectors:
            offset = sector * 2048
            for f, fstart, fend in layout:
                if fstart <= offset < fend:
                    fh = handles.get(f)
                    if fh is None:
                        fh = open(f, "rb")
                        handles[f] = fh
                    fh.seek(offset - fstart)
                    data = fh.read(2048)
                    ids = _read_nav_ids_from_sector(data)
                    if ids is not None:
                        results[sector] = ids
                    break
    finally:
        for fh in handles.values():
            fh.close()
    return results


def _build_main_edition_vobu_ranges(
    ifo_data: bytes,
    vobu_admap: list[int],
    inputs: list[Path],
    pgc_number: int | None = None,
) -> list[tuple[int, int]] | None:
    """Build (start_byte, end_byte) VOBU ranges for the main PGC's own edition.

    On seamless-branching discs (e.g. multiple parallel cuts of a film
    sharing common footage), several editions are physically interleaved at
    the VOBU level within a single VTS. A cell that is part of an
    interleaved block (CellPlaybackInfo block_mode != 0) records
    first_sector/last_sector spanning the *entire* interleaved block (all
    editions), not just the data belonging to our PGC.

    This determines cell ownership the same way a real DVD player (and
    MakeMKV) does: every VOBU's own NAV pack (DSI) records the exact
    (VOB_ID, Cell_ID) its data belongs to. We read the main PGC's Cell
    Position Info table to get the (VOB_ID, Cell_ID) pairs that belong to
    *our* PGC, then scan each VOBU inside the PGC's overall sector span and
    keep only the ones whose own NAV pack matches one of those pairs.

    When none of the PGC's cells are part of an interleaved block (the
    common, non-branching case), this skips the expensive VOBU-by-VOBU scan
    and just returns each cell's own contiguous sector range directly.

    Returns a list of ``(start_byte, end_byte)`` tuples, or None on any
    parse failure (callers fall back to the contiguous byte-range).
    """
    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return None
    pgc_abs, _, n_cells = main
    if n_cells < 1:
        return None

    pb_off_raw = (
        _read_u16(ifo_data, pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET)
        if pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET + 2 <= len(ifo_data)
        else 0
    )
    if not pb_off_raw:
        return None
    pb_base = pgc_abs + pb_off_raw
    if pb_base + n_cells * _PGCOffset.CELL_PLAYBACK_INFO_LEN > len(ifo_data):
        return None

    pos_off = (
        _read_u16(ifo_data, pgc_abs + _PGCOffset.CELL_POSITION_INFO_TABLE_OFFSET)
        if pgc_abs + _PGCOffset.CELL_POSITION_INFO_TABLE_OFFSET + 2 <= len(ifo_data)
        else 0
    )
    if not pos_off:
        return None
    pos_base = pgc_abs + pos_off

    # Detect which angle this PGC selects via its pre-commands.
    # On multi-angle seamless-branching discs, different editions share the
    # same PGC cell table but set different angles. Angle N selects the Nth
    # cell within each interleaved (angle) block.
    pgc_angle = _pgc_angle_from_commands(ifo_data, pgc_abs)
    if pgc_angle != 1:
        log_debug(f"  PGC uses Angle {pgc_angle}")
    # Angle is 1-based; we'll select the (angle-1)-th cell in each block.
    angle_idx = pgc_angle - 1

    # Gather each cell's block_mode, sector range, duration, and (vob_id, cell_id).
    # Walk the cell table, collecting interleaved block cells and selecting
    # the angle-appropriate cell from each block.
    cells: list[dict[str, int | float]] = []
    any_interleaved = False
    current_block: list[dict[str, int | float]] = []

    def _finalize_block():
        """Select the angle-appropriate cell from a completed block."""
        nonlocal current_block
        if not current_block:
            return
        if angle_idx < len(current_block):
            cells.append(current_block[angle_idx])
        else:
            # Angle index exceeds block size; take last cell.
            cells.append(current_block[-1])
        current_block = []

    for cell_idx in range(n_cells):
        pb_off = pb_base + cell_idx * _PGCOffset.CELL_PLAYBACK_INFO_LEN
        if pb_off + _CELL_PB_LAST_SECTOR_OFF + 4 > len(ifo_data):
            break
        pos_entry_off = pos_base + cell_idx * _CELL_POS_ENTRY.size
        if pos_entry_off + _CELL_POS_ENTRY.size > len(ifo_data):
            break
        block_mode = (ifo_data[pb_off] >> 6) & 0x03
        first_sector = _read_u32(ifo_data, pb_off + _CELL_PB_FIRST_SECTOR_OFF)
        last_sector = _read_u32(ifo_data, pb_off + _CELL_PB_LAST_SECTOR_OFF)
        vob_id, cell_id = _CELL_POS_ENTRY.unpack_from(ifo_data, pos_entry_off)
        if first_sector == 0 and last_sector == 0:
            continue
        if block_mode != 0:
            any_interleaved = True
        cell_dur = _bcd_playback_seconds(
            ifo_data, pb_off + _PGCOffset.CELL_DURATION_OFFSET
        )
        cell_data = {
            "first": first_sector,
            "last": last_sector,
            "vob_id": vob_id,
            "cell_id": cell_id,
            "dur": cell_dur,
            "block_mode": block_mode,
            "cell_idx": cell_idx,
        }
        if block_mode == 0:
            # Normal cell - finalize any pending block, then include directly.
            _finalize_block()
            cells.append(cell_data)
        elif block_mode == 1:
            # Start of a new interleaved block.
            _finalize_block()
            current_block = [cell_data]
        elif block_mode in (2, 3):
            # Continuation or end of an interleaved block.
            current_block.append(cell_data)
            if block_mode == 3:
                _finalize_block()
    # Finalize any trailing block.
    _finalize_block()

    if not cells:
        return None

    # Some discs append a run of tiny (sub-second) cells after the real
    # movie content - e.g. per-chapter thumbnail/keyframe cells used by a
    # scene-selection menu, which technically belong to the main PGC's cell
    # table but are not meant to be played back-to-back with the movie.
    # Trim any such trailing run: real movie cells are essentially always
    # several seconds or longer, so a contiguous tail of sub-second cells is
    # a reliable signal of non-content data rather than a legitimate quick
    # scene cut.
    _MIN_REAL_CELL_SECONDS = 1.0
    trimmed = 0
    while len(cells) > 1 and cells[-1]["dur"] < _MIN_REAL_CELL_SECONDS:
        cells.pop()
        trimmed += 1
    if trimmed:
        log_debug(
            f"Main-edition cells: trimmed {trimmed} trailing sub-second "
            "cell(s) (thumbnail/keyframe data, not movie content)"
        )

    if not any_interleaved:
        # Fast path: no interleaving, so each cell's own range is already
        # exact and there is no need to scan individual VOBUs.
        runs: list[tuple[int, int]] = []
        for c in cells:
            c_first = int(c["first"])
            c_last = int(c["last"])
            end_byte = (
                _vobu_end_byte(c_last, vobu_admap)
                if vobu_admap
                else (c_last + 1) * 2048
            )
            runs.append((c_first * 2048, end_byte))
        log_debug(f"Main-edition ranges (no interleaving): {len(runs)} run(s)")
        return runs

    if not vobu_admap:
        return None

    # Interleaved: scan every VOBU across our PGC's overall sector span and
    # keep only the ones whose own NAV pack identifies them as belonging to
    # one of our PGC's (VOB_ID, Cell_ID) pairs.
    target = {(int(c["vob_id"]), int(c["cell_id"])) for c in cells}
    # Diagnostic: log the target cell set and block_mode distribution.
    _bm_counts: dict[int, int] = {}
    for c in cells:
        _bm_counts[int(c["block_mode"])] = _bm_counts.get(int(c["block_mode"]), 0) + 1
    log_debug(
        f"NAV scan targets: {len(target)} unique (vob_id,cell_id) pairs, "
        f"block_mode distribution: {_bm_counts}, "
        f"first 5 targets: {sorted(target)[:5]}"
    )
    # Use all gathered cells (block_mode 0 and 1) to determine the scan range.
    # Block_mode 1 cells have sector ranges spanning the entire interleaved
    # block, so their first_sector/last_sector define the bounds that contain
    # the VOBUs we need to check.
    if cells:
        lo = min(int(c["first"]) for c in cells)
        hi = max(int(c["last"]) for c in cells)
    else:
        lo = vobu_admap[0]
        hi = vobu_admap[-1]
    admap_index = {vs: i for i, vs in enumerate(vobu_admap)}
    scan_sectors = sorted(vs for vs in vobu_admap if lo <= vs <= hi)
    if not scan_sectors:
        return None

    log_debug(
        f"Seamless-branching disc detected ({len(cells)} cells, "
        f"{len(target)} unique (vob_id,cell_id) target(s)); "
        f"scanning {len(scan_sectors)} VOBU NAV packs for cell ownership..."
    )
    nav_ids = _scan_vobu_cell_ids(inputs, scan_sectors)

    # Diagnostic: count unique cell IDs found in NAV packs.
    _nav_unique: set[tuple[int, int]] = set()
    for ids in nav_ids.values():
        _nav_unique.add(ids)
    _nav_sample = sorted(_nav_unique)[:10]
    log_debug(
        f"NAV scan found {len(_nav_unique)} unique (vob_id,cell_id) "
        f"in {len(nav_ids)} VOBUs; sample: {_nav_sample}"
    )

    # Build a mapping from (vob_id, cell_id) -> cell playback order index.
    # The cells list is already in PGC cell-table order (cell_idx 0, 1, 2, ...),
    # which is the playback sequence.
    cell_order: dict[tuple[int, int], int] = {}
    for i, c in enumerate(cells):
        key = (int(c["vob_id"]), int(c["cell_id"]))
        if key not in cell_order:
            cell_order[key] = i

    # Group matched VOBUs by their cell, preserving sector order within each cell.
    # Then output cells in PGC playback order (not sector order).
    # This is critical for seamless-branching discs where interleaved VOBUs
    # from different editions are physically mixed in sector order but must
    # be extracted in cell-playback order to produce the correct movie.
    cell_vobus: dict[int, list[int]] = {}  # cell_order_idx -> [admap_indices]
    for vs in scan_sectors:
        ids = nav_ids.get(vs)
        if ids is not None and ids in target:
            co = cell_order.get(ids)
            if co is not None:
                cell_vobus.setdefault(co, []).append(admap_index[vs])

    # Build runs: iterate cells in playback order, output each cell's VOBUs
    # as one or more contiguous byte ranges.
    runs: list[tuple[int, int]] = []
    total_matched = 0
    for co in sorted(cell_vobus.keys()):
        indices = sorted(cell_vobus[co])  # sector order within cell
        total_matched += len(indices)
        # Group contiguous VOBUs within this cell into runs.
        run_start = indices[0]
        prev = indices[0]
        for idx in indices[1:]:
            if idx != prev + 1:
                runs.append(
                    (
                        vobu_admap[run_start] * 2048,
                        _vobu_end_byte(vobu_admap[prev], vobu_admap),
                    )
                )
                run_start = idx
            prev = idx
        runs.append(
            (
                vobu_admap[run_start] * 2048,
                _vobu_end_byte(vobu_admap[prev], vobu_admap),
            )
        )

    if not runs:
        log_debug("NAV scan matched 0 VOBUs against target cells; falling back")
        return None

    log_debug(
        f"Main-edition VOBU ranges via NAV scan: {len(runs)} run(s) "
        f"across {len(cell_vobus)} cells (playback-ordered), "
        f"{total_matched}/{len(scan_sectors)} VOBUs matched"
    )
    return runs


def _lookup_main_feature_range(
    ifo_data: bytes,
    vob_total_bytes: int,
    pgc_number: int | None = None,
) -> tuple[int, int] | None:
    """Determine the main feature byte range from IFO cell address tables.

    Uses the PGC cell position info table (maps PGC cells to VOB_ID/Cell_ID)
    and VTS_C_ADT (maps VOB_ID/Cell_ID to sector ranges) to find the cell
    sequence belonging to the longest (main) PGC, then converts sector addresses
    to byte offsets in the VOB area.

    Returns ``(start_byte, end_byte)`` or None when:
      - The IFO has no valid PGCIT or C_ADT
      - The cell address table is missing or malformed
      - The computed range is empty or invalid

    Callers fall back to PTS-based ffprobe scanning on None.
    """
    # 1. Parse VTS_C_ADT and build a lookup by (VOB_ID, Cell_ID).
    #    When the C_ADT is empty (e.g. seamless-branching discs where
    #    vob_id/cell_id filtering rejects all entries), fall through
    #    to the PGC cell-playback-info-table fallback below rather
    #    than returning None immediately.
    cells = _parse_vts_c_adt(ifo_data)
    if not cells:
        log_debug(
            "IFO cell trim: no cells in VTS_C_ADT, trying PGC cell playback table"
        )
    else:
        log_debug(f"IFO cell trim: {len(cells)} cells in VTS_C_ADT")
    cell_map: dict[tuple[int, int], tuple[int, int]] = {
        (c["vob_id"], c["cell_id"]): (c["start_sector"], c["end_sector"]) for c in cells
    }

    # 2. Parse VTS_VOBU_ADMAP (sector pointer 0xE4) for precise end-boundary
    #    resolution when the PGC cell playback table doesn't provide a last-VOBU.
    vobu_admap = _parse_vts_vobu_admap(ifo_data)

    # 3. Find the main PGC.
    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        log_debug("IFO cell trim: no main PGC found")
        return None
    pgc_abs = main[0]
    n_cells = main[2]
    if n_cells < 1:
        log_debug(f"IFO cell trim: main PGC has {n_cells} cells")
        return None
    # Detect angle for angle-aware cell filtering.
    angle_bm_lmr = _pgc_angle_from_commands(ifo_data, pgc_abs)
    angle_idx_lmr = angle_bm_lmr - 1  # 0-based position within block

    # 4. Read cell position info from the main PGC.
    #    Position info table offset is at PGC + 0xEA (2 bytes).
    pos_off = (
        _read_u16(ifo_data, pgc_abs + 0xEA) if pgc_abs + 0xEC <= len(ifo_data) else 0
    )
    if not pos_off:
        log_debug(
            f"IFO cell trim: cell position table offset is 0 (pgc_abs=0x{pgc_abs:x})"
        )
        return None
    pos_base = pgc_abs + pos_off

    # 5. Look up each PGC cell in the C_ADT to find the sector range.
    #    Only accept the range when ALL PGC cells are found in C_ADT.
    #    Partial matches can come from other PGCs (menus/extras) that
    #    happen to share Cell_ID values, producing an incorrect range.
    #    When matching fails, the CellPlaybackInfo fallback (below)
    #    provides the correct range from the PGC's own cell table.
    start_sector: int | None = None
    end_sector: int | None = None
    n_matched = 0
    for cell_idx in range(1, n_cells + 1):
        off = pos_base + (cell_idx - 1) * _CELL_POS_ENTRY.size
        if off + _CELL_POS_ENTRY.size > len(ifo_data):
            break
        vob_id, cell_id = _CELL_POS_ENTRY.unpack_from(ifo_data, off)
        key = (vob_id, cell_id)
        entry = cell_map.get(key)
        if entry is None:
            # Fallback: match by Cell_ID alone (discs where
            # VOB_ID differs between C_ADT and position table).
            entry = next(
                (v for k, v in cell_map.items() if k[1] == cell_id),
                None,
            )
        if entry is not None:
            n_matched += 1
            cs, ce = entry
            if start_sector is None or cs < start_sector:
                start_sector = cs
            if end_sector is None or ce > end_sector:
                end_sector = ce

    # Only use C_ADT-based range if ALL cells matched.
    # Partial matches can pick cells from unrelated PGCs.
    if n_matched < n_cells:
        log_debug(
            f"IFO cell trim: {n_matched}/{n_cells} PGC cells matched in C_ADT, "
            "falling back to PGC cell playback table"
        )
        start_sector = None
        end_sector = None

    if start_sector is None or end_sector is None or end_sector <= start_sector:
        # === Fallback: use the PGC cell playback table directly. ===
        # Each CellPlaybackInfo entry (24 B, starting at PGC+0xE8) contains
        # the first and last VOBU start sectors for that cell, which gives
        # us the exact byte range without needing the C_ADT at all.
        pb_off_raw = (
            _read_u16(ifo_data, pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET)
            if pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET + 2 <= len(ifo_data)
            else 0
        )
        if (
            pb_off_raw
            and pgc_abs + pb_off_raw + n_cells * _PGCOffset.CELL_PLAYBACK_INFO_LEN
            <= len(ifo_data)
        ):
            pb_base = pgc_abs + pb_off_raw
            pb_start: int | None = None
            pb_end: int | None = None
            last_fvobu: int | None = None
            _lmr_block: list[int] | None = None
            _lmr_sel: set[int] = set()
            for cell_idx in range(n_cells):
                off = pb_base + cell_idx * _PGCOffset.CELL_PLAYBACK_INFO_LEN
                if off + _CELL_PB_LAST_SECTOR_OFF + 4 > len(ifo_data):
                    break
                # Skip cells not belonging to this PGC's angle.
                # Track position within interleaved blocks.
                cell_type = (ifo_data[off] >> 6) & 0x03
                if cell_type == 0:
                    pass  # normal cell - always include
                elif cell_type == 1:
                    _lmr_block = [cell_idx]  # start new block
                    continue
                elif cell_type in (2, 3):
                    if _lmr_block is None:
                        _lmr_block = []
                    _lmr_block.append(cell_idx)
                    if cell_type == 3:
                        # End of block - select angle-appropriate cell
                        sel = (
                            _lmr_block[min(angle_idx_lmr, len(_lmr_block) - 1)]
                            if _lmr_block
                            else cell_idx
                        )
                        _lmr_sel.add(sel)
                        _lmr_block = None
                    continue
                if cell_idx not in _lmr_sel and cell_type != 0:
                    continue
                # First VOBU start (first_sector, 4 bytes at +8) and the
                # cell's true end (last_sector, 4 bytes at +0x14).  Do NOT
                # use +0x0C (first_ilvu_end_sector) here — that field only
                # covers the first interleaved unit and is unrelated to the
                # cell's actual end, which produced wrong ranges on this
                # seamless-branching (interleaved) disc.
                fvobu = _read_u32(ifo_data, off + _CELL_PB_FIRST_SECTOR_OFF)
                lvobu = _read_u32(ifo_data, off + _CELL_PB_LAST_SECTOR_OFF)
                if fvobu == 0 and lvobu == 0:
                    if pb_start is None:
                        # First content cell with fvobu=0: sector 0 is the start
                        # (some discs set both first/last VOBU to 0 but the
                        # cell still has content, e.g. Jackie Chan DVD).
                        pb_start = 0
                    continue
                if pb_start is None or fvobu < pb_start:
                    pb_start = fvobu
                if fvobu > 0:
                    last_fvobu = fvobu
                if lvobu > 0 and (pb_end is None or lvobu > pb_end):
                    pb_end = lvobu
            # Use VOBU_ADMAP or fallback padding to find the end boundary
            # when no cell provides a last-VOBU sector.
            if pb_end is None and last_fvobu is not None:
                # Attempt precise VOBU_ADMAP lookup first.
                if vobu_admap is not None:
                    for i, vs in enumerate(vobu_admap):
                        if vs >= last_fvobu:
                            # Next VOBU's start minus 1 is the last sector.
                            if i + 1 < len(vobu_admap):
                                pb_end = vobu_admap[i + 1] - 1
                                log_debug(
                                    "IFO cell trim: VOBU_ADMAP end after "
                                    f"sector {last_fvobu} -> next VOBU start {vobu_admap[i + 1]} -> end {pb_end}"
                                )
                            break
                if pb_end is None:
                    # Fallback padding (~300 MB) when neither lvobu nor VOBU_ADMAP
                    # provides the end boundary.
                    est_end = last_fvobu + 150000
                    vob_total_sectors = vob_total_bytes // 2048
                    pb_end = min(est_end, vob_total_sectors)
                    log_debug(
                        "IFO cell trim: PGC cell PB last_fvobu fallback "
                        f"sectors {last_fvobu}+150000 -> {pb_end}"
                    )
            if pb_start is not None and pb_end is not None and pb_end > pb_start:
                start_sector = pb_start
                end_sector = pb_end
                log_debug(
                    "IFO cell trim: PGC cell playback table fallback "
                    f"sectors {start_sector}-{end_sector} "
                    f"({(end_sector - start_sector) * 2048 / 1e9:.1f} GB)"
                )

    if start_sector is None or end_sector is None or end_sector <= start_sector:
        # Last resort: use all C_ADT entries that belong to VOB 1.
        cadt_vob1 = [c for c in cells if c["vob_id"] == 1]
        if len(cadt_vob1) >= n_cells:
            start_sector = cadt_vob1[0]["start_sector"]
            end_sector = cadt_vob1[n_cells - 1]["end_sector"]
            log_debug(
                "IFO cell trim: sequential fallback (VOB 1) "
                f"sectors {start_sector}-{end_sector}"
            )
        else:
            log_debug(
                "IFO cell trim: no cell sector range "
                f"(start={start_sector}, end={end_sector}, "
                f"cadt_vob1={len(cadt_vob1)}, need={n_cells})"
            )
            return None

    # 5. Convert sector addresses to byte offsets. C_ADT sectors are already
    #    VOB-relative (per the DVD spec / mpucoder), so we just multiply by
    #    2048 without subtracting any VTS_VOB_Start offset.
    start_byte = start_sector * 2048
    end_byte = (end_sector + 1) * 2048
    start_byte = max(0, start_byte)
    end_byte = min(end_byte, vob_total_bytes)
    if end_byte <= start_byte:
        return None
    return start_byte, end_byte


# =============================================================================
# DVD IFO palette extraction
# =============================================================================


def _extract_dvd_ifo_palette(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> list[tuple[int, int, int]] | None:
    """Extract 16-entry RGB palette from a VTS IFO's main PGC palette table.

    The DVD PGC stores a 16-entry YCbCr palette at offset 0x0A4, with each
    entry being 4 bytes, stored as a big-endian ``uint32_t`` whose byte
    layout (in file order) is:

        [zero_1 (reserved)] [Y] [Cr] [Cb]

    We skip the reserved byte, read Y/Cr/Cb, and convert to RGB using
    CCIR-601 BT.601 (studio-swing range).

    Some discs (Warner Bros., Disney, etc.) have a completely zeroed PGC
    palette (all Y=0) — these are rejected by the caller.  Discs with
    legitimate Y values produce usable colours.

    References
    ----------
    - libdvdread ``ifo_types.h``: ``pgc_t.palette[16]`` stored as big-endian
      ``uint32_t`` with comment ``{zero_1, Y, Cr, Cb}``.
    - FFmpeg ``dvdsubdec.c`` ``parse_ifo_palette``: reads Y/Cr/Cb from
      offsets 1/2/3 within each 4-byte entry.

    Returns a list of 16 ``(R, G, B)`` tuples, or ``None`` if the IFO is
    malformed or the palette cannot be found.
    """
    main_pgc = _find_main_pgc(ifo_data, pgc_number)
    if main_pgc is None:
        return None
    pgc_abs = main_pgc[0]
    pal_off = pgc_abs + _PGCOffset.PALETTE
    if pal_off + 64 > len(ifo_data):
        return None
    palette: list[tuple[int, int, int]] = []
    for i in range(16):
        # Each entry is 4 bytes: [reserved] [Y] [Cr] [Cb]
        y = ifo_data[pal_off + i * 4 + 1]
        cr = ifo_data[pal_off + i * 4 + 2]
        cb = ifo_data[pal_off + i * 4 + 3]
        palette.append(_ycbcr_to_rgb(y, cb, cr))
    return palette
