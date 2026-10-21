"""Blu-ray parser tests on synthetic playlists and clip info, so they run
everywhere.

The files come from tests/synth_bluray.py, which follows
docs/formats/bluray.md (libbluray), not the parser: these tests check the
parser against the format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import bluray
from mkvsmith.bluray import _parse_bdmv_disc_name, _parse_clpi, _parse_mpls
from mkvsmith.models import StreamType
from tests.synth_bluray import (
    AC3,
    BT2020,
    DOLBY_VISION,
    DTSHD_MA,
    FORMAT_1080P,
    FORMAT_2160P,
    H264,
    HDR10,
    HEVC,
    IG,
    MULTI,
    PG,
    RATE_24,
    TRUEHD,
    ClipStream,
    PlayItem,
    Stn,
    Stream,
    SubPath,
    SubPlayItem,
    bdmt,
    clpi,
    mpls,
)


def _feature_stn() -> Stn:
    return Stn(
        video=[Stream(H264, 0x1011)],
        audio=[
            Stream(TRUEHD, 0x1100, presentation=MULTI),
            Stream(AC3, 0x1101, lang="fra"),
        ],
        pg=[Stream(PG, 0x1200), Stream(PG, 0x1201, lang="spa")],
        ig=[Stream(IG, 0x1400)],
    )


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_play_items_chapters_and_streams(tmp_path: Path) -> None:
    # Two clips; chapters are marks on each item's own clock (the first
    # item starts 10 s into its clip).
    stn = _feature_stn()
    playlist = mpls(
        [
            PlayItem("00001", 10, 1210, stn),
            PlayItem("00002", 0, 600, stn, connection=6),
        ],
        chapters=[(0, 10), (0, 610), (1, 0), (1, 300)],
    )

    info = _parse_mpls(_write(tmp_path, "00800.mpls", playlist))

    assert info is not None
    assert [(item["clip"], item["duration"]) for item in info["play_items"]] == [
        ("00001", 1200.0),
        ("00002", 600.0),
    ]
    assert info["chapter_times"] == pytest.approx([0.0, 600.0, 1200.0, 1500.0])
    streams = [(s["type"], s["pid"], s["codec"], s["lang"]) for s in info["streams"]]
    # The interactive-graphics menu stream (IG) isn't a subtitle.
    assert streams == [
        (StreamType.VIDEO, 0x1011, "h264", "und"),
        (StreamType.AUDIO, 0x1100, "truehd", "eng"),
        (StreamType.AUDIO, 0x1101, "ac3", "fra"),
        (StreamType.SUBTITLE, 0x1200, "hdmv_pgs_subtitle", "eng"),
        (StreamType.SUBTITLE, 0x1201, "hdmv_pgs_subtitle", "spa"),
    ]


def test_clip_info_streams() -> None:
    info = _parse_clpi(
        clpi(
            [
                ClipStream(0x1011, H264, video_format=FORMAT_1080P, rate=RATE_24),
                ClipStream(0x1100, TRUEHD, presentation=MULTI),
                ClipStream(0x1101, AC3, lang="fra"),
                ClipStream(0x1200, PG, lang="spa"),
            ]
        )
    )

    video = info[0x1011]
    assert (video["codec"], video["height"], video["interlaced"]) == (
        "h264",
        1080,
        False,
    )
    assert video["framerate"] == pytest.approx(24.0)
    assert video["aspect"] == "16:9"
    assert (info[0x1100]["codec"], info[0x1100]["channels"]) == ("truehd", 6)
    assert (info[0x1101]["channels"], info[0x1101]["language"]) == (2, "fra")
    assert (info[0x1200]["codec"], info[0x1200]["language"]) == (
        "hdmv_pgs_subtitle",
        "spa",
    )


def test_clip_info_fills_in_the_playlists_streams(tmp_path: Path) -> None:
    clipinf = tmp_path / "CLIPINF"
    clipinf.mkdir()
    _write(
        clipinf,
        "00001.clpi",
        clpi(
            [ClipStream(0x1011, H264), ClipStream(0x1100, TRUEHD, presentation=MULTI)]
        ),
    )
    playlist = _write(
        tmp_path, "00800.mpls", mpls([PlayItem("00001", 0, 600, _feature_stn())])
    )

    info = _parse_mpls(playlist, clipinf)

    assert info is not None
    by_pid = {s["pid"]: s for s in info["streams"]}
    assert by_pid[0x1011].get("height") == 1080
    assert by_pid[0x1100].get("channels") == 6


def test_disc_name_from_the_disc_library(tmp_path: Path) -> None:
    meta = tmp_path / "BDMV" / "META" / "DL"
    meta.mkdir(parents=True)
    (meta / "bdmt_eng.xml").write_text(bdmt("A Film - Blu-ray"))

    assert _parse_bdmv_disc_name(tmp_path / "BDMV") == "A Film - Blu-ray"


def test_streams_in_subpaths_and_the_subpaths_themselves(tmp_path: Path) -> None:
    # Commentaries ride in secondary-audio SubPaths (type 2) on the same
    # PID; the menu is an interactive-graphics SubPath (type 3).
    stn = Stn(
        video=[Stream(H264, 0x1011)],
        audio=[
            Stream(DTSHD_MA, 0x1100),
            Stream(AC3, 0x1100, stream_type=2, subpath=0),
            Stream(AC3, 0x1100, stream_type=2, subpath=1, lang="fra"),
        ],
    )
    subpaths = [
        SubPath(2, [SubPlayItem("00010", 12, 1212)]),
        SubPath(2, [SubPlayItem("00011", 0, 1200, connection=5)]),
        SubPath(3, [SubPlayItem("00013", 0, 30)]),
    ]
    playlist = mpls([PlayItem("00001", 0, 1200, stn)], subpaths=subpaths)

    info = _parse_mpls(_write(tmp_path, "00307.mpls", playlist))

    assert info is not None
    audio = [s for s in info["streams"] if s["type"] == StreamType.AUDIO]
    assert [(s["pid"], s.get("subpath_id")) for s in audio] == [
        (0x1100, None),
        (0x1100, 0),
        (0x1100, 1),
    ]
    entries = info["subpath_entries"]
    assert [(e["type"], e["clips"]) for e in entries] == [
        (2, ["00010"]),
        (2, ["00011"]),
        (3, ["00013"]),
    ]
    first = entries[0]["items"][0]
    assert (first["clip"], first["in_time"], first["connection_condition"]) == (
        "00010",
        12 * 45_000,
        1,
    )
    assert entries[1]["items"][0]["connection_condition"] == 5


def test_the_stream_table_is_found_after_a_multi_angle_items_angles(
    tmp_path: Path,
) -> None:
    playlist = mpls(
        [PlayItem("00001", 0, 600, _feature_stn(), angles=["00002", "00003"])]
    )

    info = _parse_mpls(_write(tmp_path, "00801.mpls", playlist))

    assert info is not None
    assert [s["pid"] for s in info["streams"]] == [
        0x1011,
        0x1100,
        0x1101,
        0x1200,
        0x1201,
    ]


def test_play_items_with_different_stream_tables_are_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(bluray, "log_debug", messages.append)
    playlist = mpls(
        [
            PlayItem("00001", 0, 600, Stn(video=[Stream(H264, 0x1011)])),
            PlayItem(
                "00002",
                0,
                600,
                Stn(video=[Stream(H264, 0x1011)], audio=[Stream(AC3, 0x1100)]),
            ),
        ]
    )

    info = _parse_mpls(_write(tmp_path, "00001.mpls", playlist))

    assert info is not None
    assert [s["type"] for s in info["streams"]] == [StreamType.VIDEO]
    assert any(
        "stream tables differ from the first at item(s) 1" in m for m in messages
    )


def test_a_uhd_dolby_vision_playlist_is_tagged_hdr(tmp_path: Path) -> None:
    from mkvsmith.scan import _streams_from_mpls

    stn = Stn(
        video=[
            Stream(
                HEVC,
                0x1011,
                video_format=FORMAT_2160P,
                rate=RATE_24,
                dynamic_range=HDR10,
                colour_space=BT2020,
            ),
        ],
        dolby_vision=[
            Stream(
                HEVC,
                0x1015,
                stream_type=4,
                video_format=FORMAT_1080P,
                rate=RATE_24,
                dynamic_range=DOLBY_VISION,
                colour_space=BT2020,
            ),
        ],
    )
    info = _parse_mpls(
        _write(tmp_path, "00010.mpls", mpls([PlayItem("00003", 0, 5400, stn)]))
    )

    assert info is not None
    videos = [s for s in info["streams"] if s["type"] == StreamType.VIDEO]
    assert [s.get("dynamic_range_type") for s in videos] == ["hdr10", "dolby_vision"]
    base = _streams_from_mpls(videos)[0]
    assert (base.color_primaries, base.color_transfer) == ("bt2020", "smpte2084")
