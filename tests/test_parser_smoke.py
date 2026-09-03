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

from bluray import _parse_bdmv_disc_name, _parse_clpi, _parse_mpls
from dvdifo import _parse_vts_ifo_languages, _parse_vts_pgc_info


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
