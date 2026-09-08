"""Tests for generated VobSub index and subtitle files."""

from __future__ import annotations

from pathlib import Path

import pytest

import vobsub
from vobsub import (
    _filter_vobsub_streams,
    _vobsub_pts_offset,
    _write_vobsub_files,
)


def test_filter_vobsub_streams_distributes_zero_only_to_missing_tracks() -> None:
    filtered, languages = _filter_vobsub_streams(
        {
            0: [(20, b"zero-one"), (40, b"zero-two")],
            0x20: [(10, b"real-one")],
        },
        {0x20: "eng", 0x21: "fra"},
    )

    assert filtered == {
        0x20: [(10, b"real-one")],
        0x21: [(20, b"zero-one"), (40, b"zero-two")],
    }
    assert languages == {0x20: "eng", 0x21: "fra"}


def test_filter_vobsub_streams_skips_zero_when_all_ifo_tracks_are_present() -> None:
    filtered, languages = _filter_vobsub_streams(
        {0: [(10, b"zero")], 0x20: [(20, b"real")]},
        {0x20: "eng"},
    )

    assert filtered == {0x20: [(20, b"real")]}
    assert languages == {0x20: "eng"}


def test_filter_vobsub_streams_adds_unknown_language() -> None:
    filtered, languages = _filter_vobsub_streams(
        {0x21: [(10, b"undeclared")]}, {0x20: "eng"}
    )

    assert filtered == {0x21: [(10, b"undeclared")]}
    assert languages == {0x20: "eng", 0x21: "und"}


def test_vobsub_pts_offset_uses_first_video_pts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vobsub, "_scan_vob_pts", lambda *_args, **_kwargs: [(90_000, 8), (91_000, 9)]
    )

    assert _vobsub_pts_offset([Path("movie.vob")]) == 90_000


def test_vobsub_pts_offset_defaults_to_zero_on_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(vobsub, "_scan_vob_pts", fail)

    assert _vobsub_pts_offset([Path("movie.vob")]) == 0


def test_write_vobsub_files_interleaves_tracks_by_pts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vobsub, "_HAS_MKVMERGE", False)
    temp_files: list[Path] = []
    base = tmp_path / "movie"

    result = _write_vobsub_files(
        {
            0x20: [(90_000, b"first"), (180_000, b"second")],
            0x21: [(90_000, b"alternate")],
        },
        {0x20: "eng", 0x21: "fra"},
        {0x21: True},
        base,
        pts_offset=90_000,
        temp_files=temp_files,
    )

    assert result is not None
    idx_path, tracks = result
    assert tracks == []
    assert idx_path == tmp_path / "movie.idx"
    assert temp_files == [tmp_path / "movie.sub", tmp_path / "movie.idx"]
    assert (tmp_path / "movie.sub").stat().st_size == 3 * 2048

    idx = idx_path.read_text(encoding="utf-8")
    assert "palette: " in idx
    assert "id: en, index: 0" in idx
    assert "id: fr, index: 1" in idx
    assert "forced: on" in idx
    assert "timestamp: 00:00:00:000, filepos: 000000000" in idx
    assert "timestamp: 00:00:01:000, filepos: 000001000" in idx
    assert "timestamp: 00:00:00:000, filepos: 000000800" in idx
