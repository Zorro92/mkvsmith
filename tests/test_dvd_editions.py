"""Multi-edition MKVs from DVD seamless branching.

DVD editions are program chains of one title set that share cells; the
combined file holds every cell once, and each edition plays its own cells
through ordered chapters, exactly like Blu-ray playlists over clips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mkv
import scan
from dvdifo import (
    _EditionCell,
    _parse_vts_pgc_info,
    _read_nav_ptm_from_sector,
    pgc_layout,
)
from models import EditionAtom, EditionSpec, Stream, StreamType, Title

_PLATINUM = Path(__file__).parent / "fixtures" / "batb_platinum_mex_vts09.ifo"


def _chain(index: int, cells: list[tuple[int, float]], vts: int = 9) -> Title:
    title = Title(
        index=index,
        source_file=Path("VTS_09_1.VOB"),
        name=f"Disc - Edition {index + 1}",
        duration_seconds=sum(duration for _cell, duration in cells),
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="mpeg2video"),
        Stream(index=1, stream_type=StreamType.AUDIO, codec="ac3", language="eng"),
    ]
    title.dvd_vts_number = vts
    title.dvd_ifo_data = b"DVDVIDEO-VTS"
    title.dvd_cell_fingerprint = tuple((cell, 1) for cell, _duration in cells)
    title.dvd_cell_durations = tuple(duration for _cell, duration in cells)
    return title


def test_dvd_editions_must_share_real_footage() -> None:
    extended = _chain(0, [(1, 2.0), (2, 2.0), (3, 2.0), (10, 3000.0), (11, 600.0)])
    theatrical = _chain(1, [(1, 2.0), (2, 2.0), (3, 2.0), (10, 3000.0), (12, 300.0)])
    # Only the opening bridge cells in common: a separately stored version.
    separate = _chain(2, [(1, 2.0), (2, 2.0), (3, 2.0), (20, 3300.0)])
    other_vts = _chain(3, [(1, 2.0), (2, 2.0), (3, 2.0), (10, 3000.0)], vts=10)

    assert scan._editions_share_clips(extended, theatrical)
    assert not scan._editions_share_clips(extended, separate)
    assert not scan._editions_share_clips(extended, other_vts)
    assert scan._detect_edition_groups([extended, theatrical, separate]) == [
        [extended, theatrical]
    ]


@pytest.mark.skipif(not _PLATINUM.exists(), reason="Platinum Edition fixture missing")
def test_combined_title_from_platinum_chains() -> None:
    """Special Edition, Theatrical and Work-in-Progress (pencil tests on
    angle 2) of the pressed Platinum Edition combine over 119 unique cells."""
    ifo = _PLATINUM.read_bytes()
    titles: list[Title] = []
    for index, pgc in enumerate((1, 2, 3)):
        layout = pgc_layout(ifo, pgc)
        assert layout is not None
        chapters, duration = _parse_vts_pgc_info(ifo, pgc)
        title = _chain(index, [])
        title.dvd_ifo_data = ifo
        title.dvd_pgc_number = pgc
        title.duration_seconds = duration
        title.chapters = chapters
        title.dvd_cell_fingerprint = layout.fingerprint
        title.dvd_cell_durations = layout.cell_durations
        title.dvd_edition_label = f"Edition {index + 1}"
        titles.append(title)

    combined = scan.build_multi_edition_title(titles)

    assert combined.name == "Disc"
    assert len(combined.dvd_edition_cells) == len(combined.clip_durations) == 119
    assert [len(edition.atoms) > 0 for edition in combined.editions] == [True] * 3
    for edition, title in zip(combined.editions, titles):
        assert edition.duration == pytest.approx(title.duration_seconds, abs=0.5)
    assert combined.editions[0].is_default


def test_dvd_edition_inputs_retime_atoms_onto_nav_durations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """IFO cell times are nominal; the mux moves atoms onto the cells' real
    durations (a 2.2 s bridge cell that plays 1.4 s shifts what follows)."""
    title = _chain(0, [])
    title.dvd_edition_cells = [
        _EditionCell(0, 9, 1, 1, 2.2, 0, 0),
        _EditionCell(10, 99, 2, 1, 100.0, 0, 1),
    ]
    title.clip_durations = [2.2, 100.0]
    title.editions = [
        EditionSpec(
            uid=1,
            name="Edition 1",
            is_default=True,
            atoms=[
                EditionAtom(0.0, 2.2, hidden=False, name="Chapter 01"),
                EditionAtom(2.2, 102.2, hidden=False, name="Chapter 02"),
            ],
        )
    ]

    def layout(*_args: object) -> tuple[list[tuple[int, int]], list[float]]:
        return [(0, 2048)], [1.4, 100.1]

    def write(*_args: object) -> tuple[list[Path], list[int]]:
        return [tmp_path / "part.vob"], [2048]

    import dvdifo

    monkeypatch.setattr(dvdifo, "edition_union_vobu_layout", layout)
    monkeypatch.setattr(mkv, "_write_vobu_trim", write)

    mkv._prepare_dvd_edition_inputs(
        title, [tmp_path / "VTS_09_1.VOB"], tmp_path, [], []
    )

    atoms = title.editions[0].atoms
    assert [(a.start, a.end) for a in atoms] == [
        (0.0, 1.4),
        (1.4, pytest.approx(101.5)),
    ]
    assert title.clip_durations == [1.4, 100.1]
    assert title.duration_seconds == pytest.approx(101.5)


def test_nav_ptm_is_read_from_the_pci() -> None:
    pack = bytearray(2048)
    pack[0:4] = b"\x00\x00\x01\xba"
    pci = 0x26
    pack[pci : pci + 4] = b"\x00\x00\x01\xbf"
    pack[pci + 6] = 0x00
    pack[pci + 7 + 12 : pci + 7 + 16] = (90000).to_bytes(4, "big")
    pack[pci + 7 + 16 : pci + 7 + 20] = (135045).to_bytes(4, "big")
    assert _read_nav_ptm_from_sector(bytes(pack)) == (90000, 135045)
