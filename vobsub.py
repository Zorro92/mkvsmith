"""
DVD VOB subtitle extraction, PTS scanning, and VOB content analysis.

Extracted from main.py. Handles:
- VOB PTS scanning for cell boundary detection
- DVD SPU subpicture extraction (pure-Python PES parser)
- VobSub .idx/.sub file generation (PES-wrapped, 2048-byte sectors)
- Main content range detection (DVD cell trimming)
"""

from __future__ import annotations

import json
import mmap
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dvdifo import _concat_file_layout, _LANG_MAP_3_TO_2

from models import (
    _HAS_MKVMERGE,
    get_language_name,
    log_debug,
    log_warn,
)

# -----------------------------------------------------------------------------
# DVD cell extraction
#
# A DVD VTS stores every Program Chain (the main movie plus short warning /
# intro / rating-card PGCs) interleaved in the same VOB files. Each cell starts
# its own PTS timeline at ~0, so copying concatenated raw VOBs collapses the
# overlapping timelines: warning cards appear before the movie and the audio ends
# up far shorter than the video (hard desync). MakeMKV follows the main PGC's
# cell list and extracts only those cells; we approximate that without a full
# DVD-nav implementation by locating the longest PTS-continuous run (the movie)
# and copying only that byte range.
# -----------------------------------------------------------------------------

# A backward PTS jump larger than this marks a cell boundary (a new PGC cell
# resetting its timeline to ~0). 5 s is well above B-frame reorder jitter while
# still catching every real cell reset (which are tens of seconds).
_CELL_PTS_RESET_TICKS = 5 * 90000
# MPEG-PS pack start code (system clock reference follows it).
_PS_PACK_START = b"\x00\x00\x01\xba"
# MPEG-PS video stream start code.
_PS_VIDEO_START = b"\x00\x00\x01"
_PS_VIDEO_SID = 0xE0
# MPEG-PS private stream 1 (AC-3 audio / DVD subpictures).
_PS_PRIVATE1_SID = 0xBD
# MPEG-PS private stream 2 (DVD navigation / some discs use for subtitles).
_PS_PRIVATE2_SID = 0xBF
# MPEG-PS pack header stream ID (system clock reference).
_PS_PACK_SID = 0xBA
# DVD subpicture sub-stream IDs range: 0x20-0x3F.
_PS_SUBP_START = 0x20
_PS_SUBP_END = 0x3F
_RAW_SPU_SCAN_LIMIT = 64 * 1024 * 1024

_FFMPEG_SPU_LEVEL_MAP = [
    [0xFF],
    [0x00, 0xFF],
    [0x00, 0x80, 0xFF],
    [0x00, 0x55, 0xAA, 0xFF],
]


@dataclass(slots=True)
class _SubpicturePayload:
    sub_stream_id: int
    start: int
    pts: int


@dataclass(slots=True)
class _Private1Packet:
    end: int | None
    subpicture: _SubpicturePayload | None


@dataclass(slots=True)
class _VobScanCounts:
    private_one: int = 0
    private_two: int = 0
    subpictures: int = 0


class _SpuAccumulator:
    """Join continuation PES packets that form one DVD subpicture."""

    def __init__(self) -> None:
        self._packets: dict[int, tuple[int, list[bytes]]] = {}

    def add(
        self, sub_stream_id: int, pts: int, chunk: bytes
    ) -> tuple[int, bytes] | None:
        if sub_stream_id not in self._packets:
            self._packets[sub_stream_id] = (pts, [chunk])
            return None

        accumulated_pts, chunks = self._packets[sub_stream_id]
        if pts and pts != accumulated_pts and chunks:
            self._packets[sub_stream_id] = (pts, [chunk])
            return accumulated_pts, b"".join(chunks)
        chunks.append(chunk)
        if pts and not accumulated_pts:
            self._packets[sub_stream_id] = (pts, chunks)
        return None

    def flush(self) -> dict[int, list[tuple[int, bytes]]]:
        result: dict[int, list[tuple[int, bytes]]] = {}
        for sub_stream_id, (accumulated_pts, chunks) in self._packets.items():
            result[sub_stream_id] = [(accumulated_pts, b"".join(chunks))]
        self._packets.clear()
        return result


@dataclass(slots=True)
class _SpuControlState:
    palette_indexes: list[int | None]
    alphas: list[int]


@dataclass(slots=True)
class _PtsScanCounts:
    start_codes: int = 0
    video_stream_ids: int = 0
    pts_values: int = 0


def _spu_nibble_quad(data: bytes, offset: int) -> tuple[int, int, int, int]:
    emphasis_two = (data[offset] >> 4) & 0x0F
    emphasis_one = data[offset] & 0x0F
    pattern = (data[offset + 1] >> 4) & 0x0F
    background = data[offset + 1] & 0x0F
    return emphasis_two, emphasis_one, pattern, background


def _parse_spu_control_state(spu_data: bytes) -> _SpuControlState | None:
    """Parse linked DVD SPU display-control sequences.

    Bytes 2-3 of the SPU point to its first control sequence. Every sequence
    contains a two-byte date, a two-byte absolute link to the next sequence,
    then variable-length opcodes ending in ``0xff``. This layout follows the
    DVD SPU control-sequence reference documented by Sam Hocevar.
    """
    if len(spu_data) < 4:
        return None
    sequence_offset = (spu_data[2] << 8) | spu_data[3]
    if sequence_offset < 4 or sequence_offset + 4 > len(spu_data):
        return None

    state = _SpuControlState(
        palette_indexes=[None, None, None, None],
        alphas=[0, 0, 0, 0],
    )
    visited: set[int] = set()
    while sequence_offset not in visited:
        visited.add(sequence_offset)
        next_offset = (spu_data[sequence_offset + 2] << 8) | spu_data[
            sequence_offset + 3
        ]
        command_offset = sequence_offset + 4
        while command_offset < len(spu_data):
            opcode = spu_data[command_offset]
            command_offset += 1
            if opcode == 0xFF:
                break
            if opcode in (0x00, 0x01, 0x02):
                continue
            if opcode in (0x03, 0x04):
                if command_offset + 2 > len(spu_data):
                    break
                emphasis_two, emphasis_one, pattern, background = _spu_nibble_quad(
                    spu_data, command_offset
                )
                if opcode == 0x03:
                    state.palette_indexes = [
                        background,
                        pattern,
                        emphasis_one,
                        emphasis_two,
                    ]
                else:
                    state.alphas = [
                        background,
                        pattern,
                        emphasis_one,
                        emphasis_two,
                    ]
                command_offset += 2
                continue
            if opcode == 0x05:
                argument_length = 6
            elif opcode == 0x06:
                argument_length = 4
            elif opcode == 0x07:
                if command_offset + 2 > len(spu_data):
                    break
                argument_length = 2 + (
                    (spu_data[command_offset] << 8) | spu_data[command_offset + 1]
                )
            else:
                break
            if command_offset + argument_length > len(spu_data):
                break
            command_offset += argument_length

        if (
            next_offset == sequence_offset
            or next_offset < 4
            or next_offset >= len(spu_data)
        ):
            break
        sequence_offset = next_offset

    has_color = any(index is not None for index in state.palette_indexes)
    has_contrast = any(alpha > 0 for alpha in state.alphas)
    if not has_color and not has_contrast:
        return None
    return state


def _build_spu_palette(
    state: _SpuControlState, clut: list[tuple[int, int, int]] | None
) -> list[tuple[int, int, int]] | None:
    """Build a 16-entry palette from an authoritative CLUT or SPU contrast."""
    if clut is not None:
        return [
            clut[index] if index < len(clut) else (0x82, 0x82, 0x82)
            for index in range(16)
        ]
    if not any(alpha > 0 for alpha in state.alphas):
        return None

    opaque_indexes = sorted(
        {
            palette_index
            for palette_value, palette_index in enumerate(state.palette_indexes)
            if palette_index is not None and state.alphas[palette_value] > 0
        }
    )
    level_count = min(len(opaque_indexes), 4)
    levels = (
        _FFMPEG_SPU_LEVEL_MAP[level_count - 1]
        if level_count > 0
        else _FFMPEG_SPU_LEVEL_MAP[0]
    )
    grayscale_by_index: dict[int, tuple[int, int, int]] = {}
    for level, palette_index in enumerate(opaque_indexes):
        grayscale = (0xFF * levels[level]) >> 8
        grayscale_by_index[palette_index] = (grayscale, grayscale, grayscale)
    palette: list[tuple[int, int, int]] = []
    for palette_value in range(16):
        if palette_value >= 4:
            palette.append((0x82, 0x82, 0x82))
            continue
        palette_index = state.palette_indexes[palette_value]
        color = grayscale_by_index.get(palette_index)
        if color is not None:
            palette.append(color)
        elif state.alphas[palette_value] == 0:
            palette.append((0, 0, 0))
        else:
            palette.append((0xFF, 0xFF, 0xFF))
    return palette


def _read_concat_bytes(inputs: list[Path], start: int, length: int) -> bytes:
    """Read ``length`` bytes starting at global offset ``start`` across inputs."""
    out = bytearray()
    remaining = length
    for f, fstart, fend in _concat_file_layout(inputs):
        if fend <= start or remaining <= 0:
            continue
        local = max(0, start - fstart)
        with open(f, "rb") as fh:
            fh.seek(local)
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining, fend - fstart - local))
                if not chunk:
                    break
                out.extend(chunk)
                remaining -= len(chunk)
                local += len(chunk)
        if remaining <= 0:
            break
    return bytes(out)


def _decode_pes_pts(data: bytearray | bytes | mmap.mmap, offset: int) -> int | None:
    """Decode a five-byte MPEG PES PTS at *offset*.

    Layout per ISO/IEC 13818-1: ``'0010'``, PTS[32:30], marker, PTS[29:15],
    marker, PTS[14:0], marker. Invalid prefix or marker bits return None.
    """
    if offset < 0 or offset + 5 > len(data):
        return None
    byte_0, byte_1, byte_2, byte_3, byte_4 = data[offset : offset + 5]
    if (byte_0 & 0xF0) not in (0x20, 0x30):
        return None
    if not (byte_2 & 0x01 and byte_4 & 0x01):
        return None
    return (
        ((byte_0 & 0x0E) << 29)
        | (byte_1 << 22)
        | ((byte_2 & 0xFE) << 14)
        | (byte_3 << 7)
        | ((byte_4 & 0xFE) >> 1)
    )


def _snap_to_pack(inputs: list[Path], pos: int, total: int) -> int:
    """Snap ``pos`` back to the nearest MPEG-PS pack header (sector boundary).

    Cell boundaries (and thus clean extraction points) always coincide with a
    pack header ``00 00 01 BA`` on a 2048-byte sector. A packet position from a
    bitstream scanner can point just past the cell's opening pack, so we search
    the preceding window.
    """
    window = 1 << 16
    base = max(0, pos - window)
    data = _read_concat_bytes(inputs, base, min(window + 16, total - base))
    idx = data.rfind(_PS_PACK_START)
    if idx < 0:
        return pos
    snapped = base + idx
    return snapped - (snapped % 2048) if snapped % 2048 else snapped


def _scan_vob_pts_window(
    data: bytearray, scan_offset: int
) -> tuple[list[tuple[int, int]], int, _PtsScanCounts]:
    """Extract video PTS values from one accumulated scan buffer."""
    result: list[tuple[int, int]] = []
    counts = _PtsScanCounts()
    data_len = len(data)

    while scan_offset < data_len - 3:
        index = data.find(_PS_VIDEO_START, scan_offset)
        if index < 0 or index + 3 >= data_len:
            scan_offset = max(scan_offset, data_len - 3)
            break
        counts.start_codes += 1
        stream_id = data[index + 3]
        if stream_id == _PS_PACK_SID:
            scan_offset = index + 4
            continue
        if stream_id != _PS_VIDEO_SID:
            if index + 5 < data_len:
                pes_length = (data[index + 4] << 8) | data[index + 5]
                scan_offset = index + 6 + pes_length if pes_length > 0 else index + 4
            else:
                scan_offset = index + 4
            continue

        counts.video_stream_ids += 1
        if index + 13 >= data_len:
            scan_offset = max(scan_offset, data_len - 3)
            break
        pes_length = (data[index + 4] << 8) | data[index + 5]
        if (data[index + 7] & 0xC0) != 0 and index + 14 <= data_len:
            header_data_length = data[index + 8]
            pts = _decode_pes_pts(data, index + 9)
            if pts is not None and header_data_length >= 5:
                result.append((pts, index))
                counts.pts_values += 1

        scan_offset = index + 6 + pes_length if pes_length > 0 else index + 4
        if len(result) >= 2_000_000:
            break
    return result, scan_offset, counts


def _scan_vob_pts(
    inputs: list[Path], max_bytes: int = 512 * 1024 * 1024
) -> list[tuple[int, int]]:
    """Scan VOB data for video PTS values and their byte positions.

    Reads MPEG-PS video PES packets directly from the bitstream and extracts
    the PTS (90 kHz clock) from each frame's PES header. Returns a list of
    ``(pts, byte_offset)`` tuples in scanning order, or an empty list if no
    video PTS values are found.

    Scanning stops early once *max_bytes* have been examined (default 512 MB).
    Most DVDs need only 30-60 MB to get enough PTS samples for cell-boundary
    detection; the limit prevents pathological discs from scanning indefinitely.
    """
    total = sum(f.stat().st_size for f in inputs)
    if total == 0:
        return []
    scan_limit = min(max_bytes, total)
    log_debug(
        f"VOB PTS scan: total={total} MB={total // (1024 * 1024)} scan_limit={scan_limit} MB={scan_limit // (1024 * 1024)}"
    )

    _CHUNK = 16 * 1024 * 1024
    data = bytearray()
    result: list[tuple[int, int]] = []
    scan_offset = 0
    read_offset = 0
    counts = _PtsScanCounts()
    did_try_read = False

    while read_offset < scan_limit:
        to_read = min(_CHUNK, scan_limit - read_offset)
        chunk = _read_concat_bytes(inputs, read_offset, to_read)
        if not chunk or len(chunk) == 0:
            log_debug(
                f"VOB PTS scan: _read_concat_bytes returned empty at offset {read_offset}"
            )
            break
        did_try_read = True
        data.extend(chunk)
        read_offset += to_read
        window_result, scan_offset, window_counts = _scan_vob_pts_window(
            data, scan_offset
        )
        result.extend(window_result)
        counts.start_codes += window_counts.start_codes
        counts.video_stream_ids += window_counts.video_stream_ids
        counts.pts_values += window_counts.pts_values
        if len(result) >= 2_000_000:
            break

    log_debug(
        f"VOB PTS scan: {len(result)} PTS entries, "
        f"{counts.start_codes} start codes, {counts.video_stream_ids} video SIDs, "
        f"{counts.pts_values} with PTS, "
        f"in {read_offset // (1024 * 1024)} MB"
        f" (did_try_read={did_try_read})"
    )
    return result


def _extract_spu_palette(
    spu_data: bytes, clut: list[tuple[int, int, int]] | None = None
) -> list[tuple[int, int, int]] | None:
    """Extract a 16-entry RGB palette from a DVD SPU's PCS command stream.

    Parses the opcode-based command stream (SET_COLOR 0x03, SET_CONTR
    0x04) to determine which pixel values are text, outline, and
    background.

    SET_CONTR alpha values are used to determine the **role** of each
    pixel value: alpha=0 is background (transparent), alpha>0 is
    visible (text or outline).  This is the only reliable way to
    disambiguate text from background — some discs use pixel 0 for
    text, others use pixel 2, and without SET_CONTR we can only guess.

    Alpha is NOT baked into the palette colours — the raw .sub SPU
    packets carry SET_CONTR per-event, and the player applies it at
    render time.

    Returns a 16-entry list of ``(R, G, B)`` tuples, or ``None`` if
    no SET_COLOR or SET_CONTR is found.
    """
    state = _parse_spu_control_state(spu_data)
    if state is None:
        return None

    return _build_spu_palette(state, clut)


def _default_vobsub_palette() -> str:
    """Return a VobSub palette string with white at all common text
    indices (1, 2, 3, 7, 13, 14, 15) and grey at anti-alias indices
    (4-6, 8-12).  Index 0 remains black (background, transparent via
    contrast).

    Since we have no reliable way to know which pixel value a given
    disc maps to text vs. outline, all non-background indices are set
    to white.  This ensures text is always white — the trade-off is
    that outline/shadow may be the same colour and thus invisible.

    References
    ----------
    - MultimediaWiki VOBsub article:
      https://wiki.multimedia.cx/index.php?title=VOBsub
    - deKonvoluted guide to VobSub colours:
      https://dekonvoluted.github.io/user%20guides/2011/01/22/
      manipulating-the-colors-of-a-vobsub-subtitle-stream.html
    - MakeMKV's default palette was the starting point; we expanded
      white to more indices to cover discs that use non-standard
      pixel-to-index mappings.
    """
    _raw = [
        (0x00, 0x00, 0x00),  # 0: black (background; Cats disc uses this for outline)
        (0xFF, 0xFF, 0xFF),  # 1: white
        (0xFF, 0xFF, 0xFF),  # 2: white
        (0xFF, 0xFF, 0xFF),  # 3: white
        (0x82, 0x82, 0x82),  # 4: medium grey (anti-alias)
        (0x82, 0x82, 0x82),  # 5
        (0x82, 0x82, 0x82),  # 6
        (0xFF, 0xFF, 0xFF),  # 7: white (text – Cats disc)
        (0x82, 0x82, 0x82),  # 8: medium grey
        (0xBA, 0xBA, 0xBA),  # 9: light grey
        (0x82, 0x82, 0x82),  # 10: medium grey
        (0x82, 0x82, 0x82),  # 11
        (0x82, 0x82, 0x82),  # 12
        (0xFF, 0xFF, 0xFF),  # 13: white
        (0xFF, 0xFF, 0xFF),  # 14: white
        (0xFF, 0xFF, 0xFF),  # 15: white (text – Belles disc)
    ]
    return ", ".join("%02x%02x%02x" % (r, g, b) for r, g, b in _raw)


def _vob_pes_skip(data: bytearray | bytes | mmap.mmap, idx: int) -> int:
    """Return the offset to advance past the PES packet at *idx*.

    Handles all MPEG-PS stream IDs found in DVD VOBs:
      - 0xBA (pack): advance by 4 so the caller will find the next start
        code within the same sector (PES packets like 0xBD/0xBF/0xE0
        follow the pack header inside each 2048-byte DVD sector).
      - 0xB9 (program end): 4 bytes total, advance by 4.
      - Others: use PES_packet_length at idx+4 (16-bit big-endian).

    Returns an offset relative to the start of the file (the caller should not
    advance before ``idx + 4`` to avoid infinite loops).
    """
    sid = data[idx + 3]
    if sid == _PS_PACK_SID:
        # Pack header (0xBA): Advance just past the start code.  The 0xBA
        # header is followed by PES packets (audio, video, subpictures)
        # within the same 2048-byte sector.  Previously this jumped to the
        # next sector boundary, which skipped all PES packets entirely.
        return idx + 4
    if sid == 0xB9 or idx + 5 > len(data):
        # Program end code (0xB9) has no length field.
        return idx + 4
    pes_len = (data[idx + 4] << 8) | data[idx + 5]
    if pes_len > 0:
        return idx + 6 + pes_len
    return idx + 4


def _try_scan_private2_for_spu(data: bytes) -> list[tuple[int, bytes]]:
    """Scan a private_stream_2 (0xBF) payload for VobSub SPU data.

    On some DVDs (particularly early Warner Bros.), subtitles are stored in
    0xBF packets without a standard sub_stream_id. We look for the distinctive
    VobSub SPU header pattern within the payload:

        [2B SPU_size] [2B start_offset=0x0004] [2B end_offset]

    Returns a list of ``(pts, spu_data)`` tuples (always pts=0 since 0xBF
    packets have no PTS extraction standard). Returns empty list if no valid
    SPU data is found.
    """
    results: list[tuple[int, bytes]] = []
    if len(data) < 6:
        return results
    # Scan the payload for the SPU header pattern:
    # A VobSub SPU starts with 2-byte total_size (equal to len + 2 for the
    # size field itself, when measured from the start of the SPU), then
    # a 4-byte header: [0x00, 0x04, end_hi, end_lo].
    # We look for 0x00 0x04 at any offset, then verify it's a valid SPU.
    i = 0
    while i < len(data) - 5:
        if data[i : i + 2] == b"\x00\x04" and i >= 2:
            # Found start_offset = 0x0004.  Read spu_size from bytes 0-1 (i-2)
            # and end_offset from bytes 4-5 (i+2).
            end_off = (data[i + 2] << 8) | data[i + 3]
            spu_size = (data[i - 2] << 8) | data[i - 1]
            # Validate: spu_size >= 6 (header), end_off > start_off (4),
            # spu_size >= end_off, and the full SPU fits in available data.
            if (
                spu_size >= 6
                and end_off > 4
                and spu_size >= end_off
                and i - 2 + spu_size <= len(data)
            ):
                spu_chunk = bytes(data[i - 2 : i - 2 + spu_size])
                results.append((0, spu_chunk))
                i += spu_size - 2  # Skip past this complete SPU
                continue
        i += 1
    return results


def _raw_vobsub_spu_scan(data: bytes, start: int = 0) -> list[tuple[int, int, bytes]]:
    """Scan raw VOB data for VobSub SPU packets outside PES containers.

    Some DVDs embed subtitle data in non-standard locations. This function
    scans for the pattern:
        [SPU_size (2B)] [00 04] [end_offset (2B BE)] [SPU data...]
    where 00 04 is the SPU start offset field.  The SPU_size (2 bytes
    before 00 04) is included in the returned data so mkvmerge can parse
    it correctly.

    Handles non-standard end_offset = 0 (meaning "use the full packet")
    by falling back to spu_size when available.

    Returns ``[(offset_in_file, sub_stream_id_hint, spu_data), ...]``.
    The sub_stream_id_hint is 0 (unknown) for raw-mode hits.
    The returned spu_data includes the full VobSub header
    (SPU_size + start_offset + end_offset + RLE data).
    """
    results: list[tuple[int, int, bytes]] = []
    if len(data) < 10:
        return results
    scan = start
    while scan < len(data) - 7:
        # Look for 0x00 0x04 (SPU start offset = 0x0004)
        idx = data.find(b"\x00\x04", scan)
        if idx < 0 or idx + 5 >= len(data):
            break
        # Read SPU_size (2 bytes before 0x00 0x04).
        if idx >= 2:
            spu_size = (data[idx - 2] << 8) | data[idx - 1]
        else:
            spu_size = 0
        end_off = (data[idx + 2] << 8) | data[idx + 3]

        # Determine the valid data range:
        # - Standard: end_off > 4, spu_size matches, content fits
        # - Non-standard (end_off = 0): use spu_size, or assume a
        #   reasonable chunk (up to end of the data we have).
        valid = False
        spu_data: bytes = b""
        data_start = idx - 2 if idx >= 2 else idx
        if end_off > 4 and end_off <= len(data) - idx and end_off < 65535:
            # Standard VobSub: end_offset points to end of pixel data.
            if spu_size == 0 or spu_size >= end_off:
                # spu_size should be end_off + 2, but be lenient.
                capture_len = end_off + (2 if idx >= 2 else 0)
                data_end = data_start + capture_len
                data_end = min(data_end, len(data))
                spu_data = bytes(data[data_start:data_end])
                valid = len(spu_data) >= 8
        elif end_off == 0 and spu_size > 4 and spu_size < 65535:
            # Non-standard: end_offset = 0, use spu_size.
            if spu_size <= len(data) - (idx - 2 if idx >= 2 else idx):
                capture_len = spu_size
                data_end = data_start + capture_len
                data_end = min(data_end, len(data))
                spu_data = bytes(data[data_start:data_end])
                valid = len(spu_data) >= 12

        if valid:
            results.append((data_start, 0, spu_data))
            scan = data_start + len(spu_data)
            continue
        scan = idx + 2
    return results


def _raw_scan_vob_subpictures(
    inputs: list[Path], max_bytes: int
) -> dict[int, list[tuple[int, bytes]]]:
    """Search VOB prefixes and tails directly for standalone SPU blobs."""
    result: dict[int, list[tuple[int, bytes]]] = {}
    total_raw = 0
    for vob_file in inputs:
        if not vob_file.exists():
            continue
        file_size = vob_file.stat().st_size
        file_limit = file_size if max_bytes <= 0 else min(max_bytes, file_size)
        raw_limit = min(file_limit, _RAW_SPU_SCAN_LIMIT)
        for tail in (False, True):
            if tail and file_size > _RAW_SPU_SCAN_LIMIT * 2:
                start = file_size - _RAW_SPU_SCAN_LIMIT
                length = _RAW_SPU_SCAN_LIMIT
                raw_data = _read_concat_bytes([vob_file], start, length)
            else:
                raw_data = _read_concat_bytes([vob_file], 0, raw_limit)
            if not raw_data:
                continue
            raw_hits = _raw_vobsub_spu_scan(raw_data)
            if raw_hits:
                total_raw += len(raw_hits)
                for _offset, _sid_hint, spu_data in raw_hits:
                    result.setdefault(0, []).append((0, spu_data))
                log_debug(
                    "  Raw SPU scan: %s -> %d SPU candidates"
                    % (vob_file.name, len(raw_hits))
                )
                break
    if total_raw > 0:
        log_debug("Raw SPU scan found %d total candidates" % total_raw)
    else:
        log_debug("Raw SPU scan found nothing either.")
    return result


def _parse_private1_packet(
    data: bytearray | bytes | mmap.mmap, offset: int, data_len: int
) -> _Private1Packet | None:
    """Parse one private stream 1 PES packet and its optional SPU payload."""
    if offset + 10 > data_len:
        return None
    pes_length = (data[offset + 4] << 8) | data[offset + 5]
    packet_end = offset + 6 + pes_length if pes_length > 0 else data_len
    packet_end = min(packet_end, data_len)

    if data[offset + 6] & 0xC0 == 0x80:
        header_length = data[offset + 8]
        sub_stream_offset = offset + 9 + header_length
    else:
        sub_stream_offset = offset + 6
    if sub_stream_offset + 1 > data_len or sub_stream_offset >= packet_end:
        return _Private1Packet(None, None)

    sub_stream_id = data[sub_stream_offset]
    spu_start = sub_stream_offset + 1
    if not (_PS_SUBP_START <= sub_stream_id <= _PS_SUBP_END):
        return _Private1Packet(packet_end, None)
    if spu_start >= packet_end:
        return _Private1Packet(packet_end, None)

    pts = 0
    if (data[offset + 6] & 0xC0) == 0x80 and (data[offset + 7] & 0xC0) != 0:
        decoded_pts = _decode_pes_pts(data, offset + 9)
        if decoded_pts is not None:
            pts = decoded_pts
    return _Private1Packet(
        packet_end,
        _SubpicturePayload(sub_stream_id, spu_start, pts),
    )


def _parse_private2_subpictures(
    data: bytearray | bytes | mmap.mmap, offset: int, data_len: int
) -> tuple[int, int, list[tuple[int, bytes]]]:
    """Parse private stream 2 data and retain any embedded SPU candidates."""
    if offset + 6 > data_len:
        return offset + 4, 0, []
    pes_length = (data[offset + 4] << 8) | data[offset + 5]
    packet_end = offset + 6 + pes_length if pes_length > 0 else data_len
    packet_end = min(packet_end, data_len)

    pts = 0
    if offset + 8 <= packet_end and data[offset + 6] & 0xC0 == 0x80:
        header_length = data[offset + 7]
        payload_start = offset + 8 + header_length
        if data[offset + 6] & 0x80:
            decoded_pts = _decode_pes_pts(data, offset + 8)
            if decoded_pts is not None:
                pts = decoded_pts
    else:
        payload_start = offset + 6

    if payload_start >= packet_end:
        return packet_end, pts, []
    payload = bytes(data[payload_start:packet_end])
    return packet_end, pts, _try_scan_private2_for_spu(payload)


def _scan_vob_subpicture_window(
    data: bytearray | bytes | mmap.mmap,
    source_name: str,
    window_offset: int,
    result: dict[int, list[tuple[int, bytes]]],
    spu_accumulator: _SpuAccumulator,
) -> _VobScanCounts:
    counts = _VobScanCounts()
    data_len = len(data)
    scan = 0
    diagnostic_count = 0
    diagnostic_limit = 5

    while scan < data_len - 3:
        index = data.find(b"\x00\x00\x01", scan)
        if index < 0:
            break
        if index + 4 >= data_len:
            scan = index + 1
            break
        stream_id = data[index + 3]
        if diagnostic_count < diagnostic_limit:
            log_debug(
                "  [DIAG] %s offset=0x%x start_code=00 00 01 %02x"
                % (source_name, window_offset + index, stream_id)
            )
            diagnostic_count += 1

        if stream_id == _PS_PRIVATE1_SID:
            counts.private_one += 1
            packet = _parse_private1_packet(data, index, data_len)
            if packet is None:
                scan = index + 4
                break
            if packet.end is None:
                scan = _vob_pes_skip(data, index)
                continue
            subpicture = packet.subpicture
            if subpicture is not None:
                counts.subpictures += 1
                spu_chunk = bytes(data[subpicture.start : packet.end])
                completed_spu = spu_accumulator.add(
                    subpicture.sub_stream_id, subpicture.pts, spu_chunk
                )
                if completed_spu is not None:
                    result.setdefault(subpicture.sub_stream_id, []).append(
                        completed_spu
                    )
            scan = max(packet.end, index + 4)
        elif stream_id == _PS_PRIVATE2_SID:
            counts.private_two += 1
            packet_end, pts, spu_chunks = _parse_private2_subpictures(
                data, index, data_len
            )
            if spu_chunks:
                counts.subpictures += len(spu_chunks)
                for _pts, spu_data in spu_chunks:
                    result.setdefault(0, []).append((pts, spu_data))
            scan = max(packet_end, index + 4)
        elif stream_id == _PS_PACK_SID:
            scan = index + 4
        else:
            scan = _vob_pes_skip(data, index)
    return counts


def _scan_vob_subpicture_file(
    vob_file: Path,
    file_limit: int,
    spu_accumulator: _SpuAccumulator,
) -> tuple[dict[int, list[tuple[int, bytes]]], _VobScanCounts]:
    result: dict[int, list[tuple[int, bytes]]] = {}
    counts = _VobScanCounts()
    scan_window = 64 * 1024 * 1024

    try:
        descriptor = os.open(str(vob_file), os.O_RDONLY | os.O_LARGEFILE)
    except OSError:
        log_debug(f"  VOB scan: could not open {vob_file.name}")
        return result, counts

    try:
        offset = 0
        while offset < file_limit:
            window_size = min(scan_window, file_limit - offset)
            try:
                data = mmap.mmap(
                    descriptor,
                    window_size,
                    access=mmap.ACCESS_READ,
                    offset=offset,
                )
            except (ValueError, OSError) as exc:
                log_debug(f"  mmap failed at offset {offset}: {exc}")
                break
            try:
                window_counts = _scan_vob_subpicture_window(
                    data, vob_file.name, offset, result, spu_accumulator
                )
                counts.private_one += window_counts.private_one
                counts.private_two += window_counts.private_two
                counts.subpictures += window_counts.subpictures
            finally:
                data.close()
            offset += window_size
    finally:
        os.close(descriptor)

    if counts.private_one > 0 or counts.private_two > 0:
        log_debug(
            "  VOB %s: %d private1, %d private2, %d subpic"
            % (
                vob_file.name,
                counts.private_one,
                counts.private_two,
                counts.subpictures,
            )
        )
    return result, counts


def _scan_vob_subpictures(
    inputs: list[Path],
    max_bytes: int = 0,
    *,
    debug: bool = False,
) -> dict[int, list[tuple[int, bytes]]]:
    """Scan VOB files for DVD subpicture SPU packets.

    Reads MPEG-PS private stream 1 (0xBD) PES packets and collects those
    with sub_stream_id in 0x20-0x3F (DVD subpictures).  Also checks
    private_stream_2 (0xBF) packets used by some Warner Bros. DVDs.

    Uses memory-mapped I/O (mmap) for efficient scanning of large VOB files,
    avoiding the chunk-boundary and buffer-management issues that affected
    the previous bytearray-based implementation.

    Returns ``{sub_stream_id: [(pts_90khz, spu_data), ...]}``.

    *max_bytes* limits per-file scanning (default 0 = scan entire file).
    """
    if not inputs:
        return {}

    result: dict[int, list[tuple[int, bytes]]] = {}
    totals = _VobScanCounts()
    spu_accumulator = _SpuAccumulator()

    for vob_file in inputs:
        if not vob_file.exists():
            log_debug("  VOB scan: skipping %s (not found)" % vob_file.name)
            continue
        file_size = vob_file.stat().st_size
        if file_size == 0:
            continue
        file_limit = file_size if max_bytes <= 0 else min(max_bytes, file_size)

        file_result, counts = _scan_vob_subpicture_file(
            vob_file, file_limit, spu_accumulator
        )
        for sub_stream_id, entries in file_result.items():
            result.setdefault(sub_stream_id, []).extend(entries)
        totals.private_one += counts.private_one
        totals.private_two += counts.private_two
        totals.subpictures += counts.subpictures

    # If standard PES-based scanning found nothing, try raw SPU pattern scan
    # as a last resort (scan all VOBs for \x00\x04 SPU headers).
    if not result and inputs:
        log_debug("PES-based scan found no subpictures; trying raw SPU pattern scan...")
        for sid, entries in _raw_scan_vob_subpictures(inputs, max_bytes).items():
            result.setdefault(sid, []).extend(entries)

    # Flush any remaining SPUs in the accumulator (PTS-based boundaries already
    # handled all splits; just emit everything remaining, no spu_size truncation).
    for sid, entries in spu_accumulator.flush().items():
        result.setdefault(sid, []).extend(entries)

    # Diagnostic: validate first few SPU entries
    if debug and result:
        for sid in sorted(result):
            entries = result[sid]
            if entries:
                spu_data = entries[0][1]
                if len(spu_data) >= 6:
                    spu_size = (spu_data[0] << 8) | spu_data[1]
                    start_off = (spu_data[2] << 8) | spu_data[3]
                    end_off = (spu_data[4] << 8) | spu_data[5]
                    log_debug(
                        "  SPU diag: sid=0x%02x first SPU: spu_size=%d start_off=%d end_off=%d actual=%d"
                        % (sid, spu_size, start_off, end_off, len(spu_data))
                    )
                else:
                    log_debug(
                        "  SPU diag: sid=0x%02x first SPU too short: %d bytes"
                        % (sid, len(spu_data))
                    )

    log_debug(
        "VobSub scan: %d private1, %d private2, %d subpicture entries, "
        "%d stream(s)"
        % (
            totals.private_one,
            totals.private_two,
            sum(len(v) for v in result.values()),
            len(result),
        )
    )
    if result:
        for sub_id in sorted(result):
            entries = result[sub_id]
            first_pts = entries[0][0] / 90000.0 if entries else 0
            log_debug(
                "  sub_id=0x%02x: %d SPU packets (%.3fs first)"
                % (sub_id, len(entries), first_pts)
            )
    return result


def _encode_pts(pts: int) -> bytes:
    """Encode a 33-bit PTS value into 5 PES-format bytes.

    PES PTS layout: ``'0010' + PTS[32:30] + '1' + PTS[29:15] + '1' + PTS[14:0] + '1'``
    """
    pts &= 0x1FFFFFFFF  # Clamp to 33 bits
    p32_30 = (pts >> 30) & 0x07
    p29_15 = (pts >> 15) & 0x7FFF
    p14_0 = pts & 0x7FFF
    b0 = 0x21 | (p32_30 << 1)  # 0010 + PTS[32:30] + marker(1)
    b1 = (p29_15 >> 7) & 0xFF  # PTS[29:22]
    b2 = ((p29_15 & 0x7F) << 1) | 0x01  # PTS[21:15] + marker(1)
    b3 = (p14_0 >> 7) & 0xFF  # PTS[14:7]
    b4 = ((p14_0 & 0x7F) << 1) | 0x01  # PTS[6:0] + marker(1)
    return bytes([b0, b1, b2, b3, b4])


_PACK_HEADER_TEMPLATE = bytes(
    [
        0x00,
        0x00,
        0x01,
        0xBA,  # pack_start_code
        0x44,
        0x00,
        0x00,
        0x00,
        0x00,
        0x01,  # SCR (filled later)
        0x00,
        0x00,
        0xC0,  # mux_rate (10.08 Mbps)
        0x00,  # stuffing_length = 0
    ]
)

_VOBSUB_SECTOR_SIZE = 2048
_SECTOR_PES_MAX = (
    _VOBSUB_SECTOR_SIZE - 14 - 6
)  # pack_header(14) + PES_start_code(4) + length(2)


def _build_pes_entry(spu_data: bytes, pts: int, sub_stream_id: int) -> list[bytearray]:
    """Build one or more VobSub sectors for a single SPU entry.

    Returns a list of 2048-byte bytearrays.  The first sector carries the
    pack header + PES header (with PTS) + sub_stream_id + first part of the
    SPU.  If the SPU is too large to fit in one sector, subsequent sectors
    carry continuation PES packets (no PTS) + sub_stream_id + remaining
    data.  This matches MakeMKV's VobSub output format exactly, including
    keeping ``end_off=0`` in the SPU header (PES packet length serves as
    the effective boundary).
    """
    # Do NOT patch end_off=0 here.  In PES-wrapped VobSub the PES packet
    # length provides the real boundary, which is what mkvmerge relies on.
    # MakeMKV keeps end_off=0 in its .sub output.
    sectors: list[bytearray] = []

    def _make_sector(data_block: bytes, scr_val: int, is_first: bool) -> bytearray:
        """Build a single 2048-byte sector."""
        sect = bytearray(_PACK_HEADER_TEMPLATE)
        # Set SCR (bytes 4-9)
        for i in range(6):
            sect[4 + i] = (scr_val >> (40 - i * 8)) & 0xFF

        if is_first:
            # PES header with PTS: sub_stream_id is embedded in data_block
            b6 = 0x80 | (1 if pts else 0)
            b7 = 0x80 if pts else 0x00
            pes_hdr = bytearray(b"\x00\x00\x01\xbd")
            extra = 5 if pts else 0
            hdr_len = extra
            pes_len = 3 + hdr_len + len(data_block)
            pes_hdr.extend([(pes_len >> 8) & 0xFF, pes_len & 0xFF])
            pes_hdr.append(b6)
            pes_hdr.append(b7)
            pes_hdr.append(hdr_len)
            if pts:
                pes_hdr.extend(_encode_pts(pts))
            sect.extend(pes_hdr)
        else:
            # Continuation PES: no PTS, but includes sub_stream_id
            # (MakeMKV includes sub_id in EVERY sector, not just the first)
            pes_hdr = bytearray(b"\x00\x00\x01\xbd")
            pes_len = 3 + 0 + len(data_block)  # flags(2) + hdr_len(1) + payload
            pes_hdr.extend([(pes_len >> 8) & 0xFF, pes_len & 0xFF])
            pes_hdr.append(0x81)  # marker_bits=10, original=1
            pes_hdr.append(0x00)  # no PTS/DTS
            pes_hdr.append(0x00)  # hdr_len = 0
            sect.extend(pes_hdr)

        sect.extend(data_block)
        # Pad to 2048 bytes
        if len(sect) < _VOBSUB_SECTOR_SIZE:
            sect.extend(b"\x00" * (_VOBSUB_SECTOR_SIZE - len(sect)))
        return sect

    raw_payload = spu_data

    # Build SCR once (based on PTS)
    scr_base = pts
    scr_ext = 0
    p32_30 = (scr_base >> 30) & 0x07
    p29_15 = (scr_base >> 15) & 0x7FFF
    p14_0 = scr_base & 0x7FFF
    scr_val = 0
    scr_val |= 0x01 << 46
    scr_val |= p32_30 << 43
    scr_val |= 1 << 42
    scr_val |= p29_15 << 27
    scr_val |= 1 << 26
    scr_val |= p14_0 << 11
    scr_val |= 1 << 10
    scr_val |= scr_ext << 1
    scr_val |= 1

    # First sector: pack_hdr(14) + PES_hdr(15: start_code(4)+len(2)+flags(3)+PTS(5)+sub_id(1))
    # Available for payload in first sector:
    first_overhead = 14 + 4 + 2 + 3 + 5 + 1  # 29 bytes
    first_available = _VOBSUB_SECTOR_SIZE - first_overhead

    if len(raw_payload) <= first_available:
        # Fits in one sector
        block = bytes([sub_stream_id]) + raw_payload
        sect = _make_sector(block, scr_val, True)
        sectors.append(sect)
    else:
        # Split across multiple sectors
        # First sector: sub_stream_id + first part
        first_remaining = first_available
        first_payload = raw_payload[:first_remaining]
        payload_consumed = first_remaining
        sect = _make_sector(bytes([sub_stream_id]) + first_payload, scr_val, True)
        sectors.append(sect)

        # Continuation sectors: include sub_stream_id in every sector
        # (matching MakeMKV's format)
        cont_overhead = 14 + 4 + 2 + 3 + 1  # pack_hdr + PES_hdr + sub_id(1)
        cont_available = _VOBSUB_SECTOR_SIZE - cont_overhead
        while payload_consumed < len(raw_payload):
            chunk = raw_payload[payload_consumed : payload_consumed + cont_available]
            payload_consumed += len(chunk)
            sect = _make_sector(bytes([sub_stream_id]) + chunk, scr_val, False)
            sectors.append(sect)

    return sectors


def _build_vobsub_subfile(
    spus_by_id: dict[int, list[tuple[int, bytes]]],
) -> tuple[bytearray, dict[int, list[tuple[int, int]]]]:
    """Interleave SPU sectors by PTS and record each event's file position."""
    entries: list[tuple[int, int, bytes]] = []
    for sub_stream_id in sorted(spus_by_id):
        entries.extend(
            (pts, sub_stream_id, spu_data)
            for pts, spu_data in spus_by_id[sub_stream_id]
        )
    entries.sort(key=lambda entry: entry[0])

    sub_data = bytearray()
    track_positions: dict[int, list[tuple[int, int]]] = {}
    for pts, sub_stream_id, spu_data in entries:
        sectors = _build_pes_entry(spu_data, pts, sub_stream_id)
        track_positions.setdefault(sub_stream_id, []).append((pts, len(sub_data)))
        for sector in sectors:
            sub_data.extend(sector)
    return sub_data, track_positions


def _select_vobsub_palette(
    spus_by_id: dict[int, list[tuple[int, bytes]]],
    ifo_palette: list[tuple[int, int, int]] | None,
) -> list[tuple[int, int, int]] | None:
    """Choose an IFO CLUT, SPU control palette, or the default fallback."""
    found_palette: list[tuple[int, int, int]] | None = None
    valid_clut: list[tuple[int, int, int]] | None = None
    if ifo_palette is not None and len(ifo_palette) == 16:
        if any(red or green or blue for red, green, blue in ifo_palette):
            found_palette = ifo_palette
            valid_clut = ifo_palette
            log_debug("VobSub: using IFO PGC palette")
        else:
            log_debug("VobSub: IFO palette is all-black (zeroed); trying SPU")

    if found_palette is not None:
        return found_palette
    for sub_stream_id in sorted(spus_by_id):
        for entry_index, (_pts, spu_data) in enumerate(spus_by_id[sub_stream_id][:10]):
            found_palette = _extract_spu_palette(spu_data, clut=valid_clut)
            if found_palette is not None:
                log_debug(
                    "VobSub: using SPU SET_COLOR palette "
                    "(entry %d of sid=0x%02x, %d entries)"
                    % (entry_index, sub_stream_id, len(found_palette))
                )
                return found_palette
    return None


def _format_vobsub_timestamp(pts: int, pts_offset: int) -> str:
    seconds = max(0, pts - pts_offset) / 90000.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds - hours * 3600 - minutes * 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    return "%02d:%02d:%02d:%03d" % (hours, minutes, whole_seconds, milliseconds)


def _build_vobsub_idx_lines(
    sorted_ids: list[int],
    track_positions: dict[int, list[tuple[int, int]]],
    lang_by_id: dict[int, str],
    forced_by_id: dict[int, bool],
    found_palette: list[tuple[int, int, int]] | None,
    pts_offset: int,
) -> list[str]:
    palette = found_palette or []
    palette_string = (
        ", ".join("%02x%02x%02x" % (red, green, blue) for red, green, blue in palette)
        if palette
        else _default_vobsub_palette()
    )
    lines = [
        "# VobSub index file, v7 (do not modify this line!)",
        "",
        "size: 720x480",
        "org: 0, 0",
        "alpha: 100%",
        "smooth: OFF",
        "fadein/out: 50, 50",
        "align: OFF at LEFT TOP",
        "time offset: 0",
        "forced subs: OFF",
        "langidx: 0",
        "palette: %s" % palette_string,
        "",
    ]

    for track_index, sub_stream_id in enumerate(sorted_ids):
        positions = sorted(track_positions.get(sub_stream_id, []))
        language_three = lang_by_id.get(sub_stream_id, "und")
        language_two = _LANG_MAP_3_TO_2.get(
            language_three,
            "en" if language_three == "und" else language_three[:2],
        )
        language_name = get_language_name(language_three) or "Unknown"
        lines.append("id: %s, index: %d" % (language_two, track_index))
        if language_three != "und":
            lines.append("# %s: %s" % (language_name, language_three))
        if forced_by_id.get(sub_stream_id, False):
            lines.append("forced: on")
        lines.extend(
            "timestamp: %s, filepos: %09x"
            % (_format_vobsub_timestamp(pts, pts_offset), file_position)
            for pts, file_position in positions
        )
        lines.append("")
    return lines


def _verify_vobsub_tracks(idx_path: Path) -> list[dict[str, Any]]:
    if not _HAS_MKVMERGE:
        return []
    try:
        process = subprocess.run(
            ["mkvmerge", "-J", str(idx_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if process.returncode != 0:
            log_debug("mkvmerge -J failed on .idx file")
            return []
        tracks = json.loads(process.stdout).get("tracks", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log_debug("mkvmerge -J exception on .idx: %s" % exc)
        return []

    log_debug(
        "VobSub .idx verified: %d track(s): %s"
        % (
            len(tracks),
            ", ".join(
                "%s lang=%s"
                % (
                    track.get("codec", "?"),
                    track.get("properties", {}).get("language", "?"),
                )
                for track in tracks
            ),
        )
    )
    return tracks


def _filter_vobsub_streams(
    spus_by_id: dict[int, list[tuple[int, bytes]]],
    lang_by_id: dict[int, str],
) -> tuple[dict[int, list[tuple[int, bytes]]], dict[int, str]]:
    """Filter VOB streams against IFO metadata and recover raw stream zero."""
    filtered: dict[int, list[tuple[int, bytes]]] = {}
    languages = dict(lang_by_id)
    if not lang_by_id:
        return dict(spus_by_id), languages

    missing_ifo_ids = [
        sub_stream_id
        for sub_stream_id in sorted(lang_by_id)
        if not spus_by_id.get(sub_stream_id)
    ]
    for sub_stream_id in sorted(spus_by_id):
        entries = spus_by_id[sub_stream_id]
        if sub_stream_id in lang_by_id:
            filtered[sub_stream_id] = entries
            continue
        if sub_stream_id == 0 and missing_ifo_ids:
            sorted_entries = sorted(entries, key=lambda entry: entry[0])
            for entry_index, entry in enumerate(sorted_entries):
                target_id = missing_ifo_ids[entry_index % len(missing_ifo_ids)]
                filtered.setdefault(target_id, []).append(entry)
            log_debug(
                "  Distributed %d sub_id=0 entries across %d missing IFO sub_ids: %s"
                % (
                    len(sorted_entries),
                    len(missing_ifo_ids),
                    ", ".join("0x%02x" % stream_id for stream_id in missing_ifo_ids),
                )
            )
            continue
        if sub_stream_id == 0:
            log_debug(
                "  Skipped sub_id=0 distribution "
                "(all IFO sub_ids already present from PES scan)"
            )
            continue

        log_debug(
            "  Including sub_id 0x%02x from VOB (not in IFO, lang=und)" % sub_stream_id
        )
        filtered[sub_stream_id] = entries
        languages[sub_stream_id] = "und"
    return filtered, languages


def _vobsub_pts_offset(vob_paths: list[Path]) -> int:
    """Return the first video PTS used to normalize VobSub timestamps."""
    try:
        first_pts = _scan_vob_pts(vob_paths, max_bytes=32 * 1024 * 1024)
        if first_pts:
            pts_offset = first_pts[0][0]
            log_debug(
                "VobSub: normalizing subtitle PTS to video baseline "
                f"{pts_offset / 90000.0:.3f}s"
            )
            return pts_offset
    except Exception as exc:
        log_debug(f"VobSub: PTS baseline scan failed ({exc}); using offset 0")
    return 0


def _write_vobsub_files(
    spus_by_id: dict[int, list[tuple[int, bytes]]],
    lang_by_id: dict[int, str],
    forced_by_id: dict[int, bool],
    base: Path,
    ifo_palette: list[tuple[int, int, int]] | None = None,
    pts_offset: int = 0,
    *,
    temp_files: list[Path],
    debug: bool = False,
) -> tuple[Path, list[dict[str, Any]]] | None:
    """Write VobSub .idx and .sub files from scanned SPU data.

    Uses MPEG-PS PES-wrapped format (pack header + PES packet per SPU,
    padded to 2048-byte sectors), matching MakeMKV's VobSub output.

    Palette priority:
    1. IFO PGC palette (when it has real luminance — Y > 0 entries).
    2. SPU SET_COLOR control sequence (rare in practice).
    3. Greyscale fallback with white at all non-background indices.

    spus_by_id: ``{sub_stream_id: [(pts, spu_data), ...]}``
    lang_by_id: ``{sub_stream_id: iso_639_2_code}``
    forced_by_id: ``{sub_stream_id: is_forced}``
    base: base path (.idx and .sub are derived from it)
    ifo_palette: optional 16-entry RGB palette from the IFO's PGC.
    pts_offset: 90 kHz-clock PTS value of the muxed video/audio's own first
        packet in the same (trimmed) VOB. mkvmerge normalizes video/audio it
        reads directly from the VOB so the first packet becomes time zero;
        our SPU timestamps are computed independently from the same raw PTS
        clock, so subtracting this offset keeps subtitles in sync with the
        muxed video instead of trailing by the VOB's true (non-zero) start
        PTS - most noticeable as subtitles appearing at a fixed offset from
        their correct time throughout the whole file.

    Returns ``(idx_path, track_list)`` on success, ``None`` on failure.
    """

    if not spus_by_id:
        log_debug("_write_vobsub_files: no SPU data, skipping")
        return None

    sub_path = base.with_suffix(".sub")
    idx_path = base.with_suffix(".idx")
    sorted_ids = sorted(spus_by_id)
    sub_data, track_positions = _build_vobsub_subfile(spus_by_id)

    sub_path.write_bytes(bytes(sub_data))
    temp_files.append(sub_path)

    found_palette = _select_vobsub_palette(spus_by_id, ifo_palette)
    if found_palette is None:
        log_debug("VobSub: using default greyscale palette (no valid IFO/SPU palette)")
    lines = _build_vobsub_idx_lines(
        sorted_ids,
        track_positions,
        lang_by_id,
        forced_by_id,
        found_palette,
        pts_offset,
    )

    idx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_files.append(idx_path)

    # Debug: save a copy alongside the real files (same temp dir, tracked for cleanup)
    if debug:
        try:
            debug_sub = base.with_name(base.name + "_debug.sub")
            debug_idx = base.with_name(base.name + "_debug.idx")
            debug_sub.write_bytes(bytes(sub_data))
            debug_idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temp_files.append(debug_sub)
            temp_files.append(debug_idx)
            log_debug(f"VobSub debug files saved: {debug_idx}")
        except Exception as e:
            log_debug(f"Failed to save debug VobSub files: {e}")

    log_debug(
        "Wrote VobSub .idx/.sub: %d tracks, %d KiB .sub"
        % (len(sorted_ids), len(sub_data) // 1024)
    )

    return idx_path, _verify_vobsub_tracks(idx_path)


def _extract_dvd_vobsubs(
    vob_paths: list[Path],
    lang_by_id: dict[int, str] | None = None,
    forced_by_id: dict[int, bool] | None = None,
    ifo_palette: list[tuple[int, int, int]] | None = None,
    vobu_parts: list[Path] | None = None,
    vobu_part_sizes: list[int] | None = None,
    total_duration: float = 0.0,
    *,
    temp_files: list[Path],
    debug: bool = False,
) -> tuple[Path, list[dict[str, Any]]] | None:
    """Extract DVD VobSub subtitles from VOB files by scanning the MPEG-PS
    bitstream directly.

    This direct parser avoids an external extraction dependency. mkvmerge cannot
    detect DVD subpicture streams in VOB files, so we parse the
    private_stream_1 (0xBD) PES packets ourselves.

    vob_paths: VOB file(s) to scan. Use the trimmed VOB when available.
    lang_by_id: {sub_stream_id: iso_639_2_code} from the IFO.
    forced_by_id: {sub_stream_id: is_forced} from the IFO.
    ifo_palette: optional 16-entry RGB palette from the VTS IFO PGC.
    vobu_parts: when the VOB was assembled from non-contiguous VOBU runs
        (seamless-branching discs), this is the list of per-run temp files.
        Each part has a continuous PTS timeline, but the concatenated VOB has
        PTS discontinuities at run boundaries. When provided, subtitles are
        scanned per-part and PTS values are remapped to form a continuous
        timeline matching the muxed video.

    Returns (idx_path, track_list) on success, None on failure.
    """
    if not vob_paths:
        log_debug("_extract_dvd_vobsubs: no VOB paths")
        return None
    if lang_by_id is None:
        lang_by_id = {}
    if forced_by_id is None:
        forced_by_id = {}

    log_debug(
        "_extract_dvd_vobsubs: scanning %d file(s), IFO has %d sub(s)"
        % (len(vob_paths), len(lang_by_id))
    )

    if vobu_parts and len(vobu_parts) > 1:
        # Seamless-branching: the concatenated VOB has PTS discontinuities at
        # each VOBU run boundary. Per-part PTS remapping is unreliable because
        # interleaved cells can reset their PTS mid-run. Instead, scan the
        # entire concatenated VOB as a single stream and normalize PTS by the
        # first video PTS — this is the same approach used for non-branching
        # discs, and while not perfectly accurate at discontinuities, it avoids
        # the compounding timing errors of per-part remapping.
        log_debug(
            "VobSub: seamless-branching with VOBU parts — "
            "using concatenated VOB scan (no per-part remap)"
        )
    spus = _scan_vob_subpictures(vob_paths, debug=debug)
    if not spus:
        log_warn(
            "DVD VobSub scan found no subpicture streams in the input VOBs. "
            "The movie may not have subtitles encoded in the scanned range."
        )
        return None

    filtered, lang_by_id = _filter_vobsub_streams(spus, lang_by_id)

    if not filtered:
        log_debug("_extract_dvd_vobsubs: no IFO-matching subpicture streams")
        return None

    pts_offset = _vobsub_pts_offset(vob_paths)
    base = Path(tempfile.NamedTemporaryFile(suffix="_vobsub", delete=False).name)
    temp_files.append(base)
    return _write_vobsub_files(
        filtered,
        lang_by_id,
        forced_by_id,
        base,
        ifo_palette=ifo_palette,
        pts_offset=pts_offset,
        temp_files=temp_files,
        debug=debug,
    )


def _dvd_main_content_range(inputs: list[Path]) -> tuple[int, int] | None:
    """Return the (start_byte, end_byte) of the movie in a multi-cell DVD VOB.

    Scans the video packet PTS across the concatenated inputs to find
    PTS-continuous segments (each bounded by a backward jump = a cell reset) and
    picks the longest one - the main feature. Returns None when the content is a
    single clean run (no warning/intro cells) and needs no trimming, or if the
    scan fails for any reason (callers fall back to the raw VOBs).
    """
    if not inputs:
        return None

    scanned = _scan_vob_pts(inputs)
    if len(scanned) < 2:
        log_debug(
            f"DVD PTS scan: only {len(scanned)} PTS entries found, skipping cell detection"
        )
        return None

    pts = [p for p, _ in scanned]
    pos = [b for _, b in scanned]

    # Build PTS-continuous runs, splitting at backward jumps (cell resets).
    segments: list[tuple[int, int, int]] = []  # (start_i, end_i, dur_ticks)
    seg_start = 0
    for i in range(1, len(pts)):
        if pts[i] < pts[i - 1] - _CELL_PTS_RESET_TICKS:
            segments.append((seg_start, i - 1, pts[i - 1] - pts[seg_start]))
            seg_start = i
    segments.append((seg_start, len(pts) - 1, pts[-1] - pts[seg_start]))

    total_ticks = sum(s[2] for s in segments)
    if total_ticks <= 0:
        return None
    longest = max(segments, key=lambda s: s[2])
    # If the movie run is essentially the whole thing, there is nothing to trim.
    if longest[2] >= total_ticks * 0.98:
        log_debug(
            "DVD PTS scan: single continuous run "
            f"({longest[2] / 90000:.0f}s / {total_ticks / 90000:.0f}s), no trimming needed"
        )
        return None

    total = sum(f.stat().st_size for f in inputs)
    start_pos = pos[longest[0]]
    # End at the start of the following cell (clean boundary), or end of stream.
    next_seg_idx = longest[1] + 1
    end_pos = pos[next_seg_idx] if next_seg_idx < len(pos) else total

    start = _snap_to_pack(inputs, start_pos, total)
    end = _snap_to_pack(inputs, end_pos, total) if end_pos < total else total
    if end <= start:
        return None
    log_debug(
        f"DVD cell trim: movie is {longest[2] / 90000:.0f}s of "
        f"{total_ticks / 90000:.0f}s; extracting bytes {start}-{end}"
    )
    return start, end


def _extract_concat_range(inputs: list[Path], start: int, end: int, out: Path) -> Path:
    """Copy global byte range [start, end) across ``inputs`` into ``out``."""
    remaining = end - start
    cursor = start
    with open(out, "wb") as out_f:
        for f, fstart, fend in _concat_file_layout(inputs):
            if fend <= cursor or remaining <= 0:
                continue
            local = cursor - fstart
            with open(f, "rb") as fh:
                fh.seek(local)
                while remaining > 0:
                    chunk = fh.read(min(1024 * 1024, remaining, fend - cursor))
                    if not chunk:
                        break
                    out_f.write(chunk)
                    remaining -= len(chunk)
                    cursor += len(chunk)
            if remaining <= 0:
                break
    return out
