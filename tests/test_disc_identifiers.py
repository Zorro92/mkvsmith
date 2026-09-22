"""Regression tests for disc-level identifiers."""

from __future__ import annotations

import shutil
import sys
import unicodedata
from pathlib import Path

import cli
import mkv
import scan
import tagger
import pytest
from dvdifo import _compute_dvd_disc_id
from matrix256 import fingerprint, fingerprint_entries
from models import DiscMetadata, Stream, StreamType, TagOptions, Title
from models import Config, RuntimeState


@pytest.mark.skipif(
    not all(
        (Path(__file__).parent / "fixtures" / name).is_file()
        for name in ("dvd_video_ts.ifo", "dvd_vts_01_0.ifo")
    ),
    reason="disc fixtures not present; capture them locally to run this test",
)
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

    assert _compute_dvd_disc_id(video_ts) == "b090283799370e5f"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink permissions vary")
def test_matrix256_matches_conformance_fragments(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    sort_order = tmp_path / "sort-order"
    (sort_order / "a").mkdir(parents=True)
    (sort_order / "a" / "b").write_bytes(b"")
    (sort_order / "a-b").write_bytes(b"")
    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    (symlink_root / "real.txt").write_bytes(b"x")
    (symlink_root / "link").symlink_to("real.txt")

    assert fingerprint(empty) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert fingerprint(sort_order) == (
        "82d1301cbc45799e538f19a52840b9ff5a9ca797d80c5e52b4d98c4750d2b5e3"
    )
    assert fingerprint(symlink_root) == (
        "1f99a83be1c9ac0d243b7937f15908a03ede98ffa24c18fcf6100fca66506df4"
    )


def test_matrix256_normalizes_unicode_paths() -> None:
    nfc_name = unicodedata.normalize("NFC", "café.txt")
    nfd_name = unicodedata.normalize("NFD", "café.txt")

    assert fingerprint_entries([(nfd_name, 0)]) == fingerprint_entries([(nfc_name, 0)])


def test_matrix256_conformance_vectors() -> None:
    vectors: list[tuple[str, list[tuple[str, int]]]] = [
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", []),
        (
            "576ada568edb673473287643d06ca9b763d81b712a080388fbf445bf580dab3d",
            [("a", 0)],
        ),
        (
            "00c8e12fff1075e74071d424a34ec9e89e2ffc96c5c4ec6a5bf7a3b5941b3324",
            [("hello.txt", 6)],
        ),
        (
            "a7cde029efe3b62bb536d2eead4b0900409eea281230c0e1146dd0db645a2042",
            [("a", 0), ("b", 0)],
        ),
        (
            "e99dec2b961d71942f740d942301fdb9e1268eeca6b21161dfaf5b7c253ed660",
            [("A", 0), ("a", 0)],
        ),
        (
            "82d1301cbc45799e538f19a52840b9ff5a9ca797d80c5e52b4d98c4750d2b5e3",
            [("a-b", 0), ("a/b", 0)],
        ),
        (
            "8f2c64be52e682809a97f2e370a2638c10e3c3f9071eaa0bda3f7fc4c6c6eccb",
            [("dir1/dir2/file.txt", 0)],
        ),
        (
            "ab44545fa7095c239cd8e9fa36eff237b1cc8e32c5126e98591b24250aa11871",
            [("a/z", 0), ("b/a", 0)],
        ),
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", []),
        (
            "00c8e12fff1075e74071d424a34ec9e89e2ffc96c5c4ec6a5bf7a3b5941b3324",
            [("hello.txt", 6)],
        ),
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", []),
        (
            "1f99a83be1c9ac0d243b7937f15908a03ede98ffa24c18fcf6100fca66506df4",
            [("real.txt", 1)],
        ),
        (
            "afd2f606ae4f4e4d644cbb28ab2f1c5d46d6f98130304efd9941db17d6a91dcd",
            [(unicodedata.normalize("NFC", "café.txt"), 0)],
        ),
        (
            "afd2f606ae4f4e4d644cbb28ab2f1c5d46d6f98130304efd9941db17d6a91dcd",
            [(unicodedata.normalize("NFD", "café.txt"), 0)],
        ),
        (
            "c044182349eea94dff66a1ce2764e6f809cbf8893b2071d5906203b41fea21c0",
            [("привет.txt", 0)],
        ),
        (
            "339e0893d9d4aa8df81e9e7d671983f7befa124bd86416dc69697c32d8112787",
            [("你好.txt", 0)],
        ),
        (
            "9ec64191ddf011278744183c8830b3b7e7c6f35fbff37c66122f0ae0e7add033",
            [("مرحبا.txt", 0)],
        ),
        (
            "7c547ce5b89040b67d9cbf5c2ec5556090fdcfa8f3120b48a856c054769b7816",
            [("🎵.txt", 0)],
        ),
        (
            "b7ce4f0d4e8cde3698b11edc79c49639b3f04cf88e128b0f1c3f0951843f7966",
            [
                ("ascii.txt", 0),
                (unicodedata.normalize("NFC", "café.txt"), 0),
                ("你好.txt", 0),
                ("🎵.txt", 0),
            ],
        ),
        (
            "ac2ee75612a4d578fe365711b2f8aef71e40b2f8c2abf212fa26308d857160e6",
            [
                ("size_0000000", 0),
                ("size_0000001", 1),
                ("size_0000255", 255),
                ("size_0000256", 256),
                ("size_0065535", 65535),
                ("size_0065536", 65536),
                ("size_1000000", 1000000),
            ],
        ),
        (
            "a164865515f0f66b25cc4aff36e558a602d3db6caf62d41d1e830f9283b3dc8f",
            [(f"f{index:03d}", 0) for index in range(100)],
        ),
        (
            "35997ed41f132aad8afc1e08a577090dff4aaa7bb23ffe5f874e879fbc38475f",
            [("/".join("abcdefghij") + "/file.txt", 0)],
        ),
        (
            "31013f1f14b4c55273b923a96047c43e157423625160c53dad1f7971de44db58",
            [("a" * 200, 0)],
        ),
        (
            "8392ec1f2dec1510d58ade51d070394768a4fbbe917c677387901f1147dd439a",
            [("bad\ufffd.txt", 0)],
        ),
        (
            "599b5d5fd9d52740c6b40f134b260b52de60bed70ee60aa0536ee8474fc65bcc",
            [("foo", 0), ("foo.txt", 0), ("foobar", 0)],
        ),
        (
            "00c8e12fff1075e74071d424a34ec9e89e2ffc96c5c4ec6a5bf7a3b5941b3324",
            [("hello.txt", 6)],
        ),
    ]

    for expected, entries in vectors:
        assert fingerprint_entries(entries) == expected


def test_scanner_adds_matrix256_fingerprint_for_folder_disc(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "disc"
    stream_dir = source / "BDMV" / "STREAM"
    stream_dir.mkdir(parents=True)
    (stream_dir / "movie.m2ts").write_bytes(b"video")
    scanner = scan.Scanner(source, Config(), RuntimeState())
    monkeypatch.setattr(
        scan, "_scan_bluray_source", lambda *_args: ([], DiscMetadata())
    )

    titles = scanner.scan()

    assert titles == []
    assert scanner.disc_metadata.matrix256_fingerprint == fingerprint(source)


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
        matrix256_fingerprint="a" * 64,
    )

    cli.display_titles([title], metadata, cli.Config(show_all=True))

    output = capsys.readouterr().out
    assert "UPC/EAN: 123456789012" in output
    assert "DVD Disc ID: 0123456789abcdef" in output
    assert f"Matrix256 fingerprint: {'a' * 64}" in output
    assert "mkvsmith metadata hash:" not in output


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
        matrix256_fingerprint="a" * 64,
    )
    prepared = tagger.MovieMetadata(title="Test Disc")
    monkeypatch.setattr(tagger, "_prepare_tagging", lambda *_args: (prepared, []))

    tagged, _attachments = mkv._prepare_mux_tags(
        title, TagOptions(enabled=True), [], metadata
    )

    assert tagged is not None
    assert tagged.custom_properties["BARCODE"] == "123456789012"
    assert tagged.custom_properties["DVD_DISC_ID"] == "0123456789abcdef"
    assert tagged.custom_properties["MATRIX256_FINGERPRINT"] == "a" * 64
    assert "MKVSMITH_METADATA_HASH" not in tagged.custom_properties
