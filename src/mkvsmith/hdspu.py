"""HD DVD subpictures: decode them from EVO files and write them as PGS.

EVO subpictures ride private stream 1 (sub-streams 0x20-0x3F) like DVD
ones, but in an HD layout mkvmerge can't read: 4-byte offsets and 8-bit
pixels with a 256-entry palette in every unit. They are decoded here and
re-encoded as a Blu-ray PGS ``.sup`` file per track, which mkvmerge muxes
as ``S_HDMV/PGS``. Both formats store palette entries as Y, Cr, Cb, so the
colours are copied, never converted. See docs/formats/subpictures.md.
"""

from __future__ import annotations

import mmap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mkvsmith.models import log_debug

_PACK_START = b"\x00\x00\x01\xba"
_PRIVATE_STREAM_1 = 0xBD
_SUBPICTURE_FIRST = 0x20
_SUBPICTURE_LAST = 0x3F
# Video PES ids: MPEG-2 and H.264 on 0xE0-0xEF, VC-1 on the extended 0xFD.
_VIDEO_EXTENDED = 0xFD
_PTS_MODULO = 1 << 33
_TICKS_PER_SECOND = 90000
# SP_DCSQ_STM counts 1024 ticks of the 90 kHz clock.
_DELAY_UNIT = 1024
# A unit with no stop command stays up until the next one, or this long.
_DEFAULT_DISPLAY_TICKS = 5 * _TICKS_PER_SECOND
_SCAN_WINDOW = 64 * 1024 * 1024
# The next EVO continues this one's clock if it starts this soon after the
# last video PTS (a GOP is about half a second).
_CONTINUITY_TICKS = 2 * _TICKS_PER_SECOND

_CMD_FORCED_START = 0x00
_CMD_START = 0x01
_CMD_STOP = 0x02
_CMD_END = 0xFF
# Command sizes after the command byte; 0x07 carries its own size.
_CMD_ARG_SIZES = {
    0x00: 0,
    0x01: 0,
    0x02: 0,
    0x03: 2,
    0x04: 2,
    0x05: 6,
    0x06: 4,
    0x83: 768,
    0x84: 256,
    0x85: 6,
    0x86: 8,
}

_PGS_PCS = 0x16
_PGS_WDS = 0x17
_PGS_PDS = 0x14
_PGS_ODS = 0x15
_PGS_END = 0x80
_PGS_EPOCH_START = 0x80
_PGS_FORCED = 0x40
_PGS_FRAME_RATE = 0x10
# A segment's size field is 2 bytes; the first ODS fragment spends 11 of
# them on its header, later ones 4.
_PGS_SEGMENT_MAX = 0xFFFF


@dataclass(frozen=True, slots=True)
class HdSubpicture:
    """One decoded subpicture, ready to write as a PGS display set.

    *start* and *end* are 90 kHz ticks on the muxed title's timeline.
    *rle* is the object's PGS run-length data; *palette* holds 256 entries
    of Y, Cr, Cb and *alpha* 256 opacities (255 = opaque).
    """

    start: int
    end: int
    x: int
    y: int
    width: int
    height: int
    rle: bytes
    palette: bytes
    alpha: bytes
    forced: bool


@dataclass(frozen=True, slots=True)
class _Decoded:
    start_delay: int
    stop_delay: int | None
    x: int
    y: int
    width: int
    height: int
    rle: bytes
    palette: bytes
    alpha: bytes
    forced: bool


class _Bits:
    """MSB-first bit reader over the pixel data."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, start: int) -> None:
        self.data = data
        self.pos = start * 8

    def get(self, count: int) -> int:
        byte = self.pos >> 3
        chunk = int.from_bytes(self.data[byte : byte + 3].ljust(3, b"\0"), "big")
        value = (chunk >> (24 - (self.pos & 7) - count)) & ((1 << count) - 1)
        self.pos += count
        return value

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


def _decode_field(
    data: bytes, start: int, width: int, lines: int
) -> list[list[tuple[int, int]]]:
    """Decode one field's lines as (colour, length) runs.

    Each run code (F ``decode_run_8bit``): 1 bit "has run", 1 bit "8-bit
    colour" (else 2-bit), the colour, then for a run either ``0`` + 3 bits
    (length + 2) or ``1`` + 7 bits (length + 9, 0 = to the end of the
    line). Lines end byte-aligned.
    """
    bits = _Bits(data, start)
    limit = len(data) * 8
    field: list[list[tuple[int, int]]] = []
    for _ in range(lines):
        runs: list[tuple[int, int]] = []
        x = 0
        while x < width:
            if bits.pos >= limit:
                raise ValueError("pixel data ends early")
            has_run = bits.get(1)
            colour = bits.get(8 if bits.get(1) else 2)
            if not has_run:
                length = 1
            elif bits.get(1):
                length = bits.get(7)
                length = width - x if length == 0 else length + 9
            else:
                length = bits.get(3) + 2
            length = min(length, width - x)
            if runs and runs[-1][0] == colour:
                runs[-1] = (colour, runs[-1][1] + length)
            else:
                runs.append((colour, length))
            x += length
        bits.align()
        field.append(runs)
    return field


def _pgs_line(runs: list[tuple[int, int]]) -> bytes:
    """Encode one line in PGS run-length form, ending with ``00 00``."""
    out = bytearray()
    for colour, length in runs:
        while length > 0:
            run = min(length, 0x3FFF)
            length -= run
            if colour and run <= 2:
                out += bytes([colour]) * run
            elif colour == 0:
                if run < 64:
                    out += bytes([0, run])
                else:
                    out += bytes([0, 0x40 | (run >> 8), run & 0xFF])
            elif run < 64:
                out += bytes([0, 0x80 | run, colour])
            else:
                out += bytes([0, 0xC0 | (run >> 8), run & 0xFF, colour])
    out += b"\0\0"
    return bytes(out)


def decode_subpicture(data: bytes) -> _Decoded | None:
    """Decode one HD subpicture unit, or None if it isn't one we can read.

    Header: ``00 00``, a 4-byte size, a 4-byte control table offset. Each
    control sequence is a 2-byte delay, a 4-byte offset of the next one,
    then commands up to ``0xFF``. Units in the DVD layout, with 2-bit
    pixels, or with no palette are refused.
    """
    if len(data) < 10 or data[:2] != b"\0\0":
        return None
    position = int.from_bytes(data[6:10], "big")
    start_delay: int | None = None
    stop_delay: int | None = None
    forced = False
    area: tuple[int, int, int, int] | None = None
    offsets: tuple[int, int] | None = None
    palette = alpha = b""
    eight_bit = False
    seen: set[int] = set()
    while position not in seen and position + 6 <= len(data):
        seen.add(position)
        delay = int.from_bytes(data[position : position + 2], "big") * _DELAY_UNIT
        following = int.from_bytes(data[position + 2 : position + 6], "big")
        cursor = position + 6
        while cursor < len(data):
            command = data[cursor]
            cursor += 1
            if command == _CMD_END:
                break
            if command == 0x07:
                size = int.from_bytes(data[cursor : cursor + 2], "big")
                cursor += max(size, 2)
                continue
            size = _CMD_ARG_SIZES.get(command)
            if size is None or cursor + size > len(data):
                return None
            argument = data[cursor : cursor + size]
            cursor += size
            if command in (_CMD_FORCED_START, _CMD_START):
                if start_delay is None:
                    start_delay = delay
                forced = forced or command == _CMD_FORCED_START
            elif command == _CMD_STOP:
                if stop_delay is None:
                    stop_delay = delay
            elif command in (0x05, 0x85):
                x1 = (argument[0] << 4) | (argument[1] >> 4)
                x2 = ((argument[1] & 0x0F) << 8) | argument[2]
                y1 = (argument[3] << 4) | (argument[4] >> 4)
                y2 = ((argument[4] & 0x0F) << 8) | argument[5]
                area = (x1, y1, x2 - x1 + 1, y2 - y1 + 1)
                eight_bit = command == 0x85
            elif command == 0x86:
                offsets = (
                    int.from_bytes(argument[:4], "big"),
                    int.from_bytes(argument[4:], "big"),
                )
            elif command == 0x83:
                palette = argument
            elif command == 0x84:
                alpha = bytes(0xFF - value for value in argument)
        if following == position:
            break
        position = following
    if start_delay is None or area is None or offsets is None or not eight_bit:
        return None
    if len(palette) != 768 or len(alpha) != 256:
        return None
    x, y, width, height = area
    if width <= 0 or height <= 0:
        return None
    try:
        top = _decode_field(data, offsets[0], width, (height + 1) // 2)
        bottom = _decode_field(data, offsets[1], width, height // 2)
    except ValueError:
        return None
    lines = [
        top[row // 2] if row % 2 == 0 else bottom[row // 2] for row in range(height)
    ]
    rle = b"".join(_pgs_line(runs) for runs in lines)
    return _Decoded(
        start_delay, stop_delay, x, y, width, height, rle, palette, alpha, forced
    )


def _pes_pts(data: mmap.mmap, offset: int) -> int | None:
    """PTS of the PES packet at *offset* when its header carries one."""
    if data[offset + 6] & 0xC0 != 0x80 or not data[offset + 7] & 0x80:
        return None
    b = data[offset + 9 : offset + 14]
    return (
        ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    )


@dataclass(slots=True)
class _FileScan:
    first_video_pts: int | None = None
    last_video_pts: int | None = None
    max_video_pts: int | None = None
    # Steps between successive video PTS (one per GOP on retail discs).
    video_steps: list[int] | None = None
    # (sub-stream, PTS, unit) in file order.
    units: list[tuple[int, int, bytes]] | None = None

    def span_to(self, following: _FileScan) -> int | None:
        """Ticks from this file's zero to the next file's, or None.

        Seamless EVOs share one clock: the next file's first video PTS
        continues this one's, and mkvmerge places the next file's audio
        there. Otherwise the span is this file's video PTS range plus its
        typical step, the length of the last GOP. The XPL's clip times
        aren't used: an EVO can run past the part the playlist plays, and
        mkvmerge muxes all of it.
        """
        if self.first_video_pts is None or self.max_video_pts is None:
            return None
        end = (self.max_video_pts - self.first_video_pts) % _PTS_MODULO
        if following.first_video_pts is not None:
            start = (following.first_video_pts - self.first_video_pts) % _PTS_MODULO
            if end < start <= end + _CONTINUITY_TICKS:
                return start
        steps: list[int] = sorted(self.video_steps or [])
        return end + (steps[len(steps) // 2] if steps else 0)


def _scan_file(path: Path, sub_ids: set[int]) -> _FileScan:
    """Walk one EVO's packs, joining each wanted sub-stream's units.

    A unit starts in a packet with a PTS and is complete once its 4-byte
    size is in. The first video PTS is the file's zero, as mkvmerge reads
    it: every stream of the sampled EVOs starts there.
    """
    scan = _FileScan(video_steps=[], units=[])
    pending: dict[int, tuple[int, bytearray]] = {}
    size = path.stat().st_size
    with path.open("rb") as handle:
        descriptor = handle.fileno()
        offset = 0
        while offset < size:
            window = min(_SCAN_WINDOW, size - offset)
            with mmap.mmap(
                descriptor, window, access=mmap.ACCESS_READ, offset=offset
            ) as data:
                _scan_window(data, sub_ids, scan, pending)
            offset += window
    return scan


def _scan_window(
    data: mmap.mmap,
    sub_ids: set[int],
    scan: _FileScan,
    pending: dict[int, tuple[int, bytearray]],
) -> None:
    units = scan.units if scan.units is not None else []
    length = len(data)
    pack = data.find(_PACK_START)
    while 0 <= pack and pack + 14 <= length:
        cursor = pack + 14 + (data[pack + 13] & 7)
        end = min(pack + 2048, length)
        while cursor + 9 <= end and data[cursor : cursor + 3] == b"\0\0\1":
            stream_id = data[cursor + 3]
            packet_end = (
                cursor + 6 + int.from_bytes(data[cursor + 4 : cursor + 6], "big")
            )
            if 0xE0 <= stream_id <= 0xEF or stream_id == _VIDEO_EXTENDED:
                pts = _pes_pts(data, cursor)
                if pts is not None:
                    _note_video_pts(scan, pts)
            elif stream_id == _PRIVATE_STREAM_1 and data[cursor + 6] & 0xC0 == 0x80:
                payload = cursor + 9 + data[cursor + 8]
                if payload < packet_end <= length:
                    sub = data[payload]
                    if sub in sub_ids:
                        _add_chunk(
                            units,
                            pending,
                            sub,
                            _pes_pts(data, cursor),
                            data[payload + 1 : packet_end],
                        )
            cursor = packet_end
        if pack + 2048 + 4 <= length and data[pack + 2048 : pack + 2052] == _PACK_START:
            pack += 2048
        else:
            pack = data.find(_PACK_START, pack + 4)


def _note_video_pts(scan: _FileScan, pts: int) -> None:
    if scan.first_video_pts is None:
        scan.first_video_pts = pts
    elif scan.last_video_pts is not None and scan.video_steps is not None:
        step = (pts - scan.last_video_pts) % _PTS_MODULO
        if 0 < step < _CONTINUITY_TICKS:
            scan.video_steps.append(step)
    scan.last_video_pts = pts
    relative = (pts - scan.first_video_pts) % _PTS_MODULO
    if (
        scan.max_video_pts is None
        or relative > (scan.max_video_pts - scan.first_video_pts) % _PTS_MODULO
    ):
        scan.max_video_pts = pts


def _add_chunk(
    units: list[tuple[int, int, bytes]],
    pending: dict[int, tuple[int, bytearray]],
    sub: int,
    pts: int | None,
    chunk: bytes,
) -> None:
    if pts is not None:
        pending[sub] = (pts, bytearray(chunk))
    elif sub in pending:
        pending[sub][1].extend(chunk)
    else:
        return
    unit_pts, buffer = pending[sub]
    if len(buffer) < 6:
        return
    unit_size = int.from_bytes(buffer[2:6], "big") if buffer[:2] == b"\0\0" else 0
    if unit_size and len(buffer) >= unit_size:
        units.append((sub, unit_pts, bytes(buffer[:unit_size])))
        del pending[sub]


def scan_evo_subpictures(
    inputs: Sequence[Path], sub_ids: set[int]
) -> tuple[dict[int, list[HdSubpicture]], int]:
    """Decode the wanted sub-streams' subpictures across the title's EVOs.

    The EVOs play one after another, as mkvmerge appends them: each file's
    times run from its first video PTS, shifted by the files before it
    (``_FileScan.span_to``). Returns the subpictures by sub-stream and the
    count of units that couldn't be decoded.
    """
    scans = [_scan_file(path, sub_ids) for path in inputs]
    found: dict[int, list[HdSubpicture]] = {}
    failed = 0
    timeline = 0
    for index, (path, scan) in enumerate(zip(inputs, scans, strict=True)):
        base = scan.first_video_pts
        if base is None:
            log_debug(f"HD subpictures: no video PTS in {path.name}; skipped")
            continue
        for sub, pts, unit in scan.units or []:
            decoded = decode_subpicture(unit)
            if decoded is None:
                failed += 1
                continue
            shown = timeline + (pts - base) % _PTS_MODULO
            start = shown + decoded.start_delay
            stop = decoded.stop_delay
            end = (
                shown + stop
                if stop is not None and stop > decoded.start_delay
                else start + _DEFAULT_DISPLAY_TICKS
            )
            found.setdefault(sub, []).append(
                HdSubpicture(
                    start,
                    end,
                    decoded.x,
                    decoded.y,
                    decoded.width,
                    decoded.height,
                    decoded.rle,
                    decoded.palette,
                    decoded.alpha,
                    decoded.forced,
                )
            )
        if index + 1 < len(scans):
            span = scan.span_to(scans[index + 1])
            timeline += span or 0
            log_debug(
                f"HD subpictures: {path.name} spans {(span or 0) / _TICKS_PER_SECOND:.3f}s"
            )
    for subs in found.values():
        subs.sort(key=lambda sub: sub.start)
    log_debug(
        "HD subpictures: "
        + (
            ", ".join(f"0x{sub:02x}={len(found[sub])}" for sub in sorted(found))
            or "none"
        )
        + (f", {failed} undecodable" if failed else "")
    )
    return found, failed


def _segment(kind: int, pts: int, payload: bytes) -> bytes:
    return (
        b"PG"
        + (pts % (1 << 32)).to_bytes(4, "big")
        + b"\0\0\0\0"
        + bytes([kind])
        + len(payload).to_bytes(2, "big")
        + payload
    )


def _composition(
    width: int,
    height: int,
    number: int,
    state: int,
    sub: HdSubpicture | None,
) -> bytes:
    payload = (
        width.to_bytes(2, "big")
        + height.to_bytes(2, "big")
        + bytes([_PGS_FRAME_RATE])
        + (number & 0xFFFF).to_bytes(2, "big")
        + bytes([state, 0, 0, 1 if sub else 0])
    )
    if sub is not None:
        payload += (
            b"\0\0\0"
            + bytes([_PGS_FORCED if sub.forced else 0])
            + sub.x.to_bytes(2, "big")
            + sub.y.to_bytes(2, "big")
        )
    return payload


def _window(sub: HdSubpicture) -> bytes:
    return (
        b"\x01\0"
        + sub.x.to_bytes(2, "big")
        + sub.y.to_bytes(2, "big")
        + sub.width.to_bytes(2, "big")
        + sub.height.to_bytes(2, "big")
    )


def _objects(sub: HdSubpicture) -> list[bytes]:
    """The object's ODS payloads, split to fit the segment size."""
    data = (
        (len(sub.rle) + 4).to_bytes(3, "big")
        + sub.width.to_bytes(2, "big")
        + sub.height.to_bytes(2, "big")
        + sub.rle
    )
    room = _PGS_SEGMENT_MAX - 4
    pieces = [data[i : i + room] for i in range(0, len(data), room)]
    payloads: list[bytes] = []
    for index, piece in enumerate(pieces):
        flags = (0x80 if index == 0 else 0) | (0x40 if index == len(pieces) - 1 else 0)
        payloads.append(b"\0\0\0" + bytes([flags]) + piece)
    return payloads


def write_sup(
    subpictures: Sequence[HdSubpicture], path: Path, video_size: tuple[int, int]
) -> None:
    """Write *subpictures* as a PGS ``.sup`` stream.

    Each one is an epoch-start display set (composition, window, palette,
    object, end) at its start, and an empty composition at its end. One
    that runs into the next isn't cleared: the next epoch replaces it.
    """
    width, height = video_size
    number = 0
    with path.open("wb") as out:
        for index, sub in enumerate(subpictures):
            following = (
                subpictures[index + 1].start if index + 1 < len(subpictures) else None
            )
            if following is not None and following <= sub.start:
                continue
            palette = b"".join(
                bytes([entry])
                + sub.palette[3 * entry : 3 * entry + 3]
                + bytes([sub.alpha[entry]])
                for entry in range(256)
            )
            out.write(
                _segment(
                    _PGS_PCS,
                    sub.start,
                    _composition(width, height, number, _PGS_EPOCH_START, sub),
                )
            )
            out.write(_segment(_PGS_WDS, sub.start, _window(sub)))
            out.write(_segment(_PGS_PDS, sub.start, b"\0\0" + palette))
            for payload in _objects(sub):
                out.write(_segment(_PGS_ODS, sub.start, payload))
            out.write(_segment(_PGS_END, sub.start, b""))
            number += 1
            if following is not None and following <= sub.end:
                continue
            out.write(
                _segment(
                    _PGS_PCS, sub.end, _composition(width, height, number, 0, None)
                )
            )
            out.write(_segment(_PGS_WDS, sub.end, _window(sub)))
            out.write(_segment(_PGS_END, sub.end, b""))
            number += 1
