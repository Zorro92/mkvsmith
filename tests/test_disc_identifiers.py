"""Regression tests for disc-level identifiers."""

from __future__ import annotations

import shutil
from pathlib import Path

import cli
import mkv
import tagger
from bluray import _parse_bdmv_metadata_hash
from dvdifo import _compute_dvd_disc_id, _compute_dvd_metadata_hash
from models import DiscMetadata, Stream, StreamType, TagOptions, Title


def test_dvd_identifiers_use_real_ifo_fixtures(
    monkeypatch, fixtures_dir: Path, tmp_path: Path
) -> None:
    video_ts = tmp_path / "VIDEO_TS"
    video_ts.mkdir()
    vmg_path = video_ts / "VIDEO_TS.IFO"
    vts_path = video_ts / "VTS_01_0.IFO"
    shutil.copy(fixtures_dir / "dvd_video_ts.ifo", vmg_path)
    shutil.copy(fixtures_dir / "dvd_vts_01_0.ifo", vts_path)
    fingerprints = {
        vmg_path: 11_644_473_611 * 10_000_000,
        vts_path: 11_644_473_612 * 10_000_000,
    }
    monkeypatch.setattr(
        "dvdifo._dvd_creation_filetime", lambda path: fingerprints[path]
    )
    entries = ((path.name, path.stat().st_size) for path in video_ts.iterdir())

    assert _compute_dvd_disc_id(video_ts) == "b090283799370e5f"
    assert (
        _compute_dvd_metadata_hash(
            entries, vmg_path.read_bytes(), vts_path.read_bytes()
        )
        == "dvd-d25ff0aada4a19d069ef7d978dfc25c9"
    )


def test_bdmv_metadata_hash_uses_real_xml_fixture(fixtures_dir: Path) -> None:
    assert (
        _parse_bdmv_metadata_hash([fixtures_dir / "bdmt_eng.xml"])
        == "bdmv-02a76a582b132fbb54fe3dd5c31bbcd3"
    )


def test_display_titles_shows_available_identifiers(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(cli, "get_terminal_width", lambda: 100)
    title = Title(
        index=0,
        source_file=tmp_path / "movie.mkv",
        name="Test Disc",
        duration_seconds=100.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]
    metadata = DiscMetadata(
        name="Test Disc",
        upc_ean="123456789012",
        dvd_disc_id="0123456789abcdef",
        metadata_hash="dvd-abcdef",
    )

    cli.display_titles([title], metadata, cli.Config(show_all=True))

    output = capsys.readouterr().out
    assert "UPC/EAN: 123456789012" in output
    assert "DVD Disc ID: 0123456789abcdef" in output
    assert "Metadata hash: dvd-abcdef" in output


def test_mux_tags_embed_disc_identifiers(monkeypatch, tmp_path: Path) -> None:
    title = Title(
        index=0,
        source_file=tmp_path / "movie.m2ts",
        name="Test Disc",
        duration_seconds=100.0,
    )
    metadata = DiscMetadata(
        upc_ean="123456789012",
        dvd_disc_id="0123456789abcdef",
        metadata_hash="dvd-abcdef",
    )
    prepared = tagger.MovieMetadata(title="Test Disc")
    monkeypatch.setattr(tagger, "_prepare_tagging", lambda *_args: (prepared, []))

    tagged, _attachments = mkv._prepare_mux_tags(
        title, TagOptions(enabled=True), [], metadata
    )

    assert tagged is not None
    assert tagged.custom_properties["BARCODE"] == "123456789012"
    assert tagged.custom_properties["DVD_DISC_ID"] == "0123456789abcdef"
    assert tagged.custom_properties["DISC_METADATA_HASH"] == "dvd-abcdef"
