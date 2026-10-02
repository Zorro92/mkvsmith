"""Tests for season/disc-aware episode titles on series discs."""

from __future__ import annotations

from pathlib import Path

import pytest

from episode_naming import episode_title, parse_series_info, play_all_title
from models import (
    Config,
    PackedSegment,
    RuntimeState,
    SeriesInfo,
    Stream,
    StreamType,
    Title,
)
from packed_episodes import split_packed_title


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (
            ["THE BIG BANG THEORY SEASON 1 DISC 2", "THE_BIG_BANG_THEORY_S1_D2"],
            SeriesInfo("THE BIG BANG THEORY", 1, 2),
        ),
        # Season only in the release folder; disc only on the disc itself.
        (
            [
                "EARTH FROM SPACE D1",
                "EARTH_FROM_SPACE_D1",
                "Earth.from.Space.S01.1080i",
            ],
            SeriesInfo("EARTH FROM SPACE", 1, 1),
        ),
        (["Sgt. Frog - Season 3", "SGT_FROG_S3"], SeriesInfo("Sgt. Frog", 3, None)),
        (["Show.S01D02.1080p"], SeriesInfo("Show", 1, 2)),
        (["Some Show Disk 3"], SeriesInfo("Some Show", None, 3)),
        (["Show S02", "test_disc_0"], SeriesInfo("Show", 2, None)),  # no disc 0
        # Movies and look-alikes: DVD9 is a format, years and sequels aren't
        # seasons.
        (["Treasure Planet", "Treasure.Planet.2002.NTSC.USA.DVD9-AndreMor"], None),
        (["Spider-Man 3", "Spider-Man.3.2007.1080p.BluRay.x264"], None),
        (["Monster High - Welcome to Monster High"], None),
        ([None, ""], None),
    ],
)
def test_parse_series_info(
    names: list[str | None], expected: SeriesInfo | None
) -> None:
    assert parse_series_info(names) == expected


@pytest.mark.parametrize(
    ("info", "episode", "play_all"),
    [
        (
            SeriesInfo("Show", 1, 2),
            "Show - S01D02 - Episode 3",
            "Show - S01D02 - Play All",
        ),
        (
            SeriesInfo("Show", 3, None),
            "Show - S03 - Episode 3",
            "Show - S03 - Play All",
        ),
        (
            SeriesInfo("Show", None, 4),
            "Show - D04 - Episode 3",
            "Show - D04 - Play All",
        ),
        (None, "Disc Name - Episode 3", "Disc Name - Play All"),
        (SeriesInfo("", 1, 1), "Disc Name - Episode 3", "Disc Name - Play All"),
    ],
)
def test_titles(info: SeriesInfo | None, episode: str, play_all: str) -> None:
    assert episode_title(info, "Disc Name", 3) == episode
    assert play_all_title(info, "Disc Name") == play_all


def test_part_suffix_is_kept() -> None:
    assert episode_title(SeriesInfo("Show", 1, 1), "x", 4, "b") == (
        "Show - S01D01 - Episode 4b"
    )


def _title(index: int, *, episode: int | None = None, play_all: bool = False) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"{index}.m2ts"),
        name="x",
        duration_seconds=1300.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    title.episode_number = episode
    title.play_all = play_all
    return title


def test_scanner_names_episodes_from_disc_and_folder(tmp_path: Path) -> None:
    from scan import Scanner

    source = tmp_path / "Earth.from.Space.S01.1080i" / "EARTH_FROM_SPACE_D1"
    source.mkdir(parents=True)
    state = RuntimeState()
    scanner = Scanner(source, runtime_state=state)
    scanner.disc_name = "EARTH FROM SPACE D1"
    scanner.titles = [
        _title(0, episode=1),
        _title(1, episode=2),
        _title(2, play_all=True),
    ]

    scanner._apply_disc_name()

    assert [t.name for t in scanner.titles] == [
        "EARTH FROM SPACE - S01D01 - Episode 1",
        "EARTH FROM SPACE - S01D01 - Episode 2",
        "EARTH FROM SPACE - S01D01 - Play All",
    ]
    assert scanner.disc_metadata.series_info == SeriesInfo("EARTH FROM SPACE", 1, 1)


def test_scanner_keeps_plain_episode_names_without_season_or_disc(
    tmp_path: Path,
) -> None:
    from scan import Scanner

    source = tmp_path / "Peanuts Collection"
    source.mkdir()
    scanner = Scanner(source, runtime_state=RuntimeState())
    scanner.disc_name = "Peanuts Collection"
    scanner.titles = [_title(0, episode=1)]

    scanner._apply_disc_name()

    assert scanner.titles[0].name == "Peanuts Collection - Episode 1"
    assert scanner.disc_metadata.series_info is None


def test_split_packed_episodes_use_series_info() -> None:
    parent = _title(0)
    parent.name = "Sgt. Frog Season 1 Disc 1"
    parent.clip_durations = [3000.0]
    parent.packed_segments = [
        PackedSegment(0.0, 1400.0, 1),
        PackedSegment(1400.0, 2800.0, 2),
        PackedSegment(2800.0, 3000.0, None),
    ]

    named = [t.name for t in split_packed_title(parent, SeriesInfo("Sgt. Frog", 1, 1))]
    plain = [t.name for t in split_packed_title(parent)]

    assert named == [
        "Sgt. Frog - S01D01 - Episode 1",
        "Sgt. Frog - S01D01 - Episode 2",
        "Sgt. Frog - S01D01 - Extra 1",
    ]
    assert plain[:2] == [
        "Sgt. Frog Season 1 Disc 1 - Episode 1",
        "Sgt. Frog Season 1 Disc 1 - Episode 2",
    ]


def test_episode_numbers_are_padded_to_the_highest() -> None:
    from episode_naming import episode_number_width

    assert [episode_number_width(n) for n in (0, 9, 10, 99, 101)] == [1, 1, 2, 2, 3]
    info = SeriesInfo("Show", 1, 1)
    assert episode_title(info, "x", 1, width=3) == "Show - S01D01 - Episode 001"
    assert episode_title(None, "Disc", 3, "b", width=2) == "Disc - Episode 03b"


def test_packed_split_pads_episode_numbers() -> None:
    parent = _title(0)
    parent.name = "Sgt. Frog - Season 3"
    parent.clip_durations = [12 * 1400.0]
    parent.packed_segments = [
        PackedSegment(n * 1400.0, (n + 1) * 1400.0, n + 1) for n in range(12)
    ]

    names = [t.name for t in split_packed_title(parent, SeriesInfo("Sgt. Frog", 3))]

    assert names[0] == "Sgt. Frog - S03 - Episode 01"
    assert names[-1] == "Sgt. Frog - S03 - Episode 12"


def test_scanner_pads_to_the_disc_highest_episode(tmp_path: Path) -> None:
    from scan import Scanner

    source = tmp_path / "Show.S01"
    source.mkdir()
    scanner = Scanner(source, runtime_state=RuntimeState())
    scanner.disc_name = "Show S01"
    scanner.titles = [_title(i, episode=n) for i, n in enumerate((1, 10, 101))]

    scanner._apply_disc_name()

    assert [t.name for t in scanner.titles] == [
        "Show - S01 - Episode 001",
        "Show - S01 - Episode 010",
        "Show - S01 - Episode 101",
    ]


def test_series_disc_titles_share_the_episode_base_name(tmp_path: Path) -> None:
    from scan import Scanner

    source = tmp_path / "SGT_FROG_S1_D1"
    source.mkdir()
    scanner = Scanner(source, runtime_state=RuntimeState())
    scanner.disc_name = "Sgt. Frog Season 1 Disc 1"
    packed = _title(0)
    packed.packed_segments = [PackedSegment(0.0, 1400.0, 1)]
    scanner.titles = [packed, _title(1)]

    scanner._apply_disc_name()

    # Not the disc's own "Season 1 Disc 1": the same base as the episodes.
    assert [t.name for t in scanner.titles] == [
        "Sgt. Frog - S01D01",
        "Sgt. Frog - S01D01",
    ]


def test_movie_disc_titles_keep_the_disc_name(tmp_path: Path) -> None:
    from scan import Scanner

    source = tmp_path / "LOTR_D1"
    source.mkdir()
    scanner = Scanner(source, runtime_state=RuntimeState())
    scanner.disc_name = "The Lord of the Rings Disc 1"
    scanner.titles = [_title(0), _title(1)]

    scanner._apply_disc_name()

    assert {t.name for t in scanner.titles} == {"The Lord of the Rings Disc 1"}


def test_title_list_hides_the_series_base(capsys: pytest.CaptureFixture[str]) -> None:
    import cli
    from models import DiscMetadata

    metadata = DiscMetadata(
        name="Sgt. Frog Season 1 Disc 1", series_info=SeriesInfo("Sgt. Frog", 1, 1)
    )
    episode = _title(0, episode=1)
    episode.name = "Sgt. Frog - S01D01 - Episode 01"
    other = _title(1)
    other.name = "Sgt. Frog - S01D01"

    base = cli._series_list_base([episode, other], metadata)

    assert base == "Sgt. Frog - S01D01"
    assert cli._title_list_name(episode, 40, base) == "Episode 01"
    assert cli._title_list_name(other, 40, base) == "Sgt. Frog - S01D01"
    # Movie discs (no episodes) keep their names as they are.
    assert cli._series_list_base([other], metadata) is None
    cli.display_titles([episode, other], metadata, Config(show_all=True))
    out = capsys.readouterr().out
    assert "SCANNED TITLES - Sgt. Frog - S01D01" in out
    assert "Episode 01" in out and "S01D01 - Episode" not in out
