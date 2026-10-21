"""DVD parser tests on synthetic IFO files, so they run everywhere.

The files come from tests/synth_dvd.py, which follows
docs/formats/dvd-video.md (libdvdread, mpucoder, dvdauthor), not the parser:
these tests check the parser against the format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import dvdifo, vobsub
from tests.synth_dvd import (
    Audio,
    Cell,
    Pgc,
    Subpicture,
    Video,
    set_angle,
    spu,
    subpicture_pack,
    video_pack,
    vmg_ifo,
    vts_ifo,
)

NTSC = 1.001  # a 30 fps dvd_time_t counts NTSC frames: 1 "second" is 1.001 s


def _one_title(
    *, audio: list[Audio] | None = None, subpictures: list[Subpicture] | None = None
) -> bytes:
    return vts_ifo([Pgc([Cell(600, 0, 999)])], audio=audio, subpictures=subpictures)


def test_only_caption_subtitles_are_hearing_impaired() -> None:
    # Code extensions per SPRM 19: 1 normal, 5/6/7 captions, 9 forced,
    # 13 director's comments. Every stream has language type 1 (a code).
    data = _one_title(
        subpictures=[
            Subpicture("en", 1),
            Subpicture("en", 5),
            Subpicture("en", 6),
            Subpicture("en", 7),
            Subpicture("fr", 9),
            Subpicture("en", 13),
        ]
    )

    attrs = dvdifo._parse_vts_subp_attrs(data)

    assert [attrs[0x20 + n].is_hearing_impaired for n in range(6)] == [
        False,
        True,
        True,
        True,
        False,
        False,
    ]
    assert [attrs[0x20 + n].is_forced for n in range(6)] == [False] * 4 + [True, False]
    assert attrs[0x25].is_commentary
    assert [attrs[0x20 + n].lang_code for n in range(6)] == ["en"] * 4 + ["fr", "en"]


def test_audio_attributes() -> None:
    # Code extensions per SPRM 17: 1 normal, 2 for the visually impaired,
    # 3 director's comments, 4 alternate director's comments.
    data = _one_title(
        audio=[
            Audio("en", 6),
            Audio("fr", 2),
            Audio("en", 2, code_extension=2),
            Audio("en", 2, code_extension=3),
            Audio("en", 2, code_extension=4),
        ]
    )

    attrs = dvdifo._parse_vts_audio_attrs(data)

    assert [(a.lang_code, a.channels, a.codec) for a in attrs.values()] == [
        ("en", 6, "AC3"),
        ("fr", 2, "AC3"),
        ("en", 2, "AC3"),
        ("en", 2, "AC3"),
        ("en", 2, "AC3"),
    ]
    assert [a.is_commentary for a in attrs.values()] == [
        False,
        False,
        False,
        True,
        True,
    ]


# --- cell address table ----------------------------------------------------------


def _feature_and_extra() -> bytes:
    """A feature in VOB 1 (two cells) and a 2-minute extra in VOB 2, whose
    cells sit after the feature's.

    The first cell ends at sector 0x201FF. Read 4 bytes off (as the parser
    once did), that end sector becomes "VOB 2, cell 1": a valid-looking
    entry pointing into the feature, so the extra was cut from the wrong
    place, as happened on real discs.
    """
    feature = Pgc(
        [
            Cell(1500, 0, 0x201FF, vob_id=1, cell_id=1),
            Cell(1500, 0x20200, 199_999, vob_id=1, cell_id=2),
        ],
        title=1,
    )
    extra = Pgc([Cell(120, 200_000, 203_999, vob_id=2, cell_id=1)], title=2)
    return vts_ifo([feature, extra])


def test_cell_address_table_entries() -> None:
    cells = dvdifo._parse_vts_c_adt(_feature_and_extra())

    assert cells == [
        {"vob_id": 1, "cell_id": 1, "start_sector": 0, "end_sector": 0x201FF},
        {"vob_id": 1, "cell_id": 2, "start_sector": 0x20200, "end_sector": 199_999},
        {"vob_id": 2, "cell_id": 1, "start_sector": 200_000, "end_sector": 203_999},
    ]


def test_each_title_is_cut_from_its_own_cells() -> None:
    data = _feature_and_extra()
    vob_bytes = 204_000 * 2048

    assert dvdifo._lookup_main_feature_range(data, vob_bytes, 1) == (
        0,
        200_000 * 2048,
    )
    assert dvdifo._lookup_main_feature_range(data, vob_bytes, 2) == (
        200_000 * 2048,
        204_000 * 2048,
    )


# --- disc and title layout -------------------------------------------------------


def test_vmg_title_map_and_provider(tmp_path: Path) -> None:
    path = tmp_path / "VIDEO_TS.IFO"
    path.write_bytes(vmg_ifo([(1, 1), (1, 2), (2, 1)], provider="A STUDIO"))

    vmg = dvdifo._parse_vmg_ifo(path)

    assert vmg.get("provider_id") == "A STUDIO"
    assert vmg.get("title_map") == {1: (1, 1), 2: (1, 2), 3: (2, 1)}


def test_chapters_start_at_each_program() -> None:
    cells = [Cell(600, 0, 999), Cell(1200, 1000, 2999), Cell(900, 3000, 4499)]
    data = vts_ifo([Pgc(cells, programs=[1, 3])])

    chapters, duration = dvdifo._parse_vts_pgc_info(data)

    assert chapters == pytest.approx([0.0, 1800 * NTSC])
    assert duration == pytest.approx(2700 * NTSC)


def test_main_pgc_is_the_first_titles_and_others_by_number() -> None:
    data = vts_ifo(
        [
            Pgc([Cell(5400, 0, 9999)], title=1),
            Pgc([Cell(12, 10_000, 10_099)], title=2),
        ]
    )

    first, second = dvdifo._enumerate_vts_pgcs(data)
    assert dvdifo._vts_ttn1_pgc_abs(data) == first[1]
    assert dvdifo._find_main_pgc(data) == (first[1], pytest.approx(5400 * NTSC), 1)
    assert dvdifo._find_main_pgc(data, 2) == (second[1], pytest.approx(12 * NTSC), 1)
    assert dvdifo._find_main_pgc(data, 3) is None


# --- streams a title plays -----------------------------------------------------------


def test_audio_of_pgcs_sharing_cells_is_listed_too() -> None:
    # The feature enables English only; a variant playing the same cell
    # also enables a commentary. A trailer on its own cell enables French.
    feature_cell = Cell(5400, 0, 9999, vob_id=1, cell_id=1)
    data = vts_ifo(
        [
            Pgc([feature_cell], audio=[0, None, None], title=1),
            Pgc([feature_cell], audio=[0, None, 2], title=2),
            Pgc([Cell(90, 20_000, 20_499, vob_id=2)], audio=[None, 1], title=3),
        ],
        audio=[Audio("en", 6), Audio("fr"), Audio("en", code_extension=3)],
    )

    feature = dvdifo.pgc_stream_ids(data, 1)

    assert feature.audio == {0x80: 0x80, 0x82: 0x82}
    assert dvdifo.pgc_stream_ids(data, 3).audio == {0x81: 0x81}


def test_widescreen_titles_play_the_wide_subtitle_stream() -> None:
    # Slot 0 shows stream 0 in 4:3 but stream 1 in 16:9.
    pgc = Pgc([Cell(600, 0, 999)], subpictures=[(0, 1)])
    subtitles = [Subpicture("en")]

    narrow = vts_ifo([pgc], video=Video(aspect=0), subpictures=subtitles)
    wide = vts_ifo([pgc], video=Video(aspect=3), subpictures=subtitles)

    assert dvdifo.pgc_stream_ids(narrow).subtitles == {0x20: 0x20}
    assert dvdifo.pgc_stream_ids(wide).subtitles == {0x21: 0x20}


# --- angles -------------------------------------------------------------------------


def _angle_pgc(angle: int) -> Pgc:
    """A title with a two-angle block between two ordinary cells, choosing
    *angle* with a SetSTN pre-command."""
    return Pgc(
        [
            Cell(100, 0, 999, vob_id=1),
            Cell(
                300,
                1000,
                3999,
                vob_id=2,
                cell_id=1,
                block_mode=1,
                block_type=1,
                interleaved=True,
            ),
            Cell(
                310,
                4000,
                6999,
                vob_id=2,
                cell_id=2,
                block_mode=3,
                block_type=1,
                interleaved=True,
            ),
            Cell(200, 7000, 8999, vob_id=3),
        ],
        programs=[1, 2, 4],
        seconds=610,
        pre=[set_angle(angle)],
        title=angle,
    )


@pytest.mark.parametrize(("angle", "cell", "seconds"), [(1, 1, 300), (2, 3, 310)])
def test_an_angle_title_plays_its_own_angle_cell(
    angle: int, cell: int, seconds: float
) -> None:
    data = vts_ifo([_angle_pgc(1), _angle_pgc(2)])
    main = dvdifo._find_main_pgc(data, angle)
    assert main is not None
    pgc_abs, _duration, cell_count = main

    assert dvdifo._pgc_angle_from_commands(data, pgc_abs) == angle
    selection = dvdifo._select_main_edition_cells(data, pgc_abs, cell_count)
    assert selection is not None
    cells, interleaved = selection
    assert interleaved is True
    assert [(c.vob_id, c.block_mode) for c in cells] == [(1, 0), (2, cell), (3, 0)]
    chapters, _total = dvdifo._parse_vts_pgc_info(data, angle)
    assert chapters == pytest.approx([0.0, 100 * NTSC, (100 + seconds) * NTSC])


def test_ordinary_cells_are_copied_as_their_own_sector_ranges() -> None:
    # Not interleaved: no need to read NAV packets, each cell is one run.
    data = vts_ifo(
        [
            Pgc(
                [
                    Cell(100, 0, 999),
                    Cell(200, 1000, 2999, cell_id=2),
                    Cell(100, 5000, 5999, vob_id=2),
                ]
            )
        ]
    )
    admap = dvdifo._parse_vts_vobu_admap(data)
    assert admap is not None

    assert dvdifo._build_main_edition_vobu_ranges(data, admap, [], 1) == [
        (0, 1000 * 2048),
        (1000 * 2048, 3000 * 2048),
        (5000 * 2048, 6000 * 2048),
    ]


# --- editions, extras and episodes ------------------------------------------------


def test_a_pgc_recutting_the_feature_is_an_edition() -> None:
    # PGC 2 plays the feature's cells with a scene added; PGC 3 is unrelated.
    first, second = (
        Cell(2000, 0, 9999, cell_id=1),
        Cell(2000, 10_000, 19_999, cell_id=2),
    )
    scene = Cell(600, 20_000, 22_999, cell_id=3)
    data = vts_ifo(
        [
            Pgc([first, second], title=1),
            Pgc([first, scene, second], title=2),
            Pgc([Cell(300, 30_000, 32_999, vob_id=2)], title=3),
        ]
    )

    assert dvdifo._find_alternate_edition_pgcs(data) == [(2, True), (3, False)]
    assert dvdifo._detect_episode_pgcs(data) == ([], None)


def test_extras_sharing_only_a_title_card_are_not_episodes_beside_a_featurette() -> (
    None
):
    # A bonus VTS: every extra opens with the same 1 s card. The extras'
    # lengths cluster like episodes, but a 15-minute featurette is more than
    # three times the longest (the parser's dwarfing ratio).
    card = Cell(1, 0, 9, vob_id=1)
    extras = [
        Pgc(
            [
                card,
                Cell(
                    60 + 10 * n,
                    1000 * (n + 1),
                    1000 * (n + 1) + 899,
                    vob_id=2,
                    cell_id=n + 1,
                ),
            ],
            title=n + 2,
        )
        for n in range(4)
    ]
    featurette = Pgc([card, Cell(900, 9000, 9999, vob_id=3)], title=6)
    data = vts_ifo([Pgc([Cell(53, 10, 99, cell_id=2)], title=1), *extras, featurette])

    assert dvdifo._find_alternate_edition_pgcs(data) == [
        (n, False) for n in (2, 3, 4, 5, 6)
    ]
    assert dvdifo._detect_episode_pgcs(data) == ([], None)


def _episode_cells() -> list[Cell]:
    return [
        Cell(1320 + 7 * n, 10_000 * n, 10_000 * n + 9999, cell_id=n + 1)
        for n in range(4)
    ]


def test_episodes_and_their_play_all_are_found() -> None:
    cells = _episode_cells()
    data = vts_ifo([*(Pgc([c], title=n + 1) for n, c in enumerate(cells)), Pgc(cells)])

    assert dvdifo._detect_episode_pgcs(data) == ([1, 2, 3, 4], 5)


def test_episode_length_titles_beside_a_feature_are_bonus_content() -> None:
    feature = Pgc([Cell(6000, 100_000, 199_999, vob_id=2)], title=1)
    data = vts_ifo(
        [feature, *(Pgc([c], title=n + 2) for n, c in enumerate(_episode_cells()))]
    )

    assert dvdifo._detect_episode_pgcs(data) == ([], None)


def test_a_still_image_montage_is_trimmed_to_its_real_length() -> None:
    # 38 three-second still cells, one program each: menu filler, though the
    # PGC header honestly declares about two minutes.
    stills = [
        Cell(3, 10 * n, 10 * n + 9, cell_id=n + 1, still_time=255) for n in range(38)
    ]
    data = vts_ifo([Pgc([Cell(5400, 1000, 99_999, vob_id=2)], title=1), Pgc(stills)])
    pgcs = dvdifo._enumerate_vts_pgcs(data)

    effective = dvdifo._effective_pgc_durations(data, pgcs)

    assert pgcs[1][2] == pytest.approx(114 * NTSC)
    assert effective[1][2] == pytest.approx(3 * NTSC)
    assert dvdifo._find_alternate_edition_pgcs(data) == []


def test_episode_part_labels_reject_non_alternating_sets() -> None:
    """A single duration family or non-interleaved sets get no a/b pairing."""
    # All one family: sequential numbering (an anthology of 16 cartoons).
    one_family = [(num, num * 10, 400.0 + num, 4) for num in range(1, 17)]
    assert dvdifo._episode_part_labels(one_family) is None

    # Two families of equal size but grouped, not alternating: pairing is
    # ambiguous.
    grouped = [
        (1, 10, 1146.0, 4),
        (2, 20, 1140.0, 4),
        (3, 30, 286.0, 3),
        (4, 40, 285.0, 3),
    ]
    assert dvdifo._episode_part_labels(grouped) is None


# --- VOB packets ------------------------------------------------------------------


def test_subpicture_packets_and_video_timestamps_are_found(tmp_path: Path) -> None:
    vob = tmp_path / "VTS_01_1.VOB"
    vob.write_bytes(
        video_pack(90_000)
        + subpicture_pack(0x20, spu(100), 90_000)
        + subpicture_pack(0x21, spu(80), 180_000)
    )

    found = vobsub._scan_vob_subpictures([vob], max_bytes=1 << 20)

    assert {
        sid: [(pts, len(data)) for pts, data in units] for sid, units in found.items()
    } == {
        0x20: [(90_000, 100)],
        0x21: [(180_000, 80)],
    }
    assert [pts for pts, _offset in vobsub._scan_vob_pts([vob], max_bytes=1 << 20)] == [
        90_000
    ]


# --- angle blocks and copy-protection layouts ------------------------------------


def _block(first: Cell, second: Cell) -> list[Cell]:
    """Two cells as a two-angle block (first / last of block, angle type)."""
    first.block_mode, first.block_type, first.interleaved = 1, 1, True
    second.block_mode, second.block_type, second.interleaved = 3, 1, True
    return [first, second]


def test_angle_blocks_with_unequal_angles_are_misauthored() -> None:
    # Real angles film the same moment, so they run equally long; "angles"
    # of 165 s and 95 s are consecutive film segments mislabelled.
    opening = Cell(300, 0, 9999)
    equal = _block(
        Cell(120, 10_000, 19_999, cell_id=2), Cell(120, 10_000, 19_999, cell_id=3)
    )
    unequal = _block(
        Cell(165, 20_000, 29_999, cell_id=4), Cell(95, 20_000, 29_999, cell_id=5)
    )
    data = vts_ifo(
        [
            Pgc([opening], title=1),
            Pgc([opening, *equal], seconds=420, title=2),
            Pgc([opening, *unequal], seconds=465, title=3),
        ]
    )

    assert [dvdifo._pgc_angle_lengths_differ(data, pgc) for pgc in (1, 2, 3)] == [
        False,
        False,
        True,
    ]


def test_padded_and_decoy_chains_are_told_from_the_real_one() -> None:
    # The real chain plays three cells in disc order with exact ranges. A
    # padded copy plays the same cells (same IDs) with ranges far beyond
    # what their durations can hold; a decoy jumps back and forth.
    def cell(cell_id: int, first: int, last: int) -> Cell:
        return Cell(10, first, last, vob_id=1, cell_id=cell_id)

    real = [cell(1, 0, 4_999), cell(2, 5_000, 9_999), cell(3, 10_000, 14_999)]
    padded = [cell(1, 0, 99_999), cell(2, 5_000, 104_999), cell(3, 10_000, 114_999)]
    decoy = [
        cell(3, 10_000, 14_999),
        cell(1, 0, 4_999),
        cell(4, 200_000, 204_999),
        cell(2, 5_000, 9_999),
        cell(5, 150_000, 154_999),
        cell(6, 1_000, 5_999),
    ]
    data = vts_ifo([Pgc(real, title=1), Pgc(padded, title=2), Pgc(decoy, title=3)])

    layouts = [dvdifo.pgc_layout(data, pgc) for pgc in (1, 2, 3)]
    assert all(layout is not None for layout in layouts)
    first, second, third = (layout for layout in layouts if layout is not None)
    assert (first.padded, first.backward_jumps) == (False, 0)
    assert (second.padded, second.backward_jumps) == (True, 0)
    assert second.fingerprint == first.fingerprint
    assert third.backward_jumps >= 3


def test_an_invalid_setstn_angle_plays_angle_one() -> None:
    # Players ignore an angle outside 1-9 (protected discs set 11).
    block = _block(Cell(120, 1000, 1999, cell_id=2), Cell(120, 1000, 1999, cell_id=3))
    cells = [Cell(60, 0, 999), *block]
    data = vts_ifo(
        [
            Pgc(cells, pre=[set_angle(1)], seconds=180, title=1),
            Pgc(cells, pre=[set_angle(11)], seconds=180, title=2),
        ]
    )
    main = dvdifo._find_main_pgc(data, 2)
    assert main is not None

    assert dvdifo._pgc_angle_from_commands(data, main[0]) == 1
    first, second = dvdifo.pgc_layout(data, 1), dvdifo.pgc_layout(data, 2)
    assert first is not None and second is not None
    assert second.fingerprint == first.fingerprint == ((1, 1), (1, 2))
