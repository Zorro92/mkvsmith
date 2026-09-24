"""DVD closed-caption (EIA-608 / CEA-608) extraction from VOB user data.

DVDs carry Line 21 closed captions inside the MPEG-2 video elementary
stream: GOP-header user data starting with ``43 43 01 f8`` ("CC", the
DVD-CC wrapper; see PixelTools' "TechTips - Closed Captioning" for the
format) followed by up to fifteen 6-byte groups whose last two bytes form
one odd-parity EIA-608 pair — up to 30 characters per GOP. The remaining
four bytes of each group are filler with invalid parity and are ignored,
as are pairs with broken parity.

The EIA-608 state machine (pop-on / roll-up / paint-on buffering, preamble
address codes, control codes, special character sets) follows FFmpeg's
``libavcodec/ccaption_dec.c`` — read as a reference per the project's
attribution policy, never invoked.

Captions are surfaced as timed ``CcCaptionEvent`` snapshots and written as
a SubRip (.srt) sidecar for muxing, matching MakeMKV's CC->Text track
behaviour ("S_CC608 ... CC→Text (Lossy conversion)").
"""

from __future__ import annotations

import mmap
import re
from dataclasses import dataclass
from pathlib import Path

from models import log_debug
from vobsub import _PtsTimelineRebaser, _video_pes_pts

# Codecs reported for the extracted text caption tracks (sidecar files
# muxed as extra mkvmerge inputs, replacing MakeMKV's S_CC608 track).
# "subrip" is the portable plain-text SRT view; "ass" carries the CC grid
# positioning (speaker column indents) and italics.
CC608_CODEC_SRT = "subrip"
CC608_CODEC_ASS = "ass"
CC608_CODECS = frozenset({CC608_CODEC_SRT, CC608_CODEC_ASS})

# DVD-CC user data layout (theneitherworld.com/mcpoodle/SCC_TOOLS,
# "DVD Closed Caption User Data Packets"): a 9-byte header — the user-data
# start code (00 00 01 b2), the DVD CC magic ("CC" 01 f8), and an
# attributes byte (bit 7 pattern flag: Field 1 then Field 2 per segment,
# or the reverse; bits 1-5 segment count; bit 0 extra-field flag) —
# followed by six-byte caption segments [field marker ff/fe][pair][field
# marker][pair], an optional 3-byte extra field, and 00 padding to a
# multiple of four. Field 1 carries the CC1 captions; Field 2 pairs are
# usually nulls (80 80, or 00 00 with broken parity on some encoders).
# Verified against Treasure Planet (2002, R1 DVD9), whose VOB parts use
# both pattern flags.
_CC_USER_DATA_PREFIX = b"\x43\x43\x01\xf8"
_USER_DATA_SEARCH = re.compile(b"\x00\x00\x01\xb2" + _CC_USER_DATA_PREFIX, re.DOTALL)
_VIDEO_PES_START = b"\x00\x00\x01\xe0"
_START_CODE = b"\x00\x00\x01"

# Field 1 marker inside a caption segment (0xff; 0xfe marks Field 2).
# Field 1's marker is 0xff in both segment patterns, including the
# capturing-device variation that uses 0xff for both fields.
_CC_FIELD_1_MARKER = 0xFF

# Attributes byte (block offset 4, after "CC" 01 f8).
_CC_ATTR_PATTERN = 0x80  # 1: Field 1 precedes Field 2 in each segment
_CC_ATTR_COUNT = 0x3E  # segment count, bits 1-5
_CC_ATTR_EXTRA = 0x01  # one extra 3-byte field follows the segments

_CC_SEGMENT_SIZE = 6

_SCREEN_ROWS = 15
_SCREEN_COLUMNS = 32

# Preamble address code row mapping: index = ((hi << 1) & 0x0e) |
# ((lo >> 5) & 0x01), value = 1-indexed caption row (ccaption_dec.c
# handle_pac row_map).
_PAC_ROW_MAP = (11, -1, 1, 2, 3, 4, 12, 13, 14, 15, 5, 6, 7, 8, 9, 10)

# PAC indent by lo & 0x1f (ccaption_dec.c pac2_attribs third column).
_PAC_INDENT = (
    (0,) * 18
    + (4,) * 2
    + (8,) * 2
    + (12,) * 2
    + (16,) * 2
    + (20,) * 2
    + (24,) * 2
    + (28,) * 2
)

# EIA-608 character set overrides (CEA-608-E; ccaption_dec.c
# CHARSET_OVERRIDE_LIST). Keys are the low 7 bits of the character byte.
_BASIC_AMERICAN = {
    0x27: "’",
    0x2A: "á",
    0x5C: "é",
    0x5E: "í",
    0x5F: "ó",
    0x60: "ú",
    0x7B: "ç",
    0x7C: "÷",
    0x7D: "Ñ",
    0x7E: "ñ",
    0x7F: "█",
}
_SPECIAL_AMERICAN = {
    0x30: "®",
    0x31: "°",
    0x32: "½",
    0x33: "¿",
    0x34: "™",
    0x35: "¢",
    0x36: "£",
    0x37: "♪",
    0x38: "à",
    0x3A: "è",
    0x3B: "â",
    0x3C: "ê",
    0x3D: "î",
    0x3E: "ô",
    0x3F: "û",
}
_EXTENDED_SPANISH_FRENCH = {
    0x20: "Á",
    0x21: "É",
    0x22: "Ó",
    0x23: "Ú",
    0x24: "Ü",
    0x25: "ü",
    0x26: "´",
    0x27: "¡",
    0x28: "*",
    0x29: "‘",
    0x2A: "-",
    0x2B: "©",
    0x2C: "℠",
    0x2D: "·",
    0x2E: "“",
    0x2F: "”",
    0x30: "À",
    0x31: "Â",
    0x32: "Ç",
    0x33: "È",
    0x34: "Ê",
    0x35: "Ë",
    0x36: "ë",
    0x37: "Î",
    0x38: "Ï",
    0x39: "ï",
    0x3A: "Ô",
    0x3B: "Ù",
    0x3C: "ù",
    0x3D: "Û",
    0x3E: "«",
    0x3F: "»",
}
_EXTENDED_PORTUGUESE_GERMAN = {
    0x20: "Ã",
    0x21: "ã",
    0x22: "Í",
    0x23: "Ì",
    0x24: "ì",
    0x25: "Ò",
    0x26: "ò",
    0x27: "Õ",
    0x28: "õ",
    0x29: "{",
    0x2A: "}",
    0x2B: "\\",
    0x2C: "^",
    0x2D: "_",
    0x2E: "|",
    0x2F: "~",
    0x30: "Ä",
    0x31: "ä",
    0x32: "Ö",
    0x33: "ö",
    0x34: "ß",
    0x35: "¥",
    0x36: "¤",
    0x37: "│",
    0x38: "Å",
    0x39: "å",
    0x3A: "Ø",
    0x3B: "ø",
    0x3C: "┐",
    0x3D: "┌",
    0x3E: "└",
    0x3F: "┘",
}


def _odd_parity(value: int) -> bool:
    return bin(value).count("1") % 2 == 1


@dataclass(frozen=True)
class CcStyledLine:
    """One caption line with its CC-grid placement.

    ``row`` is the 0-indexed screen row (0-14) and ``column`` the first
    non-space column (0-31) — together they reproduce the Line 21 speaker
    positioning that plain text flattens. ``segments`` are ``(text,
    italic)`` runs.
    """

    row: int
    column: int
    segments: tuple[tuple[str, bool], ...]

    def plain(self) -> str:
        return "".join(text for text, _italic in self.segments)


@dataclass(frozen=True)
class CcCaptionEvent:
    """One displayed-caption snapshot at a 90 kHz PTS.

    ``rows`` is the flattened plain-text view (one entry per used screen
    row; an empty tuple means the screen was cleared); ``styled`` carries
    the same caption with grid positioning and italic runs for the ASS
    writer.
    """

    pts: int
    rows: tuple[str, ...]
    styled: tuple[CcStyledLine, ...] = ()


class _CcScreen:
    """A 15x32 caption screen buffer (ccaption_dec.c ``struct Screen``).

    Cells past the write cursor hold ``None`` sentinels (FFmpeg's NUL
    terminator), hiding stale characters from earlier captions when a new
    caption overwrites only part of a reused row. A parallel ``italics``
    grid records the per-cell italic state for the ASS view.
    """

    def __init__(self) -> None:
        self.characters: list[list[str | None]] = [
            [None] * _SCREEN_COLUMNS for _ in range(_SCREEN_ROWS)
        ]
        self.italics: list[list[bool]] = [
            [False] * _SCREEN_COLUMNS for _ in range(_SCREEN_ROWS)
        ]
        self.row_used = [False] * _SCREEN_ROWS

    def clear(self) -> None:
        self.row_used = [False] * _SCREEN_ROWS

    def text_rows(self) -> tuple[str, ...]:
        rows: list[str] = []
        for row in range(_SCREEN_ROWS):
            if not self.row_used[row]:
                continue
            chars: list[str] = []
            for cell in self.characters[row]:
                if cell is None:
                    break
                chars.append(cell)
            line = "".join(chars).strip()
            if line:
                rows.append(line)
        return tuple(rows)

    def styled_rows(self) -> tuple[CcStyledLine, ...]:
        lines: list[CcStyledLine] = []
        for row in range(_SCREEN_ROWS):
            if not self.row_used[row]:
                continue
            cells = self.characters[row]
            italics = self.italics[row]
            end = 0
            for column in range(_SCREEN_COLUMNS):
                if cells[column] is None:
                    break
                end = column + 1
            # First non-space column: the CC indent that positions the
            # line under its speaker.
            start = 0
            while start < end and (cells[start] or " ").isspace():
                start += 1
            if start >= end:
                continue
            segments: list[tuple[str, bool]] = []
            for column in range(start, end):
                text = cells[column] or ""
                italic = italics[column]
                if segments and segments[-1][1] == italic:
                    segments[-1] = (segments[-1][0] + text, italic)
                else:
                    segments.append((text, italic))
            if "".join(text for text, _ in segments).strip():
                lines.append(
                    CcStyledLine(row=row, column=start, segments=tuple(segments))
                )
        return tuple(lines)


class _Cc608Decoder:
    """EIA-608 field-1 state machine (pop-on / roll-up / paint-on).

    Buffered model per FFmpeg ``ccaption_dec.c``: pop-on captions write to
    the inactive screen; End Of Caption flips the buffers and erases what
    was displayed; roll-up captions roll on carriage return. Each
    ``capture_screen`` produces one event.
    """

    def __init__(self) -> None:
        self.screens = (_CcScreen(), _CcScreen())
        self.active = 0
        self.mode = "popon"
        self.rollup_rows = 2
        self.cursor_row = 0
        self.cursor_column = 0
        self.cursor_italic = False
        self.charset = _BASIC_AMERICAN
        self.prev_cmd: tuple[int, int] = (0, 0)

    # -- screen handling ------------------------------------------------

    def _writing_screen(self) -> _CcScreen:
        # Pop-on writes to the inactive screen; direct modes write live.
        if self.mode == "popon":
            return self.screens[1 - self.active]
        return self.screens[self.active]

    def _write_char(self, ch: str | None) -> None:
        """Write one cell; ``None`` writes only the end-of-row sentinel."""
        screen = self._writing_screen()
        if self.cursor_row >= _SCREEN_ROWS:
            return
        if self.cursor_column >= _SCREEN_COLUMNS:
            if ch is not None:
                log_debug("cc608: character dropped, screen width exceeded")
            return
        if ch is not None:
            screen.characters[self.cursor_row][self.cursor_column] = ch
            screen.italics[self.cursor_row][self.cursor_column] = self.cursor_italic
            screen.row_used[self.cursor_row] = True
            self.cursor_column += 1
        # NUL terminator hides stale cells past the write cursor
        # (ccaption_dec.c write_char).
        if self.cursor_column < _SCREEN_COLUMNS:
            screen.characters[self.cursor_row][self.cursor_column] = None

    def _roll_up(self) -> None:
        """Scroll the writing screen up within the roll-up window."""
        if self.mode == "text":
            return
        screen = self._writing_screen()
        keep = min(self.cursor_row + 1, self.rollup_rows)
        # Rows outside the [cursor_row - keep, cursor_row] window are unused.
        for row in range(_SCREEN_ROWS):
            if not (self.cursor_row - keep < row <= self.cursor_row):
                screen.row_used[row] = False
        # Shift the window's rows up by one; the bottom row is cleared.
        for i in range(keep):
            row = self.cursor_row - keep + i + 1
            if row + 1 < _SCREEN_ROWS:
                screen.characters[row] = list(screen.characters[row + 1])
                screen.italics[row] = list(screen.italics[row + 1])
                screen.row_used[row] = screen.row_used[row + 1]
            else:
                screen.characters[row] = [None] * _SCREEN_COLUMNS
                screen.row_used[row] = False
        screen.row_used[self.cursor_row] = False
        screen.characters[self.cursor_row] = [None] * _SCREEN_COLUMNS

    def _delete_end_of_row(self) -> None:
        # Erase from the cursor to end of row: just move the sentinel
        # (ccaption_dec.c writes a single 0 at the cursor).
        screen = self._writing_screen()
        if self.cursor_row < _SCREEN_ROWS and self.cursor_column < _SCREEN_COLUMNS:
            screen.characters[self.cursor_row][self.cursor_column] = None

    def _capture(self, pts: int) -> CcCaptionEvent:
        screen = self.screens[self.active]
        return CcCaptionEvent(
            pts=pts, rows=screen.text_rows(), styled=screen.styled_rows()
        )

    def _handle_edm(self, pts: int) -> CcCaptionEvent:
        # Buffered mode: capture what was displayed before wiping it.
        event = self._capture(pts)
        self.screens[self.active].clear()
        return event

    # -- code handlers --------------------------------------------------

    def _handle_pac(self, hi: int, lo: int) -> None:
        index = ((hi << 1) & 0x0E) | ((lo >> 5) & 0x01)
        row = _PAC_ROW_MAP[index]
        if row <= 0:
            return
        self.cursor_row = row - 1
        self.cursor_column = 0
        self.charset = _BASIC_AMERICAN
        # PACs are "no formatting" except the italics pair (0x0E/0x0F of
        # the attribute bits, i.e. 0x4E/0x4F low bytes).
        self.cursor_italic = (lo & 0x1F) in (0x0E, 0x0F)
        indent = _PAC_INDENT[lo & 0x1F]
        screen = self._writing_screen()
        screen.row_used[self.cursor_row] = True
        for _ in range(indent):
            self._write_char(" ")

    def _handle_eoc(self, pts: int) -> CcCaptionEvent:
        # Flip buffers; the previously-displayed screen is captured and
        # wiped (buffered model), the newly-active screen becomes visible.
        self.active = 1 - self.active
        event = self._handle_edm(pts)
        self.cursor_column = 0
        return event

    def _handle_char(self, hi: int, lo: int) -> None:
        if hi == 0x11:
            self.charset = _SPECIAL_AMERICAN
        elif hi == 0x12:
            if self.cursor_column > 0:
                self.cursor_column -= 1
            self.charset = _EXTENDED_SPANISH_FRENCH
        elif hi == 0x13:
            if self.cursor_column > 0:
                self.cursor_column -= 1
            self.charset = _EXTENDED_PORTUGUESE_GERMAN
        else:
            self.charset = _BASIC_AMERICAN
            self._write_char(self.charset.get(hi, chr(hi)))
        if lo:
            self._write_char(self.charset.get(lo, chr(lo)))

    # -- pair dispatch ---------------------------------------------------

    def feed(self, hi: int, lo: int, pts: int) -> CcCaptionEvent | None:
        """Feed one parity-stripped pair; returns an event on screen change."""

        if (hi, lo) == self.prev_cmd:
            return None
        self.prev_cmd = (hi, lo)

        if (hi == 0x10 and 0x40 <= lo <= 0x5F) or (
            0x11 <= hi <= 0x17 and 0x40 <= lo <= 0x7F
        ):
            self._handle_pac(hi, lo)
        elif (hi == 0x11 and 0x20 <= lo <= 0x2F) or (hi == 0x17 and 0x2E <= lo <= 0x2F):
            # Mid-row text attribute: color/style codes clear italics
            # ("no formatting"); 0x2E/0x2F turn italics on.
            self.cursor_italic = lo in (0x2E, 0x2F)
            self.charset = _BASIC_AMERICAN
            self._write_char(" ")
        elif hi == 0x10 and 0x20 <= lo <= 0x2F:
            # Background attribute: styling only.
            pass
        elif hi in (0x14, 0x15, 0x1C):
            return self._handle_control(lo, pts)
        elif hi in (0x11, 0x12, 0x13):
            self._handle_char(hi, lo)
        elif hi >= 0x20:
            self._handle_char(hi, lo)
            self.prev_cmd = (0, 0)
        elif hi == 0x17 and 0x21 <= lo <= 0x23:
            for _ in range(lo - 0x20):
                self._handle_char(0x20, 0)
        return None

    def _handle_control(self, lo: int, pts: int) -> CcCaptionEvent | None:
        if lo == 0x20:  # Resume Caption Loading
            self.mode = "popon"
        elif lo == 0x24:  # Delete to End of Row
            self._delete_end_of_row()
        elif lo in (0x25, 0x26, 0x27):  # Roll-up 2/3/4
            self.rollup_rows = lo - 0x23
            self.mode = "rollup"
        elif lo == 0x29:  # Resume Direct Captioning
            self.mode = "painton"
        elif lo == 0x2B:  # Resume Text Display
            self.mode = "text"
        elif lo == 0x2C:  # Erase Displayed Memory
            return self._handle_edm(pts)
        elif lo == 0x2D:  # Carriage Return
            event = self._capture(pts) if self.mode != "popon" else None
            self._roll_up()
            self.cursor_column = 0
            return event
        elif lo == 0x2F:  # End Of Caption
            return self._handle_eoc(pts)
        return None


# =============================================================================
# VOB scanning
# =============================================================================


def _cc_pairs_from_block(block: bytes) -> list[tuple[int, int]]:
    """Extract Field-1 EIA-608 pairs from one DVD-CC user-data block.

    The attributes byte selects which triplet of each six-byte caption
    segment is Field 1 (pattern flag) and how many segments follow (count
    bits), plus an optional trailing 3-byte extra field. Pairs with broken
    parity (Field-2 nulls such as 00 00) are dropped.
    """
    pairs: list[tuple[int, int]] = []
    if len(block) < 5 or block[:4] != _CC_USER_DATA_PREFIX:
        return pairs
    attributes = block[4]
    pattern_field1_first = bool(attributes & _CC_ATTR_PATTERN)
    segment_count = (attributes & _CC_ATTR_COUNT) >> 1
    has_extra_field = bool(attributes & _CC_ATTR_EXTRA)

    # Field 1's triplet position within a segment: [marker][pair] first
    # for pattern 1, second ([other marker][pair]) for pattern 0.
    field1_offset = 0 if pattern_field1_first else 3

    def _take_field1(position: int) -> None:
        if position + 3 > len(block):
            return
        # Field 1's marker is 0xff in both patterns, including the
        # capturing-device variation that uses 0xff for both fields
        # (the pattern flag, not the marker, selects the position).
        if block[position] != _CC_FIELD_1_MARKER:
            return
        first, second = block[position + 1], block[position + 2]
        if _odd_parity(first) and _odd_parity(second):
            pairs.append((first & 0x7F, second & 0x7F))

    offset = 5
    for _ in range(segment_count):
        if offset + _CC_SEGMENT_SIZE > len(block):
            break
        _take_field1(offset + field1_offset)
        offset += _CC_SEGMENT_SIZE
    if has_extra_field:
        _take_field1(offset)
    return pairs


def _scan_vob_cc608_file(path: Path) -> list[tuple[int, tuple[int, int]]]:
    """Scan one VOB for (pts, pair) DVD-CC captions in stream order."""
    results: list[tuple[int, tuple[int, int]]] = []
    try:
        with path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                for match in _USER_DATA_SEARCH.finditer(data):
                    start = match.start() + 4  # at "CC"
                    end = data.find(_START_CODE, start)
                    if end == -1:
                        end = min(start + 96, len(data))
                    block = bytes(data[start:end])
                    pes_start = data.rfind(_VIDEO_PES_START, 0, match.start())
                    pts = _video_pes_pts(data, pes_start) if pes_start != -1 else None
                    for pair in _cc_pairs_from_block(block):
                        results.append((pts if pts is not None else 0, pair))
    except (OSError, ValueError) as exc:
        log_debug(f"cc608: scan skipped for {path.name}: {exc}")
    return results


def extract_cc608_captions(inputs: list[Path]) -> list[CcCaptionEvent]:
    """Decode closed-caption events from VOB files (or trimmed parts).

    The raw PTS of each caption's video PES is re-based into a continuous
    timeline via ``_PtsTimelineRebaser``: non-seamless DVDs restart the
    system clock at cell boundaries (raw PTS then jumps backward), so each
    reset is snapped to the end of the previous timeline — the same
    re-basing mkvmerge applies when it appends program-stream segments.
    Returns timed caption snapshots in playback order; empty when the
    input carries no DVD-CC user data.
    """
    decoder = _Cc608Decoder()
    events: list[CcCaptionEvent] = []
    rebaser = _PtsTimelineRebaser()
    pending_pts = 0
    for path in inputs:
        for raw_pts, pair in _scan_vob_cc608_file(path):
            if raw_pts:
                pending_pts = raw_pts
            event = decoder.feed(pair[0], pair[1], rebaser.rebase(pending_pts))
            if event is not None:
                events.append(event)
    return events


def _has_cc608_data(path: Path, max_bytes: int = 32 * 1024 * 1024) -> bool:
    """Whether a VOB prefix carries DVD-CC caption user data.

    Reads bounded chunks (overlapping by the maximum block size so a block
    straddling a chunk boundary is still found) and stops at the first hit
    — captions arrive once per GOP, so a disc that carries CC shows a block
    within the first couple of GOPs of muxed video.
    """
    chunk_size = 4 * 1024 * 1024
    overlap = 128
    tail = b""
    try:
        with path.open("rb") as handle:
            read_total = 0
            while read_total < max_bytes:
                chunk = handle.read(min(chunk_size, max_bytes - read_total))
                if not chunk:
                    break
                if _USER_DATA_SEARCH.search(tail + chunk) is not None:
                    return True
                tail = chunk[-overlap:]
                read_total += len(chunk)
    except OSError:
        return False
    return False


# =============================================================================
# SubRip / ASS output
# =============================================================================


def _format_srt_timestamp(pts: int) -> str:
    seconds = pts / 90000.0
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = round((seconds - int(seconds)) * 1000)
    if millis == 1000:
        millis = 0
        secs += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _caption_cues(
    events: list[CcCaptionEvent],
) -> list[tuple[int, int, CcCaptionEvent]]:
    """Group caption snapshots into display cues.

    A caption runs from its snapshot's PTS until the next differing
    snapshot (or a clear event); the final caption gets a five-second end.
    Cues key on the full displayed content (text, grid placement, and
    styling) so position-only changes re-render in positioned formats
    while producing identical SRT output.
    """
    cues: list[tuple[int, int, CcCaptionEvent]] = []
    shown: tuple[tuple[str, ...], tuple[CcStyledLine, ...]] | None = None
    current: CcCaptionEvent | None = None
    start = 0
    for event in events:
        key = (event.rows, event.styled)
        if key == shown:
            continue
        if shown is not None and current is not None:
            cues.append((start, event.pts, current))
        shown = key
        current = event if event.rows else None
        start = event.pts
    if shown is not None and current is not None:
        cues.append((start, start + 5 * 90000, current))
    return cues


def write_cc608_srt(
    events: list[CcCaptionEvent], out_path: Path, pts_offset: int = 0
) -> Path:
    """Write caption events as a SubRip file; *pts_offset* recentres time 0."""
    lines: list[str] = []
    for index, (start_pts, end_pts, event) in enumerate(_caption_cues(events), start=1):
        lines.append(str(index))
        lines.append(
            f"{_format_srt_timestamp(max(start_pts - pts_offset, 0))} --> "
            f"{_format_srt_timestamp(max(end_pts - pts_offset, 0))}"
        )
        lines.extend(event.rows)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _format_ass_timestamp(pts: int) -> str:
    # ASS uses H:MM:SS.cc (centiseconds), unlike SRT's milliseconds.
    centis = round(pts / 900.0)
    hours, remainder = divmod(centis, 360000)
    minutes, secs_cs = divmod(remainder, 6000)
    secs, cs = divmod(secs_cs, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_line_text(line: CcStyledLine) -> str:
    parts: list[str] = []
    for text, italic in line.segments:
        if italic:
            parts.append(f"{{\\i1}}{text}{{\\i0}}")
        else:
            parts.append(text)
    return "".join(parts)


def write_cc608_ass(
    events: list[CcCaptionEvent],
    out_path: Path,
    pts_offset: int = 0,
    resolution: tuple[int, int] = (720, 480),
) -> Path:
    """Write caption events as an ASS file preserving the CC grid layout.

    Each Line 21 row/column is mapped onto the video resolution so the
    horizontal speaker indents and top-row sound cues land where the
    broadcast captions did; italic runs (off-screen dialog, music) carry
    over as ``\\i`` tags. *pts_offset* recentres time 0.
    """
    play_x, play_y = resolution
    font_size = max(16, play_y // 15)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_x}\n"
        f"PlayResY: {play_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: CC,Courier New,{font_size},&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,10,10,10,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
        " Effect, Text\n"
    )
    lines: list[str] = [header]
    for start_pts, end_pts, event in _caption_cues(events):
        start = _format_ass_timestamp(max(start_pts - pts_offset, 0))
        end = _format_ass_timestamp(max(end_pts - pts_offset, 0))
        for line in event.styled:
            # CC rows/cols map onto the video frame; row baseline sits in
            # the lower third of its 1/15 band, columns on the 1/32 grid.
            x = round(line.column * play_x / _SCREEN_COLUMNS)
            y = round((line.row + 0.8) * play_y / _SCREEN_ROWS)
            text = _ass_line_text(line)
            lines.append(
                f"Dialogue: 0,{start},{end},CC,,0,0,0,,{{\\pos({x},{y})}}{text}"
            )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
