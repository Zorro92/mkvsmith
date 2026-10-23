"""Build synthetic TrueHD access units for tests.

Each access unit starts with a check nibble, its length in 16-bit words
and its input timing; a unit that starts a sequence then carries the
major sync (0xF8726FBA) with the sample rate and channel map. There is no
sound and no substream data, which mkvmerge doesn't read
(src/common/truehd.cpp parses only these headers).
"""

from __future__ import annotations

UNIT_TICKS = 75  # 40 samples at 48 kHz, in 90 kHz ticks
_SAMPLES = 40
_SYNC_EVERY = 16  # access units between major syncs
_UNIT_BYTES = 40
_SYNC_BYTES = 64


def access_unit(index: int) -> bytes:
    """The *index*-th access unit of a 48 kHz 2.0 stream."""
    sync = index % _SYNC_EVERY == 0
    size = _SYNC_BYTES if sync else _UNIT_BYTES
    timing = (index * _SAMPLES) & 0xFFFF
    header = ((0xF << 12) | size // 2).to_bytes(2, "big") + timing.to_bytes(2, "big")
    if not sync:
        return header + bytes(size - len(header))
    major = (
        b"\xf8\x72\x6f\xba"  # format_sync: TrueHD
        + bytes([0x00])  # rate 48 kHz, reserved
        + bytes([0x00, 0x00, 0x01])  # channel maps: L R
        + b"\xb7\x52"  # major_sync_info_signature
        + bytes(4)  # flags, reserved
        + bytes([0x00, 0x00])  # constant rate, peak data rate
        + bytes([0x10])  # one substream
        + bytes(15)  # substream info, dialnorm, summary info, checksum
    )
    return header + major + bytes(size - len(header) - len(major))
