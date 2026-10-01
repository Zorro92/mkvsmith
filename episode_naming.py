"""Names for episode titles on series discs.

A single disc can't tell which episodes earlier discs of the set held, so
real episode numbers are only known when TheDiscDB matches. Instead, the
season and disc numbers are read from the disc's own name and its folder
names, and episode titles say exactly what is known:

    THE BIG BANG THEORY - S01 Disc 2 - Episode 3   (season and disc)
    Sgt. Frog - S03 - Episode 5                     (season only)
    EARTH FROM SPACE - Disc 1 - Episode 2           (disc only)
    <disc name> - Episode N                         (neither)
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from models import SeriesInfo

# "S01", "S1", "Season 1" (optionally glued to a disc token: "S01D02").
_SEASON = re.compile(r"(?<![a-z0-9])s(?:eason)?\s*0*(\d{1,2})(?=d\d|[^a-z0-9]|$)")
# "D2", "Disc 2", "Disk 2". Not "DVD9" / "BD25" (formats, not disc numbers).
_DISC = re.compile(r"(?<![a-z])(?:dis[ck]|d)\s*0*(\d{1,2})(?![a-z0-9])")


def _normalise(name: str) -> str:
    """Lowercase, with ``.``/``_`` as spaces (one for one, so offsets match)."""
    return re.sub(r"[._]", " ", name).lower()


def _show_from(name: str, start: int) -> str:
    """The part of *name* before character *start*, tidied, in its own case.

    Dots and underscores only separate words in release-style names with no
    spaces (``The.Big.Bang.Theory``); otherwise they are punctuation to keep
    (``Sgt. Frog``).
    """
    head = name[:start]
    if " " not in name:
        head = re.sub(r"[._]", " ", head)
    return re.sub(r"\s+", " ", head).strip(" -,:;._")


def parse_series_info(names: Iterable[str | None]) -> SeriesInfo | None:
    """Show / season / disc from *names*, most authoritative first.

    Each of season and disc comes from the first name that carries it; the
    show name comes from the first name carrying either, cut at its first
    season/disc token. ``None`` when no name carries a season or a disc.
    """
    season: int | None = None
    disc: int | None = None
    show: str | None = None
    for name in names:
        if not name:
            continue
        text = _normalise(name)
        season_match = _SEASON.search(text)
        disc_match = _DISC.search(text)
        if season is None and season_match:
            season = int(season_match.group(1))
        if disc is None and disc_match:
            disc = int(disc_match.group(1))
        if show is None and (season_match or disc_match):
            first = min(m.start() for m in (season_match, disc_match) if m)
            show = _show_from(name, first) or None
    if season is None and disc is None:
        return None
    return SeriesInfo(show or "", season, disc)


def _prefix(info: SeriesInfo | None, fallback: str) -> str:
    if info is None or not info.show:
        return fallback
    parts = [info.show]
    if info.season is not None and info.disc is not None:
        parts.append(f"S{info.season:02d} Disc {info.disc}")
    elif info.season is not None:
        parts.append(f"S{info.season:02d}")
    elif info.disc is not None:
        parts.append(f"Disc {info.disc}")
    return " - ".join(parts)


def episode_title(
    info: SeriesInfo | None, fallback: str, number: int, part: str = ""
) -> str:
    """Title for episode *number* (*fallback* is the plain disc name)."""
    return f"{_prefix(info, fallback)} - Episode {number}{part}"


def play_all_title(info: SeriesInfo | None, fallback: str) -> str:
    return f"{_prefix(info, fallback)} - Play All"
