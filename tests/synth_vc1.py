"""Build synthetic VC-1 streams (headers only) for tests.

SMPTE 421M advanced-profile sequence headers, entry points and frame
headers, carried as an HD DVD EVO (MPEG-2 program stream, VC-1 on 0xFD
with stream_id_extension 0x55) or as a Blu-ray M2TS clip (PAT, PMT and
VC-1 on PID 0x1011, a PTS and DTS on every frame). There is no picture
data; mkvmerge reads the headers and muxes the frames, so the streams also
show what mkvmerge does with VC-1 timing.
"""

from __future__ import annotations

import struct
from pathlib import Path

from tests import synth_m2ts

FIELD = 90000 / (2 * 30000 / 1001)  # ticks per field at 29.97 frames/s
FRAME = 3003  # 90 kHz ticks per frame at 29.97 frames/s


class BitWriter:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def put(self, value: int, count: int) -> BitWriter:
        self._bits += [(value >> (count - 1 - i)) & 1 for i in range(count)]
        return self

    def to_bytes(self, size: int) -> bytes:
        bits = (self._bits + [0] * (size * 8))[: size * 8]
        return bytes(
            int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)
        )


def sequence_header(
    interlace: int = 1, pulldown: int = 1, frame_rate: bool = True
) -> bytes:
    bits = BitWriter().put(3, 2).put(3, 3).put(1, 2).put(0, 3 + 5 + 1)
    bits.put(959, 12).put(539, 12)  # (1920 / 2 - 1, 1080 / 2 - 1)
    bits.put(pulldown, 1).put(interlace, 1).put(0, 1).put(0, 1).put(0, 1).put(0, 1)
    bits.put(1, 1).put(1919, 14).put(1079, 14).put(0, 1)  # DISPLAY_EXT, no aspect
    if frame_rate:
        bits.put(1, 1).put(0, 1).put(3, 8).put(2, 4)  # 30000 / 1001
    else:
        bits.put(0, 1)
    return bits.to_bytes(16)


# PTYPE codes: P 0, B 10, I 110, BI 1110, skipped 1111.
PTYPE = {"P": (0, 1), "B": (2, 2), "I": (6, 3), "BI": (14, 4), "S": (15, 4)}


def frame_header(kind: str, rff: int) -> bytes:
    """An interlaced frame's header (FCM progressive), with pulldown's RFF."""
    code, length = PTYPE[kind]
    return BitWriter().put(0, 1).put(code, length).put(0, 1).put(rff, 1).to_bytes(16)


def progressive_frame_header(kind: str) -> bytes:
    """A frame's header in a progressive sequence without pulldown."""
    code, length = PTYPE[kind]
    return BitWriter().put(code, length).to_bytes(16)


def _timestamp(value: int, marker: int) -> bytes:
    return bytes(
        [
            marker | ((value >> 29) & 0x0E) | 1,
            (value >> 22) & 0xFF,
            ((value >> 14) & 0xFE) | 1,
            (value >> 7) & 0xFF,
            ((value << 1) & 0xFE) | 1,
        ]
    )


def pes(payload: bytes, pts: int | None, dts: int | None = None) -> bytes:
    """A VC-1 PES (0xFD, stream_id_extension 0x55 in PES_extension_2)."""
    fields = b""
    flags = 0x01
    if pts is not None and dts is not None:
        flags |= 0xC0
        fields = _timestamp(pts, 0x30) + _timestamp(dts, 0x10)
    elif pts is not None:
        flags |= 0x80
        fields = _timestamp(pts, 0x20)
    fields += b"\x01\x81\x55"  # PES_extension_flag_2, length 1, extension 0x55
    body = bytes([0x81, flags, len(fields)]) + fields + payload
    return b"\x00\x00\x01\xfd" + len(body).to_bytes(2, "big") + body


def pack(pes: bytes) -> bytes:
    pack = b"\x00\x00\x01\xba" + bytes([0x44, 0, 4, 0, 4, 1, 0, 0, 3, 0xF8]) + pes
    assert len(pack) <= 2048
    return pack + bytes(2048 - len(pack))


# Decode order of one GOP, with RFF flags: the 3:2 cadence alternates
# 2- and 3-field frames in display order (I B B P B B P ...).
GOP = [("I", 0), ("P", 1), ("B", 1), ("B", 0), ("P", 0), ("B", 0), ("B", 1)]


def write_evo(path: Path, gops: int, first_pts: int = 900_000) -> list[float]:
    """An EVO of *gops* GOPs; returns the expected sorted frame times."""
    packs: list[bytes] = []
    pts = first_pts
    expected: list[float] = []
    clock = 0.0
    for _gop in range(gops):
        for index, (kind, rff) in enumerate(GOP):
            unit = b""
            if index == 0:
                unit += b"\x00\x00\x01\x0f" + sequence_header()
                unit += b"\x00\x00\x01\x0e" + bytes(8)
            unit += b"\x00\x00\x01\x0d" + frame_header(kind, rff) + b"\x55" * 64
            packs.append(pack(pes(unit, pts if index == 0 else None)))
        # Display order I B B P B B P: durations by each frame's RFF.
        shown = [GOP[0], GOP[2], GOP[3], GOP[1], GOP[5], GOP[6], GOP[4]]
        for _kind, rff in shown:
            expected.append(clock)
            clock += (2 + rff) * FIELD
        pts = first_pts + round(clock)
    path.write_bytes(b"".join(packs))
    return expected


def _crc32(data: bytes) -> int:
    """CRC-32/MPEG-2, as PSI sections carry it (ISO/IEC 13818-1 Annex A)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _section(table_id: int, extension: int, body: bytes) -> bytes:
    length = 5 + len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    section = head + struct.pack(">H", extension) + bytes([0xC1, 0, 0]) + body
    return section + struct.pack(">I", _crc32(section))


_PMT_PID = 0x100
_PCR_PID = 0x1001
_PAT = _section(0x00, 1, struct.pack(">HH", 1, 0xE000 | _PMT_PID))
_REGISTRATION = b"\x05\x04HDMV"
_PMT = _section(
    0x02,
    1,
    struct.pack(">HH", 0xE000 | _PCR_PID, 0xF000 | len(_REGISTRATION))
    + _REGISTRATION
    + bytes([0xEA])  # stream_type: VC-1
    + struct.pack(">HH", 0xE000 | synth_m2ts.VIDEO_PID, 0xF000),
)


def _psi() -> list[bytes]:
    return [
        synth_m2ts.source_packet(0, b"\x00" + _PAT),
        synth_m2ts.source_packet(_PMT_PID, b"\x00" + _PMT),
    ]


def m2ts_clip(frames: int, start: int = 90000) -> bytes:
    """A Blu-ray clip of *frames* progressive 29.97 fps VC-1 frames.

    Decode order I P B B P B B ..., one GOP; the first frame shows at
    *start* (90 kHz). As on a disc every frame has a PTS and a DTS, the DTS
    running a frame behind the I frame's PTS. *frames* is 1 + 3n.
    """
    if frames % 3 != 1:
        raise ValueError("frames must be 1 + 3n")
    order = [("I", 0)]
    for anchor in range(3, frames, 3):
        order += [("P", anchor), ("B", anchor - 2), ("B", anchor - 1)]
    packets = _psi()
    for index, (kind, shown) in enumerate(order):
        unit = b""
        if kind == "I":
            unit += b"\x00\x00\x01\x0f" + sequence_header(interlace=0, pulldown=0)
            unit += b"\x00\x00\x01\x0e" + bytes(8)
        unit += b"\x00\x00\x01\x0d" + progressive_frame_header(kind) + b"\x55" * 32
        payload = pes(unit, start + shown * FRAME, start + (index - 1) * FRAME)
        packets.append(
            synth_m2ts.source_packet(synth_m2ts.VIDEO_PID, payload, counter=index)
        )
        if index % 5 == 4:
            packets += _psi()
    # mkvmerge probes a few hundred packets before it accepts a stream.
    packets += [synth_m2ts.null_packet()] * 400
    return b"".join(packets)
