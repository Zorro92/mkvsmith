"""Multi-edition (seamless branching) title building and chapter XML tests.

Covers the xin1generator-ported atom computation (boundary-aligned chapters,
hidden continuation atoms), the combined-title builder (clip union order,
per-edition specs), and the Matroska chapters/tags XML writers. When
``mkvmerge`` is installed, the writers are additionally round-tripped through
a real mux so regressions in the "magic chapter file" format are caught.
"""

# The functions under test are private (underscore-prefixed) internal helpers;
# accessing them from tests is intentional.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mkv import _write_multi_edition_chapters_xml, _write_tags_xml_mkvmerge
from models import EditionAtom, EditionSpec, Stream, StreamType, Title
from scan import _detect_edition_groups, _edition_atoms, build_multi_edition_title


def _child(elem: ET.Element, tag: str) -> ET.Element:
    """ElementTree.find() narrowed for tests (fails the test if missing)."""
    child = elem.find(tag)
    assert child is not None, f"missing <{tag}> under <{elem.tag}>"
    return child


def _mk_title(
    idx: int,
    clips: list[str],
    durations: list[float],
    chapters: list[float],
    name: str = "T",
    playlist: str | None = None,
) -> Title:
    """Synthetic Blu-ray playlist title over fake clip paths."""
    t = Title(idx, Path(f"/disc/{clips[0]}.m2ts"), name, sum(durations))
    t.append_clips = [Path(f"/disc/{c}.m2ts") for c in clips[1:]]
    t.chapters = chapters
    t.clip_durations = durations
    t.clip_sizes = [1000] * len(clips)
    t.playlist_name = playlist
    t.streams = [
        Stream(0, StreamType.VIDEO, "h264", "und"),
        Stream(1, StreamType.AUDIO, "truehd", "eng"),
    ]
    return t


# --- _edition_atoms --------------------------------------------------------


def test_atoms_single_clip_two_chapters() -> None:
    # One clip [0, 10), chapters at 0 and 4 -> two visible atoms.
    atoms = _edition_atoms([0], [0.0], [10.0], [0.0, 4.0])
    assert len(atoms) == 2
    assert (atoms[0].start, atoms[0].end, atoms[0].hidden) == (0.0, 4.0, False)
    assert (atoms[1].start, atoms[1].end, atoms[1].hidden) == (4.0, 10.0, False)


def test_atoms_hidden_continuation_across_boundary() -> None:
    # Two clips [0,6) and [6,12); chapter at 10 (inside clip 2 only).
    # Clip 1 contributes one visible atom [0,6) (chapter 0 start);
    # clip 2 is split: hidden continuation [6,10) + visible [10,12).
    atoms = _edition_atoms([0, 1], [0.0, 6.0], [6.0, 6.0], [0.0, 10.0])
    assert len(atoms) == 3
    assert (atoms[0].start, atoms[0].end, atoms[0].hidden) == (0.0, 6.0, False)
    assert (atoms[1].start, atoms[1].end, atoms[1].hidden) == (6.0, 10.0, True)
    assert (atoms[2].start, atoms[2].end, atoms[2].hidden) == (10.0, 12.0, False)


def test_atoms_chapter_exactly_on_boundary() -> None:
    # Chapters at 0 and 6 where 6 == boundary: clip 1's final atom is the
    # visible chapter atom [0,6); clip 2's atom [6,12) is visible (chapter
    # sits exactly at its start). No hidden atoms.
    atoms = _edition_atoms([0, 1], [0.0, 6.0], [6.0, 6.0], [0.0, 6.0])
    assert [(a.start, a.end, a.hidden) for a in atoms] == [
        (0.0, 6.0, False),
        (6.0, 12.0, False),
    ]


def test_atoms_nonlinear_global_order() -> None:
    # Combined timeline A=[0,5) B=[5,10) C=[10,15); an edition playing
    # A -> C must skip B entirely: [0,5) then [10,15).
    atoms = _edition_atoms([0, 2], [0.0, 5.0, 10.0], [5.0, 5.0, 5.0], [0.0])
    assert [(a.start, a.end) for a in atoms] == [(0.0, 5.0), (10.0, 15.0)]


def test_atoms_no_chapters_one_atom_per_clip() -> None:
    atoms = _edition_atoms([1, 0], [0.0, 5.0], [5.0, 5.0], [])
    assert [(a.start, a.end) for a in atoms] == [(5.0, 10.0), (0.0, 5.0)]


# --- build_multi_edition_title ----------------------------------------------


def test_build_combines_clip_union_in_first_appearance_order() -> None:
    t1 = _mk_title(0, ["A", "B", "C"], [5.0, 5.0, 5.0], [0.0], playlist="00800")
    t2 = _mk_title(
        1, ["A", "X", "B", "Y"], [5.0, 2.0, 5.0, 2.0], [0.0], playlist="00801"
    )
    combined = build_multi_edition_title([t1, t2])
    keys = [Path(combined.source_file).stem] + [
        Path(p).stem for p in combined.append_clips
    ]
    # Union: A,B,C from t1 then novel X,Y from t2.
    assert keys == ["A", "B", "C", "X", "Y"]
    assert combined.clip_durations == [5.0, 5.0, 5.0, 2.0, 2.0]
    assert combined.duration_seconds == 19.0
    assert combined.estimated_size_bytes == 5000
    assert [e.name for e in combined.editions] == ["T", "Playlist 00801"]
    assert combined.editions[0].is_default and not combined.editions[1].is_default
    assert [e.uid for e in combined.editions] == [1, 2]
    # Edition 1 = A,B,C = 15s; edition 2 = A,X,B,Y = 14s.
    assert combined.editions[0].duration == pytest.approx(15.0)
    assert combined.editions[1].duration == pytest.approx(14.0)
    # Streams are copies, not shared mutable objects.
    assert combined.streams[0] is not t1.streams[0]


def test_build_edition_atoms_reference_global_timeline() -> None:
    t1 = _mk_title(0, ["A", "B"], [10.0, 10.0], [0.0, 5.0], playlist="00800")
    t2 = _mk_title(1, ["C", "B"], [4.0, 10.0], [0.0, 2.0], playlist="00801")
    combined = build_multi_edition_title([t1, t2])
    # Union: A[0,10) B[10,20) C[20,24).
    e1, e2 = combined.editions
    # e1: chapters 0 and 5 -> [0,5) [5,10) | [10,20)
    assert [(a.start, a.end, a.hidden) for a in e1.atoms] == [
        (0.0, 5.0, False),
        (5.0, 10.0, False),
        (10.0, 20.0, True),
    ]
    # e2 plays C then B: [20,22) is the edition's opening (chapter 0 starts
    # here, so visible), [22,24) is chapter 2, then B [10,20) is a hidden
    # continuation of chapter 2.
    assert [(a.start, a.end, a.hidden) for a in e2.atoms] == [
        (20.0, 22.0, False),
        (22.0, 24.0, False),
        (10.0, 20.0, True),
    ]
    # Visible atoms get sequential names.
    vis = [a for a in e1.atoms if not a.hidden]
    assert [a.name for a in vis] == ["Chapter 01", "Chapter 02"]


def test_build_rejects_mismatched_inputs() -> None:
    t1 = _mk_title(0, ["A"], [5.0], [0.0], playlist="00800")
    t2 = _mk_title(1, ["B"], [5.0], [0.0], playlist="00801")
    t2.streams = [Stream(0, StreamType.VIDEO, "mpeg2video", "und")]
    with pytest.raises(ValueError, match="at least two"):
        build_multi_edition_title([t1])
    with pytest.raises(ValueError, match="different stream layout"):
        build_multi_edition_title([t1, t2])
    t3 = _mk_title(2, ["C"], [5.0], [0.0])  # no playlist_name
    with pytest.raises(ValueError, match="playlist"):
        build_multi_edition_title([t1, t3])


def test_build_iso_source_union() -> None:
    t1 = _mk_title(0, ["A", "B"], [5.0, 5.0], [0.0], playlist="00800")
    t2 = _mk_title(1, ["B", "C"], [5.0, 5.0], [0.0], playlist="00801")
    for t in (t1, t2):
        t.source_file = Path("/disc/disc.iso")
        t.iso_internal_paths = [
            f"BDMV/STREAM/{Path(p).stem}.m2ts"
            for p in ([t.source_file] if False else [])
        ]
    # Redo internal paths from clip keys.
    t1.iso_internal_paths = ["BDMV/STREAM/A.m2ts", "BDMV/STREAM/B.m2ts"]
    t2.iso_internal_paths = ["BDMV/STREAM/B.m2ts", "BDMV/STREAM/C.m2ts"]
    combined = build_multi_edition_title([t1, t2])
    assert combined.iso_internal_paths == [
        "BDMV/STREAM/A.m2ts",
        "BDMV/STREAM/B.m2ts",
        "BDMV/STREAM/C.m2ts",
    ]
    assert combined.source_file == Path("/disc/disc.iso")
    assert not combined.append_clips


def test_detect_edition_groups() -> None:
    main = _mk_title(0, ["A", "B", "C", "D"], [30.0] * 4, [0.0], playlist="00800")
    alt = _mk_title(1, ["A", "X", "C", "D"], [30.0] * 4, [0.0], playlist="00801")
    alt2 = _mk_title(2, ["A", "B", "C", "Y"], [30.0] * 4, [0.0], playlist="00802")
    extra = _mk_title(3, ["Z", "Z2", "Z3"], [30.0] * 3, [0.0], playlist="00900")
    groups = _detect_edition_groups([main, alt, alt2, extra])
    assert len(groups) == 1
    assert {t.playlist_name for t in groups[0]} == {"00800", "00801", "00802"}


def test_detect_edition_groups_rejects_duration_outliers() -> None:
    main = _mk_title(0, ["A", "B", "C"], [60.0] * 3, [0.0], playlist="00800")
    short = _mk_title(1, ["A", "B", "C"], [10.0] * 3, [0.0], playlist="00801")
    assert _detect_edition_groups([main, short]) == []


# --- XML writers ------------------------------------------------------------


def _spec() -> EditionSpec:
    return EditionSpec(
        uid=1,
        name="Theatrical",
        is_default=True,
        atoms=[
            EditionAtom(0.0, 4.0, False, "Chapter 01"),
            EditionAtom(4.0, 6.0, True),
            EditionAtom(6.0, 10.0, False, "Chapter 02"),
        ],
    )


def test_chapters_xml_structure(tmp_path: Path) -> None:
    out = tmp_path / "chapters.xml"
    _write_multi_edition_chapters_xml([_spec()], out)
    root = ET.parse(out).getroot()
    assert root.tag == "Chapters"
    (edition,) = root.findall("EditionEntry")
    assert edition.findtext("EditionUID") == "1"
    assert edition.findtext("EditionFlagDefault") == "1"
    assert edition.findtext("EditionFlagOrdered") == "1"
    atoms = edition.findall("ChapterAtom")
    assert len(atoms) == 3
    assert atoms[0].findtext("ChapterTimeStart") == "00:00:00.000000000"
    assert atoms[0].findtext("ChapterTimeEnd") == "00:00:04.000000000"
    assert atoms[0].findtext("ChapterFlagHidden") == "0"
    assert _child(atoms[0], "ChapterDisplay").findtext("ChapterString") == "Chapter 01"
    # Hidden atoms carry no ChapterDisplay.
    assert atoms[1].findtext("ChapterFlagHidden") == "1"
    assert atoms[1].find("ChapterDisplay") is None
    assert atoms[2].findtext("ChapterTimeStart") == "00:00:06.000000000"


def test_tags_xml_edition_titles(tmp_path: Path) -> None:
    out = tmp_path / "tags.xml"
    specs = [
        EditionSpec(1, "Theatrical", True),
        EditionSpec(2, "Extended", False),
    ]
    _write_tags_xml_mkvmerge(out, md=None, editions=specs)
    root = ET.parse(out).getroot()
    tags = root.findall("Tag")
    assert len(tags) == 2
    assert _child(tags[0], "Targets").findtext("EditionUID") == "1"
    assert _child(tags[0], "Simple").findtext("Name") == "TITLE"
    assert _child(tags[0], "Simple").findtext("String") == "Theatrical"
    assert _child(tags[1], "Targets").findtext("EditionUID") == "2"
    assert _child(tags[1], "Simple").findtext("String") == "Extended"


# --- mkvmerge round-trip -----------------------------------------------------

HAS_MKVMERGE = shutil.which("mkvmerge") is not None


@pytest.mark.skipif(not HAS_MKVMERGE, reason="mkvmerge not installed")
def test_multi_edition_xml_round_trips_through_mkvmerge(tmp_path: Path) -> None:
    """Mux a tiny appended input with the multi-edition XML and extract it back.

    Guards the "magic chapter file" contract against mkvmerge format changes:
    multiple ordered editions, hidden atoms, out-of-order ranges, and
    edition-targeted TITLE tags must all survive the mux.
    """
    import wave

    # Two 1-second PCM wavs as stand-in appended clips.
    wav_paths: list[Path] = []
    for i in range(2):
        wv = tmp_path / f"seg{i}.wav"
        with wave.open(str(wv), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 8000)
        wav_paths.append(wv)

    # Combined timeline: seg0=[0,1) seg1=[1,2).
    editions = [
        EditionSpec(
            uid=1,
            name="Forward",
            is_default=True,
            atoms=[
                EditionAtom(0.0, 1.0, False, "Chapter 01"),
                EditionAtom(1.0, 2.0, False, "Chapter 02"),
            ],
        ),
        EditionSpec(
            uid=2,
            name="Reverse",
            is_default=False,
            atoms=[
                EditionAtom(1.0, 2.0, True),
                EditionAtom(0.0, 1.0, False, "Chapter 01"),
            ],
        ),
    ]
    chapters_file = tmp_path / "chapters.xml"
    tags_file = tmp_path / "tags.xml"
    _write_multi_edition_chapters_xml(editions, chapters_file)
    _write_tags_xml_mkvmerge(tags_file, md=None, editions=editions)

    out = tmp_path / "out.mkv"
    res = subprocess.run(
        [
            "mkvmerge",
            "-o",
            str(out),
            "--chapters",
            str(chapters_file),
            "--global-tags",
            str(tags_file),
            str(wav_paths[0]),
            "+",
            str(wav_paths[1]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr

    # Extract chapters back out and validate the survived structure.
    chap = subprocess.run(
        ["mkvextract", str(out), "chapters"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert chap.returncode == 0, chap.stderr
    root = ET.fromstring(chap.stdout)
    eds = root.findall("EditionEntry")
    assert len(eds) == 2
    assert eds[0].findtext("EditionFlagOrdered") == "1"
    assert eds[0].findtext("EditionFlagDefault") == "1"
    assert eds[1].findtext("EditionFlagDefault") == "0"
    # Edition 2 plays out of order: first atom starts at 1s.
    e2_atoms = eds[1].findall("ChapterAtom")
    assert e2_atoms[0].findtext("ChapterTimeStart") == "00:00:01.000000000"
    assert e2_atoms[0].findtext("ChapterFlagHidden") == "1"
    assert e2_atoms[1].findtext("ChapterTimeStart") == "00:00:00.000000000"
    assert e2_atoms[1].findtext("ChapterFlagHidden") == "0"

    tags = subprocess.run(
        ["mkvextract", str(out), "tags"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert tags.returncode == 0, tags.stderr
    troot = ET.fromstring(tags.stdout)
    titles = {
        _child(t, "Targets").findtext("EditionUID"): _child(t, "Simple").findtext(
            "String"
        )
        for t in troot.findall("Tag")
        if _child(t, "Targets").findtext("EditionUID") is not None
    }
    assert titles == {"1": "Forward", "2": "Reverse"}
