"""Tests for folder-based Blu-ray source scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import scan
from models import Config, Stream, StreamType, Title


def make_title(index: int, source: Path, name: str, duration: float) -> Title:
    title = Title(index, source, name, duration)
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    return title


@pytest.fixture
def bluray_source(tmp_path: Path) -> Path:
    source = tmp_path / "disc"
    playlist_dir = source / "BDMV" / "PLAYLIST"
    stream_dir = source / "BDMV" / "STREAM"
    clipinfo_dir = source / "BDMV" / "CLIPINF"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir()
    clipinfo_dir.mkdir()
    (stream_dir / "A.m2ts").write_bytes(b"A" * 10)
    (stream_dir / "B.m2ts").write_bytes(b"B" * 10)
    (playlist_dir / "00802.mpls").write_bytes(b"missing clip")
    (playlist_dir / "00800.mpls").write_bytes(b"valid")
    (playlist_dir / "00801.mpls").write_bytes(b"short")
    return source


def test_playlist_clip_paths_requires_every_clip(tmp_path: Path) -> None:
    present = tmp_path / "A.m2ts"
    present.write_bytes(b"A")

    assert scan._playlist_clip_paths([{"clip": "A"}], tmp_path) == [present]
    assert scan._playlist_clip_paths([{"clip": "A"}, {"clip": "B"}], tmp_path) is None


def test_scan_bluray_source_orders_filters_and_skips_incomplete(
    monkeypatch: pytest.MonkeyPatch, bluray_source: Path
) -> None:
    parsed: list[str] = []
    built: list[str] = []

    def parse_mpls(playlist: Path, clpi_dir: Path | None = None):
        parsed.append(playlist.stem)
        if playlist.stem == "00800":
            return {"play_items": [{"clip": "A", "duration": 100.0}]}
        if playlist.stem == "00801":
            return {"play_items": [{"clip": "A", "duration": 10.0}]}
        if playlist.stem == "00802":
            return {"play_items": [{"clip": "MISSING", "duration": 100.0}]}
        return None

    def build_title(
        titles: list[Title],
        playlist: Path,
        clip_paths: list[Path],
        _info: dict[str, Any],
        disc_name: str | None,
        _disc_barcode: str | None,
    ) -> Title:
        built.append(playlist.stem)
        title = make_title(
            len(titles),
            clip_paths[0],
            f"Playlist {playlist.stem}",
            100.0,
        )
        title.playlist_name = playlist.stem
        return title

    monkeypatch.setattr(scan, "_parse_bdmv_disc_name", lambda _bdmv: "Test Disc")
    monkeypatch.setattr(scan, "_parse_bdmv_catalog_number", lambda _bdmv: "12345")
    monkeypatch.setattr(scan, "_parse_mpls", parse_mpls)
    monkeypatch.setattr(scan, "_build_bluray_title_from_mpls", build_title)
    monkeypatch.setattr(scan, "_log_bluray_subpaths", lambda *_args: None)
    monkeypatch.setattr(scan, "_dedup_duplicate_playlists", lambda titles: titles)
    monkeypatch.setattr(scan, "_scan_m2ts_dir", lambda *_args: None)

    titles, disc_name = scan._scan_bluray_source(bluray_source, Config(min_duration=50))

    assert disc_name == "Test Disc"
    assert parsed == ["00800", "00801", "00802"]
    assert built == ["00800"]
    assert [title.name for title in titles] == ["Playlist 00800"]
    assert titles[0].playlist_name == "00800"


def test_scan_bluray_source_falls_back_to_raw_m2ts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "disc"
    stream_dir = source / "bdmv" / "STREAM"
    stream_dir.mkdir(parents=True)
    fallback_calls: list[tuple[Path, list[Title]]] = []

    def scan_m2ts(directory: Path, titles: list[Title]) -> None:
        fallback_calls.append((directory, list(titles)))
        titles.append(make_title(0, directory / "raw.m2ts", "Raw", 100.0))

    monkeypatch.setattr(scan, "_parse_bdmv_disc_name", lambda _bdmv: None)
    monkeypatch.setattr(scan, "_parse_bdmv_catalog_number", lambda _bdmv: None)
    monkeypatch.setattr(scan, "_scan_m2ts_dir", scan_m2ts)
    monkeypatch.setattr(scan, "_dedup_duplicate_playlists", lambda titles: titles)

    titles, disc_name = scan._scan_bluray_source(source)

    assert disc_name is None
    assert fallback_calls == [(stream_dir, [])]
    assert [title.name for title in titles] == ["Raw"]


def test_find_bdmv_directory_prefers_uppercase(tmp_path: Path) -> None:
    upper = tmp_path / "BDMV"
    upper.mkdir()

    assert scan._find_bdmv_directory(tmp_path) == upper
    assert scan._find_bdmv_directory(tmp_path / "lower") == tmp_path / "lower" / "bdmv"
