"""
mkvmerge probing helpers.

Extracted from main.py: runs ``mkvmerge -J`` (the project's only external
media tool) to identify the tracks of a source file and converts the JSON
output into Stream objects on a Title. Used by the scanner fallback paths
(DVD VOB, Blu-ray M2TS, plain video files) and the DVD title builder.

Copyright (C) 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

# Licensed under GPL-3.0-or-later

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from models import StreamType, Stream, Title, _HAS_MKVMERGE


# =============================================================================
# Core Helpers
# =============================================================================
_MKVMERGE_CODEC_MAP = {
    "MPEG-2 Video": "mpeg2video",
    "AVC/H.264": "h264",
    "HEVC/H.265": "h265",
    "VC-1": "vc1",
    "AC-3": "ac3",
    "AC-3/E-AC-3": "eac3",
    "E-AC-3": "eac3",
    "DTS": "dts",
    "DTS-HD Master Audio": "dts_hd_ma",
    "DTS-HD High Resolution Audio": "dts_hd_hr",
    "PCM": "lpcm",
    "TrueHD": "truehd",
    "FLAC": "flac",
    "Vorbis": "vorbis",
    "AAC": "aac",
    "MP3": "mp3",
    "SubStationAlpha": "ass",
    "VobSub": "dvd_subtitle",
    "PGS": "hdmv_pgs_subtitle",
    "HDMV PGS": "hdmv_pgs_subtitle",
    "Advanced SubStation Alpha": "ass",
    "SSA": "ass",
    "UTF-8": "srt",
}


def _probe_with_mkvmerge(path: Path) -> dict[str, Any] | None:
    """Probe a source file using mkvmerge -J.

    Returns dict with 'tracks' (list) and 'duration' (float seconds),
    or None on failure.
    """
    if not _HAS_MKVMERGE:
        return None
    try:
        proc = subprocess.run(
            ["mkvmerge", "-J", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        duration = 0.0
        if data.get("container", {}).get("duration"):
            duration = data["container"]["duration"] / 1_000_000_000.0
        return {"tracks": data.get("tracks", []), "duration": duration}
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return None


def _mkvmerge_stream_type(track_type: str) -> StreamType | None:
    if track_type == "video":
        return StreamType.VIDEO
    if track_type == "audio":
        return StreamType.AUDIO
    if track_type == "subtitles":
        return StreamType.SUBTITLE
    return None


def _stream_from_mkvmerge_track(
    track: dict[str, Any], stream_type: StreamType, type_index: int
) -> Stream:
    properties = track.get("properties", {})
    codec_name = track.get("codec", "")
    codec = _MKVMERGE_CODEC_MAP.get(codec_name, codec_name)
    stream = Stream(
        index=track.get("id", 0),
        stream_type=stream_type,
        codec=codec or "unknown",
        language=properties.get("language", "und"),
        title=properties.get("track_name", ""),
        is_default=properties.get("default_track", False),
        is_forced=properties.get("forced_track", False),
        type_index=type_index,
    )

    source_number = properties.get("number")
    if source_number is not None:
        stream.sub_id = source_number
        stream.pid = source_number
    return stream


def _apply_mkvmerge_video_properties(
    stream: Stream, properties: dict[str, Any]
) -> None:
    dimensions = properties.get("pixel_dimensions", "")
    if "x" not in dimensions:
        return
    try:
        width, height = dimensions.split("x")
        stream.width = int(width)
        stream.height = int(height)
    except (ValueError, IndexError):
        pass


def _apply_mkvmerge_audio_properties(
    stream: Stream, properties: dict[str, Any]
) -> None:
    channels = properties.get("audio_channels")
    if channels is not None:
        stream.channels = channels
    sample_rate = properties.get("audio_sampling_frequency")
    if sample_rate is not None:
        stream.sample_rate = str(sample_rate)


def _parse_mkvmerge_streams(probe_data: dict[str, Any], title: Title) -> None:
    """Parse mkvmerge -J output into Stream objects, appending to title.streams."""
    type_counts = {StreamType.VIDEO: 0, StreamType.AUDIO: 0, StreamType.SUBTITLE: 0}
    for track in probe_data.get("tracks", []):
        stream_type = _mkvmerge_stream_type(track.get("type", ""))
        if stream_type is None:
            continue

        stream = _stream_from_mkvmerge_track(
            track, stream_type, type_counts[stream_type]
        )
        properties = track.get("properties", {})
        if stream_type == StreamType.VIDEO:
            _apply_mkvmerge_video_properties(stream, properties)
        elif stream_type == StreamType.AUDIO:
            _apply_mkvmerge_audio_properties(stream, properties)

        title.streams.append(stream)
        type_counts[stream_type] += 1
