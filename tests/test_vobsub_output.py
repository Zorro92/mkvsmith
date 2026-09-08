"""Tests for generated VobSub index and subtitle files."""

from __future__ import annotations

from pathlib import Path

import pytest

import vobsub
from vobsub import _write_vobsub_files


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
