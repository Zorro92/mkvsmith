"""VC-1 frame times read from HD DVD EVOs, to correct mkvmerge's.

An EVO stamps only the first frame of each GOP with a PTS; every other
frame's time follows from the frame order and the 3:2 pulldown cadence
(each frame shows for two or three fields). mkvmerge loses that cadence
for the first GOP after a sequence header that starts a chapter: two
frames get duplicate timestamps and a ~133 ms hole follows, which players
show as a stutter. Its ``--timestamps`` files can't help, since mkvmerge
assigns them by its own (wrong) frame order.

So the times are rebuilt here from the frame headers (SMPTE 421M advanced
profile: frame type, RFF / RPTFRM) and written over the finished MKV's
block timestamps in place (``mkvtimes``). VC-1 in Matroska uses the VfW
mode, where block timestamps rise in storage order (the decoder adds the
reorder delay): a decoder shows each frame at the time of the block
after it, so block *i* gets the presentation time of frame *i - 1* and
the first block repeats the first, as mkvmerge times Blu-ray VC-1. Each
EVO's times hang off mkvmerge's time for its first frame, which carries
the EVO's first PTS. See docs/formats/hd-dvd.md.
"""

from __future__ import annotations

import mmap
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mkvsmith.i18n import tr
from mkvsmith.mkvtimes import is_vc1_track, read_track_times, write_block_times
from mkvsmith.models import log_debug

_PACK_START = b"\x00\x00\x01\xba"
_START_CODE = b"\x00\x00\x01"
_VIDEO_EXTENDED = 0xFD
_PRIMARY_VC1 = 0x55
_PTS_MODULO = 1 << 33
_TICKS_PER_SECOND = 90000
_SEQUENCE_HEADER = 0x0F
_ENTRY_POINT = 0x0E
_FRAME = 0x0D
# Bytes after a start code that hold every header field read here.
_HEADER_BYTES = 16
# FRAMERATENR / FRAMERATEDR codes (SMPTE 421M 6.1.14.4).
_FRAME_RATE_NR = {1: 24, 2: 25, 3: 30, 4: 50, 5: 60, 6: 48, 7: 72}
_FRAME_RATE_DR = {1: 1000, 2: 1001}
# A predicted frame time this far from the disc's PTS means the cadence
# was misread; more than _MAX_MISSES of them and the times aren't used.
_PTS_TOLERANCE = 90
_MAX_MISSES = 0.01
# Within this of mkvmerge's own time a frame counts as agreeing; most
# frames must agree, or the frames aren't the ones mkvmerge muxed. Over a
# field (16.7 ms): mkvmerge's cadence is often a field out of phase with
# the decoder's, while a frame out of line is 33 ms or more off. Where a
# stream drops its B frames mkvmerge runs frames off for long stretches
# (89% agreeing on one feature), so the bar is below that.
_AGREE_MS = 20.0
_MIN_AGREEMENT = 0.8
# mkvmerge's time for an EVO's first frame anchors the EVO; its other
# frames run a few frames late (~150 ms), so a first frame further than
# this from that run means the anchor isn't the frame it should be.
_MAX_ANCHOR_MS = 500.0
# An EVO whose first PTS lands this close after the previous EVO's last
# frame carries on its clock (a seamless join, read as one stream).
_CLOCK_JOIN_MS = 500.0


class FrameTimingError(ValueError):
    """The EVO frames can't be lined up with the MKV's; nothing was changed."""


@dataclass(frozen=True, slots=True)
class EvoFrameTimes:
    """An EVO's first PTS and its frames' sorted times from it (90 kHz)."""

    first_pts: int
    times: list[float]


@dataclass(slots=True)
class _Sequence:
    interlace: bool = False
    pulldown: bool = False
    frame_counter: bool = False
    progressive_segmented: bool = False
    field_ticks: float | None = None


@dataclass(slots=True)
class _Frame:
    shown_at_once: bool  # B and BI frames are shown when decoded
    fields: int
    pts: int | None = None


class _Bits:
    def __init__(self, data: bytes) -> None:
        self._value = int.from_bytes(data, "big")
        self._size = len(data) * 8
        self._pos = 0

    def get(self, count: int) -> int:
        self._pos += count
        if self._pos > self._size:
            raise ValueError("header truncated")
        return (self._value >> (self._size - self._pos)) & ((1 << count) - 1)


def _unescape(data: bytes) -> bytes:
    """Drop the emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _parse_sequence_header(data: bytes) -> _Sequence | None:
    """Advanced-profile sequence header fields, or None for another profile."""
    bits = _Bits(_unescape(data))
    if bits.get(2) != 3:
        return None
    # LEVEL, COLORDIFF_FORMAT, FRMRTQ/BITRTQ_POSTPROC, POSTPROCFLAG,
    # MAX_CODED_WIDTH, MAX_CODED_HEIGHT.
    bits.get(3 + 2 + 3 + 5 + 1 + 12 + 12)
    seq = _Sequence()
    seq.pulldown = bool(bits.get(1))
    seq.interlace = bool(bits.get(1))
    seq.frame_counter = bool(bits.get(1))
    bits.get(2)  # FINTERPFLAG, reserved
    seq.progressive_segmented = bool(bits.get(1))
    if bits.get(1):  # DISPLAY_EXT
        bits.get(14 + 14)
        if bits.get(1) and bits.get(4) == 15:  # ASPECT_RATIO_FLAG, ASPECT_RATIO
            bits.get(16)
        if bits.get(1):  # FRAMERATE_FLAG
            fps: float | None = None
            if bits.get(1):  # FRAMERATEIND: FRAMERATEEXP in 1/32 fps
                fps = (bits.get(16) + 1) / 32
            else:
                numerator = _FRAME_RATE_NR.get(bits.get(8))
                denominator = _FRAME_RATE_DR.get(bits.get(4))
                if numerator and denominator:
                    fps = numerator * 1000 / denominator
            if fps:
                seq.field_ticks = _TICKS_PER_SECOND / (2 * fps)
    return seq


def _parse_frame_header(data: bytes, seq: _Sequence) -> _Frame:
    """Frame type and displayed field count from an advanced-profile header."""
    bits = _Bits(_unescape(data))
    field_pictures = False
    if seq.interlace and bits.get(1):  # FCM: 0 progressive, 10 frame, 11 field
        field_pictures = bool(bits.get(1))
    if field_pictures:
        # FPTYPE: 0-3 pair I/P fields, 4-7 pair B/BI fields.
        shown_at_once = bits.get(3) >= 4
    else:
        # PTYPE: 0 P, 10 B, 110 I, 1110 BI, 1111 skipped (a P copy).
        shown_at_once = False
        if bits.get(1):
            if not bits.get(1):
                shown_at_once = True
            elif bits.get(1):
                shown_at_once = not bits.get(1)
    if seq.frame_counter:
        bits.get(8)  # TFCNTR
    fields = 2
    if seq.pulldown:
        if not seq.interlace or seq.progressive_segmented:
            fields = 2 * (1 + bits.get(2))  # RPTFRM repeats whole frames
        else:
            bits.get(1)  # TFF
            fields = 2 + bits.get(1)  # RFF
    return _Frame(shown_at_once, fields)


def _pes_pts(data: mmap.mmap, offset: int) -> int | None:
    if data[offset + 6] & 0xC0 != 0x80 or not data[offset + 7] & 0x80:
        return None
    b = data[offset + 9 : offset + 14]
    return (
        ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    )


def _pes_stream_extension(data: mmap.mmap, offset: int) -> int | None:
    """stream_id_extension from PES_extension_2, or None when absent."""
    flags = data[offset + 7]
    if data[offset + 6] & 0xC0 != 0x80 or not flags & 0x01:
        return None
    cursor = offset + 9
    cursor += {0x80: 5, 0xC0: 10}.get(flags & 0xC0, 0)
    for flag, size in ((0x20, 6), (0x10, 3), (0x08, 1), (0x04, 1), (0x02, 2)):
        if flags & flag:
            cursor += size
    extension = data[cursor]
    cursor += 1
    if extension & 0x80:  # PES_private_data
        cursor += 16
    if extension & 0x40:  # pack_header_field
        cursor += 1 + data[cursor]
    if extension & 0x20:  # program_packet_sequence_counter
        cursor += 2
    if extension & 0x10:  # P-STD_buffer
        cursor += 2
    if not extension & 0x01:
        return None
    if cursor + 1 >= offset + 9 + data[offset + 8]:
        return None
    return data[cursor + 1] & 0x7F


class _FrameParser:
    """Splits the VC-1 elementary stream into frames as payloads arrive."""

    def __init__(self) -> None:
        self.frames: list[_Frame] = []
        self.sequence: _Sequence | None = None
        self._buffer = bytearray()
        self._base = 0  # stream offset of _buffer[0]
        self._pts: deque[tuple[int, int]] = deque()
        self._unit_start: int | None = None

    def add(self, payload: bytes, pts: int | None) -> None:
        if pts is not None:
            self._pts.append((self._base + len(self._buffer), pts))
        self._buffer += payload
        self._parse()

    def finish(self) -> None:
        """Read the last frame, which can be shorter than a full header."""
        self._buffer += bytes(_HEADER_BYTES)
        self._parse()

    def _parse(self) -> None:
        buffer = self._buffer
        cursor = 0
        while True:
            found = buffer.find(_START_CODE, cursor)
            if found < 0 or found + 4 + _HEADER_BYTES > len(buffer):
                break
            code = buffer[found + 3]
            header = bytes(buffer[found + 4 : found + 4 + _HEADER_BYTES])
            position = self._base + found
            if code == _SEQUENCE_HEADER:
                self.sequence = _parse_sequence_header(header)
                if self._unit_start is None:
                    self._unit_start = position
            elif code == _ENTRY_POINT:
                if self._unit_start is None:
                    self._unit_start = position
            elif code == _FRAME and self.sequence is not None:
                self._add_frame(header, position)
            cursor = found + 3
        found = buffer.find(_START_CODE, cursor)
        keep = found if found >= 0 else max(cursor, len(buffer) - 3)
        del buffer[:keep]
        self._base += keep

    def _add_frame(self, header: bytes, position: int) -> None:
        assert self.sequence is not None
        start = self._unit_start if self._unit_start is not None else position
        self._unit_start = None
        frame = _parse_frame_header(header, self.sequence)
        # A PES PTS belongs to the first access unit starting in that
        # packet's payload; the unit starts at its sequence header or
        # entry point when it has them.
        while self._pts and self._pts[0][0] <= start:
            frame.pts = self._pts.popleft()[1]
        self.frames.append(frame)


def _scan_frames(path: Path) -> tuple[list[_Frame], _Sequence | None]:
    """The primary VC-1 stream's frames in decode order."""
    parser = _FrameParser()
    with (
        path.open("rb") as handle,
        mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        length = len(data)
        pack = data.find(_PACK_START)
        while 0 <= pack and pack + 14 <= length:
            cursor = pack + 14 + (data[pack + 13] & 7)
            end = min(pack + 2048, length)
            while cursor + 9 <= end and data[cursor : cursor + 3] == _START_CODE:
                packet_end = (
                    cursor + 6 + int.from_bytes(data[cursor + 4 : cursor + 6], "big")
                )
                if (
                    data[cursor + 3] == _VIDEO_EXTENDED
                    and packet_end <= length
                    and _pes_stream_extension(data, cursor) == _PRIMARY_VC1
                ):
                    payload = cursor + 9 + data[cursor + 8]
                    parser.add(data[payload:packet_end], _pes_pts(data, cursor))
                cursor = packet_end
            following = pack + 2048
            if data[following : following + 4] == _PACK_START:
                pack = following
            else:
                pack = data.find(_PACK_START, pack + 4)
    parser.finish()
    return parser.frames, parser.sequence


def _presentation_order(frames: Sequence[_Frame]) -> list[_Frame]:
    """Display order: B frames at once, I/P frames when the next one arrives."""
    shown: list[_Frame] = []
    held: _Frame | None = None
    for frame in frames:
        if frame.shown_at_once:
            shown.append(frame)
            continue
        if held is not None:
            shown.append(held)
        held = frame
    if held is not None:
        shown.append(held)
    return shown


def evo_frame_times(path: Path) -> EvoFrameTimes | None:
    """Sorted presentation times (90 kHz ticks) of an EVO's VC-1 frames.

    Times count from the file's first PTS, which is where mkvmerge starts
    the file. ``None`` when the EVO has no primary VC-1 stream, no frame
    rate to time it by, or the cadence disagrees with the disc's PTSs.
    """
    frames, sequence = _scan_frames(path)
    if not frames or sequence is None or sequence.field_ticks is None:
        return None
    first = next((frame.pts for frame in frames if frame.pts is not None), None)
    if first is None:
        return None
    times: list[float] = []
    clock: float | None = None
    stamped = misses = 0
    for frame in _presentation_order(frames):
        if frame.pts is not None:
            pts = (frame.pts - first) % _PTS_MODULO
            if clock is not None:
                stamped += 1
                if abs(pts - clock) > _PTS_TOLERANCE:
                    misses += 1
            clock = float(pts)
        elif clock is None:
            clock = 0.0
        times.append(clock)
        clock += frame.fields * sequence.field_ticks
    log_debug(
        "VC-1 frames in %s: %d, %d of %d GOP PTSs off the cadence"
        % (path.name, len(frames), misses, stamped)
    )
    if misses > _MAX_MISSES * stamped:
        return None
    return EvoFrameTimes(first, sorted(times))


def correct_vc1_frame_times(mkv_path: Path, evo_paths: Sequence[Path]) -> int | None:
    """Rewrite *mkv_path*'s VC-1 block times from its source EVOs.

    *evo_paths* are the files muxed, in order. Each file's frames start at
    mkvmerge's time for its first block (the frame with its first PTS),
    or where its PTSs put it when it carries on the previous file's
    clock, after checking that their cadence matches mkvmerge's own times, which
    are right for most frames but run late. Returns the number of blocks
    changed, ``None`` when there is no VC-1 track, and raises
    ``FrameTimingError`` (leaving the file alone) when the frames don't
    line up.
    """
    track = read_track_times(mkv_path, is_vc1_track)
    if track is None:
        return None
    per_file: list[EvoFrameTimes] = []
    for path in evo_paths:
        evo = evo_frame_times(path)
        if evo is None:
            raise FrameTimingError(
                tr("could not read the frame cadence of {name}", name=path.name)
            )
        per_file.append(evo)
    total = sum(len(evo.times) for evo in per_file)
    if total != len(track.blocks):
        raise FrameTimingError(
            tr(
                "{frames} frames in the source, {blocks} in the MKV",
                frames=total,
                blocks=len(track.blocks),
            )
        )
    units_per_ms = 1e6 / track.timestamp_scale
    units_per_tick = 1000 * units_per_ms / _TICKS_PER_SECOND
    agree = _AGREE_MS * units_per_ms
    shown: list[float] = []
    start = 0
    previous: tuple[EvoFrameTimes, float] | None = None  # with its anchor
    for path, evo in zip(evo_paths, per_file):
        run = track.blocks[start : start + len(evo.times)]
        start += len(evo.times)
        muxed = sorted(block.time for block in run)
        ours = [tick * units_per_tick for tick in evo.times]
        offsets = sorted(mine - theirs for mine, theirs in zip(muxed, ours))
        offset = offsets[len(offsets) // 2]
        agreeing = sum(
            abs(mine - theirs - offset) <= agree for mine, theirs in zip(muxed, ours)
        )
        if agreeing < _MIN_AGREEMENT * len(ours):
            raise FrameTimingError(
                tr(
                    "only {agreeing} of {frames} frame times agree with mkvmerge's",
                    agreeing=agreeing,
                    frames=len(ours),
                )
            )
        anchor = float(run[0].time)
        if previous is not None:
            # On a shared clock the PTSs place the EVO exactly; mkvmerge's
            # frame at the join can be a block off.
            before, before_anchor = previous
            on_clock = (
                before_anchor
                + (evo.first_pts - before.first_pts) % _PTS_MODULO * units_per_tick
            )
            if 0 <= on_clock - shown[-1] <= _CLOCK_JOIN_MS * units_per_ms:
                anchor = on_clock
        if abs(offset - anchor) > _MAX_ANCHOR_MS * units_per_ms:
            raise FrameTimingError(
                tr(
                    "mkvmerge starts the frames of {name} {ms} ms away from the rest",
                    name=path.name,
                    ms=round((anchor - offset) / units_per_ms),
                )
            )
        shown.extend(anchor + time for time in ours)
        previous = evo, anchor
    shown.sort()
    # Each block carries the time of the frame shown before it.
    corrected = [round(time) for time in shown[:1] + shown[:-1]]
    return write_block_times(mkv_path, track, corrected)


def retime_vc1_join_frames(mkv_path: Path) -> int | None:
    """Show the frame before each appended clip for its own time.

    mkvmerge starts each appended VC-1 file with two blocks at one time:
    in VfW mode a block carries the time of the frame before it, and the
    first repeats the file's first. At a join that first block carries
    the previous clip's last frame, which so shows for no time while the
    frame before it is held. It gets the time one frame before the next
    block. Returns the blocks changed, ``None`` with no VC-1 track.
    """
    track = read_track_times(mkv_path, is_vc1_track)
    if track is None:
        return None
    times = [block.time for block in track.blocks]
    if len(times) < 3 or times[-1] <= times[1]:
        return 0
    # The mean step: a frame of 41.708 ms, not the millisecond steps.
    frame = (times[-1] - times[1]) / (len(times) - 2)
    retimed = list(times)
    for index in range(1, len(times) - 1):
        earlier = round(times[index + 1] - frame)
        if times[index] == times[index + 1] and earlier > times[index - 1]:
            retimed[index] = earlier
    return write_block_times(mkv_path, track, retimed)
