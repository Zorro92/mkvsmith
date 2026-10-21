"""Debugging helper: compare mkvsmith's closed captions with CCExtractor's.

Run from the project root, with ``ccextractor`` on PATH (a testing tool, never
a runtime dependency; see AGENTS.md):

    uv run python scripts/compare_cc608_ccextractor.py VTS_01_1.VOB [more VOBs]

Decodes the VOBs with both, pairs the cues by text in order, and prints how
far apart their start times are, in frames. CCExtractor puts every caption
of a DVD GOP at one time; mkvsmith times each by its frame, so expect them a
GOP or less apart (-3 to +13 frames on retail discs, since CCExtractor's GOP
time isn't always the first displayed frame), with the text matching cue
for cue (docs/formats/closed-captions.md).
"""

from __future__ import annotations

import collections
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from mkvsmith.cc608 import extract_cc608_captions, write_cc608_srt

_FRAME = 1001 / 30000
_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+),(\d+) --> ")
_MIN_PTS = re.compile(r"Min PTS:\s+(\d+):(\d+):(\d+):(\d+)")


def _seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds, millis = (int(group) for group in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _cues(path: Path) -> list[tuple[float, str]]:
    """(start, text) per cue; text normalised the way the two tools differ
    (CCExtractor pads lines and keeps straight apostrophes)."""
    cues: list[tuple[float, str]] = []
    text = path.read_text(errors="replace").replace("\r", "")
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        match = _SRT_TIME.match(lines[1]) if len(lines) >= 3 else None
        if match is None:
            continue
        caption = " ".join(re.sub(r"<[^>]+>", "", line) for line in lines[2:])
        caption = re.sub(r"\s+", " ", caption.replace("’", "'")).strip()
        cues.append((_seconds(match), caption.upper()))
    return cues


def main(vobs: list[Path]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ours = _cues(write_cc608_srt(extract_cc608_captions(vobs), Path(tmp) / "a.srt"))
        theirs_path = Path(tmp) / "b.srt"
        log = subprocess.run(
            ["ccextractor", *map(str, vobs), "-o", str(theirs_path)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        # CCExtractor counts from the first PTS; mkvsmith keeps the PTS.
        min_pts = _MIN_PTS.search(log)
        base = _seconds(min_pts) if min_pts else 0.0
        theirs = [(start + base, caption) for start, caption in _cues(theirs_path)]

    diffs: list[int] = []
    next_index = 0
    for start, caption in ours:
        for index in range(next_index, min(next_index + 5, len(theirs))):
            if theirs[index][1] == caption:
                diffs.append(round((start - theirs[index][0]) / _FRAME))
                next_index = index + 1
                break
    print(
        f"cues: mkvsmith {len(ours)}, CCExtractor {len(theirs)}, matched {len(diffs)}"
    )
    print("start difference in frames:", sorted(collections.Counter(diffs).items()))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main([Path(arg) for arg in sys.argv[1:]])
