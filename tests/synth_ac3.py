"""Build synthetic AC-3 frames for tests.

A 48 kHz 2/0 syncframe at 192 kbit/s (ATSC A/52 5.3): the sync word, the
syncinfo and bit-stream info, then zeros for the audio blocks, with
``crc1`` and ``crc2`` making its CRCs check out (mkvmerge verifies them
on a TrueHD stream's AC-3 core). There is no sound; mkvmerge
reads the headers and muxes the frames.
"""

from __future__ import annotations

from tests.synth_vc1 import BitWriter

FRAME_BYTES = 768  # frmsizecod 20 at 48 kHz: 384 16-bit words
FRAME_TICKS = 2880  # 1536 samples at 48 kHz, in 90 kHz ticks


def _crc16(data: bytes) -> int:
    """CRC-16 with A/52's polynomial x^16 + x^15 + x^2 + 1."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x8005 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _crc1(data: bytearray) -> int:
    """The crc1 that makes the CRC of the frame's first 5/8 zero.

    The CRC is linear, so the 16 values of crc1 with one bit set show how
    each bit moves it; solving for the rest's CRC gives crc1.
    """
    end = (len(data) // 2 * 5 // 8) * 2  # the first 5/8 of the frame, in words
    rest = bytearray(data[2:end])
    rest[0:2] = b"\x00\x00"
    target = _crc16(bytes(rest))
    columns = []
    for bit in range(16):
        probe = bytearray(len(rest))
        probe[0:2] = (1 << bit).to_bytes(2, "big")
        columns.append(_crc16(bytes(probe)))
    # Gaussian elimination over GF(2): find bits whose columns XOR to target.
    rows = [(columns[bit], 1 << bit) for bit in range(16)]
    basis: dict[int, tuple[int, int]] = {}
    for value, combo in rows:
        for top in sorted(basis, reverse=True):
            if value >> top & 1:
                value ^= basis[top][0]
                combo ^= basis[top][1]
        if value:
            basis[value.bit_length() - 1] = (value, combo)
    result = 0
    for top in sorted(basis, reverse=True):
        if target >> top & 1:
            target ^= basis[top][0]
            result ^= basis[top][1]
    assert target == 0
    return result


def frame() -> bytes:
    """One AC-3 syncframe."""
    bits = BitWriter().put(0, 2).put(20, 6)  # fscod 48 kHz, frmsizecod 192 kbit/s
    bits.put(8, 5).put(0, 3).put(2, 3)  # bsid 8, bsmod 0, acmod 2/0
    bits.put(0, 2).put(0, 1)  # dsurmod, lfeon
    bits.put(27, 5)  # dialnorm
    bits.put(0, 1).put(0, 1).put(0, 1)  # no compr, langcod, audprodie
    bits.put(1, 1).put(1, 1)  # copyrightb, origbs
    bits.put(0, 1).put(0, 1).put(0, 1)  # no timecodes, no addbsi
    data = bytearray(b"\x0b\x77\x00\x00" + bits.to_bytes(FRAME_BYTES - 4))
    # crc1 covers the first 5/8 of the frame, crc2 (its last word) all of
    # it after the sync word; each makes its CRC zero.
    data[2:4] = _crc1(data).to_bytes(2, "big")
    data[-2:] = _crc16(bytes(data[2:-2])).to_bytes(2, "big")
    return bytes(data)
