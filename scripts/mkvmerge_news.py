"""Maintenance helper: list mkvmerge changes since the last review.

Run from the project root:

    uv run python scripts/mkvmerge_news.py [--git] [--news NEWS.md] [--since vN.M] [--all]

Reads the version docs/formats/mkvmerge.md was last reviewed against (its
"Changelog reviewed through: vN.M" line), fetches MKVToolNix's NEWS.md
from mkvtoolnix.download (with --git, from the main branch on Codeberg,
which also has the changes merged for the next release; or a local copy
with --news), and prints every mkvmerge entry of each newer release. Entries that touch something mkvsmith depends on or works
around are marked with *, the rest are listed for completeness; --all also
shows the entries for the other MKVToolNix programs. Afterwards, check the
marked entries against docs/formats/mkvmerge.md, run
tests/test_mkvmerge_contract.py with the new mkvmerge, and move the
"reviewed through" line on.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

NEWS_URL = "https://mkvtoolnix.download/doc/NEWS.md"
GIT_NEWS_URL = "https://codeberg.org/mbunkus/mkvtoolnix/raw/branch/main/NEWS.md"
# The main branch heads its unreleased changes "# Version ?".
_UNRELEASED = (1 << 30, 0)
_DOC = Path(__file__).resolve().parent.parent / "docs" / "formats" / "mkvmerge.md"
_REVIEWED = re.compile(r"Changelog reviewed through: v(\d+)\.(\d+)")
_RELEASE = re.compile(r"^# Version (?:(\d+)\.(\d+)(?:\.\d+)?\b|\?)(.*)$")
# What mkvsmith feeds mkvmerge, reads back from it, or works around
# (docs/formats/mkvmerge.md).
_WATCHED = re.compile(
    r"MPEG TS|transport stream|M2TS|MPLS|MPEG PS|program stream|EVO|VOB"
    r"|VC-1|VC1|AVC|H\.264|HEVC|H\.265|MPEG-1|MPEG-2|Dolby Vision"
    r"|TrueHD|MLP|DTS|AC-3|AC3|E-AC-3|LPCM|PCM"
    r"|PGS|HDMV|VobSub|subtitle|chapter|tags?\b|statistics|language|BCP 47"
    r"|append|--split|--sync|timestamp|interlace|field order|stereo mode"
    r"|identification|JSON|progress|exit code|warning|track flag|flag",
    re.IGNORECASE,
)


def _reviewed_version() -> tuple[int, int]:
    match = _REVIEWED.search(_DOC.read_text(encoding="utf-8"))
    if match is None:
        sys.exit(f"no 'Changelog reviewed through: vN.M' line in {_DOC}")
    return int(match.group(1)), int(match.group(2))


def _installed_version() -> str:
    if shutil.which("mkvmerge") is None:
        return "not installed"
    result = subprocess.run(
        ["mkvmerge", "--version"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def _news_text(path: Path | None, git: bool) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    with urllib.request.urlopen(
        GIT_NEWS_URL if git else NEWS_URL, timeout=30
    ) as response:
        data = response.read()
    assert isinstance(data, bytes)
    return data.decode("utf-8")


def _entries(lines: list[str]) -> list[str]:
    """A release's bullets, each joined onto one line."""
    entries: list[str] = []
    for line in lines:
        if line.startswith("* "):
            entries.append(line[2:].strip())
        elif line.startswith("  ") and entries:
            entries[-1] += " " + line.strip()
    return entries


def _releases(text: str) -> list[tuple[tuple[int, int], str, list[str]]]:
    releases: list[tuple[tuple[int, int], str, list[str]]] = []
    for line in text.splitlines():
        match = _RELEASE.match(line)
        if match:
            if match.group(1) is None:
                version, name = _UNRELEASED, "(unreleased, main branch)"
            else:
                version = (int(match.group(1)), int(match.group(2)))
                name = match.group(3).strip()
            releases.append((version, name, []))
        elif releases:
            releases[-1][2].append(line)
    return releases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--git",
        action="store_true",
        help="read NEWS.md from the main branch on Codeberg",
    )
    parser.add_argument("--news", type=Path, help="a local copy of NEWS.md")
    parser.add_argument(
        "--since", help="list releases after this one (vN.M) instead of the doc's"
    )
    parser.add_argument(
        "--all", action="store_true", help="also list the other programs' entries"
    )
    args = parser.parse_args()

    if args.since:
        match = re.fullmatch(r"v?(\d+)\.(\d+)", args.since)
        if match is None:
            sys.exit("--since takes a version such as v96.0")
        reviewed = int(match.group(1)), int(match.group(2))
    else:
        reviewed = _reviewed_version()
    print(f"Reviewed through: v{reviewed[0]}.{reviewed[1]}")
    print(f"Installed: {_installed_version()}")
    newer = [r for r in _releases(_news_text(args.news, args.git)) if r[0] > reviewed]
    if not newer:
        print("No newer releases.")
        return
    watched = 0
    for (major, minor), name, lines in sorted(newer):
        label = "next release" if (major, minor) == _UNRELEASED else f"v{major}.{minor}"
        print(f"\n{label} {name}")
        for entry in _entries(lines):
            ours = entry.startswith(("mkvmerge", "all"))
            if not ours and not args.all:
                continue
            mark = "*" if ours and _WATCHED.search(entry) else " "
            watched += mark == "*"
            print(f"  {mark} {entry}")
    print(
        f"\n{watched} marked entr{'y' if watched == 1 else 'ies'} in "
        f"{len(newer)} release(s) to check against {_DOC.name}."
    )


if __name__ == "__main__":
    main()
