"""Smoke tests for binary-parser defensive guards.

These exercise the parsers' empty / garbage / wrong-magic input handling —
behaviour that must hold regardless of disc data, so no real fixtures are
required. When a real disc image is available, add fixture-based regression
tests alongside these (see the ``parser-regression-test`` skill).
"""

# The parsers under test are private (underscore-prefixed) internal helpers;
# accessing them from tests is intentional.
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
import struct

import dvdifo
import pytest

from bluray import _parse_bdmv_disc_name, _parse_clpi, _parse_mpls
from dvdifo import (
    _active_pgc_audio_streams,
    _active_pgc_subpicture_streams,
    _parse_vts_ifo_languages,
    _parse_pgc_stream_languages,
    _parse_vts_pgc_info,
    _parse_vts_vobu_admap,
    _pgc_offset_table_base,
    _vts_pgc_absolute_offset,
    _vts_ptt_srpt_base,
    _vts_title_unit_base,
    _vts_ttn1_pgc_number,
    _pgc_program_cell_range,
    _pgc_program_tables,
    _trim_trailing_menu_programs,
)


# --- CLPI ------------------------------------------------------------------


def test_parse_clpi_empty_returns_empty_dict() -> None:
    assert _parse_clpi(b"") == {}


def test_parse_clpi_too_short_returns_empty_dict() -> None:
    assert _parse_clpi(b"\x00" * 39) == {}


def test_parse_clpi_bad_magic_returns_empty_dict() -> None:
    # 40 bytes (past the length guard) but the 8-byte magic is not a recognised
    # HDMV/HDBD identifier, so the magic check rejects it.
    assert _parse_clpi(b"\x00" * 40) == {}


# --- MPLS ------------------------------------------------------------------


def test_parse_mpls_missing_file_returns_none(tmp_path: Path) -> None:
    assert _parse_mpls(tmp_path / "does_not_exist.mpls") is None


def test_parse_mpls_too_short_returns_none(tmp_path: Path) -> None:
    (tmp_path / "short.mpls").write_bytes(b"\x00" * 10)
    assert _parse_mpls(tmp_path / "short.mpls") is None


def test_parse_mpls_bad_magic_returns_none(tmp_path: Path) -> None:
    (tmp_path / "badmagic.mpls").write_bytes(b"XXXX" + b"\x00" * 40)
    assert _parse_mpls(tmp_path / "badmagic.mpls") is None


# --- BDMV disc name --------------------------------------------------------


def test_parse_bdmv_disc_name_missing_dir_returns_none(tmp_path: Path) -> None:
    assert _parse_bdmv_disc_name(tmp_path / "no_such_bdmv") is None


# --- DVD IFO ---------------------------------------------------------------


def test_parse_vts_ifo_languages_empty_returns_empty_dicts() -> None:
    assert _parse_vts_ifo_languages(b"") == ({}, {})


def test_parse_vts_pgc_info_empty_returns_empty_chapters_zero_duration() -> None:
    assert _parse_vts_pgc_info(b"") == ([], 0.0)


def _vobu_admap_buffer(entries: list[int]) -> bytes:
    # Point VTSI_MAT+0xE4 at sector 1. The table begins with a four-byte end
    # address followed by big-endian 32-bit VOBU start sectors.
    data = bytearray(0x800)
    data[0xE4:0xE8] = struct.pack(">I", 1)
    data.extend(struct.pack(">I", 0x804))
    data.extend(b"".join(struct.pack(">I", entry) for entry in entries))
    return bytes(data)


def test_parse_vts_vobu_admap_keeps_leading_zero_before_nonzero_entry() -> None:
    assert _parse_vts_vobu_admap(_vobu_admap_buffer([0, 52])) == [0, 52]


def test_parse_vts_vobu_admap_stops_at_trailing_zero_padding() -> None:
    assert _parse_vts_vobu_admap(_vobu_admap_buffer([52, 53, 0, 99])) == [52, 53]


def test_parse_vts_vobu_admap_all_zero_entries_returns_none() -> None:
    assert _parse_vts_vobu_admap(_vobu_admap_buffer([0, 0])) is None


def test_parse_vts_vobu_admap_rejects_missing_or_truncated_table() -> None:
    assert _parse_vts_vobu_admap(b"\x00" * 0xE8) is None
    truncated = bytearray(_vobu_admap_buffer([52]))
    assert _parse_vts_vobu_admap(bytes(truncated[:-3])) is None


def test_active_pgc_audio_streams_inline_mode() -> None:
    data = bytearray(0x300)
    data[0x202:0x204] = struct.pack(">H", 2)
    pgc_abs = 0x200

    # PGC+0x0C contains eight 2-byte inline audio entries. Bit 7 marks an
    # entry available; bits 0-2 select stream 0-7.
    data[pgc_abs + 0x0C : pgc_abs + 0x0C + 6] = bytes([0x80, 0, 0x81, 0, 0x00, 0])

    assert _active_pgc_audio_streams(bytes(data), pgc_abs, offset_mode=False) == {
        0x80,
        0x81,
    }


def test_active_pgc_audio_streams_offset_mode() -> None:
    data = bytearray(0x600)
    data[0x202:0x204] = struct.pack(">H", 2)
    pgc_abs = 0x400
    asct_base = pgc_abs + 0x40

    data[pgc_abs + 0x0C : pgc_abs + 0x0E] = struct.pack(">H", 0x40)
    data[asct_base : asct_base + 16] = (
        struct.pack(">H", 0x8001)
        + b"\x00" * 6
        + struct.pack(">H", 0x8002)
        + b"\x00" * 6
    )

    assert _active_pgc_audio_streams(bytes(data), pgc_abs, offset_mode=True) == {
        0x81,
        0x82,
    }


def test_active_pgc_subpicture_streams_inline_and_offset_modes() -> None:
    inline = bytearray(0x300)
    inline[0x200 + 0x1C : 0x200 + 0x1C + 8] = bytes([0x82, 0, 0, 0, 0x83, 0, 0, 0])
    assert _active_pgc_subpicture_streams(bytes(inline), 0x200, offset_mode=False) == {
        0x22,
        0x23,
    }

    offset = bytearray(0x600)
    pgc_abs = 0x400
    spst_base = pgc_abs + 0x80
    offset[pgc_abs + 0x1C : pgc_abs + 0x1E] = struct.pack(">H", 0x80)
    offset[spst_base : spst_base + 8] = bytes([0x84, 0, 0, 0, 0x9F, 5, 0, 0])

    assert _active_pgc_subpicture_streams(bytes(offset), pgc_abs, offset_mode=True) == {
        0x24
    }


def test_pgc_offset_table_base_rejects_missing_or_truncated_pointers() -> None:
    valid = bytearray(0x210)
    valid[0x20C:0x20E] = struct.pack(">H", 0x10)
    assert _pgc_offset_table_base(bytes(valid), 0x200, 0x0C) == 0x210

    zero_pointer = bytearray(valid)
    zero_pointer[0x20C:0x20E] = b"\x00\x00"
    assert _pgc_offset_table_base(bytes(zero_pointer), 0x200, 0x0C) is None
    assert _pgc_offset_table_base(b"\x00" * 0x20D, 0x200, 0x0C) is None


def test_parse_pgc_stream_languages_offset_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bytearray(0x700)
    data[:12] = b"DVDVIDEO-VTS"
    data[0x202:0x204] = struct.pack(">H", 2)
    pgc_abs = 0x300
    data[pgc_abs : pgc_abs + 2] = struct.pack(">H", 0x0002)

    asct_base = pgc_abs + 0x40
    data[pgc_abs + 0x0C : pgc_abs + 0x0E] = struct.pack(">H", 0x40)
    data[asct_base : asct_base + 24] = (
        struct.pack(">H", 0x8001)
        + b"en"
        + b"\x00" * 4
        + struct.pack(">H", 0x8002)
        + b"fr"
        + b"\x00" * 4
        + struct.pack(">H", 0x0003)
        + b"de"
        + b"\x00" * 4
    )

    spst_base = pgc_abs + 0x80
    data[pgc_abs + 0x1C : pgc_abs + 0x1E] = struct.pack(">H", 0x80)
    data[spst_base : spst_base + 12] = (
        struct.pack(">H", 0x8001)
        + b"es"
        + b"\x00" * 0
        + struct.pack(">H", 0x0002)
        + b"it"
        + b"\x00" * 0
        + struct.pack(">H", 0x8020)
        + b"xx"
        + b"\x00" * 0
    )

    monkeypatch.setattr(
        dvdifo,
        "_find_main_pgc",
        lambda _data, _pgc_number: (pgc_abs, 100.0, 1),
    )

    assert _parse_pgc_stream_languages(bytes(data)) == (
        {0x81: "en", 0x82: "fr"},
        {0x21: "es"},
    )


def test_find_main_pgc_falls_back_to_longest_then_most_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = bytearray(0x200)
    data[:12] = b"DVDVIDEO-VTS"
    pgcs = [
        (1, 0x100, 50.0, 2),
        (2, 0x180, 75.0, 3),
        (3, 0x200, 75.0, 4),
    ]
    monkeypatch.setattr(dvdifo, "_vts_ttn1_pgc_abs", lambda _data: None)
    monkeypatch.setattr(dvdifo, "_enumerate_vts_pgcs", lambda _data: pgcs)

    assert dvdifo._find_main_pgc(bytes(data)) == (0x200, 75.0, 4)
    assert dvdifo._find_main_pgc(bytes(data), 1) == (0x100, 50.0, 2)

    monkeypatch.setattr(dvdifo, "_enumerate_vts_pgcs", lambda _data: [])
    assert dvdifo._find_main_pgc(bytes(data)) is None


def _vts_ttn1_pointer_buffer() -> bytes:
    data = bytearray(0x1100)
    data[0xC8:0xCC] = struct.pack(">I", 1)  # VTS_PTT_SRPT sector
    data[0xCC:0xD0] = struct.pack(">I", 2)  # VTS_PGCIT sector
    data[0x800:0x802] = struct.pack(">H", 1)  # one SRPT title
    data[0x808:0x80C] = struct.pack(">I", 0x10)
    data[0x810:0x812] = struct.pack(">H", 1)  # one PTT
    data[0x812:0x814] = struct.pack(">H", 2)  # PGC number 2
    data[0x1010:0x1018] = struct.pack(">I", 0) + struct.pack(">I", 0x20)
    return bytes(data)


def test_vts_ttn1_pointer_stages() -> None:
    data = _vts_ttn1_pointer_buffer()

    assert _vts_ptt_srpt_base(data) == 0x800
    assert _vts_title_unit_base(data, 0x800) == 0x810
    assert _vts_ttn1_pgc_number(data, 0x810) == 2
    assert _vts_pgc_absolute_offset(data, 2) == 0x1020
    assert dvdifo._vts_ttn1_pgc_abs(data) == 0x1020


def test_vts_ttn1_pointer_stages_reject_invalid_entries() -> None:
    zero_srpt = bytearray(_vts_ttn1_pointer_buffer())
    zero_srpt[0xC8:0xCC] = b"\x00" * 4
    assert _vts_ptt_srpt_base(bytes(zero_srpt)) is None

    zero_titles = bytearray(_vts_ttn1_pointer_buffer())
    zero_titles[0x800:0x802] = b"\x00\x00"
    assert _vts_ptt_srpt_base(bytes(zero_titles)) is None

    zero_ptts = bytearray(_vts_ttn1_pointer_buffer())
    zero_ptts[0x810:0x812] = b"\x00\x00"
    assert _vts_title_unit_base(bytes(zero_ptts), 0x800) is None

    zero_pgcn = bytearray(_vts_ttn1_pointer_buffer())
    zero_pgcn[0x812:0x814] = b"\x00\x00"
    assert _vts_ttn1_pgc_number(bytes(zero_pgcn), 0x810) is None

    zero_pgcit = bytearray(_vts_ttn1_pointer_buffer())
    zero_pgcit[0xCC:0xD0] = b"\x00" * 4
    assert _vts_pgc_absolute_offset(bytes(zero_pgcit), 2) is None


def test_pgc_program_tables_and_ranges() -> None:
    data = bytearray(0x500)
    pgc_abs = 0x200
    data[pgc_abs + 2] = 2  # programs
    data[pgc_abs + 3] = 3  # cells
    data[pgc_abs + 0xE6 : pgc_abs + 0xE8] = struct.pack(">H", 0x120)
    data[pgc_abs + 0xE8 : pgc_abs + 0xEA] = struct.pack(">H", 0x200)
    data[0x320:0x323] = bytes([1, 2, 3])
    parsed = bytes(data)

    assert _pgc_program_tables(parsed, pgc_abs) == (0x320, 0x400, 2, 3)
    assert _pgc_program_cell_range(parsed, 0x320, 0, 2, 3) == (1, 1)
    assert _pgc_program_cell_range(parsed, 0x320, 1, 2, 3) == (2, 3)

    invalid = bytearray(data)
    invalid[pgc_abs + 3] = 1
    assert _pgc_program_tables(bytes(invalid), pgc_abs) is None


def test_trim_trailing_menu_programs_stops_at_real_program() -> None:
    chapters = [0.0, 100.0, 110.0]
    durations = [100.0, 10.0, 1.0]

    result = _trim_trailing_menu_programs(chapters, durations, 111.0)

    assert result == ([0.0, 100.0], 110.0)
