"""Characterization tests for DVD source orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import dvdifo
import pytest
import dvdbuild
from cc608 import CC608_CODEC_SRT
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


def _patch_title_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int | None, str | None]]:
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
        scan_cache: object | None = None,
    ) -> Title:
        calls.append((pgc_number, title_name))
        title = Title(len(titles), vob_parts[0], title_name or "", 100.0)
        title.append_clips = vob_parts[1:]
        return title

    monkeypatch.setattr(dvdbuild, "_build_title_from_ifo", build_title)
    return calls


def test_plan_dvd_pgc_titles_episode_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def detect_episode_pgcs(
        _data: bytes, _minimum: float
    ) -> tuple[list[int], int | None]:
        return ([2, 3], 5)

    def default_pgc_number(_data: bytes) -> int | None:
        return 2

    def enumerate_vts_pgcs(_data: bytes) -> list[tuple[int, int, float, int]]:
        return [
            (1, 0, 30.0, 1),
            (2, 0, 100.0, 1),
            (3, 0, 105.0, 1),
            (4, 0, 120.0, 1),
            (5, 0, 205.0, 1),
            (6, 0, 59.0, 1),
        ]

    monkeypatch.setattr(dvdbuild, "_detect_episode_pgcs", detect_episode_pgcs)
    monkeypatch.setattr(dvdbuild, "_default_pgc_number", default_pgc_number)
    monkeypatch.setattr(dvdifo, "_enumerate_vts_pgcs", enumerate_vts_pgcs)

    plan = dvdbuild._plan_dvd_pgc_titles(b"ifo")

    assert plan.episode_pgcs == [2, 3]
    assert plan.play_all_pgc == 5
    assert plan.default_pgc_num == 2
    # PGC 4 is substantial and outside the group (extra); PGC 5 is the
    # play-all chain; PGC 1 and 6 are below the 60s minimum duration.
    assert plan.extras == [4]
    assert plan.editions == []


def test_plan_dvd_pgc_titles_alternate_editions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def detect_episode_pgcs(
        _data: bytes, _minimum: float
    ) -> tuple[list[int], int | None]:
        return ([], None)

    def default_pgc_number(_data: bytes) -> int | None:
        return 1

    def find_alternate_edition_pgcs(
        _data: bytes, _minimum: float
    ) -> list[tuple[int, bool]]:
        return [(2, True), (3, False)]

    monkeypatch.setattr(dvdbuild, "_detect_episode_pgcs", detect_episode_pgcs)
    monkeypatch.setattr(dvdbuild, "_default_pgc_number", default_pgc_number)
    monkeypatch.setattr(
        dvdbuild, "_find_alternate_edition_pgcs", find_alternate_edition_pgcs
    )

    plan = dvdbuild._plan_dvd_pgc_titles(b"ifo")

    assert plan.episode_pgcs == []
    assert plan.play_all_pgc is None
    assert plan.default_pgc_num == 1
    assert plan.extras == []
    assert plan.editions == [(2, True), (3, False)]


def test_plan_dvd_pgc_titles_empty_ifo() -> None:
    assert dvdbuild._plan_dvd_pgc_titles(b"") == dvdbuild._DvdPgcPlan(
        [], None, None, [], []
    )


def test_scan_dvd_source_builds_episodes_play_all_and_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _make_dvd_source(tmp_path)
    vmg: VmgInfo = {
        "disc_name": "Test Disc",
        "barcode": "12345",
        "title_map": {7: (1, 1)},
    }

    def parse_vmg_ifo(_path: Path) -> VmgInfo:
        return vmg

    monkeypatch.setattr(dvdbuild, "_parse_vmg_ifo", parse_vmg_ifo)
    calls = _patch_title_builder(monkeypatch)
    plan = dvdbuild._DvdPgcPlan(
        episode_pgcs=[1, 2],
        play_all_pgc=3,
        default_pgc_num=1,
        extras=[4],
        editions=[],
    )

    def plan_dvd_pgc_titles(
        _ifo_bytes: bytes, _config: Config | None = None
    ) -> dvdbuild._DvdPgcPlan:
        return plan

    monkeypatch.setattr(dvdbuild, "_plan_dvd_pgc_titles", plan_dvd_pgc_titles)

    titles, metadata = dvdbuild._scan_dvd_source(source)

    assert metadata.name == "Test Disc"
    assert [title.name for title in titles] == [
        "Title 7 (VTS 1)",
        "Title 7 (VTS 1) - Episode 2",
        "Title 7 (VTS 1) - Play All",
        "Title 7 (VTS 1) - Extra",
    ]
    assert [title.episode_number for title in titles] == [1, 2, None, None]
    assert titles[2].play_all is True
    assert metadata.upc_ean == "12345"
    assert metadata.dvd_disc_id is not None
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _make_dvd_source(tmp_path)

    def parse_vmg_ifo(_path: Path) -> VmgInfo:
        return VmgInfo()

    monkeypatch.setattr(dvdbuild, "_parse_vmg_ifo", parse_vmg_ifo)
    _patch_title_builder(monkeypatch)
    plan = dvdbuild._DvdPgcPlan(
        episode_pgcs=[],
        play_all_pgc=None,
        default_pgc_num=1,
        extras=[],
        editions=[(2, True), (3, True)],
    )

    def plan_dvd_pgc_titles(
        _ifo_bytes: bytes, _config: Config | None = None
    ) -> dvdbuild._DvdPgcPlan:
        return plan

    monkeypatch.setattr(dvdbuild, "_plan_dvd_pgc_titles", plan_dvd_pgc_titles)

    titles, metadata = dvdbuild._scan_dvd_source(source)

    assert metadata.name is None
    assert [title.name for title in titles] == [
        "Title 1 - Edition 1",
        "Title 1 - Edition 2",
        "Title 1 - Edition 3",
    ]
    assert [title.disc_name for title in titles] == [None, None, None]
    assert [title.dvd_edition_label for title in titles] == [
        "Edition 1",
        "Edition 2",
        "Edition 3",
    ]


def test_build_dvd_streams_rejects_invalid_ifo() -> None:
    assert dvdbuild._build_dvd_streams_from_ifo(b"NOTDVDVTS\x00\x00", 100.0) == []


def test_build_dvd_streams_uses_pgc_streams_and_pgc_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)

    def get_pgc_stream_ids(
        _data: bytes, _pgc_number: int | None
    ) -> dvdifo.PgcStreamIds:
        return dvdifo.PgcStreamIds({0x80: 0x80, 0x81: 0x81}, {0x20: 0x20, 0x21: 0x21})

    def parse_vts_video_attrs(_data: bytes) -> dvdifo._IFOVideoAttrs | None:
        return dvdifo._IFOVideoAttrs(
            mpeg_version="MPEG-2",
            standard="NTSC",
            aspect_ratio="16:9",
            resolution=(720, 480),
            letterboxed=False,
            film_mode=False,
            cc_field_1=False,
            cc_field_2=False,
        )

    def parse_vts_ifo_languages(
        _data: bytes,
    ) -> tuple[dict[int, str], dict[int, str]]:
        return (
            {0x80: "und", 0x81: "fra"},
            {0x20: "und", 0x21: "eng"},
        )

    def parse_pgc_stream_languages(
        _data: bytes, _pgc_number: int | None
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({0x81: "eng"}, {0x21: "fre"})

    def parse_vts_audio_attrs(_data: bytes) -> dict[int, dvdifo._IFOAudioAttrs]:
        return {
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
        }

    def ifo_audio_title(_attrs: dvdifo._IFOAudioAttrs | None) -> str | None:
        return "DTS 5.1"

    def parse_vts_subp_attrs(
        _data: bytes,
    ) -> dict[int, dvdifo._IFOSubpictureAttrs]:
        return {
            0x21: dvdifo._IFOSubpictureAttrs(
                coding_mode="run-length",
                code_extension=9,
                lang_code="fre",
                is_hearing_impaired=True,
            )
        }

    monkeypatch.setattr(dvdbuild, "pgc_stream_ids", get_pgc_stream_ids)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", parse_vts_video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_ifo_languages", parse_vts_ifo_languages)
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", parse_pgc_stream_languages
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", parse_vts_audio_attrs)
    monkeypatch.setattr(dvdbuild, "_ifo_audio_title", ifo_audio_title)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", parse_vts_subp_attrs)

    streams = dvdbuild._build_dvd_streams_from_ifo(ifo_data, 100.0, 2)

    # The PGC's streams drive the listing; attributes and languages come
    # from each stream's attribute slot, PGC control overriding languages.
    assert [(stream.stream_type, stream.sub_id) for stream in streams] == [
        (StreamType.VIDEO, 0x1E0),
        (StreamType.AUDIO, 0x80),
        (StreamType.AUDIO, 0x81),
        (StreamType.SUBTITLE, 0x20),
        (StreamType.SUBTITLE, 0x21),
    ]
    video = streams[0]
    assert video.codec == "mpeg2video"
    assert (video.width, video.height) == (720, 480)
    assert video.sample_aspect_ratio == "1.185185"
    assert video.color_primaries == "smpte170m"
    assert video.color_transfer == "bt709"
    assert video.color_space == "smpte170m"

    first_audio, dts_audio = streams[1], streams[2]
    assert first_audio.language == "und"
    assert first_audio.codec == "ac3"
    assert first_audio.channels == 2
    assert first_audio.type_index == 0
    assert dts_audio.codec == "dts"
    assert dts_audio.language == "eng"
    assert dts_audio.channels == 6
    assert dts_audio.sample_rate == "48000"
    assert dts_audio.bits_per_sample == 24
    assert dts_audio.title == "DTS 5.1"
    assert dts_audio.is_commentary is True
    assert dts_audio.type_index == 1

    plain_subtitle, forced_subtitle = streams[3], streams[4]
    assert plain_subtitle.codec == "dvd_subtitle"
    assert plain_subtitle.language == "und"
    assert plain_subtitle.type_index == 0
    assert forced_subtitle.language == "fre"
    assert forced_subtitle.is_forced is True
    assert forced_subtitle.is_hearing_impaired is True
    assert forced_subtitle.type_index == 1


def test_build_dvd_streams_fall_back_to_vts_language_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without PGC stream control, the attribute tables list the streams."""
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)

    def get_pgc_stream_ids(_data: bytes, _pgc: int | None) -> dvdifo.PgcStreamIds:
        return dvdifo.PgcStreamIds({}, {})

    def parse_vts_video_attrs(_data: bytes) -> dvdifo._IFOVideoAttrs | None:
        return None

    def parse_vts_ifo_languages(
        _data: bytes,
    ) -> tuple[dict[int, str], dict[int, str]]:
        return (
            {0x81: "eng", 0x80: "und"},
            {0x21: "fre", 0x20: "und"},
        )

    def parse_pgc_stream_languages(
        _data: bytes, _pgc: int | None
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({}, {})

    monkeypatch.setattr(dvdbuild, "pgc_stream_ids", get_pgc_stream_ids)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", parse_vts_video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_ifo_languages", parse_vts_ifo_languages)
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", parse_pgc_stream_languages
    )

    def parse_vts_audio_attrs(_data: bytes) -> dict[int, dvdifo._IFOAudioAttrs]:
        return {}

    def parse_vts_subp_attrs(
        _data: bytes,
    ) -> dict[int, dvdifo._IFOSubpictureAttrs]:
        return {}

    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", parse_vts_audio_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", parse_vts_subp_attrs)

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


def test_build_dvd_streams_list_pgc_streams_without_attribute_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PGC's streams are listed even when the attribute tables are empty."""
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)

    def get_pgc_stream_ids(_data: bytes, _pgc: int | None) -> dvdifo.PgcStreamIds:
        return dvdifo.PgcStreamIds({0x81: 0x81}, {0x21: 0x21})

    def parse_vts_video_attrs(_data: bytes) -> dvdifo._IFOVideoAttrs | None:
        return None

    def parse_vts_ifo_languages(
        _data: bytes,
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({}, {})

    def parse_pgc_stream_languages(
        _data: bytes, _pgc: int | None
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({}, {})

    monkeypatch.setattr(dvdbuild, "pgc_stream_ids", get_pgc_stream_ids)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", parse_vts_video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_ifo_languages", parse_vts_ifo_languages)
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", parse_pgc_stream_languages
    )

    def parse_vts_audio_attrs(_data: bytes) -> dict[int, dvdifo._IFOAudioAttrs]:
        return {}

    def parse_vts_subp_attrs(
        _data: bytes,
    ) -> dict[int, dvdifo._IFOSubpictureAttrs]:
        return {}

    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", parse_vts_audio_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", parse_vts_subp_attrs)

    streams = dvdbuild._build_dvd_streams_from_ifo(ifo_data, 100.0)

    assert [stream.sub_id for stream in streams] == [0x1E0, 0x81, 0x21]


def test_build_dvd_streams_label_a_stream_from_its_attribute_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Physical stream 0x80 played through slot 4 takes slot 4's attributes.

    Cats Don't Dance's trailers do this: slot 4 is English 5.1 and points
    at the feature's physical stream 0.
    """
    ifo_data = dvdifo._VTS_IFO_IDENT + bytes(100)
    english_51 = dvdifo._IFOAudioAttrs(
        codec="AC3",
        channels=6,
        sample_rate="48 kHz",
        quantization="DRC",
        bits_per_sample=None,
        dsur=False,
        code_extension=1,
        lang_code="en",
    )
    monkeypatch.setattr(
        dvdbuild,
        "pgc_stream_ids",
        lambda _d, _n: dvdifo.PgcStreamIds({0x80: 0x84}, {0x23: 0x20}),
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", lambda _d: None)
    monkeypatch.setattr(
        dvdbuild,
        "_parse_vts_ifo_languages",
        lambda _d: ({0x80: "fr", 0x84: "en"}, {0x20: "en", 0x23: "de"}),
    )
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", lambda _d, _n: ({}, {})
    )
    monkeypatch.setattr(
        dvdbuild, "_parse_vts_audio_attrs", lambda _d: {0x84: english_51}
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", lambda _d: {})

    _video, audio, subtitle = dvdbuild._build_dvd_streams_from_ifo(ifo_data, 100.0, 32)

    assert (audio.sub_id, audio.language, audio.channels) == (0x80, "en", 6)
    assert (subtitle.sub_id, subtitle.language) == (0x23, "en")


def test_scan_dvd_source_labels_plain_pgcs_not_editions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _make_dvd_source(tmp_path)

    def parse_vmg_ifo(_path: Path) -> VmgInfo:
        return VmgInfo()

    monkeypatch.setattr(dvdbuild, "_parse_vmg_ifo", parse_vmg_ifo)
    _patch_title_builder(monkeypatch)
    plan = dvdbuild._DvdPgcPlan(
        episode_pgcs=[],
        play_all_pgc=None,
        default_pgc_num=1,
        extras=[],
        editions=[(2, True), (3, False), (4, True)],
    )

    def plan_dvd_pgc_titles(
        _ifo_bytes: bytes, _config: Config | None = None
    ) -> dvdbuild._DvdPgcPlan:
        return plan

    monkeypatch.setattr(dvdbuild, "_plan_dvd_pgc_titles", plan_dvd_pgc_titles)

    titles, _metadata = dvdbuild._scan_dvd_source(source)

    # Every genuine re-cut is an edition, the default version included
    # ("Edition 1"); unrelated substantial PGCs (bonus features sharing the
    # VTS) get a neutral "PGC N" label.
    assert [title.name for title in titles] == [
        "Title 1 - Edition 1",
        "Title 1 - Edition 2",
        "Title 1 - PGC 3",
        "Title 1 - Edition 3",
    ]
    assert [title.dvd_edition_label for title in titles] == [
        "Edition 1",
        "Edition 2",
        "PGC 3",
        "Edition 3",
    ]


def test_apply_dvd_ifo_languages_updates_streams_and_timing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    def parse_vts_ifo_languages(
        _data: bytes,
    ) -> tuple[dict[int, str], dict[int, str]]:
        return (
            {0x80: "und", 0x81: "fra"},
            {0x20: "und", 0x21: "eng"},
        )

    def parse_pgc_stream_languages(
        _data: bytes, _pgc_number: int | None = None
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({0x80: "eng"}, {0x21: "fre"})

    def parse_vts_audio_attrs(_data: bytes) -> dict[int, dvdifo._IFOAudioAttrs]:
        return audio_attrs

    def parse_vts_video_attrs(_data: bytes) -> dvdifo._IFOVideoAttrs | None:
        return video_attrs

    def parse_vts_subp_attrs(
        _data: bytes,
    ) -> dict[int, dvdifo._IFOSubpictureAttrs]:
        return subp_attrs

    def parse_vts_pgc_info(_data: bytes) -> tuple[list[float], float]:
        return ([0.0, 10.0], 120.0)

    monkeypatch.setattr(dvdbuild, "_parse_vts_ifo_languages", parse_vts_ifo_languages)
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", parse_pgc_stream_languages
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", parse_vts_audio_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", parse_vts_video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", parse_vts_subp_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_pgc_info", parse_vts_pgc_info)

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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    title = Title(
        index=0,
        source_file=tmp_path / "movie.vob",
        name="Title",
        duration_seconds=90.0,
    )

    def fail_read() -> NoReturn:
        raise OSError("blocked")

    def read_bytes(_self: Path) -> bytes:
        return fail_read()

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    dvdbuild._apply_dvd_ifo_languages(title, tmp_path / "missing.IFO")

    assert title.dvd_ifo_data is None
    assert title.chapters == []
    assert title.duration_seconds == 90.0


def test_build_title_from_ifo_falls_back_on_invalid_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_path.write_bytes(b"invalid")
    first_vob = tmp_path / "VTS_01_1.VOB"
    fallback = Title(7, first_vob, "Fallback", 100.0)
    calls: list[tuple[list[Title], Path, str]] = []

    def create_title(
        titles: list[Title], source: Path, name: str, **_kwargs: object
    ) -> Title | None:
        calls.append((titles, source, name))
        return fallback

    monkeypatch.setattr(dvdbuild, "_create_title", create_title)

    result = dvdbuild._build_title_from_ifo(
        [], first_vob, ifo_path, [first_vob], 1, "Fallback"
    )

    assert result is fallback
    assert calls == [([], first_vob, "Fallback")]


def _patch_successful_ifo_title(
    monkeypatch: pytest.MonkeyPatch, streams: list[Stream]
) -> None:
    def parse_vts_pgc_info(
        _data: bytes, _pgc_number: int | None
    ) -> tuple[list[float], float]:
        return ([0.0, 60.0], 120.0)

    def build_dvd_streams_from_ifo(
        _data: bytes, _duration: float, _pgc_number: int | None
    ) -> list[Stream]:
        return streams

    def parse_vts_ifo_languages(
        _data: bytes,
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({0x80: "eng"}, {0x20: "und"})

    def parse_pgc_stream_languages(
        _data: bytes, _pgc_number: int | None
    ) -> tuple[dict[int, str], dict[int, str]]:
        return ({}, {})

    def parse_vts_audio_attrs(_data: bytes) -> dict[int, dvdifo._IFOAudioAttrs]:
        return {}

    def parse_vts_video_attrs(_data: bytes) -> dvdifo._IFOVideoAttrs | None:
        return None

    def parse_vts_subp_attrs(
        _data: bytes,
    ) -> dict[int, dvdifo._IFOSubpictureAttrs]:
        return {}

    monkeypatch.setattr(dvdbuild, "_parse_vts_pgc_info", parse_vts_pgc_info)
    monkeypatch.setattr(
        dvdbuild, "_build_dvd_streams_from_ifo", build_dvd_streams_from_ifo
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_ifo_languages", parse_vts_ifo_languages)
    monkeypatch.setattr(
        dvdbuild, "_parse_pgc_stream_languages", parse_pgc_stream_languages
    )
    monkeypatch.setattr(dvdbuild, "_parse_vts_audio_attrs", parse_vts_audio_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_video_attrs", parse_vts_video_attrs)
    monkeypatch.setattr(dvdbuild, "_parse_vts_subp_attrs", parse_vts_subp_attrs)


def test_build_title_from_ifo_recovers_undeclared_subpicture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    scan_calls: list[tuple[list[Path], int, bool]] = []

    def scan_vob_subpictures(
        vobs: list[Path], max_bytes: int, debug: bool, **_: object
    ) -> dict[int, list[tuple[int, bytes]]]:
        scan_calls.append((vobs, max_bytes, debug))
        return {0x21: [(0, b"spu")]}

    monkeypatch.setattr(dvdbuild, "_scan_vob_subpictures", scan_vob_subpictures)

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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ifo_path = tmp_path / "VTS_01_0.IFO"
    ifo_path.write_bytes(dvdifo._VTS_IFO_IDENT + bytes(0x300))
    first_vob = tmp_path / "VTS_01_1.VOB"
    streams = [Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0)]
    _patch_successful_ifo_title(monkeypatch, streams)

    def parse_vts_pgc_info(
        _data: bytes, _pgc_number: int | None
    ) -> tuple[list[float], float]:
        return ([], 30.0)

    def scan_vob_subpictures(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError

    monkeypatch.setattr(dvdbuild, "_parse_vts_pgc_info", parse_vts_pgc_info)
    monkeypatch.setattr(dvdbuild, "_scan_vob_subpictures", scan_vob_subpictures)

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


def test_undeclared_subpicture_phase_survives_malformed_ifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = Title(
        index=0,
        source_file=Path("movie.vob"),
        name="Movie",
        duration_seconds=120.0,
    )
    title.streams = [Stream(index=0, stream_type=StreamType.VIDEO, sub_id=0x1E0)]

    def scan_vob_subpictures(
        *_args: object, **_kwargs: object
    ) -> dict[int, list[tuple[int, bytes]]]:
        return {0x21: [(0, b"spu")]}

    def read_u16(_data: bytes, _offset: int) -> NoReturn:
        raise IndexError("short IFO")

    monkeypatch.setattr(dvdbuild, "_scan_vob_subpictures", scan_vob_subpictures)
    monkeypatch.setattr(dvdbuild, "_read_u16", read_u16)

    dvdbuild._append_undeclared_dvd_subpictures(
        title,
        dvdifo._VTS_IFO_IDENT,
        [Path("movie.vob")],
        120.0,
        Config(min_duration=60),
    )

    assert len(title.streams) == 1


def test_append_dvd_closed_captions_respects_config_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CC text track is opt-out via Config.cc608_srt (--no-cc-srt)."""
    vob = tmp_path / "VTS_01_1.VOB"
    vob.write_bytes(b"packets")

    def has_cc608_data(_path: Path) -> bool:
        return True

    monkeypatch.setattr(dvdbuild, "_has_cc608_data", has_cc608_data)

    def make_title() -> Title:
        title = Title(0, vob, "Title 1", 100.0)
        title.dvd_video_attrs = dvdifo._IFOVideoAttrs(
            mpeg_version="MPEG-2",
            standard="NTSC",
            aspect_ratio="16:9",
            resolution=(720, 480),
            letterboxed=False,
            film_mode=False,
            cc_field_1=True,
            cc_field_2=False,
        )
        title.streams = [
            Stream(index=0, stream_type=StreamType.VIDEO, codec="mpeg2video"),
            Stream(index=1, stream_type=StreamType.AUDIO, codec="ac3", language="en"),
        ]
        return title

    enabled = make_title()
    dvdbuild._append_dvd_closed_captions(
        enabled, [vob], config=Config(extract_cc608=True)
    )
    assert [stream.codec for stream in enabled.subtitle_streams] == [CC608_CODEC_SRT]
    cc_stream = enabled.subtitle_streams[0]
    assert cc_stream.is_hearing_impaired is True
    assert cc_stream.language == "en"

    disabled = make_title()
    dvdbuild._append_dvd_closed_captions(
        disabled, [vob], config=Config(extract_cc608=False)
    )
    assert disabled.subtitle_streams == []


def test_label_cross_vts_episodes_labels_one_per_vts_series(
    tmp_path: Path,
) -> None:
    """One-episode-per-VTS authoring (Tales from the Cryptkeeper S1:
    seven VTSs of ~21-minute episodes) is labelled by the disc-level pass."""
    titles: list[Title] = []
    for title_id, duration in (
        (1, 16.0),
        (2, 25.6),
        (3, 1254.0),
        (4, 1253.4),
        (5, 1254.2),
        (6, 1252.3),
        (7, 1253.1),
        (8, 1252.2),
        (9, 1257.1),
        (10, 0.4),
    ):
        title = Title(len(titles), tmp_path / "v.vob", f"Title {title_id}", duration)
        title.dvd_title_id = title_id
        titles.append(title)

    dvdbuild._label_cross_vts_episodes(titles, config=Config(min_duration=60))

    assert {t.dvd_title_id: t.episode_number for t in titles} == {
        1: None,
        2: None,
        3: 1,
        4: 2,
        5: 3,
        6: 4,
        7: 5,
        8: 6,
        9: 7,
        10: None,
    }


def test_label_cross_vts_episodes_requires_three_titles(tmp_path: Path) -> None:
    """Two similar-duration titles are the classic widescreen/fullscreen
    pair of one movie, not a series."""
    titles: list[Title] = []
    for title_id, duration in ((1, 5700.0), (2, 5710.0)):
        title = Title(len(titles), tmp_path / "v.vob", f"Title {title_id}", duration)
        title.dvd_title_id = title_id
        titles.append(title)

    dvdbuild._label_cross_vts_episodes(titles, config=Config(min_duration=60))

    assert all(title.episode_number is None for title in titles)


def test_label_cross_vts_episodes_skips_labelled_playall_and_editions(
    tmp_path: Path,
) -> None:
    """Within-VTS episodes, play-all chains, and alternate editions never
    join a cross-VTS cluster."""
    titles: list[Title] = []
    specs: list[tuple[int, float, dict[str, object]]] = [
        (1, 1460.0, {"episode_number": 1}),
        (2, 1461.0, {"episode_number": 2}),
        (3, 8800.0, {"play_all": True}),
        (4, 2000.0, {"dvd_edition_label": "Edition 2"}),
        (5, 1254.0, {}),
        (6, 1253.0, {}),
        (7, 1252.0, {}),
    ]
    for title_id, duration, flags in specs:
        title = Title(len(titles), tmp_path / "v.vob", f"Title {title_id}", duration)
        title.dvd_title_id = title_id
        for field, value in flags.items():
            setattr(title, field, value)
        titles.append(title)

    dvdbuild._label_cross_vts_episodes(titles, config=Config(min_duration=60))

    assert {t.dvd_title_id: t.episode_number for t in titles} == {
        1: 1,  # within-VTS labels untouched
        2: 2,
        3: None,
        4: None,
        5: 1,
        6: 2,
        7: 3,
    }


def test_label_cross_vts_episodes_suppressed_when_dwarfed(tmp_path: Path) -> None:
    """A cluster of short titles beside much longer content is extras, not
    episodes (Peanuts: three ~100s intro clips in their own VTSs beside
    24-minute specials)."""
    titles: list[Title] = []
    for title_id, duration in (
        (1, 8800.0),
        (2, 105.3),
        (3, 100.5),
        (4, 86.2),
    ):
        title = Title(len(titles), tmp_path / "v.vob", f"Title {title_id}", duration)
        title.dvd_title_id = title_id
        titles.append(title)

    dvdbuild._label_cross_vts_episodes(titles, config=Config(min_duration=60))

    assert all(title.episode_number is None for title in titles)


def test_demote_dwarfed_episode_groups_strips_movie_disc_labels(
    tmp_path: Path,
) -> None:
    """Treasure Planet's VTS 11: seven ~2-minute featurettes + compilation
    look like an anthology within the VTS, but the disc's 95-minute movie
    dwarfs them — bonus content, not episodes."""
    titles: list[Title] = []
    for index, duration, episode in (
        (0, 5718.0, None),  # the movie
        (1, 730.0, None),  # the featurettes' compilation (play-all)
        (2, 141.0, 7),
        (3, 139.0, 6),
        (4, 103.0, 2),
        (5, 96.0, 1),
    ):
        title = Title(index, tmp_path / "v.vob", f"Title {index}", duration)
        title.dvd_title_id = index
        title.episode_number = episode
        if index == 1:
            title.play_all = True
        titles.append(title)

    dvdbuild._demote_dwarfed_episode_groups(titles, config=Config(min_duration=60))

    assert all(title.episode_number is None for title in titles)


def test_demote_dwarfed_episode_groups_keeps_series_discs(tmp_path: Path) -> None:
    """Series discs pass: their longest non-play-all titles are episodes or
    shorter extras (Superman: a 13-minute bonus beside 19-minute episodes)."""
    titles: list[Title] = []
    for index, duration, episode, part in (
        (0, 1146.0, 1, "a"),
        (1, 286.0, 1, "b"),
        (2, 811.0, None, None),  # bonus featurette, shorter than episodes
        (3, 10018.0, None, None),  # play-all
    ):
        title = Title(index, tmp_path / "v.vob", f"Title {index}", duration)
        title.dvd_title_id = index
        title.episode_number = episode
        title.dvd_episode_part = part
        if index == 3:
            title.play_all = True
        titles.append(title)

    dvdbuild._demote_dwarfed_episode_groups(titles, config=Config(min_duration=60))

    assert [title.episode_number for title in titles] == [1, 1, None, None]
    assert titles[0].dvd_episode_part == "a"
    assert titles[1].dvd_episode_part == "b"
