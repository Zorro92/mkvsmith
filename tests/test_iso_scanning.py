"""Tests for ISO scanner dispatch and DVD VOB-path mapping."""

from __future__ import annotations

from pathlib import Path

import disc_reader
import pytest
from bluray import _parse_mpls
import scan
from dvdifo import VmgInfo
from models import RuntimeState, Stream, StreamType, Title
from scan import Scanner, _build_bluray_title_from_mpls


def test_dvd_iso_vob_maps_first_file_and_sorts_parts() -> None:
    vobs = [
        "VIDEO_TS/VTS_01_3.VOB",
        "VIDEO_TS/VTS_01_1.VOB",
        "VIDEO_TS/VTS_02_1.VOB",
        "VIDEO_TS/VTS_01_0.VOB",
    ]

    first_vob, all_vobs = Scanner._dvd_iso_vob_maps(vobs)

    assert first_vob == {1: "VIDEO_TS/VTS_01_1.VOB", 2: "VIDEO_TS/VTS_02_1.VOB"}
    assert all_vobs == {
        1: ["VIDEO_TS/VTS_01_1.VOB", "VIDEO_TS/VTS_01_3.VOB"],
        2: ["VIDEO_TS/VTS_02_1.VOB"],
    }


def test_scan_iso_7z_dispatches_bluray_by_playlist(monkeypatch, tmp_path: Path) -> None:
    scanner = Scanner(tmp_path / "movie.iso")
    calls: list[tuple[list[str], dict[str, int], list[str], list[str]]] = []

    def scan_bluray(
        paths: list[str],
        sizes: dict[str, int],
        playlists: list[str],
        streams: list[str],
    ) -> None:
        calls.append((paths, sizes, playlists, streams))

    monkeypatch.setattr(scanner, "_scan_iso_bluray", scan_bluray)
    paths = ["BDMV/PLAYLIST/00800.mpls", "BDMV/STREAM/00800.m2ts"]
    sizes = {path: 1024 for path in paths}
    monkeypatch.setattr(
        disc_reader,
        "_list_iso_files_7z",
        lambda _source, symlinks=None: (paths, sizes),
    )

    scanner._scan_iso_7z()

    assert calls == [
        (paths, sizes, ["BDMV/PLAYLIST/00800.mpls"], ["BDMV/STREAM/00800.m2ts"])
    ]


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "00800.mpls").is_file(),
    reason="disc fixtures not present; capture them locally to run this test",
)
def test_build_bluray_title_from_mpls(fixtures_dir: Path, tmp_path: Path) -> None:
    playlist = fixtures_dir / "00800.mpls"
    info = _parse_mpls(playlist)
    assert info is not None

    clip_paths = [tmp_path / f"{item['clip']}.m2ts" for item in info["play_items"]]
    for clip_path in clip_paths:
        clip_path.write_bytes(b"0123456789")

    title = _build_bluray_title_from_mpls(
        [], playlist, clip_paths, info, "Test Disc", "12345"
    )
    assert title is not None

    assert title.name == "Playlist 00800"
    assert title.disc_name == "Test Disc"
    assert title.disc_barcode == "12345"
    assert title.source_file == clip_paths[0]
    assert title.append_clips == clip_paths[1:]
    assert len(title.streams) == 15
    assert title.chapters[-1] < title.duration_seconds


def test_scan_iso_dvd_builds_vts_from_vmg_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = RuntimeState()
    scanner = Scanner(tmp_path / "movie.iso", runtime_state=runtime_state)
    paths = [
        "VIDEO_TS/VTS_01_3.VOB",
        "VIDEO_TS/VTS_01_1.VOB",
        "VIDEO_TS/VTS_01_2.VOB",
        "VIDEO_TS/VTS_02_1.VOB",
        "VIDEO_TS/VTS_01_0.IFO",
        "VIDEO_TS/VIDEO_TS.IFO",
    ]
    sizes = {
        "VIDEO_TS/VTS_01_1.VOB": 10,
        "VIDEO_TS/VTS_01_2.VOB": 30,
        "VIDEO_TS/VTS_01_3.VOB": 20,
        "VIDEO_TS/VTS_02_1.VOB": 99,
    }
    vmg = VmgInfo(
        disc_name="Test Disc",
        barcode="12345",
        title_map={7: (1, 1)},
    )
    extracted_ifos = [tmp_path / "VIDEO_TS.IFO", tmp_path / "VTS_01_0.IFO"]
    for extracted_ifo in extracted_ifos:
        extracted_ifo.write_bytes(b"mock IFO")
    first_vob = tmp_path / "probe.vob"
    extraction_calls: list[tuple[Path, list[str], Path]] = []
    partial_calls: list[tuple[Path, str]] = []
    build_calls: list[tuple[Path, Path, int, str]] = []
    alternate_calls: list[tuple[Path, Path, str, int]] = []

    monkeypatch.setattr(
        disc_reader,
        "_extract_with_7z",
        lambda source, internal_paths, out_dir, symlinks=None: (
            extraction_calls.append((source, internal_paths, out_dir)) or extracted_ifos
        ),
    )
    monkeypatch.setattr(
        disc_reader,
        "_extract_partial_7z",
        lambda source, internal_path, **_kwargs: (
            partial_calls.append((source, internal_path)) or first_vob
        ),
    )
    monkeypatch.setattr(scan, "_parse_vmg_ifo", lambda _path: vmg)

    def build_title(
        titles: list[Title],
        source: Path,
        ifo_path: Path,
        _vob_parts: list[Path],
        vts: int,
        title_name: str | None = None,
        pgc_number: int | None = None,
        config: object | None = None,
    ) -> Title:
        build_calls.append((source, ifo_path, vts, title_name or ""))
        return Title(len(titles), source, title_name or "", 120.0)

    monkeypatch.setattr(scan, "_build_title_from_ifo", build_title)
    monkeypatch.setattr(
        scanner,
        "_scan_iso_dvd_alternate_editions",
        lambda ifo_path, source, title_name, vts, *_args: alternate_calls.append(
            (ifo_path, source, title_name, vts)
        ),
    )

    scanner._scan_iso_dvd(paths, sizes)

    assert extraction_calls == [
        (
            scanner.source,
            ["VIDEO_TS/VIDEO_TS.IFO", "VIDEO_TS/VTS_01_0.IFO"],
            runtime_state.cleanup.temp_dirs[0],
        )
    ]
    assert partial_calls == [(scanner.source, "VIDEO_TS/VTS_01_1.VOB")]
    assert build_calls == [(first_vob, extracted_ifos[1], 1, "Title 7 (VTS 1)")]
    assert alternate_calls == [(extracted_ifos[1], first_vob, "Title 7 (VTS 1)", 1)]
    assert scanner.disc_name == "Test Disc"
    assert len(scanner.titles) == 1
    title = scanner.titles[0]
    assert title.source_file == scanner.source
    assert title.iso_internal_paths == [
        "VIDEO_TS/VTS_01_1.VOB",
        "VIDEO_TS/VTS_01_2.VOB",
        "VIDEO_TS/VTS_01_3.VOB",
    ]
    assert title.estimated_size_bytes == 60
    assert title.disc_name == "Test Disc"
    assert title.disc_barcode == "12345"


def test_first_iso_playlist_clpi_reads_first_clip_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    playlist = tmp_path / "00800.mpls"
    playlist.write_bytes(b"MPLS" + bytes(8) + b"12345" + bytes(32))
    clpi = tmp_path / "12345.clpi"
    monkeypatch.setattr(scan, "_read_u32", lambda _data, _offset: 0)
    monkeypatch.setattr(scan, "_read_u16", lambda _data, _offset: 32)

    assert scan._first_iso_playlist_clpi(playlist, {"12345": clpi}) == clpi
    assert scan._first_iso_playlist_clpi(playlist, {}) is None


def test_iso_clip_internals_keeps_available_clips() -> None:
    play_items = [{"clip": "A"}, {"clip": "MISSING"}, {"clip": "B"}]

    assert scan._iso_clip_internals(
        play_items, {"A": "BDMV/STREAM/A.m2ts", "B": "BDMV/STREAM/B.m2ts"}
    ) == ["BDMV/STREAM/A.m2ts", "BDMV/STREAM/B.m2ts"]


def test_build_iso_bluray_playlist_title_sets_source_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    playlist = tmp_path / "00800.mpls"
    playlist.write_bytes(b"MPLS")
    source = tmp_path / "movie.iso"
    clpi_dir = tmp_path / "clpi"
    stream = Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")
    parse_calls: list[Path | None] = []

    def parse_mpls(path: Path, clpi_dir: Path | None = None):
        parse_calls.append(clpi_dir)
        return {
            "play_items": [
                {"clip": "A", "duration": 40.0},
                {"clip": "MISSING", "duration": 80.0},
            ],
            "chapter_times": [0.0, 40.0, 120.0],
        }

    monkeypatch.setattr(
        scan,
        "_first_iso_playlist_clpi",
        lambda _playlist, _extracted: clpi_dir / "A.clpi",
    )
    monkeypatch.setattr(scan, "_parse_mpls", parse_mpls)
    monkeypatch.setattr(scan, "_streams_from_mpls", lambda _streams: [stream])

    title = scan._build_iso_bluray_playlist_title(
        playlist_path=playlist,
        index=3,
        source_file=source,
        extracted_clpi={"A": clpi_dir / "A.clpi"},
        m2ts_by_clip={"A": "BDMV/STREAM/A.m2ts"},
        sizes={"BDMV/STREAM/A.m2ts": 123},
        minimum_duration=50,
    )

    assert title is not None
    assert title.index == 3
    assert title.source_file == source
    assert title.name == "Playlist 00800"
    assert title.duration_seconds == 120.0
    assert title.chapters == [0.0, 40.0]
    assert title.iso_internal_paths == ["BDMV/STREAM/A.m2ts"]
    assert title.estimated_size_bytes == 123
    assert title.playlist_name == "00800"
    assert title.clip_durations == [40.0, 80.0]
    assert title.clip_sizes == [123]
    assert title.streams == [stream]
    assert parse_calls == [clpi_dir]


def test_build_iso_bluray_playlist_title_filters_short_and_streamless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    playlist = tmp_path / "00800.mpls"
    playlist.write_bytes(b"MPLS")
    info = {"play_items": [{"clip": "A", "duration": 10.0}]}
    monkeypatch.setattr(scan, "_parse_mpls", lambda *_args, **_kwargs: info)

    assert (
        scan._build_iso_bluray_playlist_title(
            playlist_path=playlist,
            index=0,
            source_file=tmp_path / "movie.iso",
            extracted_clpi={},
            m2ts_by_clip={"A": "BDMV/STREAM/A.m2ts"},
            sizes={},
            minimum_duration=50,
        )
        is None
    )

    monkeypatch.setattr(scan, "_streams_from_mpls", lambda _streams: [])
    assert (
        scan._build_iso_bluray_playlist_title(
            playlist_path=playlist,
            index=0,
            source_file=tmp_path / "movie.iso",
            extracted_clpi={},
            m2ts_by_clip={"A": "BDMV/STREAM/A.m2ts"},
            sizes={},
            minimum_duration=5,
        )
        is None
    )
