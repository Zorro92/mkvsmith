"""Tests for HD DVD XPL playlist and DISCID.DAT parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith.hddvd import (
    HddvdTitle,
    describe_language,
    find_playlist,
    parse_discid,
    parse_xpl,
    parse_xpl_time,
)
from mkvsmith.models import Stream, StreamType, Title
from mkvsmith.scan import _apply_hddvd_languages

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
        <Title titleNumber="10" titleDuration="00:02:28:23" id="Trailer1" displayName="IronMan">
            <PrimaryAudioVideoClip titleTimeBegin="00:00:00:00" titleTimeEnd="00:02:28:23" src="file:///dvddisc/HVDVD_TS/TRAILER.MAP" dataSource="Disc">
                <Video track="1" mediaAttr="2" />
            </PrimaryAudioVideoClip>
        </Title>
        <Title titleNumber="11" titleDuration="00:01:52:36" id="Trailer2" displayName="Teaser Trailer">
            <PrimaryAudioVideoClip titleTimeBegin="00:00:00:00" titleTimeEnd="00:01:52:36" src="file:///dvddisc/HVDVD_TS/TEASER.MAP" dataSource="Disc">
                <Video track="1" mediaAttr="2" />
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
    assert describe_language("English for the Hearing Impaired") == (
        "eng",
        False,
        False,
        True,
    )
    assert describe_language("Brazilian Portuguese") == ("por", False, False, False)
    assert describe_language("") == ("und", False, False, False)


def test_parse_xpl_titles_clips_chapters(tmp_path: Path) -> None:
    adv_obj, hvdt = _write_disc(tmp_path)
    disc = parse_xpl(adv_obj / "VPLST000.XPL", hvdt)

    assert [t.number for t in disc.titles] == [3, 8, 10, 11]
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
    # Trailer kind surfaces from the XPL id, once, when the name lacks it.
    by_number = {t.number: t for t in disc.titles}
    assert by_number[10].name == "IronMan (Trailer)"
    assert by_number[11].name == "Teaser Trailer"


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


# Shaped like a real Advanced Content disc: an MLP track first in the menu
# but stream 1 of the EVO, DD+ after it, the navigation list's code
# extensions and ``forced`` attribute, and the MediaAttributeList codecs.
XPL_FLAGS = """<?xml version="1.0" encoding="UTF-8"?>
<Playlist majorVersion="1" minorVersion="0"
    xmlns="http://www.dvdforum.org/2005/HDDVDVideo/Playlist">
    <MediaAttributeList>
        <VideoAttributeItem index="1" codec="VC-1" />
        <AudioAttributeItem index="1" codec="DD+" channels="6" />
        <AudioAttributeItem index="2" codec="MLP" channels="6" />
        <AudioAttributeItem index="3" codec="DD+" channels="2" />
        <SubpictureAttributeItem index="1" codec="8bitRLC" />
    </MediaAttributeList>
    <TitleSet timeBase="60fps" defaultLanguage="en">
        <Title titleNumber="2" titleDuration="01:00:00:00" id="Feature">
            <PrimaryAudioVideoClip titleTimeBegin="00:00:00:00" titleTimeEnd="01:00:00:00" src="file:///dvddisc/HVDVD_TS/FEATURE_1.MAP" dataSource="Disc">
                <Video track="1" mediaAttr="1" />
                <Audio track="1" streamNumber="2" mediaAttr="1" />
                <Audio track="2" streamNumber="1" mediaAttr="2" />
                <Audio track="3" streamNumber="3" mediaAttr="3" />
                <Subtitle track="1" streamNumber="1" mediaAttr="1" />
                <Subtitle track="2" streamNumber="2" mediaAttr="1" />
                <Subtitle track="3" streamNumber="3" mediaAttr="1" />
            </PrimaryAudioVideoClip>
            <TrackNavigationList>
                <AudioTrack track="1" langcode="en:00" description="English 5.1" />
                <AudioTrack track="2" langcode="fr:01" description="French" />
                <AudioTrack track="3" langcode="en:03" description="Director" />
                <SubtitleTrack track="1" langcode="en:05" forced="false" description="English" />
                <SubtitleTrack track="2" langcode="es:00" forced="true" description="Spanish" />
                <SubtitleTrack track="3" langcode="fr:09" description="French" />
            </TrackNavigationList>
        </Title>
    </TitleSet>
</Playlist>
"""


def _flags_title(tmp_path: Path) -> HddvdTitle:
    adv_obj = tmp_path / "ADV_OBJ"
    hvdt = tmp_path / "HVDVD_TS"
    adv_obj.mkdir()
    hvdt.mkdir()
    (adv_obj / "VPLST000.XPL").write_text(XPL_FLAGS)
    (hvdt / "FEATURE_1.EVO").write_bytes(b"evo")
    (title,) = parse_xpl(adv_obj / "VPLST000.XPL", hvdt).titles
    return title


def test_navigation_code_extensions_and_forced_set_flags(tmp_path: Path) -> None:
    title = _flags_title(tmp_path)
    audio = [s for s in title.streams if s.kind == "audio"]
    subs = [s for s in title.streams if s.kind == "subtitle"]

    assert [(s.language, s.is_commentary) for s in audio] == [
        ("eng", False),
        ("fra", False),
        ("eng", True),  # en:03
    ]
    # The navigation description stands in for the clip's missing one.
    assert audio[0].description == "English 5.1"
    assert [(s.language, s.is_hearing_impaired, s.is_forced) for s in subs] == [
        ("eng", True, False),  # en:05
        ("spa", False, True),  # forced="true"
        ("fra", False, True),  # fr:09
    ]


def test_audio_sub_streams_come_from_codec_and_stream_number(tmp_path: Path) -> None:
    title = _flags_title(tmp_path)
    audio = [s for s in title.streams if s.kind == "audio"]

    assert [(s.codec, s.sub_id) for s in audio] == [
        ("DD+", 0xC1),
        ("MLP", 0xB0),
        ("DD+", 0xC2),
    ]


def test_probed_audio_is_matched_by_sub_stream_not_position(tmp_path: Path) -> None:
    title = _flags_title(tmp_path)
    # mkvmerge lists the MLP track (0xB0) first; the XPL lists it second.
    probed = [
        Stream(index=i, stream_type=StreamType.AUDIO, codec=codec, type_index=i)
        for i, codec in enumerate(("truehd", "eac3", "eac3"))
    ]
    for stream, sub in zip(probed, (0xB0, 0xC1, 0xC2), strict=True):
        stream.sub_id = sub << 32 | 0xBD
    scanned = Title(
        index=0,
        source_file=Path("FEATURE_1.EVO"),
        name="Feature",
        duration_seconds=3600.0,
        streams=probed,
    )

    _apply_hddvd_languages(scanned, title)

    assert [(s.language, s.is_commentary) for s in probed] == [
        ("fra", False),
        ("eng", False),
        ("eng", True),
    ]
