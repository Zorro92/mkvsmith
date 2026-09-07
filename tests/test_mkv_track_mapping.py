"""Tests for mapping scanned streams to mkvmerge track IDs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import disc_reader
import mkv
from mkv import MappedStream, _map_streams_to_ident_tracks, _track_filter_options
import models
from models import StreamType
from models import Stream, Title
from tagger import ArtAttachment, MovieMetadata


def test_streams_match_by_source_id_and_refine_codec() -> None:
    streams = [
        Stream(
            index=0,
            stream_type=StreamType.VIDEO,
            codec="h264",
            type_index=0,
            pid=4113,
        ),
        Stream(
            index=1,
            stream_type=StreamType.AUDIO,
            codec="dts_hd_hr",
            type_index=0,
            pid=4352,
        ),
    ]
    ident_tracks = [
        {
            "id": 0,
            "type": "video",
            "codec": "AVC/H.264",
            "properties": {"number": 4113},
        },
        {
            "id": 1,
            "type": "audio",
            "codec": "DTS-HD Master Audio",
            "properties": {"number": 4352, "audio_channels": 8},
        },
    ]

    mapped = _map_streams_to_ident_tracks(streams, ident_tracks)

    assert [(entry["input_id"], entry["type"]) for entry in mapped] == [
        (0, "video"),
        (1, "audio"),
    ]
    assert streams[1].codec == "dts_hd_ma"
    assert mapped[1]["ident_channels"] == 8


def test_unmatched_streams_use_positional_or_negative_ids() -> None:
    subtitle = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        codec="hdmv_pgs_subtitle",
        type_index=0,
        sub_id=0x20,
    )
    audio = Stream(
        index=1,
        stream_type=StreamType.AUDIO,
        codec="ac3",
        type_index=1,
        pid=9999,
    )
    ident_tracks = [
        {"id": 0, "type": "subtitles", "codec": "PGS", "properties": {}},
        {"id": 1, "type": "audio", "codec": "AC-3", "properties": {}},
    ]

    mapped = _map_streams_to_ident_tracks([audio, subtitle], ident_tracks)

    assert [(entry["input_id"], entry["type"]) for entry in mapped] == [
        (-1, "audio"),
        (0, "subtitles"),
    ]


def test_dvd_audio_fallback_omits_audio_filter() -> None:
    title = Title(
        index=0,
        source_file=Path("source.vob"),
        name="Source",
        duration_seconds=100,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.AUDIO, codec="ac3"),
        Stream(index=1, stream_type=StreamType.AUDIO, codec="ac3"),
    ]
    title.dvd_ifo_data = b"IFO data"
    ident_tracks = [{"id": 0, "type": "audio", "codec": "AC-3", "properties": {}}]
    mapped = _map_streams_to_ident_tracks(title.audio_streams[:1], ident_tracks)

    assert _track_filter_options(ident_tracks, mapped, title) == []


def test_dvd_subtitle_fallback_passes_ifo_attributes(
    monkeypatch, tmp_path: Path
) -> None:
    title = Title(
        index=0,
        source_file=tmp_path / "source.vob",
        name="Source",
        duration_seconds=120,
    )
    title.dvd_ifo_data = b"IFO data"
    subtitle = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="en",
        is_forced=True,
        type_index=0,
        sub_id=0x20,
    )
    mapped: list[MappedStream] = [
        {
            "input_id": -1,
            "type": "subtitle",
            "stream": subtitle,
            "ident_channels": None,
        }
    ]
    calls = []

    def extract(
        inputs,
        language_by_id,
        forced_by_id,
        *,
        ifo_palette,
        vobu_parts,
        vobu_part_sizes,
        total_duration,
        temp_files,
        debug,
    ):
        calls.append(
            (
                inputs,
                language_by_id,
                forced_by_id,
                ifo_palette,
                vobu_parts,
                vobu_part_sizes,
                total_duration,
                temp_files,
                debug,
            )
        )
        return tmp_path / "subs.idx", [{"id": 0, "type": "subtitles"}]

    monkeypatch.setattr(
        mkv, "_extract_dvd_ifo_palette", lambda _data, _pgc: [(1, 2, 3)]
    )
    monkeypatch.setattr(mkv, "_extract_dvd_vobsubs", extract)

    temp_files: list[Path] = []

    result = mkv._extract_dvd_subtitle_fallback(
        title, mapped, [title.source_file], None, None, temp_files
    )

    assert result[0] == tmp_path / "subs.idx"
    assert result[1] == [{"id": 0, "type": "subtitles"}]
    assert result[2] == [subtitle]
    assert calls == [
        (
            [title.source_file],
            {0x20: "en"},
            {0x20: True},
            [(1, 2, 3)],
            None,
            None,
            120,
            [],
            False,
        )
    ]


def test_create_chapters_file_filters_trailing_chapter(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    temp_files: list[Path] = []
    title = Title(
        index=0,
        source_file=tmp_path / "source.m2ts",
        name="Source",
        duration_seconds=100.0,
    )
    title.chapters = [0.0, 50.0, 100.0]
    cleanup: list[Path] = []

    chapters_file = mkv._create_chapters_file(title, cleanup, temp_files)

    assert chapters_file is not None
    assert cleanup == [chapters_file]
    assert chapters_file.is_file()
    root = ET.parse(chapters_file).getroot()
    assert len(root.findall(".//ChapterAtom")) == 2


def test_prepare_dvd_inputs_extracts_contiguous_range(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    temp_files: list[Path] = []
    source = tmp_path / "source.vob"
    source.write_bytes(b"0123456789")
    title = Title(
        index=0,
        source_file=source,
        name="Source",
        duration_seconds=100.0,
    )
    monkeypatch.setattr(mkv, "_dvd_main_content_range", lambda _inputs: (2, 8))
    calls: list[tuple[list[Path], int, int, Path]] = []

    def extract(inputs: list[Path], start: int, end: int, output: Path) -> Path:
        calls.append((inputs, start, end, output))
        output.write_bytes(b"trimmed")
        return output

    monkeypatch.setattr(mkv, "_extract_concat_range", extract)
    cleanup: list[Path] = []

    result = mkv._prepare_dvd_inputs(title, [source], None, cleanup, temp_files)

    assert calls == [([source], 2, 8, result.inputs[0])]
    assert result.inputs[0].read_bytes() == b"trimmed"
    assert result.vobu_parts is None
    assert result.vobu_part_sizes is None
    assert cleanup == [result.inputs[0]]
    assert temp_files == cleanup


def test_prepare_dvd_inputs_concatenates_vobu_runs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    temp_files: list[Path] = []
    source = tmp_path / "source.vob"
    source.write_bytes(b"0123456789")
    title = Title(
        index=0,
        source_file=source,
        name="Source",
        duration_seconds=100.0,
    )
    title.dvd_ifo_data = b"IFO data"
    ranges = [(0, 2), (4, 6)]
    monkeypatch.setattr(mkv, "_lookup_main_feature_range", lambda *_args: (0, 8))
    monkeypatch.setattr(mkv, "_parse_vts_vobu_admap", lambda _data: [0, 2, 4, 6])
    monkeypatch.setattr(mkv, "_build_main_edition_vobu_ranges", lambda *_args: ranges)
    calls: list[tuple[int, int]] = []

    def extract(_inputs: list[Path], start: int, end: int, output: Path) -> Path:
        calls.append((start, end))
        output.write_bytes(f"{start}:{end}".encode())
        return output

    monkeypatch.setattr(mkv, "_extract_concat_range", extract)
    cleanup: list[Path] = []

    result = mkv._prepare_dvd_inputs(title, [source], None, cleanup, temp_files)

    assert calls == [(0, 2), (4, 6)]
    assert result.inputs[0].read_bytes() == b"0:24:6"
    assert result.vobu_parts is not None
    assert len(result.vobu_parts) == 2
    assert result.vobu_part_sizes == [2, 2]
    assert cleanup == [result.inputs[0], *result.vobu_parts]
    assert temp_files == cleanup


def test_is_dvd_vob_input_requires_mpeg2_vob() -> None:
    video = Stream(index=0, stream_type=StreamType.VIDEO, codec="mpeg2video")
    vob = Path("movie.vob")
    m2ts = Path("movie.m2ts")

    assert mkv._is_dvd_vob_input([video], [vob])
    assert not mkv._is_dvd_vob_input([video], [m2ts])
    assert not mkv._is_dvd_vob_input([], [vob])


def test_append_input_files_repeats_track_filters() -> None:
    cmd = ["mkvmerge"]
    inputs = [Path("a.m2ts"), Path("b.m2ts"), Path("c.m2ts")]
    filters = ["--video-tracks", "0"]

    mkv._append_input_files(cmd, inputs, filters)

    assert cmd == [
        "mkvmerge",
        "--append-mode",
        "track",
        "a.m2ts",
        "+",
        "--video-tracks",
        "0",
        "b.m2ts",
        "+",
        "--video-tracks",
        "0",
        "c.m2ts",
    ]


def test_dvd_subtitle_fallback_options_apply_idx_track_attributes() -> None:
    subtitle = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="en",
        title="English SDH",
        is_forced=True,
        is_hearing_impaired=True,
        is_commentary=True,
        type_index=0,
        sub_id=0x20,
    )

    options = mkv._dvd_subtitle_fallback_options(
        [subtitle], [{"id": 0}], [{"id": 0, "type": "subtitles"}]
    )

    assert options == [
        "--language",
        "0:en",
        "--default-track",
        "0:no",
        "--forced-track",
        "0:yes",
        "--hearing-impaired-flag",
        "0:yes",
        "--commentary-flag",
        "0:yes",
        "--track-name",
        "0:English SDH",
    ]


def test_dvd_subtitle_fallback_options_require_ident_tracks() -> None:
    subtitle = Stream(index=0, stream_type=StreamType.SUBTITLE, language="en")

    assert mkv._dvd_subtitle_fallback_options([subtitle], [{"id": 0}], []) == []


def test_build_mkvmerge_command_orders_metadata_inputs_and_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    temp_files: list[Path] = []
    source = tmp_path / "movie.m2ts"
    source.write_bytes(b"video")
    title = Title(
        index=3,
        source_file=source,
        name="Movie",
        duration_seconds=100.0,
    )
    title.chapters = [0.0, 50.0, 100.0]
    metadata = MovieMetadata(title="Tagged Movie")
    art_path = tmp_path / "poster.jpg"
    art_path.write_bytes(b"image")
    subtitle_fallback = tmp_path / "subs.idx"
    subtitle_fallback.write_bytes(b"subtitles")
    subtitle = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="en",
        type_index=0,
        sub_id=0x20,
    )
    cleanup: list[Path] = []

    cmd = mkv._build_mkvmerge_command(
        title,
        tmp_path / "movie.mkv",
        [source, tmp_path / "append.m2ts"],
        [],
        [],
        metadata,
        [
            {
                "path": art_path,
                "mime": "image/jpeg",
                "filename": "poster.jpg",
                "label": "Poster",
            }
        ],
        subtitle_fallback,
        [{"id": 0, "type": "subtitles"}],
        [subtitle],
        cleanup,
        temp_files,
    )

    assert cmd[:5] == [
        "mkvmerge",
        "-o",
        str(tmp_path / "movie.mkv"),
        "--title",
        "Tagged Movie",
    ]
    assert cmd[cmd.index("--chapters") + 1] == str(cleanup[0])
    assert cmd[cmd.index("--global-tags") + 1] == str(cleanup[1])
    attachment_start = cmd.index("--attachment-name")
    assert cmd[attachment_start : attachment_start + 7] == [
        "--attachment-name",
        "poster.jpg",
        "--attachment-mime-type",
        "image/jpeg",
        "--attachment-description",
        "Poster",
        str(art_path),
    ]
    assert cmd[-6:] == [
        "--append-mode",
        "track",
        str(source),
        "+",
        str(tmp_path / "append.m2ts"),
        str(subtitle_fallback),
    ]
    assert cleanup == [cleanup[0], cleanup[1], subtitle_fallback]
    assert temp_files == cleanup[:2]


def test_output_file_for_title_sanitizes_name(tmp_path: Path) -> None:
    title = Title(
        index=7,
        source_file=tmp_path / "source.mkv",
        name='Movie: A "Test*"?  ©',
        duration_seconds=100.0,
    )

    assert mkv._output_file_for_title(tmp_path, title) == (
        tmp_path / "Movie_ A _Test____t07.mkv"
    )


def test_prepare_inputs_preserves_folder_inputs(tmp_path: Path) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    source = tmp_path / "movie.m2ts"
    append = tmp_path / "append.m2ts"
    title = Title(
        index=0,
        source_file=source,
        name="Movie",
        duration_seconds=100.0,
        estimated_size_bytes=123,
    )
    title.append_clips = [append]
    video = Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")

    plan = creator._prepare_inputs(title, [video])

    assert plan.inputs == [source, append]
    assert plan.cleanup == []
    assert plan.is_dvd_vob is False
    assert plan.vobu_parts is None
    assert plan.vobu_part_sizes is None


def test_prepare_inputs_uses_iso_and_injected_registries(
    monkeypatch, tmp_path: Path
) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    source = tmp_path / "movie.iso"
    title = Title(
        index=0,
        source_file=source,
        name="Movie",
        duration_seconds=100.0,
        estimated_size_bytes=456,
    )
    title.iso_internal_paths = ["BDMV/STREAM/00000.m2ts"]
    video = Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")
    extracted = tmp_path / "extracted.m2ts"
    calls = {}

    def extract_full(_source, internals, *, temp_base, temp_dirs, symlinks):
        calls["temp_base"] = temp_base
        calls["temp_dirs"] = temp_dirs
        calls["symlinks"] = symlinks
        return [extracted]

    monkeypatch.setattr(
        disc_reader, "temp_base_for_title", lambda _size, _config: tmp_path / "spill"
    )
    monkeypatch.setattr(disc_reader, "_extract_full_for_muxing", extract_full)

    plan = creator._prepare_inputs(title, [video])

    assert plan.inputs == [extracted]
    assert plan.cleanup == [extracted]
    assert plan.is_dvd_vob is False
    assert calls == {
        "temp_base": tmp_path / "spill",
        "temp_dirs": runtime_state.cleanup.temp_dirs,
        "symlinks": runtime_state.cleanup.symlinks,
    }


def test_prepare_tracks_dispatches_dvd_subtitle_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    title = Title(
        index=0,
        source_file=tmp_path / "movie.vob",
        name="Movie",
        duration_seconds=100.0,
    )
    video = Stream(index=0, stream_type=StreamType.VIDEO, codec="mpeg2video")
    mapped: list[MappedStream] = [
        {
            "input_id": 0,
            "type": "video",
            "stream": video,
            "ident_channels": None,
        }
    ]
    fallback = tmp_path / "subs.idx"
    calls = []
    plan = mkv._MuxInputPlan(
        inputs=[title.source_file],
        cleanup=[],
        is_dvd_vob=True,
        vobu_parts=[tmp_path / "part-0.vob"],
        vobu_part_sizes=[20],
    )
    monkeypatch.setattr(mkv, "_identify_input_tracks", lambda _path: [])
    monkeypatch.setattr(
        mkv, "_map_streams_to_ident_tracks", lambda _streams, _tracks: mapped
    )

    def extract_fallback(
        _title,
        _mapped,
        inputs,
        vobu_parts,
        vobu_part_sizes,
        temp_files,
        debug,
    ):
        calls.append((inputs, vobu_parts, vobu_part_sizes, temp_files, debug))
        return fallback, [{"id": 0}], [video]

    monkeypatch.setattr(mkv, "_extract_dvd_subtitle_fallback", extract_fallback)

    prepared = creator._prepare_tracks(title, [video], plan)

    assert prepared.ident_tracks == []
    assert prepared.mapped == mapped
    assert prepared.subtitle_fallback == fallback
    assert prepared.fallback_tracks == [{"id": 0}]
    assert prepared.unmatched_ifo_subs == [video]
    assert calls == [
        (
            plan.inputs,
            plan.vobu_parts,
            plan.vobu_part_sizes,
            runtime_state.cleanup.temp_files,
            False,
        )
    ]


def test_validate_mux_result_reports_timeout_and_failure(tmp_path: Path) -> None:
    creator = mkv.MKVCreator(tmp_path)
    title = Title(
        index=0,
        source_file=tmp_path / "movie.mkv",
        name="Movie",
        duration_seconds=100.0,
    )
    streams = [Stream(index=0, stream_type=StreamType.VIDEO)]
    out_file = tmp_path / "movie_t00.mkv"
    out_file.write_bytes(b"partial")

    with pytest.raises(models.RipError, match="timed out"):
        creator._validate_mux_result(
            title,
            streams,
            ["mkvmerge"],
            out_file,
            returncode=0,
            output_text="",
            timed_out=True,
        )

    with pytest.raises(models.RipError, match=r"mkvmerge failed \(2\)"):
        creator._validate_mux_result(
            title,
            streams,
            ["mkvmerge"],
            out_file,
            returncode=2,
            output_text="failure",
            timed_out=False,
        )


def test_execute_mux_finalizes_output_and_metadata(monkeypatch, tmp_path: Path) -> None:
    runtime_state = models.RuntimeState()
    tag_options = models.TagOptions(save_xml=True)
    creator = mkv.MKVCreator(
        tmp_path,
        tag_opts=tag_options,
        runtime_state=runtime_state,
    )
    title = Title(
        index=0,
        source_file=tmp_path / "movie.mkv",
        name="Movie",
        duration_seconds=100.0,
    )
    streams = [Stream(index=0, stream_type=StreamType.VIDEO)]
    out_file = tmp_path / "movie_t00.mkv"
    metadata = MovieMetadata(title="Movie")
    art: list[ArtAttachment] = [
        {
            "path": tmp_path / "poster.jpg",
            "mime": "image/jpeg",
            "filename": "poster.jpg",
            "label": "Poster",
        }
    ]
    run_calls = []

    def run(command, label, duration, timeout=3600):
        run_calls.append((command, label, duration, timeout))
        out_file.write_bytes(b"matroska")
        return 1, "Warning: retained for tests", False

    monkeypatch.setattr(creator, "_run_mkvmerge", run)
    creator.logger.set_debug(True)

    try:
        result = creator._execute_mux(
            title,
            streams,
            ["mkvmerge"],
            out_file,
            metadata,
            art,
        )
    finally:
        runtime_state.active_processes.unregister_output(out_file)

    assert result == out_file
    assert result.read_bytes() == b"matroska"
    assert result.with_suffix(".xml").is_file()
    assert run_calls == [(["mkvmerge"], out_file.name, 100.0, 3600)]
    assert runtime_state.active_processes.output_files == []


def test_source_id_match_takes_priority_over_position() -> None:
    streams = [
        Stream(
            index=0,
            stream_type=StreamType.AUDIO,
            codec="ac3",
            type_index=0,
            pid=4352,
        ),
        Stream(
            index=1,
            stream_type=StreamType.AUDIO,
            codec="ac3",
            type_index=1,
        ),
    ]
    ident_tracks = [
        {
            "id": 0,
            "type": "audio",
            "codec": "AC-3",
            "properties": {"number": 4351},
        },
        {
            "id": 1,
            "type": "audio",
            "codec": "AC-3",
            "properties": {"number": 4352},
        },
    ]

    mapped = _map_streams_to_ident_tracks(streams, ident_tracks)

    assert [entry["input_id"] for entry in mapped] == [1, 1]
    assert [entry["stream"] for entry in mapped] == streams
    assert mkv._ident_tracks_by_position(ident_tracks) == {
        ("audio", 0): ident_tracks[0],
        ("audio", 1): ident_tracks[1],
    }


def test_append_track_options_apply_state_and_synthesized_audio_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = Stream(
        index=0,
        stream_type=StreamType.AUDIO,
        codec="ac3",
        language="es",
        is_forced=True,
        is_hearing_impaired=True,
        is_commentary=True,
    )
    entry: MappedStream = {
        "input_id": 3,
        "type": "audio",
        "stream": stream,
        "ident_channels": 6,
    }
    monkeypatch.setattr(
        mkv, "_audio_title", lambda _stream, channels: f"Audio {channels}"
    )

    cmd: list[str] = []
    mkv._append_track_options(cmd, [entry])

    assert cmd == [
        "--language",
        "3:es",
        "--default-track",
        "3:no",
        "--forced-track",
        "3:yes",
        "--hearing-impaired-flag",
        "3:yes",
        "--commentary-flag",
        "3:yes",
        "--track-name",
        "3:Audio 6",
    ]


def test_append_track_options_apply_video_color_and_siting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = Stream(
        index=0,
        stream_type=StreamType.VIDEO,
        codec="mpeg2video",
        title="Main Video",
    )
    entry: MappedStream = {
        "input_id": 0,
        "type": "video",
        "stream": stream,
        "ident_channels": None,
    }
    monkeypatch.setattr(
        mkv,
        "_resolve_video_color",
        lambda _stream: ("bt709", "bt709", "bt709", "limited"),
    )
    monkeypatch.setattr(
        mkv, "_chroma_siting_for_codec", lambda codec: f"{codec}-siting"
    )

    cmd: list[str] = []
    mkv._append_track_options(cmd, [entry])

    assert cmd == [
        "--language",
        "0:und",
        "--default-track",
        "0:no",
        "--color-primaries",
        "0:1",
        "--color-transfer-characteristics",
        "0:1",
        "--color-matrix-coefficients",
        "0:1",
        "--color-range",
        "0:1",
        "--chroma-siting",
        "0:mpeg2video-siting",
        "--track-name",
        "0:Main Video",
    ]


def test_append_track_options_skips_unmatched_streams() -> None:
    stream = Stream(index=0, stream_type=StreamType.VIDEO)
    entry: MappedStream = {
        "input_id": -1,
        "type": "video",
        "stream": stream,
        "ident_channels": None,
    }
    cmd = ["mkvmerge"]

    mkv._append_track_options(cmd, [entry])

    assert cmd == ["mkvmerge"]


def test_parse_and_read_mkvmerge_progress_across_chunks() -> None:
    assert mkv._parse_mkvmerge_progress("Progress: 999%") == 100
    assert mkv._parse_mkvmerge_progress("no progress") is None

    progress_prefix = "Progress: 4"
    first_chunk = "x" * (512 - len(progress_prefix)) + progress_prefix
    second_chunk = "2%\n" + "y" * 64
    third_chunk = "Progress: 999%\n"
    stdout = _FakeTextStdout([first_chunk, second_chunk, third_chunk])
    progress: list[int] = []
    stdout_for_reader: Any = stdout

    output = mkv._read_mkvmerge_output(stdout_for_reader, progress.append)

    assert output == first_chunk + second_chunk + third_chunk
    assert progress == [42, 100]


def test_kill_mkvmerge_process_falls_back_to_current_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killpg_calls: list[tuple[int, int]] = []

    class _FakeProcess:
        pid = 123
        killed = False

        def kill(self) -> None:
            self.killed = True

    process = _FakeProcess()

    def killpg(process_id: int, sig: int) -> None:
        killpg_calls.append((process_id, sig))
        if len(killpg_calls) == 1:
            raise OSError(process_id)

    monkeypatch.setattr(mkv.os, "killpg", killpg)
    monkeypatch.setattr(mkv.os, "getpgid", lambda _pid: 432)

    process_for_kill: Any = process
    mkv._kill_mkvmerge_process(process_for_kill)

    assert killpg_calls == [
        (123, mkv.signal.SIGKILL),
        (432, mkv.signal.SIGKILL),
    ]
    assert process.killed is False


def test_mkvmerge_watchdog_records_timeout_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = mkv._MkvmergeTimeoutState()
    kill_calls: list[object] = []
    timers: list[_FakeTimer] = []

    class _FakeTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr(mkv.threading, "Timer", _FakeTimer)
    monkeypatch.setattr(
        mkv, "_kill_mkvmerge_process", lambda process: kill_calls.append(process)
    )
    process = object()

    process_for_watchdog: Any = process
    watchdog: Any = mkv._start_mkvmerge_watchdog(
        process_for_watchdog,
        state,
        timeout=17,
    )

    assert watchdog.interval == 17
    assert watchdog.daemon is True
    assert watchdog.started is True

    watchdog.function()

    assert state.timed_out is True
    assert kill_calls == [process]


class _FakeTextStdout:
    def __init__(self, chunks: list[str] | BaseException):
        self.chunks = chunks

    def read(self, _size: int) -> str:
        if isinstance(self.chunks, BaseException):
            raise self.chunks
        if not self.chunks:
            return ""
        return self.chunks.pop(0)


class _FakeMuxProcess:
    def __init__(self, stdout: _FakeTextStdout, returncode: int = 0):
        self.stdout = stdout
        self.pid = 975
        self.returncode = returncode
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return self.returncode


def test_run_mkvmerge_collects_output_and_unregisters_muxer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    process = _FakeMuxProcess(_FakeTextStdout(["Progress: 42%\nwarning\n"]))
    progress: list[int] = []
    watchdog_timeouts: list[int] = []
    monkeypatch.setattr(mkv.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        mkv,
        "_start_mkvmerge_watchdog",
        lambda _process, _state, timeout: (
            watchdog_timeouts.append(timeout),
            SimpleNamespace(cancel=lambda: None),
        )[1],
    )
    monkeypatch.setattr(
        creator,
        "_show_progress",
        lambda _label, percentage: progress.append(percentage),
    )

    result = creator._run_mkvmerge(["mkvmerge"], "movie.mkv", 100.0, timeout=17)

    assert result == (0, "Progress: 42%\nwarning\n", False)
    assert progress == [42]
    assert watchdog_timeouts == [17]
    assert process.waited is True
    assert runtime_state.active_processes.muxer_pgids == []


def test_run_mkvmerge_interrupt_kills_waits_and_finishes_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    process = _FakeMuxProcess(_FakeTextStdout(KeyboardInterrupt()))
    killed: list[object] = []
    monkeypatch.setattr(mkv.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        mkv,
        "_start_mkvmerge_watchdog",
        lambda _process, _state, timeout: SimpleNamespace(cancel=lambda: None),
    )
    monkeypatch.setattr(mkv, "_kill_mkvmerge_process", killed.append)
    runtime_state.active_processes.set_progress_active(True)

    with pytest.raises(KeyboardInterrupt):
        creator._run_mkvmerge(["mkvmerge"], "movie.mkv", 100.0)

    assert killed == [process]
    assert process.waited is True
    assert runtime_state.active_processes.muxer_pgids == []
    assert runtime_state.active_processes.progress_active is False
    assert capsys.readouterr().err == "\n"


def test_run_mkvmerge_kills_child_on_unexpected_reader_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = models.RuntimeState()
    creator = mkv.MKVCreator(tmp_path, runtime_state=runtime_state)
    process = _FakeMuxProcess(_FakeTextStdout(RuntimeError("pipe closed")))
    killed: list[object] = []
    monkeypatch.setattr(mkv.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        mkv,
        "_start_mkvmerge_watchdog",
        lambda _process, _state, timeout: SimpleNamespace(cancel=lambda: None),
    )
    monkeypatch.setattr(mkv, "_kill_mkvmerge_process", killed.append)

    with pytest.raises(RuntimeError, match="pipe closed"):
        creator._run_mkvmerge(["mkvmerge"], "movie.mkv", 100.0)

    assert killed == [process]
    assert process.waited is False
    assert runtime_state.active_processes.muxer_pgids == []


def make_stream_selection_title(tmp_path: Path) -> Title:
    title = Title(
        index=0,
        source_file=tmp_path / "movie.mkv",
        name="Movie",
        duration_seconds=100.0,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, type_index=0),
        Stream(
            index=1,
            stream_type=StreamType.AUDIO,
            language="eng",
            type_index=0,
        ),
        Stream(
            index=2,
            stream_type=StreamType.AUDIO,
            language="fre",
            type_index=1,
        ),
        Stream(
            index=3,
            stream_type=StreamType.SUBTITLE,
            language="eng",
            type_index=0,
        ),
        Stream(
            index=4,
            stream_type=StreamType.SUBTITLE,
            language="fre",
            type_index=1,
        ),
    ]
    return title


def test_select_explicit_supports_all_language_and_index(
    tmp_path: Path,
) -> None:
    title = make_stream_selection_title(tmp_path)

    selected = mkv._select_explicit(
        title, ["s:all", "a:eng", "a:1", "invalid", "x:all"]
    )

    assert [stream.index for stream in selected] == [3, 4, 1, 2]


def test_select_explicit_ignores_malformed_selectors(
    tmp_path: Path,
) -> None:
    title = make_stream_selection_title(tmp_path)

    selected = mkv._select_explicit(title, ["audio", "a:not-a-number", "v:", "x:0"])

    assert selected == []


def test_select_explicit_deduplicates_streams(tmp_path: Path) -> None:
    title = make_stream_selection_title(tmp_path)

    selected = mkv._select_explicit(title, ["a:all", "a:0", "a:eng", "a:1"])

    assert [stream.index for stream in selected] == [1, 2]


def test_dvd_subtitle_fallback_options_synthesize_language_name() -> None:
    subtitle = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="eng",
        is_default=True,
    )

    options = mkv._dvd_subtitle_fallback_options_for_track(2, subtitle)

    assert options == [
        "--language",
        "2:eng",
        "--default-track",
        "2:yes",
        "--track-name",
        "2:Subtitles (English)",
    ]
    assert (
        mkv._subtitle_fallback_track_name(
            Stream(index=1, stream_type=StreamType.SUBTITLE, language="und")
        )
        == ""
    )


def test_dvd_subtitle_fallback_options_stop_at_extracted_track_count() -> None:
    first = Stream(
        index=0,
        stream_type=StreamType.SUBTITLE,
        language="eng",
        title="First",
    )
    second = Stream(
        index=1,
        stream_type=StreamType.SUBTITLE,
        language="fre",
        title="Second",
    )

    options = mkv._dvd_subtitle_fallback_options(
        [first, second], [{"id": 0}], [{"id": 0, "type": "subtitles"}]
    )

    assert options == [
        "--language",
        "0:eng",
        "--default-track",
        "0:no",
        "--track-name",
        "0:First",
    ]


def make_mapped_entry(input_id: int, track_type: str, stream: Stream) -> MappedStream:
    return {
        "input_id": input_id,
        "type": track_type,
        "stream": stream,
        "ident_channels": None,
    }


def test_track_filter_options_include_each_selected_track_family() -> None:
    title = Title(
        index=0,
        source_file=Path("movie.m2ts"),
        name="Movie",
        duration_seconds=100.0,
    )
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO),
        Stream(index=1, stream_type=StreamType.AUDIO),
        Stream(index=2, stream_type=StreamType.SUBTITLE),
        Stream(index=3, stream_type=StreamType.SUBTITLE),
    ]
    mapped = [
        make_mapped_entry(2, "video", title.streams[0]),
        make_mapped_entry(5, "audio", title.streams[1]),
        make_mapped_entry(7, "subtitles", title.streams[2]),
        make_mapped_entry(9, "subtitle", title.streams[3]),
        make_mapped_entry(-1, "audio", Stream(index=4, stream_type=StreamType.AUDIO)),
    ]
    ident_tracks = [{"id": 2, "type": "video"}]

    options = _track_filter_options(ident_tracks, mapped, title)

    assert options == [
        "--video-tracks",
        "2",
        "--audio-tracks",
        "5",
        "--subtitle-tracks",
        "7,9",
    ]


def test_track_filter_options_require_ident_and_mapped_tracks() -> None:
    title = Title(
        index=0,
        source_file=Path("movie.m2ts"),
        name="Movie",
        duration_seconds=100.0,
    )
    mapped = [
        make_mapped_entry(0, "video", Stream(index=0, stream_type=StreamType.VIDEO))
    ]

    assert _track_filter_options([], mapped, title) == []
    assert _track_filter_options([{"id": 0, "type": "video"}], [], title) == []


def test_audio_filter_logic_for_complete_and_incomplete_dvd_audio() -> None:
    complete_title = Title(
        index=0,
        source_file=Path("movie.vob"),
        name="Complete",
        duration_seconds=100.0,
    )
    complete_title.streams = [
        Stream(index=0, stream_type=StreamType.AUDIO),
        Stream(index=1, stream_type=StreamType.AUDIO),
    ]
    complete_title.dvd_ifo_data = b"IFO"
    incomplete_title = Title(
        index=1,
        source_file=Path("movie.vob"),
        name="Incomplete",
        duration_seconds=100.0,
    )
    incomplete_title.streams = [
        Stream(index=0, stream_type=StreamType.AUDIO),
        Stream(index=1, stream_type=StreamType.AUDIO),
    ]
    incomplete_title.dvd_ifo_data = b"IFO"
    ident_tracks = [{"id": 0, "type": "audio"}]

    complete_ident_tracks = [
        {"id": 0, "type": "audio"},
        {"id": 1, "type": "audio"},
    ]

    assert mkv._should_use_audio_filter(complete_ident_tracks, complete_title) is True
    assert mkv._should_use_audio_filter(ident_tracks, incomplete_title) is False
