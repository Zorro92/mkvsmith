"""Fixture-based regression tests for the binary parsers.

These capture real disc-structure blobs and assert the parsed output, so a
format-parsing change (an off-by-one in a struct offset, wrong endianness, or a
misread channel config) breaks a test instead of silently shipping.

Fixtures (see ``tests/fixtures/``):
  - ``00800.mpls`` / ``00875.clpi`` / ``bdmt_eng.xml`` — Monsters University
    (2013) Blu-ray.
  - ``dvd_video_ts.ifo`` / ``dvd_vts_01_0.ifo`` — Cats Don't Dance (1997) DVD.
  - ``beauty_vts_09_0.ifo`` / ``beauty_vts_09_subpictures.vob`` — Beauty and
    the Beast (1991) multi-angle DVD.
  - ``treasure_vts_01_0.ifo`` / ``treasure_vts_09_0.ifo`` — Treasure Planet
    (2002) DVD: a movie VTS whose alternate PGC re-cuts the default title
    (seamless branching), and a bonus-features VTS with one PGC per extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluray import _parse_bdmv_disc_name, _parse_clpi, _parse_mpls
from dvdifo import (
    _EditionCell,
    _detect_episode_pgcs,
    _effective_pgc_durations,
    _enumerate_vts_pgcs,
    _episode_part_labels,
    _find_alternate_edition_pgcs,
    _find_main_pgc,
    _get_active_pgc_streams,
    _parse_pgc_stream_languages,
    _lookup_main_feature_range,
    _parse_vmg_ifo,
    _pgc_angle_from_commands,
    _pgc_cell_durations,
    _parse_vts_c_adt,
    _parse_vts_ifo_languages,
    _parse_vts_pgc_info,
    _parse_vts_subp_attrs,
    _parse_vts_video_attrs,
    _parse_vts_audio_attrs,
    _parse_vts_vobu_admap,
    _select_main_edition_cells,
    _shared_runtime_fraction,
    _vts_ttn1_pgc_abs,
    _build_main_edition_vobu_ranges,
)
from dvdbuild import _build_dvd_streams_from_ifo
from models import StreamType
from vobsub import _extract_spu_palette, _scan_vob_pts, _scan_vob_subpictures

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

    # All 38 VMG titles live in VTS 1, title k pointing at VTS title k
    # (which resolves to PGC k — see test_vts_ttn_pointer_resolution).
    # The previously pinned {1: (1, 207)} was TT_SRPT misread start-sector
    # bytes: entries begin after the 8-byte table header, and VTS number /
    # VTS_TTN are single bytes at entry offsets 6 and 7.
    assert vmg["provider_id"] == "WARNER HOME VIDEO"
    assert vmg["title_map"] == {k: (1, k) for k in range(1, 39)}


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


def test_parse_vts_subp_attrs_and_pgc_stream_languages(
    fixtures_dir: Path,
) -> None:
    data = (fixtures_dir / "dvd_vts_01_0.ifo").read_bytes()

    attrs = _parse_vts_subp_attrs(data)

    assert sorted(attrs) == [0x20, 0x21, 0x22]
    assert [
        (attrs[sid].lang_code, attrs[sid].is_hearing_impaired) for sid in sorted(attrs)
    ] == [
        ("en", True),
        ("fr", True),
        ("es", True),
    ]
    assert all(attrs[sid].code_extension == 0 for sid in attrs)
    assert _parse_pgc_stream_languages(data) == ({}, {})


def test_parse_vts_c_adt_and_vobu_admap(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "dvd_vts_01_0.ifo").read_bytes()

    cells = _parse_vts_c_adt(data)
    vobus = _parse_vts_vobu_admap(data)

    assert len(cells) == 53
    assert cells[0] == {
        "vob_id": 6,
        "cell_id": 70,
        "start_sector": 399360,
        "end_sector": 411275,
    }
    assert cells[-1] == {
        "vob_id": 28,
        "cell_id": 210,
        "start_sector": 655616,
        "end_sector": 1888940,
    }
    assert all(cell["start_sector"] < cell["end_sector"] for cell in cells)

    assert vobus is not None
    assert len(vobus) == 9703
    assert vobus[:4] == [0, 52, 53, 54]
    assert vobus[-4:] == [1901086, 1901138, 1901191, 1901245]
    assert vobus == sorted(vobus)


def test_get_active_pgc_streams(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "dvd_vts_01_0.ifo").read_bytes()

    assert _get_active_pgc_streams(data) == (
        {0x80, 0x81, 0x82},
        {0x20, 0x21, 0x22},
    )
    assert _get_active_pgc_streams(data, 1) == (
        {0x80, 0x81, 0x82},
        {0x20, 0x21, 0x22},
    )
    assert _get_active_pgc_streams(data, 2) == (
        {0x80, 0x81, 0x82},
        {0x20, 0x21, 0x22},
    )


def test_find_main_pgc_and_enumerate_vts_pgcs(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "dvd_vts_01_0.ifo").read_bytes()

    assert _vts_ttn1_pgc_abs(data) == 4408
    assert _find_main_pgc(data) == (4408, 4484.433766666667, 71)
    assert _find_main_pgc(data, 1) == (4408, 4484.433766666667, 71)
    assert _find_main_pgc(data, 2) == (7128, 12.0, 1)
    assert _find_main_pgc(data, 3) == (7722, 32.033366666666666, 1)
    assert _find_main_pgc(data, 99) is None

    pgcs = _enumerate_vts_pgcs(data)
    assert len(pgcs) == 38
    assert pgcs[:3] == [
        (1, 4408, 4484.433766666667, 71),
        (2, 7128, 12.0, 1),
        (3, 7722, 32.033366666666666, 1),
    ]
    assert pgcs[-3:] == [
        (36, 19316, 201.16683333333333, 4),
        (37, 19722, 63.266933333333334, 2),
        (38, 20072, 1104.4337666666668, 14),
    ]

    # The three ~3-minute cartoons (PGCs 32/35/36) duration-cluster, but the
    # VTS also holds the 74-minute film (PGC 1) and an 18-minute featurette
    # (PGC 38) that dwarf them: bonus content on a movie disc, not episodes
    # (see _cluster_is_dwarfed).
    assert _detect_episode_pgcs(data) == ([], None)


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "beauty_vts_09_0.ifo").is_file(),
    reason="Beauty and the Beast multi-angle VTS fixture not present",
)
def test_parse_multi_angle_pgc_chapters(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "beauty_vts_09_0.ifo").read_bytes()

    assert _find_main_pgc(data) == (4136, 5478.033366666667, 92)
    assert _find_main_pgc(data, 2) == (7124, 5507.8341666666665, 108)
    assert _find_main_pgc(data, 3) == (10534, 5507.8341666666665, 108)
    assert _pgc_angle_from_commands(data, 7124) == 1
    assert _pgc_angle_from_commands(data, 10534) == 2

    angle_one_chapters, angle_one_duration = _parse_vts_pgc_info(data, 2)
    assert len(angle_one_chapters) == 21
    assert angle_one_duration == pytest.approx(5507.367433333334)
    assert angle_one_chapters[:4] == pytest.approx(
        [0.0, 167.0, 433.002, 790.44], abs=1e-3
    )
    assert angle_one_chapters[-4:] == pytest.approx(
        [4543.565, 4716.332, 4989.699, 5241.6], abs=1e-3
    )

    angle_two_chapters, angle_two_duration = _parse_vts_pgc_info(data, 3)
    assert len(angle_two_chapters) == 21
    assert angle_two_duration == pytest.approx(5704.533433333333)
    assert angle_two_chapters[:4] == pytest.approx(
        [0.0, 97.334, 288.236, 971.439], abs=1e-3
    )
    assert angle_two_chapters[-4:] == pytest.approx(
        [4740.731, 4913.498, 5186.865, 5438.766], abs=1e-3
    )


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "beauty_vts_09_0.ifo").is_file(),
    reason="Beauty and the Beast multi-angle VTS fixture not present",
)
def test_lookup_multi_angle_main_feature_ranges(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "beauty_vts_09_0.ifo").read_bytes()
    vob_total_bytes = 254_951_424 + 1_073_739_776 * 4 + 38_658_048

    assert _lookup_main_feature_range(data, vob_total_bytes, 1) == (
        8_192,
        3_019_911_168,
    )
    assert _lookup_main_feature_range(data, vob_total_bytes, 2) == (
        8_192,
        4_333_602_816,
    )
    assert _lookup_main_feature_range(data, vob_total_bytes, 3) == (
        8_192,
        4_333_617_152,
    )


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "beauty_vts_09_0.ifo").is_file(),
    reason="Beauty and the Beast multi-angle VTS fixture not present",
)
def test_select_multi_angle_edition_cells(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "beauty_vts_09_0.ifo").read_bytes()

    default_selection = _select_main_pgc_cells(data, 1)
    angle_one_selection = _select_main_pgc_cells(data, 2)
    angle_two_selection = _select_main_pgc_cells(data, 3)
    assert default_selection is not None
    assert angle_one_selection is not None
    assert angle_two_selection is not None

    default_cells, default_interleaved = default_selection
    angle_one_cells, angle_one_interleaved = angle_one_selection
    angle_two_cells, angle_two_interleaved = angle_two_selection

    assert len(default_cells) == 65
    assert (default_cells[0].first_sector, default_cells[0].last_sector) == (4, 82)
    assert (default_cells[-1].first_sector, default_cells[-1].last_sector) == (
        1_414_280,
        1_474_422,
    )

    assert angle_one_interleaved is True
    assert len(angle_one_cells) == 71
    assert (angle_one_cells[1].vob_id, angle_one_cells[1].block_mode) == (3, 1)

    assert angle_two_interleaved is True
    assert len(angle_two_cells) == 71
    assert (angle_two_cells[1].vob_id, angle_two_cells[1].block_mode) == (4, 3)

    assert _detect_episode_pgcs(data) == ([], None)


def _select_main_pgc_cells(
    data: bytes, pgc_number: int
) -> tuple[list[_EditionCell], bool] | None:
    main = _find_main_pgc(data, pgc_number)
    assert main is not None
    pgc_abs, _, cell_count = main
    return _select_main_edition_cells(data, pgc_abs, cell_count)


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "beauty_vts_09_0.ifo").is_file(),
    reason="Beauty and the Beast multi-angle VTS fixture not present",
)
def test_build_default_edition_vobu_ranges(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "beauty_vts_09_0.ifo").read_bytes()
    vobu_admap = _parse_vts_vobu_admap(data)
    assert vobu_admap is not None

    ranges = _build_main_edition_vobu_ranges(data, vobu_admap, [], 1)

    assert ranges is not None
    assert len(ranges) == 65
    assert ranges[:3] == [
        (8_192, 169_984),
        (169_984, 91_590_656),
        (165_996_544, 217_221_120),
    ]
    assert ranges[-1] == (2_896_445_440, 3_019_618_304)
    assert sum(end - start for start, end in ranges) == 3_019_610_112


@pytest.mark.skipif(
    not (
        Path(__file__).parent / "fixtures" / "beauty_vts_09_subpictures.vob"
    ).is_file(),
    reason="Beauty and the Beast VOB subpicture fixture not present",
)
def test_scan_multi_angle_vob_subpictures(fixtures_dir: Path) -> None:
    result = _scan_vob_subpictures(
        [fixtures_dir / "beauty_vts_09_subpictures.vob"],
        max_bytes=8192,
    )

    assert 0x20 in result
    assert 0x21 in result
    assert all(
        _extract_spu_palette(spu_data) is None
        for entries in (result[0x20], result[0x21])
        for _pts, spu_data in entries
    )
    for sub_stream_id in (0x20, 0x21):
        entries = result[sub_stream_id]
        assert [(pts, len(data)) for pts, data in entries] == [(25257, 988)]
        assert entries[0][1][:8] == bytes.fromhex("03dc03c400000000")
        assert entries[0][1][-8:] == bytes.fromhex("21df06000601e6ff")


@pytest.mark.skipif(
    not (
        Path(__file__).parent / "fixtures" / "beauty_vts_09_subpictures.vob"
    ).is_file(),
    reason="Beauty and the Beast VOB subpicture fixture not present",
)
def test_scan_multi_angle_vob_video_pts(fixtures_dir: Path) -> None:
    result = _scan_vob_pts(
        [fixtures_dir / "beauty_vts_09_subpictures.vob"], max_bytes=8192
    )

    assert result == [(25257, 2062)]


# --- DVD alternate PGCs (editions vs bonus features) ---------------------------


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_01_0.ifo").is_file(),
    reason="Treasure Planet movie VTS fixture not present",
)
def test_alternate_pgc_recutting_default_is_an_edition(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "treasure_vts_01_0.ifo").read_bytes()

    # The default movie PGC's audio-control table marks the commentary
    # stream (0x83) unavailable; the alternate PGC 2 plays the same cells
    # plus 21 half-second interstitials and enables it.
    assert _get_active_pgc_streams(data, 1) == ({0x80, 0x81, 0x82}, {0x20})

    default = _find_main_pgc(data)
    assert default is not None
    default_durations = _pgc_cell_durations(data, default[0], default[2])
    _num, pgc2_abs, _duration, pgc2_cells = _enumerate_vts_pgcs(data)[1]
    candidate = _pgc_cell_durations(data, pgc2_abs, pgc2_cells)
    assert _shared_runtime_fraction(default_durations, candidate) > 0.99

    # PGC 2 re-cuts the default title -> an edition. PGC 3 is a 1s link
    # PGC and falls below the substantial-PGC threshold.
    assert _find_alternate_edition_pgcs(data) == [(2, True)]

    # The movie + commentary variant pass the duration-cluster and
    # distinct-cell-table checks but share ~100% of their runtime: they are
    # a re-cut pair, not episodes (before the shared-footage guard this
    # surfaced live as "Episode 1"/"Episode 2" titles on the same disc).
    assert _detect_episode_pgcs(data) == ([], None)


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_01_0.ifo").is_file(),
    reason="Treasure Planet movie VTS fixture not present",
)
def test_build_dvd_streams_lists_pg_unavailable_streams(fixtures_dir: Path) -> None:
    """Streams a PGC marks unavailable still exist in the VOBs; list them.

    MakeMKV lists all four audio streams for this PGC (verified with
    makemkvcon v1.18.4) even though its stream-control table disables the
    commentary; mkvsmith must not drop it.
    """
    data = (fixtures_dir / "treasure_vts_01_0.ifo").read_bytes()

    streams = _build_dvd_streams_from_ifo(data, 5719.0, 1)

    audio = [s for s in streams if s.stream_type == StreamType.AUDIO]
    assert [s.sub_id for s in audio] == [0x80, 0x81, 0x82, 0x83]
    assert [(s.language, s.channels) for s in audio] == [
        ("en", 6),
        ("fr", 2),
        ("es", 2),
        ("en", 2),
    ]
    assert audio[3].is_commentary is True

    subtitles = [s for s in streams if s.stream_type == StreamType.SUBTITLE]
    assert [(s.sub_id, s.language, s.is_forced) for s in subtitles] == [
        (0x20, "en", False),
        (0x21, "fr", True),
        (0x22, "es", True),
    ]


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_09_0.ifo").is_file(),
    reason="Treasure Planet extras VTS fixture not present",
)
def test_detect_episode_pgcs_on_unrelated_bonus_shorts(fixtures_dir: Path) -> None:
    """Bonus shorts that duration-cluster are suppressed by the dwarfing
    check, not labelled episodes.

    The VTS 9 shorts (PGCs 5/7/10/13/19/21, 62-81s) share only a 1-2s
    title-card cell, so the shared-footage guard cannot separate them from
    genuine episodes by VTS content alone. VMG title-table corroboration
    was evaluated and falsified as a discriminator (every bonus PGC here
    is itself a VMG title), and a duration floor was falsified by Tex
    Avery (6-9 minute cartoons are legitimately an anthology's episodes).
    The discriminator that holds: the VTS contains an 869s featurette
    dwarfing the cluster's 81s maximum (11x) — bonus content on a movie
    disc, not a series VTS. This test pins the suppression.
    """
    data = (fixtures_dir / "treasure_vts_09_0.ifo").read_bytes()

    assert _detect_episode_pgcs(data) == ([], None)


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_16_0.ifo").is_file(),
    reason="Treasure Planet menu montage VTS fixture not present",
)
def test_still_cell_montage_pgc_not_exposed(fixtures_dir: Path) -> None:
    """Exposure gates measure the built title's post-trim runtime.

    PGC 2 declares 112s in its PGC header (38 x 3s still-cell thumbnails);
    the trailing-menu trim (programs under 10s are menu filler) collapses
    it to a 3s title, so it must not be exposed as a substantial PGC
    despite the honest header duration. All four PGCs here are menu mont
    chains and none survive the 60s gate.
    """
    data = (fixtures_dir / "treasure_vts_16_0.ifo").read_bytes()

    assert [
        (num, runtime, cells)
        for num, _pgc_abs, runtime, cells in _effective_pgc_durations(
            data, _enumerate_vts_pgcs(data)
        )
    ] == [
        (1, 0.5005, 13),
        (2, 3.0, 38),
        (3, 0.5005, 11),
        (4, 0.5005, 24),
    ]
    assert _find_alternate_edition_pgcs(data) == []
    assert _detect_episode_pgcs(data) == ([], None)


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_09_0.ifo").is_file(),
    reason="Treasure Planet extras VTS fixture not present",
)
def test_bonus_feature_pgcs_are_not_editions(fixtures_dir: Path) -> None:
    """A shared extras VTS (one PGC per bonus feature) is not an edition set.

    The default PGC is a 53s title card; the 20 substantial PGCs each play
    one bonus feature and share at most a 1-second card cell with it.
    """
    data = (fixtures_dir / "treasure_vts_09_0.ifo").read_bytes()

    assert _find_main_pgc(data) == (4280, 53.0, 3)
    assert _find_alternate_edition_pgcs(data) == [
        (num, False)
        for num in (
            2,
            3,
            4,
            5,
            6,
            7,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
            21,
            22,
        )
    ]


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "superman_vts_03_0.ifo").is_file(),
    reason="Superman (1988) episode VTS fixture not present",
)
def test_tied_shorts_cluster_yields_to_episodes(fixtures_dir: Path) -> None:
    """Superman (1988): seven ~19-minute episodes tie against seven ~5-minute
    "Family Album" shorts, and the 10018s play-all sums to both together.

    Detection must pick the episodes cluster (the preferred shorter cluster
    is dwarfed by the episodes), merge the shorts back in via the play-all
    (split episodes), identify the play-all chain itself, and pair the
    alternating PGCs as Episode 1a/1b ... 7a/7b.
    """
    data = (fixtures_dir / "superman_vts_03_0.ifo").read_bytes()

    episodes, play_all = _detect_episode_pgcs(data)

    assert episodes == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert play_all == 1

    all_pgcs = _effective_pgc_durations(data, _enumerate_vts_pgcs(data))
    by_number = {pgc[0]: pgc for pgc in all_pgcs}
    parts = _episode_part_labels([by_number[num] for num in episodes])
    assert parts is not None
    assert parts[7] == (1, "a")  # "Destroy the Defendroids" (episode 1)
    assert parts[8] == (1, "b")
    assert parts[10] == (2, "b")  # "The Supermarket" (episode 2b)
    assert parts[20] == (7, "b")


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "superman_vts_03_0.ifo").is_file(),
    reason="Superman (1988) episode VTS fixture not present",
)
def test_episode_part_labels_reject_non_alternating_sets() -> None:
    """A single duration family or non-interleaved sets get no a/b pairing."""
    # All one family: sequential numbering (Tex Avery's 16 cartoons).
    one_family = [(num, num * 10, 400.0 + num, 4) for num in range(1, 17)]
    assert _episode_part_labels(one_family) is None

    # Two families of equal size but grouped, not alternating: pairing is
    # ambiguous.
    grouped = [
        (1, 10, 1146.0, 4),
        (2, 20, 1140.0, 4),
        (3, 30, 286.0, 3),
        (4, 40, 285.0, 3),
    ]
    assert _episode_part_labels(grouped) is None
