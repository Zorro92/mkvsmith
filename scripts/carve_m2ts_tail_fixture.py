"""Carve a small, header-only M2TS tail fixture for tests/test_m2ts.py.

Keeps only the source packets that start a PES (payload_unit_start) for the
given PIDs within the clip's last *window* bytes, and zeroes every byte after
the PES header, so the fixture preserves the real PTS / stream_id_extension
layout without carrying any audio or video essence.

    uv run python scripts/carve_m2ts_tail_fixture.py SRC.m2ts OUT.m2ts \
        [--window BYTES] [--pids 0x1011,0x1100,...]
"""

from __future__ import annotations

import argparse
from pathlib import Path

PACKET = 192


def carve(src: Path, window: int, pids: set[int]) -> bytes:
    size = src.stat().st_size
    start = max(0, size - window)
    start -= start % PACKET
    with src.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    out = bytearray()
    for pos in range(0, len(data) - PACKET + 1, PACKET):
        packet = bytearray(data[pos : pos + PACKET])
        ts = packet[4:]
        if ts[0] != 0x47 or not ts[1] & 0x40:
            continue
        if ((ts[1] & 0x1F) << 8 | ts[2]) not in pids:
            continue
        payload = 4
        if ts[3] & 0x20:
            payload += 1 + ts[4]
        pes = payload + 4  # offset within the source packet
        if bytes(packet[pes : pes + 3]) != b"\x00\x00\x01":
            continue
        header_end = pes + 9 + packet[pes + 8]
        packet[header_end:] = bytes(PACKET - header_end)
        out += packet
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--window", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--pids", default="0x1011,0x1100,0x1101,0x1103")
    args = parser.parse_args()
    pids = {int(value, 0) for value in args.pids.split(",")}
    args.out.write_bytes(carve(args.src, args.window, pids))


if __name__ == "__main__":
    main()
