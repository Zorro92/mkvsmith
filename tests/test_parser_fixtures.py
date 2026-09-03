"""Fixture-based regression tests for the binary parsers.

These capture real disc-structure blobs and assert the parsed output, so a
format-parsing change (an off-by-one in a struct offset, wrong endianness, or a
misread channel config) breaks a test instead of silently shipping.

Fixtures (see ``tests/fixtures/``):
  - ``00800.mpls`` / ``00875.clpi`` / ``bdmt_eng.xml`` — Monsters University
    (2013) Blu-ray.
  - ``dvd_video_ts.ifo`` / ``dvd_vts_01_0.ifo`` — Cats Don't Dance (1997) DVD.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluray import _parse_bdmv_disc_name, _parse_clpi, _parse_mpls
from dvdifo import (
    _parse_vmg_ifo,
    _parse_vts_ifo_languages,
    _parse_vts_pgc_info,
    _parse_vts_video_attrs,
    _parse_vts_audio_attrs,
)
from models import StreamType

# Disc-derived fixtures are not committed (to avoid redistributing disc
# metadata). These tests skip on a fresh clone; capture the fixtures locally
# to run them (see scripts/inspect_fixtures.py and the README "Disc fixtures"
# section).
_FIXTURES = (
    "00800.mpls",
    "00875.clpi",
    "bdmt_eng.xml",
    "dvd_video_ts.ifo",
    "dvd_vts_01_0.ifo",
)

pytestmark = pytest.mark.skipif(
    not all((Path(__file__).parent / "fixtures" / f).exists() for f in _FIXTURES),
    reason="disc fixtures not present; capture them locally (see README)",
)


# --- Blu-ray MPLS ------------------------------------------------------------


def test_parse_mpls_main_movie(fixtures_dir: Path) -> None:
    info = _parse_mpls(fixtures_dir / "00800.mpls")
    assert info is not None

    assert len(info["play_items"]) == 132
    duration = sum(pi["duration"] for pi in info["play_items"])
    assert duration == pytest.approx(6228.0, abs=1.0)

    chapters = info["chapter_times"]
    assert len(chapters) == 33
    assert chapters[0] == pytest.approx(0.0)

    streams = info["streams"]
    assert len(streams) == 15

    video = [s for s in streams if s["type"] is StreamType.VIDEO]
    audio = [s for s in streams if s["type"] is StreamType.AUDIO]
    subs = [s for s in streams if s["type"] is StreamType.SUBTITLE]
    assert len(video) == 1
    assert len(audio) == 6
    assert len(subs) == 8

    assert video[0]["pid"] == 4113
    assert video[0]["codec"] == "h264"
    assert audio[0]["lang"] == "eng"


# --- Blu-ray CLPI ------------------------------------------------------------


def test_parse_clpi_main_movie_clip(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "00875.clpi").read_bytes()
    result = _parse_clpi(data)

    assert len(result) == 15

    video = result[4113]
    assert video["codec"] == "h264"
    assert video["height"] == 1080
    assert video["framerate"] == pytest.approx(24000 / 1001, abs=1e-3)

    truehd = result[4352]
    assert truehd["codec"] == "truehd"
    assert truehd["channels"] == 6
    assert truehd["language"] == "eng"

    assert result[4353]["codec"] == "ac3"
    assert result[4608]["codec"] == "hdmv_pgs_subtitle"
    assert result[4608]["language"] == "eng"


# --- Blu-ray disc metadata ---------------------------------------------------


def test_parse_bdmv_disc_name(fixtures_dir: Path, tmp_path: Path) -> None:
    meta = tmp_path / "BDMV" / "META" / "DL"
    meta.mkdir(parents=True)
    (meta / "bdmt_eng.xml").write_bytes((fixtures_dir / "bdmt_eng.xml").read_bytes())

    assert (
        _parse_bdmv_disc_name(tmp_path / "BDMV")
        == "Monsters University - Blu-ray\u2122"
    )


# --- DVD VMG -----------------------------------------------------------------


def test_parse_vmg_ifo(fixtures_dir: Path) -> None:
    vmg = _parse_vmg_ifo(fixtures_dir / "dvd_video_ts.ifo")

    assert vmg["provider_id"] == "WARNER HOME VIDEO"
    assert vmg["title_map"] == {1: (1, 207)}


# --- DVD VTS -----------------------------------------------------------------


def test_parse_vts_ifo(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "dvd_vts_01_0.ifo").read_bytes()

    chapters, duration = _parse_vts_pgc_info(data)
    assert len(chapters) == 23
    assert duration == pytest.approx(4484.0, abs=1.0)
    assert chapters[0] == pytest.approx(0.0)

    audio_lang, sub_lang = _parse_vts_ifo_languages(data)
    assert audio_lang[128] == "en"
    assert audio_lang[129] == "fr"
    assert sub_lang[32] == "en"
    assert sub_lang[34] == "es"

    video = _parse_vts_video_attrs(data)
    assert video is not None
    assert video.resolution == (720, 480)
    assert video.mpeg_version == "MPEG-2"
    assert video.standard == "NTSC"

    audio_attrs = _parse_vts_audio_attrs(data)
    assert audio_attrs[0x80].codec == "AC3"
    assert audio_attrs[0x80].channels == 6
    assert audio_attrs[0x81].channels == 2
