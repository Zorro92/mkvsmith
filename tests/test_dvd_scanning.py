"""Characterization tests for DVD source orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dvdifo
import dvdbuild
from dvdifo import VmgInfo
from models import Config, Stream, StreamType, Title


def _make_dvd_source(tmp_path: Path) -> Path:
    source = tmp_path / "VIDEO_TS"
    source.mkdir()
    (source / "VIDEO_TS.IFO").write_bytes(b"vmg")
    (source / "VTS_01_0.IFO").write_bytes(b"vts")
    (source / "VTS_01_1.VOB").write_bytes(b"one")
    (source / "VTS_01_3.VOB").write_bytes(b"three")
    (source / "VTS_01_2.VOB").write_bytes(b"two")
    return source


def _patch_title_builder(monkeypatch: Any) -> list[tuple[int | None, str | None]]:
    calls: list[tuple[int | None, str | None]] = []

    def build_title(
        titles: list[Title],
        _first_vob: Path,
        _ifo_path: Path,
        vob_parts: list[Path],
        _vts: int,
        title_name: str | None = None,
        pgc_number: int | None = None,
        config: object | None = None,
    ) -> Title:
        calls.append((pgc_number, title_name))
        title = Title(len(titles), vob_parts[0], title_name or "", 100.0)
        title.append_clips = vob_parts[1:]
        return title

    monkeypatch.setattr(dvdbuild, "_build_title_from_ifo", build_title)
    return calls


def test_scan_dvd_source_builds_episodes_play_all_and_extra(
    monkeypatch: Any, tmp_path: Path
) -> None:
    source = _make_dvd_source(tmp_path)
    vmg: VmgInfo = {
        "disc_name": "Test Disc",
        "barcode": "12345",
        "title_map": {7: (1, 1)},
    }
    monkeypatch.setattr(dvdbuild, "_parse_vmg_ifo", lambda _path: vmg)
    calls = _patch_title_builder(monkeypatch)
    monkeypatch.setattr(
        dvdbuild, "_detect_episode_pgcs", lambda _data, _minimum: ([1, 2], 3)
    )
    monkeypatch.setattr(dvdbuild, "_default_pgc_number", lambda _data: 1)
    monkeypatch.setattr(
        dvdifo,
        "_enumerate_vts_pgcs",
        lambda _data: [
            (1, 0, 100.0, []),
            (2, 0, 100.0, []),
            (3, 0, 200.0, []),
            (4, 0, 120.0, []),
        ],
    )

    titles, disc_name = dvdbuild._scan_dvd_source(source)

    assert disc_name == "Test Disc"
    assert [title.name for title in titles] == [
        "Title 7 (VTS 1)",
        "Title 7 (VTS 1) - Episode 2",
        "Title 7 (VTS 1) - Play All",
        "Title 7 (VTS 1) - Extra",
    ]
    assert [title.dvd_episode_number for title in titles] == [1, 2, None, None]
    assert titles[2].dvd_play_all is True
    assert [title.disc_barcode for title in titles] == ["12345"] * 4
    assert titles[0].append_clips == [
        source / "VTS_01_2.VOB",
        source / "VTS_01_3.VOB",
    ]
    assert calls == [
        (None, "Title 7 (VTS 1)"),
        (2, "Title 7 (VTS 1) - Episode 2"),
        (3, "Title 7 (VTS 1) - Play All"),
        (4, "Title 7 (VTS 1) - Extra"),
    ]


def test_scan_dvd_source_builds_alternate_editions(
    monkeypatch: Any, tmp_path: Path
) -> None:
    source = _make_dvd_source(tmp_path)
    monkeypatch.setattr(dvdbuild, "_parse_vmg_ifo", lambda _path: VmgInfo())
    _patch_title_builder(monkeypatch)
    monkeypatch.setattr(
        dvdbuild, "_detect_episode_pgcs", lambda _data, _minimum: ([], None)
    )
    monkeypatch.setattr(dvdbuild, "_default_pgc_number", lambda _data: 1)
    monkeypatch.setattr(
        dvdbuild, "_find_alternate_edition_pgcs", lambda _data, _minimum: [2, 3]
    )

    titles, disc_name = dvdbuild._scan_dvd_source(source)

    assert disc_name is None
    assert [title.name for title in titles] == [
        "Title 1",
        "Title 1 - Edition 2",
        "Title 1 - Edition 3",
    ]
    assert [title.disc_name for title in titles] == [None, None, None]
    assert [title.dvd_edition_label for title in titles] == [
        None,
        "Edition 2",
        "Edition 3",
    ]


def test_build_dvd_streams_rejects_invalid_ifo() -> None:
    assert dvdbuild._build_dvd_streams_from_ifo(b"NOTDVDVTS\x00\x00", 100.0) == []


def test_build_dvd_streams_uses_active_pgc_and_attributes(monkeypatch: Any) -> None:
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)
    monkeypatch.setattr(
        dvdbuild,
        "_get_active_pgc_streams",
        lambda _data, _pgc_number: ({0x81}, {0x21}),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_video_attrs",
        lambda _data: dvdifo._IFOVideoAttrs(
            mpeg_version="MPEG-2",
            standard="NTSC",
            aspect_ratio="16:9",
            resolution=(720, 480),
            letterboxed=False,
            film_mode=False,
            cc_field_1=False,
            cc_field_2=False,
        ),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_ifo_languages",
        lambda _data: (
            {0x80: "und", 0x81: "fra"},
            {0x20: "und", 0x21: "eng"},
        ),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_pgc_stream_languages",
        lambda _data, _pgc_number: ({0x81: "eng"}, {0x21: "fre"}),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_audio_attrs",
        lambda _data: {
            0x81: dvdifo._IFOAudioAttrs(
                codec="DTS",
                channels=6,
                sample_rate="48 kHz",
                quantization="24-bit",
                bits_per_sample=24,
                dsur=False,
                code_extension=2,
                lang_code="eng",
            )
        },
    )
    monkeypatch.setattr(
        dvdbuild,
        "_ifo_audio_title",
        lambda _attrs: "DTS 5.1",
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_subp_attrs",
        lambda _data: {
            0x21: dvdifo._IFOSubpictureAttrs(
                coding_mode="run-length",
                code_extension=9,
                lang_code="fre",
                is_hearing_impaired=True,
            )
        },
    )

    streams = dvdbuild._build_dvd_streams_from_ifo(ifo_data, 100.0, 2)

    assert [(stream.stream_type, stream.sub_id) for stream in streams] == [
        (StreamType.VIDEO, 0x1E0),
        (StreamType.AUDIO, 0x81),
        (StreamType.SUBTITLE, 0x21),
    ]
    video = streams[0]
    assert video.codec == "mpeg2video"
    assert (video.width, video.height) == (720, 480)
    assert video.sample_aspect_ratio == "1.185185"
    assert video.color_primaries == "smpte170m"
    assert video.color_transfer == "bt709"
    assert video.color_space == "smpte170m"

    audio = streams[1]
    assert audio.codec == "dts"
    assert audio.language == "eng"
    assert audio.channels == 6
    assert audio.sample_rate == "48000"
    assert audio.bits_per_sample == 24
    assert audio.title == "DTS 5.1"
    assert audio.is_commentary is True
    assert audio.type_index == 0

    subtitle = streams[2]
    assert subtitle.codec == "dvd_subtitle"
    assert subtitle.language == "fre"
    assert subtitle.is_forced is True
    assert subtitle.is_hearing_impaired is True
    assert subtitle.type_index == 0


def test_build_dvd_streams_falls_back_to_vts_ids(monkeypatch: Any) -> None:
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)
    monkeypatch.setattr(
        dvdbuild, "_get_active_pgc_streams", lambda _data, _pgc: (set(), set())
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_video_attrs",
        lambda _data: None,
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_ifo_languages",
        lambda _data: (
            {0x81: "eng", 0x80: "und"},
            {0x21: "fre", 0x20: "und"},
        ),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_pgc_stream_languages",
        lambda _data, _pgc: ({}, {}),
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", lambda _data: {})
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", lambda _data: {})

    streams = dvdbuild._build_dvd_streams_from_ifo(ifo_data, 100.0)

    assert [stream.sub_id for stream in streams] == [
        0x1E0,
        0x80,
        0x81,
        0x20,
        0x21,
    ]
    assert [stream.language for stream in streams[1:]] == [
        "und",
        "eng",
        "und",
        "fre",
    ]
    assert streams[1].codec == "ac3"
    assert streams[1].channels == 2


def test_apply_dvd_ifo_languages_updates_streams_and_timing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_data = b"DVDVIDEO-VTS"
    ifo_path.write_bytes(ifo_data)
    title = Title(
        index=0,
        source_file=tmp_path / "VTS_01_1.VOB",
        name="Title",
        duration_seconds=90.0,
    )
    video = Stream(
        index=0,
        stream_type=StreamType.VIDEO,
        codec="mpeg2video",
        sub_id=0x1E0,
    )
    primary_audio = Stream(
        index=1,
        stream_type=StreamType.AUDIO,
        codec="ac3",
        sub_id=0x80,
    )
    secondary_audio = Stream(
        index=2,
        stream_type=StreamType.AUDIO,
        codec="ac3",
        language="fra",
        bits_per_sample=16,
        sub_id=0x81,
    )
    declared_subtitle = Stream(
        index=3,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        sub_id=0x20,
    )
    title.streams = [video, primary_audio, secondary_audio, declared_subtitle]

    audio_attrs = {
        0x80: dvdifo._IFOAudioAttrs(
            codec="AC3",
            channels=6,
            sample_rate="48 kHz",
            quantization="24-bit",
            bits_per_sample=24,
            dsur=False,
            code_extension=0,
            lang_code="eng",
        )
    }
    video_attrs = dvdifo._IFOVideoAttrs(
        mpeg_version="MPEG-2",
        standard="NTSC",
        aspect_ratio="16:9",
        resolution=(720, 480),
        letterboxed=False,
        film_mode=False,
        cc_field_1=False,
        cc_field_2=False,
    )
    subp_attrs = {
        0x21: dvdifo._IFOSubpictureAttrs(
            coding_mode="run-length",
            code_extension=9,
            lang_code="fre",
            is_hearing_impaired=False,
        )
    }
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_ifo_languages",
        lambda _data: (
            {0x80: "und", 0x81: "fra"},
            {0x20: "und", 0x21: "eng"},
        ),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_pgc_stream_languages",
        lambda _data, _pgc_number=None: ({0x80: "eng"}, {0x21: "fre"}),
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", lambda _data: audio_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", lambda _data: video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", lambda _data: subp_attrs)
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_pgc_info",
        lambda _data: ([0.0, 10.0], 120.0),
    )

    dvdbuild._apply_dvd_ifo_languages(title, ifo_path)

    assert title.dvd_ifo_data == ifo_data
    assert title.dvd_audio_lang == {0x80: "eng", 0x81: "fra"}
    assert title.dvd_sub_lang == {0x20: "und", 0x21: "fre"}
    assert title.dvd_audio_attrs is audio_attrs
    assert title.dvd_video_attrs is video_attrs
    assert title.dvd_subp_attrs is subp_attrs
    assert primary_audio.language == "eng"
    assert primary_audio.bits_per_sample == 24
    assert secondary_audio.language == "fra"
    assert secondary_audio.bits_per_sample == 16
    assert (video.width, video.height) == (720, 480)
    assert title.chapters == [0.0, 10.0]
    assert title.duration_seconds == 120.0

    subtitles = title.subtitle_streams
    assert [stream.sub_id for stream in subtitles] == [0x20, 0x21]
    assert [stream.language for stream in subtitles] == ["und", "fre"]
    assert subtitles[1].is_forced is True


def test_apply_dvd_ifo_languages_ignores_read_failure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    title = Title(
        index=0,
        source_file=tmp_path / "movie.vob",
        name="Title",
        duration_seconds=90.0,
    )

    def fail_read():
        raise OSError("blocked")

    monkeypatch.setattr(Path, "read_bytes", lambda _self: fail_read())
    dvdbuild._apply_dvd_ifo_languages(title, tmp_path / "missing.IFO")

    assert title.dvd_ifo_data is None
    assert title.chapters == []
    assert title.duration_seconds == 90.0


def test_build_title_from_ifo_falls_back_on_invalid_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_path.write_bytes(b"invalid")
    first_vob = tmp_path / "VTS_01_1.VOB"
    fallback = Title(7, first_vob, "Fallback", 100.0)
    calls = []

    def create_title(titles, source, name, **_kwargs):
        calls.append((titles, source, name))
        return fallback

    monkeypatch.setattr(dvdbuild, "_create_title", create_title)

    result = dvdbuild._build_title_from_ifo(
        [], first_vob, ifo_path, [first_vob], 1, "Fallback"
    )

    assert result is fallback
    assert calls == [([], first_vob, "Fallback")]


def _patch_successful_ifo_title(monkeypatch: Any, streams):
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_pgc_info",
        lambda _data, _pgc_number: ([0.0, 60.0], 120.0),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_build_dvd_streams_from_ifo",
        lambda _data, _duration, _pgc_number: streams,
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_ifo_languages",
        lambda _data: ({0x80: "eng"}, {0x20: "und"}),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_parse_pgc_stream_languages",
        lambda _data, _pgc_number: ({}, {}),
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", lambda _data: {})
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", lambda _data: None)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", lambda _data: {})


def test_build_title_from_ifo_recovers_undeclared_subpicture(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ifo_data = bytearray(dvdifo._VTS_IFO_IDENT + bytes(0x300))
    # VTS_SPST_ATRT entry 1 (sub-stream 0x21): run-length, language "fr",
    # code extension 9 (forced).
    ifo_data[0x25C:0x262] = b"\x00\x00fr\x00\x09"
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_path.write_bytes(ifo_data)
    first_vob = tmp_path / "VTS_01_1.VOB"
    streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0),
        Stream(index=1, stream_type=StreamType.AUDIO, codec="ac3", sub_id=0x80),
    ]
    _patch_successful_ifo_title(monkeypatch, streams)
    scan_calls = []
    monkeypatch.setattr(
        dvdbuild,
        "_scan_vob_subpictures",
        lambda vobs, max_bytes, debug: (
            scan_calls.append((vobs, max_bytes, debug)) or {0x21: [(0, b"spu")]}
        ),
    )

    title = dvdbuild._build_title_from_ifo(
        [],
        first_vob,
        ifo_path,
        [first_vob],
        1,
        "Recovered",
        config=Config(min_duration=60),
    )

    assert title is not None
    assert title.name == "Recovered"
    assert title.duration_seconds == 120.0
    assert title.chapters == [0.0, 60.0]
    assert title.dvd_pgc_number is None
    assert title.dvd_audio_lang == {0x80: "eng"}
    assert title.dvd_sub_lang == {0x20: "und", 0x21: "fr"}
    assert scan_calls == [([first_vob], 128 * 1024 * 1024, False)]

    subtitle = title.streams[-1]
    assert subtitle.sub_id == 0x21
    assert subtitle.language == "fr"
    assert subtitle.is_forced is True
    assert subtitle.index == 2
    assert subtitle.type_index == 0
    assert title.dvd_subp_attrs[0x21].lang_code == "fr"


def test_build_title_from_ifo_skips_extra_scan_for_short_titles(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_path.write_bytes(dvdifo._VTS_IFO_IDENT + bytes(0x300))
    first_vob = tmp_path / "VTS_01_1.VOB"
    streams = [Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0)]
    _patch_successful_ifo_title(monkeypatch, streams)
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_pgc_info",
        lambda _data, _pgc_number: ([], 30.0),
    )
    monkeypatch.setattr(
        dvdbuild,
        "_scan_vob_subpictures",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError),
    )

    title = dvdbuild._build_title_from_ifo(
        [],
        first_vob,
        ifo_path,
        [first_vob],
        1,
        "Short",
        config=Config(min_duration=60),
    )

    assert title is not None
    assert title.duration_seconds == 30.0
    assert len(title.streams) == 1


def test_undeclared_subpicture_uses_und_for_invalid_language() -> None:
    ifo_data = bytearray(dvdifo._VTS_IFO_IDENT + bytes(0x300))
    ifo_data[0x25C:0x262] = b"\x00\x00\x00!\x00\x00"
    title = Title(
        index=0,
        source_file=Path("movie.vob"),
        name="Movie",
        duration_seconds=120.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0)]

    dvdbuild._append_undeclared_subpicture(
        title,
        bytes(ifo_data),
        0x21,
        stream_index=1,
        type_index=0,
        declared_count=0,
    )

    subtitle = title.streams[-1]
    assert subtitle.language == "und"
    assert subtitle.is_forced is False
    assert 0x21 not in title.dvd_sub_lang
    assert 0x21 not in title.dvd_subp_attrs


def test_undeclared_subpicture_phase_survives_malformed_ifo(monkeypatch: Any) -> None:
    title = Title(
        index=0,
        source_file=Path("movie.vob"),
        name="Movie",
        duration_seconds=120.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0)]
    monkeypatch.setattr(
        dvdbuild,
        "_scan_vob_subpictures",
        lambda *_args, **_kwargs: {0x21: [(0, b"spu")]},
    )
    monkeypatch.setattr(
        dvdbuild,
        "_read_u16",
        lambda _data, _offset: (_ for _ in ()).throw(IndexError("short IFO")),
    )

    dvdbuild._append_undeclared_dvd_subpictures(
        title,
        dvdifo._VTS_IFO_IDENT,
        [Path("movie.vob")],
        120.0,
        Config(min_duration=60),
    )

    assert len(title.streams) == 1
