"""Blu-ray series detection (one playlist per episode) and loop-proof
main-feature picking.

Modelled on Earth from Space (two ~58-minute episode playlists plus a
play-all per disc, beside an 8-hour looped menu playlist) and Face/Off (a
71-minute menu loop that used to out-chapter the 139-minute film).
"""

from __future__ import annotations

from pathlib import Path

from mkvsmith.models import Config, Stream, StreamType, Title
from mkvsmith.scan import _label_bluray_episodes, pick_main_feature

_CONFIG = Config(min_duration=60)


def _bd_title(
    index: int,
    playlist: str,
    clips: list[str],
    minutes: float,
    *,
    chapters: int = 7,
    iso: bool = False,
) -> Title:
    title = Title(
        index=index,
        source_file=Path("disc.iso") if iso else Path(f"STREAM/{clips[0]}.m2ts"),
        name=f"Playlist {playlist}",
        duration_seconds=minutes * 60,
    )
    title.playlist_name = playlist
    if iso:
        title.iso_internal_paths = [f"BDMV/STREAM/{clip}.m2ts" for clip in clips]
    else:
        title.append_clips = [Path(f"STREAM/{clip}.m2ts") for clip in clips[1:]]
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="h264"),
        Stream(index=1, stream_type=StreamType.AUDIO, codec="dts", language="eng"),
        Stream(index=2, stream_type=StreamType.SUBTITLE, codec="pgs", language="eng"),
    ]
    title.chapters = [float(n * 60) for n in range(chapters)]
    return title


def _labels(titles: list[Title]) -> dict[str, object]:
    return {
        t.playlist_name or "": "all" if t.play_all else t.episode_number for t in titles
    }


def test_two_episodes_with_a_play_all_are_a_series() -> None:
    titles = [
        _bd_title(0, "00017", ["00008"] * 100, 501, chapters=501),  # menu loop
        _bd_title(1, "00022", ["00000", "00001"], 115.5, chapters=13),
        _bd_title(2, "00018", ["00000"], 57.8),
        _bd_title(3, "00001", ["00001"], 57.8),
        _bd_title(4, "00000", ["00000"], 57.8, chapters=6),  # duplicate of 00018
    ]

    _label_bluray_episodes(titles, _CONFIG)

    # Numbered in play-all order (clip 00000 first), duplicates collapse.
    assert _labels(titles) == {
        "00017": None,
        "00022": "all",
        "00018": 1,
        "00001": 2,
        "00000": None,
    }


def test_two_same_length_titles_without_a_play_all_stay_movies() -> None:
    titles = [
        _bd_title(0, "00800", ["00001"], 92.0),
        _bd_title(1, "00801", ["00002"], 95.0),
    ]

    _label_bluray_episodes(titles, _CONFIG)

    assert _labels(titles) == {"00800": None, "00801": None}


def test_three_episodes_are_numbered_by_playlist() -> None:
    titles = [
        _bd_title(0, "00003", ["00012"], 44.0),
        _bd_title(1, "00001", ["00010"], 45.0),
        _bd_title(2, "00002", ["00011"], 43.5),
    ]

    _label_bluray_episodes(titles, _CONFIG)

    assert _labels(titles) == {"00001": 1, "00002": 2, "00003": 3}


def test_editions_sharing_clips_are_not_episodes() -> None:
    titles = [
        _bd_title(0, "00800", ["00010", "00011", "00013"], 120.0),
        _bd_title(1, "00801", ["00010", "00012", "00013"], 124.0),
        _bd_title(2, "00802", ["00010", "00014", "00013"], 118.0),
    ]

    _label_bluray_episodes(titles, _CONFIG)

    assert all(t.episode_number is None for t in titles)


def test_bonus_cluster_dwarfed_by_a_feature_is_not_a_series() -> None:
    titles = [
        _bd_title(0, "00800", ["00001"], 120.0, chapters=24),
        _bd_title(1, "00010", ["00020"], 12.0),
        _bd_title(2, "00011", ["00021"], 11.0),
        _bd_title(3, "00012", ["00022"], 12.5),
    ]

    _label_bluray_episodes(titles, _CONFIG)

    assert all(t.episode_number is None for t in titles)


def test_iso_titles_use_internal_paths() -> None:
    titles = [
        _bd_title(0, "00022", ["00000", "00001"], 115.5, iso=True),
        _bd_title(1, "00018", ["00000"], 57.8, iso=True),
        _bd_title(2, "00001", ["00001"], 57.8, iso=True),
    ]

    _label_bluray_episodes(titles, _CONFIG)

    assert _labels(titles) == {"00022": "all", "00018": 1, "00001": 2}


def test_looped_menu_playlist_never_wins_main_feature() -> None:
    # Face/Off: a 71-minute menu loop with 100 chapters beside the film.
    titles = [
        _bd_title(0, "00028", ["00002"] * 100, 71.6, chapters=100),
        _bd_title(1, "00003", ["00003"], 139.0, chapters=40),
    ]

    assert pick_main_feature(titles, _CONFIG) == 1


def test_episode_length_extra_with_another_track_layout_is_not_an_episode() -> None:
    # The Big Bang Theory S1 D2: a 17-minute featurette (1 audio / 4 subs)
    # beside 8 episodes (3 audio / 5 subs) falls inside the length window.
    titles = [
        _bd_title(n, f"0005{n + 1}", [f"0002{n}"], 20.0 + n / 4) for n in range(8)
    ]
    for title in titles:
        title.streams += [
            Stream(index=3, stream_type=StreamType.AUDIO, codec="ac3", language="fre"),
            Stream(index=4, stream_type=StreamType.AUDIO, codec="ac3", language="spa"),
        ]
    extra = _bd_title(8, "00201", ["00001"], 17.3)
    titles.append(extra)

    _label_bluray_episodes(titles, _CONFIG)

    assert [t.episode_number for t in titles[:8]] == list(range(1, 9))
    assert extra.episode_number is None


def _write_bdmt(meta: Path, name: str, filename: str = "bdmt_eng.xml") -> None:
    meta.mkdir(parents=True, exist_ok=True)
    (meta / filename).write_text(
        '<?xml version="1.0"?><disclib xmlns="urn:BDA:bdmv;disclib">'
        f'<di:discinfo xmlns:di="urn:BDA:bdmv;discinfo"><di:title><di:name>{name}'
        "</di:name></di:title></di:discinfo></disclib>"
    )


def test_placeholder_disc_names_are_ignored(tmp_path: Path) -> None:
    from mkvsmith.bluray import _parse_bdmv_disc_name

    bdmv = tmp_path / "BDMV"
    _write_bdmt(bdmv / "META" / "DL", "Blu-ray")
    assert _parse_bdmv_disc_name(bdmv) is None  # caller falls back to the folder
    _write_bdmt(
        bdmv / "META" / "DL", "THE BIG BANG THEORY SEASON 1 DISC 1", "bdmt_fra.xml"
    )
    assert _parse_bdmv_disc_name(bdmv) == "THE BIG BANG THEORY SEASON 1 DISC 1"
