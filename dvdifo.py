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
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, ClassVar, TypedDict


# =============================================================================
# Debug logging
# =============================================================================
#
# cli.py injects RuntimeLogger.debug at startup. Until that call is made,
# debug output is suppressed.

_debug_logger: Callable[[str], None] | None = None


def set_debug(debug_logger: Callable[[str], None] | None) -> None:
    global _debug_logger
    _debug_logger = debug_logger


def log_debug(message: str) -> None:
    if _debug_logger is not None:
        _debug_logger(message)


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
# DVDs store audio/subtitle languages and codec info here; raw .VOB
# bitstream probing cannot recover them, so we read VTS_*_0.IFO directly.
#
# VTS_C_ADT (Cell Address Table) at sector pointer 0xE0 maps each cell's
# VOB_ID/Cell_ID to its sector range, enabling IFO-based trimming without
# scanning the VOB bitstream.
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

# A DVD logical sector is always 2 KiB (libdvdread DVD_BLOCK_SIZE).
_DVD_SECTOR_SIZE = 2048

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


def _parse_vmg_text_metadata(ifo_data: bytes) -> tuple[str | None, str | None]:
    """Return the VMG disc name and barcode, when present."""
    txtdt_sector = _read_u32(ifo_data, _VMG_PTR_TXTDT_MG)
    if not txtdt_sector:
        return None, None

    base_offset = txtdt_sector * 2048
    disc_name = _extract_vmg_disc_name(ifo_data, base_offset)
    barcode = _extract_vmg_barcode(ifo_data, base_offset)
    return disc_name, barcode


def _parse_vmg_provider_id(ifo_data: bytes) -> str:
    raw_provider_id = ifo_data[
        _VMG_PROVIDER_ID : _VMG_PROVIDER_ID + _VMG_PROVIDER_ID_LEN
    ]
    return raw_provider_id.split(b"\x00")[0].decode("ascii", "ignore").strip()


def _parse_vmg_title_map(ifo_data: bytes) -> dict[int, tuple[int, int]]:
    tt_srpt_sector = _read_u32(ifo_data, _VMG_PTR_TT_SRPT)
    title_map: dict[int, tuple[int, int]] = {}
    if not tt_srpt_sector:
        return title_map

    tt_base = tt_srpt_sector * 2048
    if tt_base + 4 > len(ifo_data):
        return title_map

    title_count = _read_u16(ifo_data, tt_base)
    for index in range(title_count):
        entry_offset = tt_base + 4 + index * _VMG_TT_SRPT_ENTRY_LEN
        if entry_offset + _VMG_TT_SRPT_ENTRY_LEN > len(ifo_data):
            break
        _title_type, vts_ttn = _VMG_TT_SRPT_ENTRY.unpack_from(ifo_data, entry_offset)
        vts_number = vts_ttn >> 8
        if vts_number > 0:
            title_map[index + 1] = (vts_number, vts_ttn & 0xFF)
    return title_map


def _parse_vmg_ifo(vmg_path: Path) -> VmgInfo:
    """Parse VIDEO_TS.IFO provider, text, and title-search metadata."""
    try:
        ifo_data = vmg_path.read_bytes()
    except Exception:
        log_debug("VMG IFO read failed")
        return {}
    if len(ifo_data) < 0x100 or ifo_data[:12] != _VMG_IFO_IDENT:
        log_debug(f"VMG IFO ident mismatch: got {ifo_data[:12]!r}")
        raise DvdIfoError(f"VMG IFO ident mismatch: got {ifo_data[:12]!r}")

    result: VmgInfo = {}
    disc_name, barcode = _parse_vmg_text_metadata(ifo_data)
    if disc_name:
        result["disc_name"] = disc_name
    if barcode:
        result["barcode"] = barcode

    provider_id = _parse_vmg_provider_id(ifo_data)
    result["provider_id"] = provider_id
    title_map = _parse_vmg_title_map(ifo_data)
    result["title_map"] = title_map

    log_debug(
        f"VMG IFO: provider='{provider_id}' disc_name={disc_name} "
        f"titles={len(title_map)}"
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
    2-char ISO 639-1 language code. Keying by ID is essential because media
    tools may enumerate PS streams by first packet appearance rather than ID,
    making positional order unreliable. Language codes that are unset (all-zero) or non-ASCII
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


def _vts_ptt_srpt_base(ifo_data: bytes) -> int | None:
    if len(ifo_data) < _VTS_PTR_PTT_SRPT + 4:
        return None
    srpt_sector = _read_u32(ifo_data, _VTS_PTR_PTT_SRPT)
    if srpt_sector == 0:
        return None

    srpt_base = srpt_sector * 2048
    if srpt_base + 8 > len(ifo_data):
        return None
    if _read_u16(ifo_data, srpt_base) < 1:
        return None
    return srpt_base


def _vts_title_unit_base(ifo_data: bytes, srpt_base: int) -> int | None:
    ttu_offset_position = srpt_base + 8
    if ttu_offset_position + 4 > len(ifo_data):
        return None

    ttu_offset = _read_u32(ifo_data, ttu_offset_position)
    ttu_base = srpt_base + ttu_offset
    if ttu_base + 4 > len(ifo_data):
        return None
    if _read_u16(ifo_data, ttu_base) < 1:
        return None
    return ttu_base


def _vts_ttn1_pgc_number(ifo_data: bytes, ttu_base: int) -> int | None:
    ptt_offset = ttu_base + 2
    if ptt_offset + 4 > len(ifo_data):
        return None

    pgc_number = _read_u16(ifo_data, ptt_offset)
    if pgc_number < 1:
        return None
    return pgc_number


def _vts_pgc_absolute_offset(ifo_data: bytes, pgc_number: int) -> int | None:
    pgcit_sector = _read_u32(ifo_data, _VTS_PTR_PGCIT)
    if pgcit_sector == 0:
        return None

    pgcit_base = pgcit_sector * 2048
    entry = pgcit_base + 8 + (pgc_number - 1) * 8
    if entry + 8 > len(ifo_data):
        return None

    pgc_offset = _read_u32(ifo_data, entry + 4) & 0x7FFFFFFF
    pgc_abs = pgcit_base + pgc_offset
    if pgc_abs + 8 > len(ifo_data):
        return None
    return pgc_abs


def _vts_ttn1_pgc_abs(ifo_data: bytes) -> int | None:
    """Return the absolute offset of the PGC used by VTS_TTN 1, chapter 1.

    VTS_PTT_SRPT is the DVD's authoritative default-title designation. A
    malformed or missing table returns None so callers can use their
    longest-PGC fallback.
    """
    srpt_base = _vts_ptt_srpt_base(ifo_data)
    if srpt_base is None:
        return None
    ttu_base = _vts_title_unit_base(ifo_data, srpt_base)
    if ttu_base is None:
        return None
    pgc_number = _vts_ttn1_pgc_number(ifo_data, ttu_base)
    if pgc_number is None:
        return None
    return _vts_pgc_absolute_offset(ifo_data, pgc_number)


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


_PgcSelection = tuple[int, float, int]


def _find_explicit_pgc(ifo_data: bytes, pgc_number: int) -> _PgcSelection | None:
    for number, pgc_abs, duration, cell_count in _enumerate_vts_pgcs(ifo_data):
        if number == pgc_number:
            return pgc_abs, duration, cell_count
    return None


def _find_designated_pgc(ifo_data: bytes) -> _PgcSelection | None:
    pgc_abs = _vts_ttn1_pgc_abs(ifo_data)
    if pgc_abs is None or pgc_abs + 4 > len(ifo_data):
        return None

    duration = _bcd_playback_seconds(ifo_data, pgc_abs + _PGCOffset.PLAYBACK_TIME)
    cell_count = ifo_data[pgc_abs + _PGCOffset.NB_CELLS]
    if cell_count <= 0:
        return None
    return pgc_abs, duration, cell_count


def _find_longest_pgc(ifo_data: bytes) -> _PgcSelection | None:
    best_pgc_abs: int | None = None
    best_duration = 0.0
    best_cells = 0
    for _number, pgc_abs, duration, cell_count in _enumerate_vts_pgcs(ifo_data):
        if duration > best_duration or (
            duration == best_duration and cell_count > best_cells
        ):
            best_duration = duration
            best_cells = cell_count
            best_pgc_abs = pgc_abs

    if best_pgc_abs is None:
        return None
    return best_pgc_abs, best_duration, best_cells


def _find_main_pgc(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> _PgcSelection | None:
    """Find the selected, designated, or longest valid VTS Program Chain.

    An explicit ``pgc_number`` is returned directly. Otherwise VTS_TTN 1 is
    preferred as the disc's own designation; malformed title sets fall back to
    the longest PGC with cell count as the tiebreaker.
    """
    if len(ifo_data) < 0x200 or ifo_data[:12] != _VTS_IFO_IDENT:
        return None
    if pgc_number is not None:
        return _find_explicit_pgc(ifo_data, pgc_number)
    return _find_designated_pgc(ifo_data) or _find_longest_pgc(ifo_data)


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


def _pgc_has_angle_command(ifo_data: bytes, pgc_abs: int) -> bool:
    command_base = _pgc_command_table_base(ifo_data, pgc_abs)
    if command_base is None:
        return False
    command_count = _pgc_pre_command_count(ifo_data, command_base)
    return _scan_pgc_angle_commands(ifo_data, command_base, command_count) > 0


# Relative duration tolerance for grouping PGCs into an episode cluster.
# Episodes of a TV series are typically within a few percent of each other;
# 15% is generous enough to absorb intro/outro variation while still
# separating a 22-minute episode from a 40-minute documentary on the same disc.
_EPISODE_DURATION_TOL = 0.15
_EnumeratedPgc = tuple[int, int, float, int]


def _largest_duration_cluster(pgcs: list[_EnumeratedPgc]) -> list[_EnumeratedPgc]:
    """Return the largest mutually duration-compatible PGC cluster."""
    best_cluster: list[_EnumeratedPgc] = []
    for pgc in pgcs:
        duration = pgc[2]
        neighbours = [
            candidate
            for candidate in pgcs
            if abs(candidate[2] - duration) / max(candidate[2], duration, 1.0)
            <= _EPISODE_DURATION_TOL
        ]
        if len(neighbours) > len(best_cluster) or (
            len(neighbours) == len(best_cluster)
            and best_cluster
            and duration < best_cluster[0][2]
        ):
            best_cluster = neighbours
    return best_cluster


def _has_distinct_pgc_cell_signatures(
    ifo_data: bytes, cluster: list[_EnumeratedPgc]
) -> bool:
    signatures = [
        signature
        for signature in (
            _pgc_cell_position_signature(ifo_data, pgc[1], pgc[3]) for pgc in cluster
        )
        if signature is not None
    ]
    return len(set(signatures)) == len(signatures)


def _find_play_all_pgc(
    all_pgcs: list[_EnumeratedPgc],
    episodes: list[_EnumeratedPgc],
    min_duration: float,
) -> int | None:
    episode_total = sum(pgc[2] for pgc in episodes)
    episode_numbers = {pgc[0] for pgc in episodes}
    for number, _pgc_abs, duration, _cells in all_pgcs:
        if number in episode_numbers or duration < min_duration:
            continue
        if episode_total > 0 and abs(duration - episode_total) / episode_total <= 0.05:
            return number
    return None


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
    substantial = [
        pgc
        for pgc in all_pgcs
        if pgc[2] >= min_duration
        and pgc[3] >= 1
        and not _pgc_has_angle_command(ifo_data, pgc[1])
    ]
    if len(substantial) < 2:
        return [], None

    best_cluster = _largest_duration_cluster(substantial)
    if len(best_cluster) < 2:
        return [], None

    if not _has_distinct_pgc_cell_signatures(ifo_data, best_cluster):
        return [], None

    episode_nums = sorted(p[0] for p in best_cluster)
    play_all = _find_play_all_pgc(all_pgcs, best_cluster, min_duration)
    return episode_nums, play_all


# =============================================================================
# Chapter / duration parsing
# =============================================================================


_PgcProgramTables = tuple[int, int, int, int]


def _pgc_program_tables(ifo_data: bytes, pgc_abs: int) -> _PgcProgramTables | None:
    if pgc_abs + 0xEA > len(ifo_data):
        return None

    program_count = ifo_data[pgc_abs + _PGCOffset.NB_PROGRAMS]
    cell_count = ifo_data[pgc_abs + _PGCOffset.NB_CELLS]
    if not (0 < program_count <= cell_count <= 255):
        return None

    program_map = pgc_abs + _read_u16(ifo_data, pgc_abs + _PGCOffset.PROGRAM_MAP_OFFSET)
    cell_table = pgc_abs + _read_u16(
        ifo_data,
        pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET,
    )
    if program_map + program_count > len(ifo_data):
        return None
    if cell_table + cell_count * _PGCOffset.CELL_PLAYBACK_INFO_LEN > len(ifo_data):
        return None
    return program_map, cell_table, program_count, cell_count


def _pgc_program_cell_range(
    ifo_data: bytes,
    program_map: int,
    program: int,
    program_count: int,
    cell_count: int,
) -> tuple[int, int] | None:
    entry_cell = ifo_data[program_map + program]
    if program < program_count - 1:
        exit_cell = ifo_data[program_map + program + 1] - 1
    else:
        exit_cell = cell_count
    if not (1 <= entry_cell <= exit_cell <= cell_count):
        return None
    return entry_cell, exit_cell


def _trim_trailing_menu_programs(
    chapters: list[float], program_durations: list[float], cumulative: float
) -> tuple[list[float], float]:
    minimum_real_program_seconds = 10.0
    while len(chapters) > 1 and program_durations[-1] < minimum_real_program_seconds:
        chapters.pop()
        program_durations.pop()
        cumulative = (
            chapters[-1] + program_durations[-1] if program_durations else cumulative
        )
    return chapters, cumulative


def _cell_playback_base(cell_table: int, cell: int) -> int:
    return cell_table + (cell - 1) * _PGCOffset.CELL_PLAYBACK_INFO_LEN


def _cell_duration(ifo_data: bytes, cell_base: int) -> float:
    return _bcd_playback_seconds(
        ifo_data,
        cell_base + _PGCOffset.CELL_DURATION_OFFSET,
    )


def _selected_angle_cell(
    angle_cells: list[int], angle_index: int, last_cell: int
) -> int:
    if not angle_cells:
        return last_cell
    return angle_cells[min(angle_index, len(angle_cells) - 1)]


def _pgc_program_cell_duration(
    ifo_data: bytes,
    cell_table: int,
    entry_cell: int,
    exit_cell: int,
    angle_index: int,
) -> float:
    duration = 0.0
    angle_cells: list[int] | None = None
    for cell in range(entry_cell, exit_cell + 1):
        cell_base = _cell_playback_base(cell_table, cell)
        cell_type = (ifo_data[cell_base] >> 6) & 0x03
        if cell_type == 0:
            angle_cells = None
            duration += _cell_duration(ifo_data, cell_base)
        elif cell_type == 1:
            angle_cells = [cell]
        elif cell_type in (2, 3):
            if angle_cells is None:
                angle_cells = []
            angle_cells.append(cell)
            if cell_type == 3:
                selected_cell = _selected_angle_cell(angle_cells, angle_index, cell)
                duration += _cell_duration(
                    ifo_data, _cell_playback_base(cell_table, selected_cell)
                )
                angle_cells = None
    return duration


def _pgc_chapters_and_duration(
    ifo_data: bytes, pgc_abs: int
) -> tuple[list[float], float]:
    """Return chapter start times and duration for one PGC.

    Programs define chapter boundaries. Normal cells and the selected cell from
    each completed angle block advance the timeline.
    """
    tables = _pgc_program_tables(ifo_data, pgc_abs)
    if tables is None:
        return [], 0.0
    program_map, cell_table, program_count, cell_count = tables

    pgc_angle = _pgc_angle_from_commands(ifo_data, pgc_abs)
    angle_index = pgc_angle - 1
    chapters: list[float] = []
    program_durations: list[float] = []
    cumulative = 0.0

    for program in range(program_count):
        cell_range = _pgc_program_cell_range(
            ifo_data, program_map, program, program_count, cell_count
        )
        if cell_range is None:
            continue
        entry_cell, exit_cell = cell_range
        chapters.append(round(cumulative, 3))
        program_start = cumulative
        cumulative += _pgc_program_cell_duration(
            ifo_data, cell_table, entry_cell, exit_cell, angle_index
        )
        program_durations.append(cumulative - program_start)

    return _trim_trailing_menu_programs(chapters, program_durations, cumulative)


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

    Used to recover authoritative chapter timings and runtime; raw-VOB probing
    can report wrong durations once timestamps wrap across VOB parts and may
    inspect only the first VOB. Returns ([], 0.0) on any parse failure.

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


def _pgc_offset_table_base(
    ifo_data: bytes, pgc_abs: int, control_offset: int
) -> int | None:
    if pgc_abs + control_offset + 2 > len(ifo_data):
        return None
    table_offset = _read_u16(ifo_data, pgc_abs + control_offset)
    if not table_offset:
        return None
    return pgc_abs + table_offset


def _active_pgc_audio_streams_offset_mode(ifo_data: bytes, pgc_abs: int) -> set[int]:
    asct_base = _pgc_offset_table_base(ifo_data, pgc_abs, _PGCOffset.AST_CTL)
    if asct_base is None:
        return set()

    audio_active: set[int] = set()
    audio_count = min(_read_vts_audio_count(ifo_data), _PGCOffset.NUM_AST_ENTRIES)
    for index in range(audio_count):
        offset = asct_base + index * _PGCOffset.AST_NORMAL_ENTRY_LEN
        if offset + 6 > len(ifo_data):
            break
        stream_number = _read_u16(ifo_data, offset)
        # Bit 15 marks availability; 0xFFFF means no stream.
        if stream_number != 0xFFFF:
            audio_active.add(0x80 + (stream_number & 0x7FFF))
    return audio_active


def _active_pgc_audio_streams_inline(ifo_data: bytes, pgc_abs: int) -> set[int]:
    audio_active: set[int] = set()
    ast_base = pgc_abs + _PGCOffset.AST_CTL
    for index in range(_PGCOffset.NUM_AST_ENTRIES):
        offset = ast_base + index * 2
        if offset + 2 > len(ifo_data):
            break
        first_byte = ifo_data[offset]
        available = bool(first_byte & 0x80)
        stream_number = first_byte & 0x07
        if available and stream_number != 0x07:
            audio_active.add(0x80 + stream_number)
    return audio_active


def _active_pgc_audio_streams(
    ifo_data: bytes, pgc_abs: int, offset_mode: bool
) -> set[int]:
    if offset_mode:
        return _active_pgc_audio_streams_offset_mode(ifo_data, pgc_abs)
    return _active_pgc_audio_streams_inline(ifo_data, pgc_abs)


def _active_pgc_subpicture_streams(
    ifo_data: bytes, pgc_abs: int, offset_mode: bool
) -> set[int]:
    if offset_mode:
        spst_base = _pgc_offset_table_base(ifo_data, pgc_abs, _PGCOffset.SPST_CTL)
        if spst_base is None:
            return set()
    else:
        spst_base = pgc_abs + _PGCOffset.SPST_CTL

    subpicture_active: set[int] = set()

    for index in range(_PGCOffset.NUM_SPST_ENTRIES):
        entry_offset = spst_base + index * _PGCOffset.SPST_ENTRY_LEN
        if entry_offset + 4 > len(ifo_data):
            break
        first_byte = ifo_data[entry_offset]
        available = bool(first_byte & 0x80)
        stream_number = first_byte & 0x1F
        if available and stream_number != 0x1F:
            subpicture_active.add(0x20 + stream_number)
    return subpicture_active


def _get_active_pgc_streams(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[set[int], set[int]]:
    """Return audio and subpicture IDs active in the selected PGC.

    Reads the PGC stream-control tables described by the DVD-Video PGC
    specification. Returns empty sets on invalid input or a missing PGC;
    callers then fall back to the authoritative VTS attribute-table IDs.
    """
    if len(ifo_data) < 0x200 or ifo_data[:12] != _VTS_IFO_IDENT:
        return set(), set()

    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return set(), set()

    pgc_abs = main[0]
    pgc_category = _read_u16(ifo_data, pgc_abs)
    offset_mode = bool(pgc_category & 0x0002)
    return (
        _active_pgc_audio_streams(ifo_data, pgc_abs, offset_mode),
        _active_pgc_subpicture_streams(ifo_data, pgc_abs, offset_mode),
    )


def _pgc_control_language(ifo_data: bytes, offset: int) -> str | None:
    if offset + 2 > len(ifo_data):
        return None
    raw = ifo_data[offset : offset + 2]
    if raw == b"\x00\x00" or not all(
        97 <= byte <= 122 or 65 <= byte <= 90 for byte in raw
    ):
        return None
    return raw.decode("ascii", "ignore")


def _pgc_audio_languages_offset_mode(ifo_data: bytes, pgc_abs: int) -> dict[int, str]:
    asct_base = _pgc_offset_table_base(ifo_data, pgc_abs, _PGCOffset.AST_CTL)
    if asct_base is None:
        return {}

    audio_languages: dict[int, str] = {}
    audio_count = min(_read_vts_audio_count(ifo_data), _PGCOffset.NUM_AST_ENTRIES)
    for index in range(audio_count):
        offset = asct_base + index * _PGCOffset.AST_NORMAL_ENTRY_LEN
        if offset + 6 > len(ifo_data):
            break
        stream_number = _read_u16(ifo_data, offset)
        if stream_number == 0xFFFF or not (stream_number & 0x8000):
            continue

        actual_stream = stream_number & 0x7FFF
        language = _pgc_control_language(ifo_data, offset + 2)
        if language:
            audio_languages[0x80 + actual_stream] = language
    return audio_languages


def _pgc_subpicture_languages_offset_mode(
    ifo_data: bytes, pgc_abs: int
) -> dict[int, str]:
    spst_base = _pgc_offset_table_base(ifo_data, pgc_abs, _PGCOffset.SPST_CTL)
    if spst_base is None:
        return {}

    subpicture_languages: dict[int, str] = {}
    for index in range(_PGCOffset.NUM_SPST_ENTRIES):
        entry_offset = spst_base + index * _PGCOffset.SPST_ENTRY_LEN
        if entry_offset + 4 > len(ifo_data):
            break
        stream_number = _read_u16(ifo_data, entry_offset)
        if stream_number == 0xFFFF or not (stream_number & 0x8000):
            continue

        actual_stream = stream_number & 0x7FFF
        if actual_stream > 0x1F:
            continue
        language = _pgc_control_language(ifo_data, entry_offset + 2)
        if language:
            subpicture_languages[0x20 + actual_stream] = language
    return subpicture_languages


def _parse_pgc_stream_languages(
    ifo_data: bytes,
    pgc_number: int | None = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Extract per-PGC audio/subpicture language overrides, when present.

    Offset-mode ASCT and SPSCT entries may carry two-letter language codes.
    Inline-mode stream-control entries contain display-format assignments but
    no languages, so callers use the VTS attribute-table languages instead.
    """
    if len(ifo_data) < 0x200 or ifo_data[0:12] != _VTS_IFO_IDENT:
        return {}, {}
    main = _find_main_pgc(ifo_data, pgc_number)
    if main is None:
        return {}, {}

    pgc_abs = main[0]
    pgc_category = _read_u16(ifo_data, pgc_abs)
    if not pgc_category & 0x0002:
        return {}, {}

    return (
        _pgc_audio_languages_offset_mode(ifo_data, pgc_abs),
        _pgc_subpicture_languages_offset_mode(ifo_data, pgc_abs),
    )


# =============================================================================
# VM command parsing (angle detection)
# =============================================================================


def _pgc_command_table_base(ifo_data: bytes, pgc_abs: int) -> int | None:
    if pgc_abs + _PGCOffset.COMMANDS_OFFSET + 2 > len(ifo_data):
        log_debug(f"    _pgc_angle: PGC offset {pgc_abs:#x} out of bounds for commands")
        return None

    command_table_offset = _read_u16(ifo_data, pgc_abs + _PGCOffset.COMMANDS_OFFSET)
    log_debug(
        f"    _pgc_angle: pgc_abs={pgc_abs:#x} "
        f"cmd_tbl_off=0x{command_table_offset:x} "
        f"(at PGC+0x{_PGCOffset.COMMANDS_OFFSET:X})"
    )
    if command_table_offset == 0:
        log_debug("    _pgc_angle: command table offset is 0, no commands")
        return None

    command_base = pgc_abs + command_table_offset
    if command_base + 6 > len(ifo_data):
        log_debug(f"    _pgc_angle: cmd_base {command_base:#x} out of bounds")
        return None
    return command_base


def _pgc_pre_command_count(ifo_data: bytes, command_base: int) -> int:
    pre_count = _read_u16(ifo_data, command_base) & 0x3F
    post_count = _read_u16(ifo_data, command_base + 2) & 0x3F
    cell_count = _read_u16(ifo_data, command_base + 4) & 0x3F
    log_debug(
        f"    _pgc_angle: nr_pre={pre_count} nr_post={post_count} nr_cell={cell_count}"
    )
    return pre_count


def _scan_pgc_angle_commands(
    ifo_data: bytes, command_base: int, command_count: int
) -> int:
    # Pre-commands start after the 8-byte command-table header.
    first_command = command_base + 8
    for index in range(command_count):
        offset = first_command + index * 8
        if offset + 8 > len(ifo_data):
            break

        command = ifo_data[offset]
        if index < 8 or command in (0x51, 0x41):
            command_bytes = ifo_data[offset : offset + 8]
            log_debug(
                f"    _pgc_angle: pre[{index}] off=0x{offset:x} "
                f"cmd=0x{command:02x} "
                f"bytes={command_bytes.hex(' ')}"
            )

        if command not in (0x51, 0x41):
            continue
        angle_byte = ifo_data[offset + 5]
        log_debug(
            f"    _pgc_angle: pre[{index}] cmd=0x{command:02x} "
            f"angle_byte=0x{angle_byte:02x}"
        )
        if angle_byte & 0x80:
            angle = angle_byte & 0x7F
            if angle > 0:
                log_debug(f"    _pgc_angle: detected Angle {angle}")
                return angle
    return 0


def _pgc_angle_from_commands(ifo_data: bytes, pgc_abs: int) -> int:
    """Detect the angle selected by SetSTN pre-commands.

    Returns a one-based angle number, defaulting to angle 1 when the command
    table is absent or contains no angle selection.
    """
    command_base = _pgc_command_table_base(ifo_data, pgc_abs)
    if command_base is None:
        return 1
    command_count = _pgc_pre_command_count(ifo_data, command_base)
    angle = _scan_pgc_angle_commands(ifo_data, command_base, command_count)
    if angle:
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


def _vts_c_adt_bounds(ifo_data: bytes) -> tuple[int, int] | None:
    """Return ``(entry_base, end_address)`` for VTS_C_ADT, or None."""
    if len(ifo_data) < _VTS_PTR_C_ADT + 4:
        return None
    sector_pointer = _read_u32(ifo_data, _VTS_PTR_C_ADT)
    if sector_pointer == 0:
        return None
    base = sector_pointer * 2048
    if base + 8 > len(ifo_data):
        return None

    # VTS_C_ADT starts with a 4-byte end address relative to the IFO start.
    # Some discs store a corrupt value far beyond the real table; callers scan
    # for zero padding instead of trusting it unconditionally.
    end_address = _read_u32(ifo_data, base)
    log_debug(
        f"VTS_C_ADT: sect_ptr={sector_pointer} base={base:#x} "
        f"end_addr=0x{end_address:08x}"
    )
    return base + 4, end_address


def _scan_vts_c_adt_entries(
    ifo_data: bytes, entry_base: int, max_slots: int, end_address: int
) -> int:
    n_address_slots = 0
    if end_address > entry_base:
        n_address_slots = (end_address - entry_base) // _VTS_C_ADT_ENTRY_LEN
    n_entries = min(max_slots, max(n_address_slots, max_slots))

    # The table is zero-padded; the first all-zero 12-byte slot is its end.
    entry_count = 0
    for index in range(n_entries):
        offset = entry_base + index * _VTS_C_ADT_ENTRY_LEN
        if offset + _VTS_C_ADT_ENTRY_LEN > len(ifo_data):
            break
        if not any(ifo_data[offset : offset + _VTS_C_ADT_ENTRY_LEN]):
            break
        entry_count = index + 1

    log_debug(
        f"VTS_C_ADT: {entry_count} raw entries "
        f"(end_addr suggests {n_address_slots}, IFO caps at {max_slots})"
    )
    return entry_count


def _decode_vts_c_adt_entry(ifo_data: bytes, offset: int) -> CadtCell | None:
    vob_id = _read_u16(ifo_data, offset)
    cell_id = ifo_data[offset + 2]
    start_sector = _read_u32(ifo_data, offset + 4)
    end_sector = _read_u32(ifo_data, offset + 8)
    if vob_id == 0 or cell_id == 0:
        return None
    if start_sector >= end_sector:
        return None
    return {
        "vob_id": vob_id,
        "cell_id": cell_id,
        "start_sector": start_sector,
        "end_sector": end_sector,
    }


def _parse_vts_c_adt(ifo_data: bytes) -> list[CadtCell]:
    """Parse the VTS Cell Address Table into a list of valid cell entries.

    Each entry maps a ``(VOB ID, cell ID)`` pair to its sector range relative
    to the VTS title VOB area. Missing, truncated, or invalid tables return an
    empty list so callers can fall back to PTS-based trimming.
    """
    bounds = _vts_c_adt_bounds(ifo_data)
    if bounds is None:
        return []
    entry_base, end_address = bounds

    max_slots = (len(ifo_data) - entry_base) // _VTS_C_ADT_ENTRY_LEN
    if max_slots <= 0:
        return []
    raw_count = _scan_vts_c_adt_entries(ifo_data, entry_base, max_slots, end_address)
    if raw_count == 0:
        return []

    cells: list[CadtCell] = []
    for index in range(raw_count):
        offset = entry_base + index * _VTS_C_ADT_ENTRY_LEN
        if offset + _VTS_C_ADT_ENTRY_LEN > len(ifo_data):
            break
        cell = _decode_vts_c_adt_entry(ifo_data, offset)
        if cell is not None:
            cells.append(cell)

    log_debug(f"VTS_C_ADT: {len(cells)} valid cell entries (out of {raw_count} raw)")
    if cells:
        log_debug(
            "  C_ADT sample: first 3 entries: "
            + ", ".join(
                f"VOB={cell['vob_id']} Cell={cell['cell_id']} "
                f"sectors={cell['start_sector']}-{cell['end_sector']}"
                for cell in cells[:3]
            )
        )
        log_debug(
            "  C_ADT sample: last 3 entries: "
            + ", ".join(
                f"VOB={cell['vob_id']} Cell={cell['cell_id']} "
                f"sectors={cell['start_sector']}-{cell['end_sector']}"
                for cell in cells[-3:]
            )
        )
    return cells


def _vts_vobu_admap_bounds(ifo_data: bytes) -> tuple[int, int] | None:
    """Return ``(entry_base, end_address)`` for VTS_VOBU_ADMAP, or None."""
    if len(ifo_data) < _VTS_PTR_VOBU_ADMAP + 4:
        return None
    sector_pointer = _read_u32(ifo_data, _VTS_PTR_VOBU_ADMAP)
    if sector_pointer == 0:
        return None
    base = sector_pointer * 2048
    if base + 8 > len(ifo_data):
        return None

    # VTS_VOBU_ADMAP starts with a four-byte end address relative to the IFO.
    end_address = _read_u32(ifo_data, base)
    log_debug(
        f"VTS_VOBU_ADMAP: sect_ptr={sector_pointer} base={base:#x} "
        f"end_addr=0x{end_address:08x}"
    )
    return base + 4, end_address


def _leading_vobu_zero_is_valid(ifo_data: bytes, offset: int) -> bool:
    """Distinguish sector zero from zero padding at the table start."""
    next_offset = offset + _VTS_VOBU_ADMAP_ENTRY_LEN
    if next_offset + _VTS_VOBU_ADMAP_ENTRY_LEN > len(ifo_data):
        return False
    next_vobu = _read_u32(ifo_data, next_offset)
    return next_vobu != 0


def _scan_vts_vobu_admap(
    ifo_data: bytes,
    entry_base: int,
    max_entries: int,
    end_address: int,
) -> list[int]:
    n_address_entries = 0
    if end_address > entry_base:
        n_address_entries = (end_address - entry_base) // _VTS_VOBU_ADMAP_ENTRY_LEN
    n_entries = min(max_entries, max(n_address_entries, max_entries))

    vobus: list[int] = []
    for index in range(n_entries):
        offset = entry_base + index * _VTS_VOBU_ADMAP_ENTRY_LEN
        if offset + _VTS_VOBU_ADMAP_ENTRY_LEN > len(ifo_data):
            break
        vobu_start = _read_u32(ifo_data, offset)
        if vobu_start == 0:
            if index == 0 and _leading_vobu_zero_is_valid(ifo_data, offset):
                vobus.append(vobu_start)
                continue
            break
        vobus.append(vobu_start)
    return vobus


def _parse_vts_vobu_admap(ifo_data: bytes) -> list[int] | None:
    """Parse VTS_VOBU_ADMAP into sorted VOBU start sectors, or None."""
    bounds = _vts_vobu_admap_bounds(ifo_data)
    if bounds is None:
        return None
    entry_base, end_address = bounds

    max_entries = (len(ifo_data) - entry_base) // _VTS_VOBU_ADMAP_ENTRY_LEN
    if max_entries <= 0:
        return None

    vobus = _scan_vts_vobu_admap(ifo_data, entry_base, max_entries, end_address)
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


@dataclass(slots=True)
class _EditionCell:
    """One angle-selected cell from a PGC playback table."""

    first_sector: int
    last_sector: int
    vob_id: int
    cell_id: int
    duration_seconds: float
    block_mode: int
    cell_index: int


def _trim_trailing_thumbnail_cells(cells: list[_EditionCell]) -> list[_EditionCell]:
    """Remove trailing menu thumbnail/keyframe cells from movie playback."""
    retained = list(cells)
    trimmed = 0
    while len(retained) > 1 and retained[-1].duration_seconds < 1.0:
        retained.pop()
        trimmed += 1
    if trimmed:
        log_debug(
            f"Main-edition cells: trimmed {trimmed} trailing sub-second "
            "cell(s) (thumbnail/keyframe data, not movie content)"
        )
    return retained


def _select_main_edition_cells(
    ifo_data: bytes,
    pgc_abs: int,
    cell_count: int,
) -> tuple[list[_EditionCell], bool] | None:
    """Select PGC cells for its chosen angle and discard thumbnail tail cells."""
    playback_base = _pgc_cell_playback_table_base(ifo_data, pgc_abs, cell_count)
    position_base = _pgc_cell_position_table_base(ifo_data, pgc_abs, cell_count)
    if playback_base is None or position_base is None:
        return None

    angle_index = _pgc_angle_from_commands(ifo_data, pgc_abs) - 1
    selected_cells: list[_EditionCell] = []
    current_block: list[_EditionCell] = []
    any_interleaved = False

    def finalize_block() -> None:
        if not current_block:
            return
        selected_index = (
            angle_index if angle_index < len(current_block) else len(current_block) - 1
        )
        selected_cells.append(current_block[selected_index])
        current_block.clear()

    for cell_index in range(cell_count):
        playback_offset = _cell_playback_base(playback_base, cell_index + 1)
        position_offset = position_base + cell_index * _CELL_POS_ENTRY.size
        block_mode = (ifo_data[playback_offset] >> 6) & 0x03
        first_sector = _read_u32(ifo_data, playback_offset + _CELL_PB_FIRST_SECTOR_OFF)
        last_sector = _read_u32(ifo_data, playback_offset + _CELL_PB_LAST_SECTOR_OFF)
        vob_id, cell_id = _CELL_POS_ENTRY.unpack_from(ifo_data, position_offset)
        if first_sector == 0 and last_sector == 0:
            continue

        cell = _EditionCell(
            first_sector=first_sector,
            last_sector=last_sector,
            vob_id=vob_id,
            cell_id=cell_id,
            duration_seconds=_bcd_playback_seconds(
                ifo_data,
                playback_offset + _PGCOffset.CELL_DURATION_OFFSET,
            ),
            block_mode=block_mode,
            cell_index=cell_index,
        )
        if block_mode == 0:
            finalize_block()
            selected_cells.append(cell)
        elif block_mode == 1:
            finalize_block()
            current_block = [cell]
        else:
            current_block.append(cell)
            if block_mode == 3:
                finalize_block()
        if block_mode != 0:
            any_interleaved = True
    finalize_block()

    if not selected_cells:
        return None
    return _trim_trailing_thumbnail_cells(selected_cells), any_interleaved


def _noninterleaved_vobu_ranges(
    cells: list[_EditionCell], vobu_admap: list[int]
) -> list[tuple[int, int]]:
    """Build direct byte ranges when cells contain no interleaving."""
    return [
        (
            cell.first_sector * _DVD_SECTOR_SIZE,
            (
                _vobu_end_byte(cell.last_sector, vobu_admap)
                if vobu_admap
                else (cell.last_sector + 1) * _DVD_SECTOR_SIZE
            ),
        )
        for cell in cells
    ]


def _interleaved_scan_sectors(
    cells: list[_EditionCell], vobu_admap: list[int]
) -> tuple[dict[int, int], list[int]] | None:
    """Return ADMAP indices and sectors covering every interleaved cell."""
    low_sector = min(cell.first_sector for cell in cells)
    high_sector = max(cell.last_sector for cell in cells)
    admap_index = {sector: index for index, sector in enumerate(vobu_admap)}
    scan_sectors = sorted(
        sector for sector in vobu_admap if low_sector <= sector <= high_sector
    )
    if not scan_sectors:
        return None
    return admap_index, scan_sectors


def _interleaved_vobu_ranges(
    cells: list[_EditionCell],
    vobu_admap: list[int],
    inputs: list[Path],
) -> list[tuple[int, int]] | None:
    """Build playback-ordered ranges from each VOBU's NAV-pack cell identity."""
    bounds = _interleaved_scan_sectors(cells, vobu_admap)
    if bounds is None:
        return None
    admap_index, scan_sectors = bounds
    target_ids = {(cell.vob_id, cell.cell_id) for cell in cells}
    block_mode_counts: dict[int, int] = {}
    for cell in cells:
        block_mode_counts[cell.block_mode] = (
            block_mode_counts.get(cell.block_mode, 0) + 1
        )
    log_debug(
        f"NAV scan targets: {len(target_ids)} unique (vob_id,cell_id) pairs, "
        f"block_mode distribution: {block_mode_counts}, "
        f"first 5 targets: {sorted(target_ids)[:5]}"
    )
    log_debug(
        f"Seamless-branching disc detected ({len(cells)} cells, "
        f"{len(target_ids)} unique (vob_id,cell_id) target(s)); "
        f"scanning {len(scan_sectors)} VOBU NAV packs for cell ownership..."
    )
    nav_ids = _scan_vobu_cell_ids(inputs, scan_sectors)
    nav_unique = set(nav_ids.values())
    log_debug(
        f"NAV scan found {len(nav_unique)} unique (vob_id,cell_id) "
        f"in {len(nav_ids)} VOBUs; sample: {sorted(nav_unique)[:10]}"
    )

    cell_order: dict[tuple[int, int], int] = {}
    for playback_index, cell in enumerate(cells):
        cell_order.setdefault((cell.vob_id, cell.cell_id), playback_index)
    cell_vobus: dict[int, list[int]] = {}
    for sector in scan_sectors:
        ids = nav_ids.get(sector)
        if ids is None or ids not in target_ids:
            continue
        playback_index = cell_order.get(ids)
        if playback_index is not None:
            cell_vobus.setdefault(playback_index, []).append(admap_index[sector])

    runs: list[tuple[int, int]] = []
    total_matched = 0
    for playback_index in sorted(cell_vobus):
        indices = sorted(cell_vobus[playback_index])
        total_matched += len(indices)
        run_start = indices[0]
        previous = indices[0]
        for index in indices[1:]:
            if index != previous + 1:
                runs.append(
                    (
                        vobu_admap[run_start] * _DVD_SECTOR_SIZE,
                        _vobu_end_byte(vobu_admap[previous], vobu_admap),
                    )
                )
                run_start = index
            previous = index
        runs.append(
            (
                vobu_admap[run_start] * _DVD_SECTOR_SIZE,
                _vobu_end_byte(vobu_admap[previous], vobu_admap),
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

    selection = _select_main_edition_cells(ifo_data, pgc_abs, n_cells)
    if selection is None:
        return None
    cells, any_interleaved = selection

    if not any_interleaved:
        runs = _noninterleaved_vobu_ranges(cells, vobu_admap)
        log_debug(f"Main-edition ranges (no interleaving): {len(runs)} run(s)")
        return runs

    if not vobu_admap:
        return None
    return _interleaved_vobu_ranges(cells, vobu_admap, inputs)


def _pgc_cell_playback_table_base(
    ifo_data: bytes, pgc_abs: int, cell_count: int
) -> int | None:
    playback_offset = (
        _read_u16(
            ifo_data,
            pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET,
        )
        if (pgc_abs + _PGCOffset.CELL_PLAYBACK_INFO_TABLE_OFFSET + 2 <= len(ifo_data))
        else 0
    )
    if not playback_offset:
        return None
    playback_base = pgc_abs + playback_offset
    if playback_base + cell_count * _PGCOffset.CELL_PLAYBACK_INFO_LEN > len(ifo_data):
        return None
    return playback_base


def _angle_selected_cell_indexes(
    ifo_data: bytes,
    playback_base: int,
    cell_count: int,
    angle_index: int,
) -> set[int]:
    selected_cells: set[int] = set()
    angle_block: list[int] | None = None
    for cell_index in range(cell_count):
        offset = _cell_playback_base(playback_base, cell_index + 1)
        cell_type = (ifo_data[offset] >> 6) & 0x03
        if cell_type == 0:
            continue
        if cell_type == 1:
            angle_block = [cell_index]
            continue
        if cell_type not in (2, 3):
            continue

        if angle_block is None:
            angle_block = []
        angle_block.append(cell_index)
        if cell_type == 3:
            selected = _selected_angle_cell(angle_block, angle_index, cell_index)
            selected_cells.add(selected)
            angle_block = None
    return selected_cells


def _accumulate_pgc_playback_sectors(
    ifo_data: bytes,
    playback_base: int,
    cell_count: int,
    selected_cells: set[int],
) -> tuple[int | None, int | None, int | None]:
    start_sector: int | None = None
    end_sector: int | None = None
    last_first_vobu: int | None = None

    for cell_index in range(cell_count):
        offset = _cell_playback_base(playback_base, cell_index + 1)
        cell_type = (ifo_data[offset] >> 6) & 0x03
        if cell_type != 0 and cell_index not in selected_cells:
            continue

        first_vobu = _read_u32(ifo_data, offset + _CELL_PB_FIRST_SECTOR_OFF)
        last_vobu = _read_u32(ifo_data, offset + _CELL_PB_LAST_SECTOR_OFF)
        if first_vobu == 0 and last_vobu == 0:
            if start_sector is None:
                # Some discs zero both fields in a content cell.
                start_sector = 0
            continue
        if start_sector is None or first_vobu < start_sector:
            start_sector = first_vobu
        if first_vobu > 0:
            last_first_vobu = first_vobu
        if last_vobu > 0 and (end_sector is None or last_vobu > end_sector):
            end_sector = last_vobu

    return start_sector, end_sector, last_first_vobu


def _resolve_pgc_playback_end_sector(
    last_first_vobu: int,
    vobu_admap: list[int] | None,
    vob_total_bytes: int,
) -> int | None:
    if vobu_admap is not None:
        for index, sector in enumerate(vobu_admap):
            if sector >= last_first_vobu and index + 1 < len(vobu_admap):
                end_sector = vobu_admap[index + 1] - 1
                log_debug(
                    "IFO cell trim: VOBU_ADMAP end after "
                    f"sector {last_first_vobu} -> next VOBU start "
                    f"{vobu_admap[index + 1]} -> end {end_sector}"
                )
                return end_sector

    estimated_end = last_first_vobu + 150_000
    total_sectors = vob_total_bytes // 2048
    end_sector = min(estimated_end, total_sectors)
    log_debug(
        "IFO cell trim: PGC cell PB last_fvobu fallback "
        f"sectors {last_first_vobu}+150000 -> {end_sector}"
    )
    return end_sector


def _lookup_pgc_cell_playback_sector_range(
    ifo_data: bytes,
    pgc_abs: int,
    cell_count: int,
    vobu_admap: list[int] | None,
    vob_total_bytes: int,
    angle_index: int,
) -> tuple[int, int] | None:
    """Recover a PGC sector range directly from CellPlaybackInfo entries."""
    playback_base = _pgc_cell_playback_table_base(ifo_data, pgc_abs, cell_count)
    if playback_base is None:
        return None

    selected_cells = _angle_selected_cell_indexes(
        ifo_data, playback_base, cell_count, angle_index
    )
    start_sector, end_sector, last_first_vobu = _accumulate_pgc_playback_sectors(
        ifo_data, playback_base, cell_count, selected_cells
    )
    if end_sector is None and last_first_vobu is not None:
        end_sector = _resolve_pgc_playback_end_sector(
            last_first_vobu, vobu_admap, vob_total_bytes
        )

    if start_sector is None or end_sector is None or end_sector <= start_sector:
        return None
    log_debug(
        "IFO cell trim: PGC cell playback table fallback "
        f"sectors {start_sector}-{end_sector} "
        f"({(end_sector - start_sector) * 2048 / 1e9:.1f} GB)"
    )
    return start_sector, end_sector


def _pgc_cell_position_table_base(
    ifo_data: bytes,
    pgc_abs: int,
    cell_count: int,
) -> int | None:
    """Return the absolute PGC Cell Position Info table base."""
    offset_field = _PGCOffset.CELL_POSITION_INFO_TABLE_OFFSET
    if pgc_abs + offset_field + 2 > len(ifo_data):
        return None
    table_offset = _read_u16(ifo_data, pgc_abs + offset_field)
    if not table_offset:
        return None
    table_base = pgc_abs + table_offset
    if table_base + cell_count * _CELL_POS_ENTRY.size > len(ifo_data):
        return None
    return table_base


def _lookup_pgc_c_adt_sector_range(
    ifo_data: bytes,
    position_base: int,
    cell_count: int,
    cell_map: dict[tuple[int, int], tuple[int, int]],
) -> tuple[int | None, int | None, int]:
    """Match PGC cell positions against VTS_C_ADT entries.

    A cell first tries its exact ``(VOB_ID, Cell_ID)`` key. Some authored discs
    disagree on VOB_ID between the position and address tables, so Cell_ID is
    the documented fallback rather than silently dropping that cell.
    """
    start_sector: int | None = None
    end_sector: int | None = None
    matched_count = 0
    for cell_index in range(cell_count):
        offset = position_base + cell_index * _CELL_POS_ENTRY.size
        if offset + _CELL_POS_ENTRY.size > len(ifo_data):
            break
        vob_id, cell_id = _CELL_POS_ENTRY.unpack_from(ifo_data, offset)
        entry = cell_map.get((vob_id, cell_id))
        if entry is None:
            entry = next(
                (value for key, value in cell_map.items() if key[1] == cell_id),
                None,
            )
        if entry is None:
            continue
        matched_count += 1
        cell_start, cell_end = entry
        if start_sector is None or cell_start < start_sector:
            start_sector = cell_start
        if end_sector is None or cell_end > end_sector:
            end_sector = cell_end
    return start_sector, end_sector, matched_count


def _sequential_vob1_sector_range(
    cells: list[CadtCell],
    cell_count: int,
) -> tuple[int, int] | None:
    """Recover a sequential title from VOB 1 C_ADT entries."""
    vob1_cells = [cell for cell in cells if cell["vob_id"] == 1]
    if len(vob1_cells) < cell_count:
        return None
    start_sector = vob1_cells[0]["start_sector"]
    end_sector = vob1_cells[cell_count - 1]["end_sector"]
    log_debug(
        "IFO cell trim: sequential fallback (VOB 1) "
        f"sectors {start_sector}-{end_sector}"
    )
    return start_sector, end_sector


def _sector_range_to_bytes(
    start_sector: int,
    end_sector: int,
    vob_total_bytes: int,
) -> tuple[int, int] | None:
    """Convert VOB-relative sector bounds to clamped byte bounds."""
    start_byte = max(0, start_sector * _DVD_SECTOR_SIZE)
    end_byte = min((end_sector + 1) * _DVD_SECTOR_SIZE, vob_total_bytes)
    if end_byte <= start_byte:
        return None
    return start_byte, end_byte


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

    Callers fall back to direct MPEG-PS PTS scanning on None.
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
        (cell["vob_id"], cell["cell_id"]): (
            cell["start_sector"],
            cell["end_sector"],
        )
        for cell in cells
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

    position_base = _pgc_cell_position_table_base(ifo_data, pgc_abs, n_cells)
    if position_base is None:
        log_debug(
            "IFO cell trim: invalid cell position table "
            f"(pgc_abs=0x{pgc_abs:x}, cells={n_cells})"
        )
        return None

    # 5. Look up each PGC cell in the C_ADT to find the sector range.
    #    Only accept the range when ALL PGC cells are found in C_ADT.
    #    Partial matches can come from other PGCs (menus/extras) that
    #    happen to share Cell_ID values, producing an incorrect range.
    #    When matching fails, the CellPlaybackInfo fallback (below)
    #    provides the correct range from the PGC's own cell table.
    start_sector, end_sector, matched_count = _lookup_pgc_c_adt_sector_range(
        ifo_data, position_base, n_cells, cell_map
    )

    # Only use C_ADT-based range if ALL cells matched.
    # Partial matches can pick cells from unrelated PGCs.
    if matched_count < n_cells:
        log_debug(
            f"IFO cell trim: {matched_count}/{n_cells} PGC cells matched in C_ADT, "
            "falling back to PGC cell playback table"
        )
        start_sector = None
        end_sector = None

    if start_sector is None or end_sector is None or end_sector <= start_sector:
        sector_range = _lookup_pgc_cell_playback_sector_range(
            ifo_data,
            pgc_abs,
            n_cells,
            vobu_admap,
            vob_total_bytes,
            angle_idx_lmr,
        )
        if sector_range is not None:
            start_sector, end_sector = sector_range

    if start_sector is None or end_sector is None or end_sector <= start_sector:
        sequential_range = _sequential_vob1_sector_range(cells, n_cells)
        if sequential_range is None:
            log_debug(
                "IFO cell trim: no cell sector range "
                f"(start={start_sector}, end={end_sector}, "
                f"cadt_vob1={sum(cell['vob_id'] == 1 for cell in cells)}, "
                f"need={n_cells})"
            )
            return None
        start_sector, end_sector = sequential_range

    # 5. Convert sector addresses to byte offsets. C_ADT sectors are already
    #    VOB-relative (per the DVD spec / mpucoder), so we just multiply by
    #    2048 without subtracting any VTS_VOB_Start offset.
    return _sector_range_to_bytes(start_sector, end_sector, vob_total_bytes)


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
