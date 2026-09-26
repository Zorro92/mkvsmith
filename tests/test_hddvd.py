"""Tests for HD DVD XPL playlist and DISCID.DAT parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hddvd import (
    describe_language,
    find_playlist,
    parse_discid,
    parse_xpl,
    parse_xpl_time,
)

XPL = """<?xml version="1.0" encoding="UTF-8"?>
<Playlist majorVersion="1" minorVersion="0"
    xmlns="http://www.dvdforum.org/2005/HDDVDVideo/Playlist">
    <TitleSet timeBase="60fps" tickBase="60fps" defaultLanguage="en">
        <Title titleNumber="3" titleDuration="02:23:18:40" id="MainMovie" displayName="Main Movie">
            <PrimaryAudioVideoClip titleTimeBegin="00:00:00:00" titleTimeEnd="01:11:49:15" src="file:///dvddisc/HVDVD_TS/FEATURE_1.MAP" dataSource="Disc">
                <Video track="1" mediaAttr="2" />
                <Audio track="1" streamNumber="1" mediaAttr="1" description="English DD+ 5.1" />
                <Audio track="2" streamNumber="2" mediaAttr="1" description="French DD+ 5.1" />
                <Subtitle track="1" streamNumber="1" mediaAttr="1" description="English" />
                <Subtitle track="2" streamNumber="6" mediaAttr="1" description="French Forced" />
                <SubVideo track="1" mediaAttr="1" description="PIP video" />
            </PrimaryAudioVideoClip>
            <PrimaryAudioVideoClip titleTimeBegin="01:11:49:15" titleTimeEnd="02:23:18:40" src="file:///dvddisc/HVDVD_TS/FEATURE_2.MAP" dataSource="Disc" seamless="true">
                <Video track="1" mediaAttr="2" />
                <Audio track="1" streamNumber="1" mediaAttr="1" description="English DD+ 5.1" />
            </PrimaryAudioVideoClip>
            <TrackNavigationList>
                <AudioTrack track="1" langcode="en:01" selectable="true" />
                <AudioTrack track="2" langcode="fr:01" selectable="true" />
                <SubtitleTrack track="1" langcode="en:01" selectable="true" />
                <SubtitleTrack track="2" langcode="fr:09" selectable="true" />
            </TrackNavigationList>
            <ChapterList>
                <Chapter displayName="Chapter  1" titleTimeBegin="00:00:00:00" />
                <Chapter displayName="Chapter  2" titleTimeBegin="00:02:04:35" />
            </ChapterList>
        </Title>
        <Title titleNumber="8" titleDuration="00:00:10:00" id="MPAACard" description="MPAA Card">
            <PrimaryAudioVideoClip titleTimeBegin="00:00:00:00" titleTimeEnd="00:00:10:00" src="file:///dvddisc/HVDVD_TS/BLACK.MAP" dataSource="Disc">
                <Video track="1" mediaAttr="1" />
            </PrimaryAudioVideoClip>
        </Title>
    </TitleSet>
</Playlist>
"""


def _write_disc(tmp_path: Path) -> tuple[Path, Path]:
    adv_obj = tmp_path / "ADV_OBJ"
    hvdt = tmp_path / "HVDVD_TS"
    adv_obj.mkdir()
    hvdt.mkdir()
    xpl = adv_obj / "VPLST000.XPL"
    xpl.write_text(XPL)
    for name in ("FEATURE_1.EVO", "FEATURE_2.EVO", "BLACK.EVO"):
        (hvdt / name).write_bytes(b"evo")
    return adv_obj, hvdt


def test_parse_xpl_time_frames() -> None:
    assert parse_xpl_time("00:00:00:00") == 0.0
    assert parse_xpl_time("00:02:04:35") == pytest.approx(124 + 35 / 60.0)
    assert parse_xpl_time("02:23:18:40") == pytest.approx(
        2 * 3600 + 23 * 60 + 18 + 40 / 60.0
    )
    assert parse_xpl_time(None) == 0.0
    assert parse_xpl_time("bogus") == 0.0


def test_describe_language_flags() -> None:
    assert describe_language("English DD+ 5.1") == ("eng", False, False, False)
    assert describe_language("French Forced") == ("fra", True, False, False)
    assert describe_language("Director Commentary DD+ 2.0") == (
        "und",
        False,
        True,
        False,
    )
    assert describe_language("English SDH") == ("eng", False, False, True)
    assert describe_language("Brazilian Portuguese") == ("por", False, False, False)
    assert describe_language("") == ("und", False, False, False)


def test_parse_xpl_titles_clips_chapters(tmp_path: Path) -> None:
    adv_obj, hvdt = _write_disc(tmp_path)
    disc = parse_xpl(adv_obj / "VPLST000.XPL", hvdt)

    assert [t.number for t in disc.titles] == [3, 8]
    main = disc.titles[0]
    assert main.name == "Main Movie"
    assert main.duration_seconds == pytest.approx(8598 + 40 / 60.0)
    assert [c.evo_path.name for c in main.clips] == ["FEATURE_1.EVO", "FEATURE_2.EVO"]
    assert main.chapters == pytest.approx([0.0, 124 + 35 / 60.0])
    # First clip carries the full track list, including PiP secondaries.
    kinds = [s.kind for s in main.streams]
    assert kinds == ["video", "audio", "audio", "subtitle", "subtitle", "subvideo"]
    assert main.streams[3].language == "eng"
    assert main.streams[4].language == "fra"
    assert main.streams[4].is_forced is True
    # Nav langcodes are authoritative; video takes the disc default language.
    assert main.streams[0].language == "eng"
    assert main.audio_nav_langs == {1: "eng", 2: "fra"}
    assert main.subtitle_nav_langs == {1: "eng", 2: "fra"}
    assert disc.default_language == "eng"
    # Undescribed video with no nav entry takes the disc default language.
    assert disc.titles[1].streams[0].language == "eng"


def test_parse_xpl_missing_file_returns_empty(tmp_path: Path) -> None:
    disc = parse_xpl(tmp_path / "nope.XPL", tmp_path)
    assert disc.titles == []


def test_clip_path_uses_on_disc_casing(tmp_path: Path) -> None:
    """XPL casing must not leak into clip paths.

    Probing for the XPL's casing fails on case-sensitive filesystems when
    the disc disagrees — and lies on case-insensitive ones (Windows,
    macOS) where every variant "exists". The directory listing carries the
    real name on every platform.
    """
    adv_obj = tmp_path / "ADV_OBJ"
    hvdt = tmp_path / "HVDVD_TS"
    adv_obj.mkdir()
    hvdt.mkdir()
    (adv_obj / "VPLST000.XPL").write_text(XPL.replace("FEATURE_1.MAP", "MiXeD.MAP"))
    (hvdt / "Mixed.Evo").write_bytes(b"evo")
    (hvdt / "FEATURE_2.EVO").write_bytes(b"evo")
    disc = parse_xpl(adv_obj / "VPLST000.XPL", hvdt)

    main = [t for t in disc.titles if t.number == 3][0]
    assert [c.evo_path.name for c in main.clips] == ["Mixed.Evo", "FEATURE_2.EVO"]


def test_find_playlist_skips_backups(tmp_path: Path) -> None:
    adv_obj, _ = _write_disc(tmp_path)
    (adv_obj / "VPLST001.BAK").write_text("backup")
    found = find_playlist(adv_obj)
    assert found is not None
    assert found.name == "VPLST000.XPL"
    assert find_playlist(tmp_path) is None


def test_parse_discid_provider(tmp_path: Path) -> None:
    discid = tmp_path / "DISCID.DAT"
    discid.write_bytes(
        b"HDDVD-V_CONF" + bytes(6) + b"PARAMOUNT_HD-SLY\x00" + bytes(100)
    )
    assert parse_discid(discid) == "PARAMOUNT_HD-SLY"
    assert parse_discid(tmp_path / "missing.DAT") is None
    bad = tmp_path / "bad.DAT"
    bad.write_bytes(b"not a disc id file" + bytes(64))
    assert parse_discid(bad) is None
