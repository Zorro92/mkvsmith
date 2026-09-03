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
    except Exception:
        return None


def _parse_mkvmerge_streams(probe_data: dict[str, Any], title: Title) -> None:
    """Parse mkvmerge -J output into Stream objects, appending to title.streams."""
    type_counts = {StreamType.VIDEO: 0, StreamType.AUDIO: 0, StreamType.SUBTITLE: 0}
    for td in probe_data.get("tracks", []):
        mtype = td.get("type", "")
        if mtype == "video":
            st = StreamType.VIDEO
        elif mtype == "audio":
            st = StreamType.AUDIO
        elif mtype == "subtitles":
            st = StreamType.SUBTITLE
        else:
            continue

        props = td.get("properties", {})
        codec = _MKVMERGE_CODEC_MAP.get(td.get("codec", ""), td.get("codec", ""))

        stream = Stream(
            index=td.get("id", 0),
            stream_type=st,
            codec=codec or "unknown",
            language=props.get("language", "und"),
            title=props.get("track_name", ""),
            is_default=props.get("default_track", False),
            is_forced=props.get("forced_track", False),
            type_index=type_counts[st],
        )

        num = props.get("number")
        if num is not None:
            stream.sub_id = num
            stream.pid = num

        if st == StreamType.VIDEO:
            dims = props.get("pixel_dimensions", "")
            if "x" in dims:
                try:
                    w_str, h_str = dims.split("x")
                    stream.width = int(w_str)
                    stream.height = int(h_str)
                except (ValueError, IndexError):
                    pass
        elif st == StreamType.AUDIO:
            ch = props.get("audio_channels")
            if ch is not None:
                stream.channels = ch
            sr = props.get("audio_sampling_frequency")
            if sr is not None:
                stream.sample_rate = str(sr)

        title.streams.append(stream)
        type_counts[st] += 1
