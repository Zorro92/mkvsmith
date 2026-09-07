"""Tests for mkvmerge probe-output parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models import Stream, StreamType, Title
from probe import (
    _mkvmerge_stream_type,
    _parse_mkvmerge_streams,
    _stream_from_mkvmerge_track,
)


def test_mkvmerge_stream_type_maps_supported_track_types() -> None:
    assert _mkvmerge_stream_type("video") is StreamType.VIDEO
    assert _mkvmerge_stream_type("audio") is StreamType.AUDIO
    assert _mkvmerge_stream_type("subtitles") is StreamType.SUBTITLE
    assert _mkvmerge_stream_type("global") is None


def test_stream_from_mkvmerge_track_normalizes_core_fields() -> None:
    track: dict[str, Any] = {
        "id": 3,
        "type": "audio",
        "codec": "DTS-HD Master Audio",
        "properties": {
            "language": "eng",
            "track_name": "DTS-HD MA 7.1",
            "default_track": True,
            "forced_track": True,
            "number": 4352,
        },
    }

    stream = _stream_from_mkvmerge_track(track, StreamType.AUDIO, 2)

    assert stream.index == 3
    assert stream.stream_type is StreamType.AUDIO
    assert stream.codec == "dts_hd_ma"
    assert stream.language == "eng"
    assert stream.title == "DTS-HD MA 7.1"
    assert stream.is_default is True
    assert stream.is_forced is True
    assert stream.type_index == 2
    assert stream.sub_id == 4352
    assert stream.pid == 4352


def test_parse_mkvmerge_streams_applies_type_specific_properties(
    tmp_path: Path,
) -> None:
    title = Title(
        index=0,
        source_file=tmp_path / "movie.vob",
        name="Movie",
        duration_seconds=100.0,
    )
    probe_data = {
        "tracks": [
            {"id": 10, "type": "global"},
            {
                "id": 0,
                "type": "video",
                "codec": "MPEG-2 Video",
                "properties": {
                    "number": 480,
                    "pixel_dimensions": "720x480",
                },
            },
            {
                "id": 1,
                "type": "video",
                "codec": "Unknown Video",
                "properties": {"pixel_dimensions": "malformed"},
            },
            {
                "id": 2,
                "type": "audio",
                "codec": "AC-3",
                "properties": {
                    "number": 128,
                    "audio_channels": 6,
                    "audio_sampling_frequency": 48000,
                },
            },
            {
                "id": 3,
                "type": "subtitles",
                "codec": "VobSub",
                "properties": {"number": 32},
            },
        ]
    }

    _parse_mkvmerge_streams(probe_data, title)

    video, second_video, audio, subtitle = title.streams
    assert video.codec == "mpeg2video"
    assert (video.width, video.height) == (720, 480)
    assert video.sub_id == video.pid == 480
    assert video.type_index == 0

    assert second_video.codec == "Unknown Video"
    assert second_video.width is None
    assert second_video.height is None
    assert second_video.type_index == 1

    assert audio.codec == "ac3"
    assert audio.channels == 6
    assert audio.sample_rate == "48000"
    assert audio.sub_id == audio.pid == 128
    assert audio.type_index == 0

    assert subtitle.codec == "dvd_subtitle"
    assert subtitle.sub_id == subtitle.pid == 32
    assert subtitle.type_index == 0


def test_malformed_dimensions_do_not_raise() -> None:
    stream = Stream(index=0, stream_type=StreamType.VIDEO)

    _probe_apply_dimensions(stream, "1920x1080x9")
    _probe_apply_dimensions(stream, "not-dimensions")

    assert stream.width is None
    assert stream.height is None


def _probe_apply_dimensions(stream: Stream, dimensions: str) -> None:
    from probe import _apply_mkvmerge_video_properties

    _apply_mkvmerge_video_properties(stream, {"pixel_dimensions": dimensions})
