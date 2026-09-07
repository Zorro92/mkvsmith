"""Tests for Scanner routing and scanned-title finalization."""

from __future__ import annotations

from pathlib import Path

import disc_reader
import scan
from models import Stream, StreamType, Title


def make_title(index: int, duration: float, **attributes) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"title-{index}.m2ts"),
        name=f"Original {index}",
        duration_seconds=duration,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    for name, value in attributes.items():
        setattr(title, name, value)
    return title


def test_sort_and_reindex_titles_prioritizes_episodes_and_demotes_play_all() -> None:
    episode_2 = make_title(0, 100.0, dvd_episode_number=2)
    episode_1 = make_title(1, 100.0, dvd_episode_number=1)
    play_all = make_title(2, 300.0, dvd_play_all=True)
    long_extra = make_title(3, 200.0)
    short_extra = make_title(4, 100.0)

    titles = [episode_2, play_all, short_extra, episode_1, long_extra]
    scan._sort_and_reindex_titles(titles)

    assert titles == [episode_1, episode_2, long_extra, short_extra, play_all]
    assert [title.index for title in titles] == [0, 1, 2, 3, 4]


def test_resolve_iso_source_selects_first_iso_in_sorted_order(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "isos"
    folder.mkdir()
    second = folder / "b.iso"
    first = folder / "a.iso"
    second.write_bytes(b"b")
    first.write_bytes(b"a")

    assert scan._resolve_iso_source(folder) == first
    assert scan._resolve_iso_source(second) == second

    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan._resolve_iso_source(empty) == empty


def test_scan_resolves_folder_iso_before_iso_scanner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "isos"
    folder.mkdir()
    selected = folder / "a.iso"
    other = folder / "z.iso"
    selected.write_bytes(b"a")
    other.write_bytes(b"z")
    scanner = scan.Scanner(folder)
    scanned_sources: list[Path] = []
    monkeypatch.setattr(
        disc_reader,
        "detect_source_type",
        lambda _source: disc_reader.SourceType.ISO_UNKNOWN,
    )
    monkeypatch.setattr(
        scanner, "_scan_iso", lambda: scanned_sources.append(scanner.source)
    )

    assert scanner.scan() == []
    assert scanned_sources == [selected]
    assert scanner.source == selected


def test_scan_routes_bluray_raw_and_applies_names_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "raw"
    scanner = scan.Scanner(source)
    titles = [
        make_title(0, 100.0),
        make_title(1, 200.0),
    ]
    routing_calls: list[Path] = []
    name_calls: list[scan.Scanner] = []

    monkeypatch.setattr(
        disc_reader,
        "detect_source_type",
        lambda _source: disc_reader.SourceType.BLURAY_RAW,
    )
    monkeypatch.setattr(
        scan,
        "_scan_bluray_raw_source",
        lambda scanned_source: (
            routing_calls.append(scanned_source) or (titles, "Injected Disc")
        ),
    )
    monkeypatch.setattr(scanner, "_apply_disc_name", lambda: name_calls.append(scanner))

    result = scanner.scan()

    assert result == titles
    assert routing_calls == [source]
    assert [title.index for title in titles] == [0, 1]
    assert scanner.disc_name == "Injected Disc"
    assert name_calls == [scanner]


def test_clean_release_name_cuts_scene_metadata() -> None:
    assert (
        scan._clean_release_name("Banjo.The.Woodpile.Cat.1979.USA.NTSC.DVD5")
        == "Banjo the Woodpile Cat 1979"
    )
    assert scan._clean_release_name("Movie.Name.1080p.BluRay") == "Movie Name"
    assert scan._clean_release_name("Movie.Name.1920x1080.Extras") == "Movie Name"


def test_clean_release_name_handles_episode_and_extension_tokens() -> None:
    assert scan._clean_release_name("Show.S01E02.Extras") == "Show S01E02"
    assert scan._clean_release_name("Some.[Movie]-(2024).mkv") == "Some Movie 2024"


def test_clean_release_name_returns_empty_for_metadata_only_name() -> None:
    assert scan._clean_release_name("1080p.NTSC.DVD5") == ""


def test_release_name_predicate_helpers() -> None:
    assert scan._is_release_year("2024")
    assert not scan._is_release_year("1899")
    assert scan._is_episode_tag("s01e02")
    assert not scan._is_episode_tag("episode")
    assert scan._is_release_metadata("blu-ray")
    assert scan._is_release_metadata("720p")
    assert not scan._is_release_metadata("movie")


def make_playlist_title(
    index: int,
    clips: list[str],
    duration: float,
    *,
    chapters: int = 0,
    stream_count: int = 1,
    audio_subtitle_count: int = 0,
) -> Title:
    title = Title(
        index=index,
        source_file=Path(f"/disc/{index}/{clips[0]}"),
        name=f"Playlist {index:05d}",
        duration_seconds=duration,
    )
    title.append_clips = [Path(f"/disc/{index}/{clip}") for clip in clips[1:]]
    title.chapters = [float(index) for index in range(chapters)]
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    title.streams.extend(
        Stream(
            index=stream_index + 1,
            stream_type=(
                StreamType.AUDIO if stream_index % 2 == 0 else StreamType.SUBTITLE
            ),
        )
        for stream_index in range(stream_count - 1 + audio_subtitle_count)
    )
    return title


def test_playlist_dedup_key_uses_clip_names_and_rounded_duration() -> None:
    folder_title = make_playlist_title(1, ["A.m2ts", "B.m2ts"], 100.49, chapters=2)
    iso_title = make_playlist_title(2, ["A.m2ts", "B.m2ts"], 99.51, chapters=1)
    iso_title.source_file = Path("/disc/movie.iso")
    iso_title.append_clips = []
    iso_title.iso_internal_paths = [
        "BDMV/STREAM/A.m2ts",
        "BDMV/STREAM/B.m2ts",
    ]

    expected_key = (("A.m2ts", "B.m2ts"), 100)
    assert scan._playlist_dedup_key(folder_title) == expected_key
    assert scan._playlist_dedup_key(iso_title) == expected_key


def test_group_duplicate_playlists_preserves_first_seen_order() -> None:
    unique = make_playlist_title(1, ["A.m2ts"], 100.0)
    first_duplicate = make_playlist_title(2, ["B.m2ts"], 200.0)
    other_unique = make_playlist_title(3, ["C.m2ts"], 300.0)
    second_duplicate = make_playlist_title(4, ["B.m2ts"], 200.0)

    groups, order = scan._group_duplicate_playlists(
        [unique, first_duplicate, other_unique, second_duplicate]
    )

    assert len(order) == 3
    assert groups[order[0]] == [unique]
    assert groups[order[1]] == [first_duplicate, second_duplicate]
    assert groups[order[2]] == [other_unique]


def test_playlist_rank_prefers_chapters_streams_then_audio_subtitles() -> None:
    chapter_rich = make_playlist_title(1, ["A.m2ts"], 100.0, chapters=2, stream_count=1)
    stream_rich = make_playlist_title(2, ["A.m2ts"], 100.0, chapters=1, stream_count=5)
    assert scan._best_duplicate_playlist([stream_rich, chapter_rich]) is (chapter_rich)

    fewer_streams = make_playlist_title(
        1, ["A.m2ts"], 100.0, chapters=2, stream_count=2
    )
    more_streams = make_playlist_title(2, ["A.m2ts"], 100.0, chapters=2, stream_count=3)
    assert scan._best_duplicate_playlist([fewer_streams, more_streams]) is (
        more_streams
    )

    fewer_audio_subs = make_playlist_title(
        1,
        ["A.m2ts"],
        100.0,
        chapters=2,
        stream_count=2,
        audio_subtitle_count=1,
    )
    more_audio_subs = make_playlist_title(
        2,
        ["A.m2ts"],
        100.0,
        chapters=2,
        stream_count=2,
        audio_subtitle_count=2,
    )
    assert (
        scan._best_duplicate_playlist([fewer_audio_subs, more_audio_subs])
        is more_audio_subs
    )


def test_dedup_duplicate_playlists_keeps_first_title_on_exact_tie() -> None:
    first = make_playlist_title(1, ["A.m2ts"], 100.0, chapters=2, stream_count=3)
    second = make_playlist_title(2, ["A.m2ts"], 100.0, chapters=2, stream_count=3)

    assert scan._dedup_duplicate_playlists([first, second]) == [first]


def test_dedup_duplicate_playlists_preserves_first_seen_group_order() -> None:
    unique_first = make_playlist_title(1, ["A.m2ts"], 100.0)
    weak_duplicate = make_playlist_title(
        2, ["B.m2ts"], 200.0, chapters=1, stream_count=1
    )
    unique_middle = make_playlist_title(3, ["C.m2ts"], 300.0)
    best_duplicate = make_playlist_title(
        4, ["B.m2ts"], 200.0, chapters=3, stream_count=3
    )

    result = scan._dedup_duplicate_playlists(
        [unique_first, weak_duplicate, unique_middle, best_duplicate]
    )

    assert result == [unique_first, best_duplicate, unique_middle]
