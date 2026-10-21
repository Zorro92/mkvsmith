"""DVD parser tests on synthetic IFO files, so they run everywhere.

The files come from tests/synth_dvd.py, which follows
docs/formats/dvd-video.md (libdvdread, mpucoder, dvdauthor), not the parser:
these tests check the parser against the format.
"""

from __future__ import annotations

from mkvsmith import dvdifo
from tests.synth_dvd import Audio, Cell, Pgc, Subpicture, vts_ifo


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
    data = _one_title(audio=[Audio("en", 6), Audio("fr", 2), Audio("en", 2, 0, 3)])

    attrs = dvdifo._parse_vts_audio_attrs(data)

    assert [(a.lang_code, a.channels, a.codec) for a in attrs.values()] == [
        ("en", 6, "AC3"),
        ("fr", 2, "AC3"),
        ("en", 2, "AC3"),
    ]
    assert [a.is_commentary for a in attrs.values()] == [False, False, True]
