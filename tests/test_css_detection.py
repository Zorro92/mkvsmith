"""CSS-encrypted DVDs are detected at scan time and refused before extracting.

mkvmerge can't read scrambled VOBs and mkvsmith doesn't decrypt them, so a
scrambled disc is flagged from a small video sample instead of failing at the
mux after extracting gigabytes.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from mkvsmith import mkv
from mkvsmith.disc_reader import css_scrambled_packets, dvd_title_is_css_encrypted
from mkvsmith.models import DiscMetadata, RipError, RuntimeState, Title


def _pack(stream_id: int, scrambled: bool, *, system_header: bool = False) -> bytes:
    """One 2048-byte DVD pack holding a single PES packet."""
    pack = b"\x00\x00\x01\xba" + bytes(9) + b"\xf8"  # MPEG-2 pack, no stuffing
    if system_header:
        pack += b"\x00\x00\x01\xbb" + struct.pack(">H", 6) + bytes(6)
    flags = 0x80 | (0x10 if scrambled else 0x00)
    payload_len = 2048 - len(pack) - 6
    pes = b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", payload_len)
    pes += bytes([flags, 0x00, 0x00]) + bytes(payload_len - 3)
    return pack + pes


def test_counts_scrambled_media_packets() -> None:
    data = (
        _pack(0xE0, True, system_header=True)
        + _pack(0xE0, False)
        + _pack(0xBD, True)
        + _pack(0xBF, True)  # NAV packets aren't media
    )
    assert css_scrambled_packets(data) == (2, 3)


def test_clear_video_is_not_encrypted(tmp_path: Path) -> None:
    vob = tmp_path / "VTS_01_1.VOB"
    vob.write_bytes(_pack(0xE0, False) * 64)
    title = Title(index=0, source_file=vob, name="x", duration_seconds=1.0)
    assert dvd_title_is_css_encrypted(title) is False
    vob.write_bytes(_pack(0xE0, True) * 64)
    assert dvd_title_is_css_encrypted(title) is True


def test_encrypted_disc_is_refused_before_any_work(tmp_path: Path) -> None:
    state = RuntimeState()
    state.disc_metadata = DiscMetadata(css_encrypted=True)
    creator = mkv.MKVCreator(tmp_path, runtime_state=state)
    title = Title(
        index=0, source_file=tmp_path / "VTS_01_1.VOB", name="x", duration_seconds=1.0
    )
    title.dvd_ifo_data = b"DVDVIDEO-VTS"

    with pytest.raises(RipError, match="CSS-encrypted"):
        creator.create_mkv(title)
