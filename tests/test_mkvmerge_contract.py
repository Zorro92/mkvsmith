"""What mkvsmith relies on mkvmerge doing, checked against the installed one.

Each test muxes a small synthetic stream with the real mkvmerge. A failure
after an MKVToolNix update means mkvmerge changed something mkvsmith
depends on or works around: see docs/formats/mkvmerge.md for what each
check guards and what to revisit. scripts/mkvmerge_versions.py runs them
with other releases.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest

from mkvsmith import mkv
from mkvsmith.mkvtimes import is_vc1_track, read_track_times
from mkvsmith.vc1time import correct_vc1_frame_times, retime_vc1_join_frames
from tests import synth_vc1

pytestmark = pytest.mark.skipif(
    shutil.which("mkvmerge") is None, reason="mkvmerge not installed"
)

_FRAME_MS = synth_vc1.FRAME / 90


def _mkvmerge(*args: str | Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["mkvmerge", *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stdout
    return result


def _identify(path: Path) -> dict[str, Any]:
    info = json.loads(_mkvmerge("-J", path).stdout)
    assert isinstance(info, dict)
    return cast(dict[str, Any], info)


def _block_times(path: Path) -> list[int]:
    track = read_track_times(path, is_vc1_track)
    assert track is not None, "no VC-1 track in VfW mode (V_MS/VFW/FOURCC, WVC1)"
    return [block.time for block in track.blocks]


def _clips(tmp_path: Path, frames: int) -> list[Path]:
    first, second = tmp_path / "00001.m2ts", tmp_path / "00002.m2ts"
    first.write_bytes(synth_vc1.m2ts_clip(frames))
    # The next clip starts on its own clock, as a seamless clip may.
    second.write_bytes(synth_vc1.m2ts_clip(frames, start=900_000))
    return [first, second]


# --- identification and progress ---------------------------------------------


def test_identification_has_the_fields_mkvsmith_reads(tmp_path: Path) -> None:
    (clip, _) = _clips(tmp_path, 19)
    info = _identify(clip)
    container = info["container"]
    assert isinstance(container, dict) and container["recognized"]
    tracks = info["tracks"]
    assert isinstance(tracks, list) and len(tracks) == 1
    track = tracks[0]
    assert track["id"] == 0 and track["type"] == "video" and track["codec"] == "VC-1"
    assert track["properties"]["pixel_dimensions"] == "1920x1080"

    out = tmp_path / "out.mkv"
    _mkvmerge("-o", out, clip)
    container = _identify(out)["container"]
    assert isinstance(container, dict)
    assert int(container["properties"]["duration"]) > 0


def test_progress_lines_are_read_while_muxing(tmp_path: Path) -> None:
    (clip, _) = _clips(tmp_path, 19)
    process = subprocess.Popen(
        ["mkvmerge", "--ui-language", "en_US", "-o", str(tmp_path / "out.mkv")]
        + [str(clip)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    seen: list[int] = []
    mkv._read_mkvmerge_output(process.stdout, seen.append)
    assert process.wait() == 0
    assert seen and seen[-1] == 100


def test_exit_codes_and_warning_lines(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    synth_vc1.write_evo(evo, gops=2)
    out = tmp_path / "a.mkv"
    # An option for a track the file doesn't have: a warning, not an error.
    # In English, as mkvsmith runs the mux; the test's own path holds
    # "warning", which only a line's start may match.
    warned = subprocess.run(
        ["mkvmerge", "--ui-language", "en_US", "-o", str(out)]
        + ["--track-name", "5:x", str(evo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert warned.returncode == 1 and out.exists()
    warnings = mkv._mkvmerge_warnings(warned.stdout)
    assert len(warnings) == 1 and "ID 5" in warnings[0]

    failed = subprocess.run(
        ["mkvmerge", "-o", str(tmp_path / "b.mkv"), str(tmp_path / "missing.evo")],
        capture_output=True,
        check=False,
    )
    assert failed.returncode == 2


# --- Track tags (mkv._apply_source_id_statistics_fix) ---------------------------


def _source_id_tag(path: Path) -> list[ET.Element]:
    """The children of the tag holding SOURCE_ID, in file order."""
    extract = subprocess.run(
        ["mkvextract", str(path), "tags"], capture_output=True, check=True
    )
    for tag in ET.fromstring(extract.stdout).findall("Tag"):
        if any(s.findtext("Name") == "SOURCE_ID" for s in tag.findall("Simple")):
            return list(tag)
    raise AssertionError("no tag holds SOURCE_ID")


def _statistics_listing(children: list[ET.Element]) -> Sequence[str]:
    for child in children:
        if child.tag == "Simple" and child.findtext("Name") == "_STATISTICS_TAGS":
            return (child.findtext("String") or "").split()
    raise AssertionError("no _STATISTICS_TAGS")


def test_source_id_tags_are_fixed_after_the_mux(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    synth_vc1.write_evo(evo, gops=2)
    tags = mkv._create_source_id_tags_file("0100FD", [], [])
    out = tmp_path / "a.mkv"
    try:
        _mkvmerge("-o", out, "--tags", f"0:{tags}", evo)
    finally:
        tags.unlink()
    muxed = _source_id_tag(out)
    # A failure here means mkvmerge now writes these tags the way players
    # need them, and the post-mux rewrite may no longer be needed.
    assert muxed[0].tag == "Simple", "the UID now comes before the Simples"
    assert "SOURCE_ID" not in _statistics_listing(muxed)

    assert mkv._apply_source_id_statistics_fix(out, [])
    fixed = _source_id_tag(out)
    assert fixed[0].tag == "Targets"
    assert fixed[0].findtext("TrackUID") == muxed_uid(muxed)
    assert "SOURCE_ID" in _statistics_listing(fixed)


def muxed_uid(children: list[ET.Element]) -> str | None:
    targets = next(child for child in children if child.tag == "Targets")
    return targets.findtext("TrackUID")


# --- HD DVD: VC-1 in an EVO (vc1time.correct_vc1_frame_times) -----------------


def test_evo_vc1_is_one_block_per_frame_from_the_first_pts(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    expected = synth_vc1.write_evo(evo, gops=6)
    out = tmp_path / "a.mkv"
    _mkvmerge("-o", out, evo)
    times = _block_times(out)
    # The rewrite needs a block per frame, and anchors the EVO on its first.
    assert len(times) == len(expected)
    assert times[0] == 0


def test_mkvmerge_still_mistimes_evo_vc1(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    right = [round(tick / 90) for tick in synth_vc1.write_evo(evo, gops=6)]
    out = tmp_path / "a.mkv"
    _mkvmerge("-o", out, evo)
    # A failure here is good news: mkvmerge now times EVO VC-1 itself, and
    # vc1time's rewrite may no longer be needed.
    assert _block_times(out) != right[:1] + right[:-1]


def test_evo_vc1_times_are_rewritten_on_the_cadence(tmp_path: Path) -> None:
    evo = tmp_path / "a.evo"
    right = [round(tick / 90) for tick in synth_vc1.write_evo(evo, gops=6)]
    out = tmp_path / "a.mkv"
    _mkvmerge("-o", out, evo)
    assert correct_vc1_frame_times(out, [evo]) is not None
    # Each block carries the time of the frame shown before it.
    assert _block_times(out) == pytest.approx(right[:1] + right[:-1], abs=1)


# --- Blu-ray: VC-1 across appended clips (mkv._append_input_files) ------------


def test_bluray_vc1_blocks_carry_the_dts(tmp_path: Path) -> None:
    (clip, _) = _clips(tmp_path, 19)
    out = tmp_path / "a.mkv"
    _mkvmerge("-o", out, clip)
    # VfW mode: each block has the frame before's time; the first repeats.
    shown = [round(k * _FRAME_MS) for k in range(19)]
    assert _block_times(out) == pytest.approx(shown[:1] + shown[:-1], abs=1)


def test_track_append_puts_the_next_vc1_clip_a_frame_early(tmp_path: Path) -> None:
    clips = _clips(tmp_path, 19)
    out = tmp_path / "a.mkv"
    _mkvmerge("-o", out, "--append-mode", "track", clips[0], "+", clips[1])
    times = _block_times(out)
    # A failure means mkvmerge now appends VC-1 where the clip ends, and
    # the one-frame --sync would put each appended clip a frame late.
    assert times[19] == pytest.approx(18 * _FRAME_MS, abs=1)
    assert times[19] == times[20]


def test_synced_and_retimed_vc1_clips_join_on_time(tmp_path: Path) -> None:
    clips = _clips(tmp_path, 19)
    out = tmp_path / "a.mkv"
    cmd = ["mkvmerge", "-o", str(out)]
    mkv._append_input_files(
        cmd, clips, [], track_mode=True, vc1_sync=(0, 1000 * 1001 / 30000)
    )
    _mkvmerge(*cmd[1:])
    assert retime_vc1_join_frames(out) == 1
    shown = [round(k * _FRAME_MS) for k in range(38)]
    assert _block_times(out) == pytest.approx(shown[:1] + shown[:-1], abs=1)
