"""Blu-ray M2TS tail analysis and audio trimming for seamless clip appends.

Seamless-branching discs split a movie into many clips joined with
``connection_condition`` 5/6. Every clip starts all its tracks at the
PlayItem IN time, but each audio track ends on its own frame grid: an AC-3
track's last 32 ms frame can overhang the video end by up to a frame, while
TrueHD (0.83 ms access units) ends almost exactly with it. mkvmerge appends
clips either by the previous file's longest track (``file`` mode — every
shorter track gets a gap at each join) or by each track's own end (``track``
mode — gapless, but every overhang shifts that track later relative to the
video, accumulating across joins).

This module makes ``track`` mode safe, following eac3to's approach to
seamless connections: it reads the last few MB of each clip (read-only),
tracks each audio track's accumulated offset against the video, and drops a
clip's trailing audio frames whenever that brings the track closer to the
video. The offset then stays within half a frame instead of growing, and no
track has gaps. Frames are dropped by rewriting their TS packets as null
packets in place, so file sizes and all other packets are untouched.

Transport stream layout per ISO/IEC 13818-1 (TS header, adaptation field,
PES header incl. the PES_extension / stream_id_extension fields) and the
BD-ROM 192-byte source packet (4-byte TP_extra_header + 188-byte TS packet).
PID assignments per the BD-ROM spec, as used by libbluray: primary video
0x1011, primary audio 0x1100-0x111F, secondary audio 0x1A00-0x1A1F.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_PACKET_SIZE = 192
_TS_OFFSET = 4  # TP_extra_header (copy permission + arrival time stamp)
_SYNC_BYTE = 0x47
_NULL_PID = 0x1FFF
PRIMARY_VIDEO_PID = 0x1011
PTS_HZ = 90_000
# Tail window per clip. Audio is muxed at most ~1 s ahead of its PTS (decoder
# buffer), so the last frames of every track sit well inside this even at
# the 48 Mbps BD maximum.
TAIL_WINDOW = 16 * 1024 * 1024
# Drift a track may accumulate beyond half a frame before seamless trimming
# is abandoned (e.g. clips whose audio is genuinely shorter than the video,
# which dropping frames cannot fix).
DRIFT_TOLERANCE = 0.010


def is_bluray_audio_pid(pid: int) -> bool:
    return 0x1100 <= pid <= 0x111F or 0x1A00 <= pid <= 0x1A1F


@dataclass
class AudioTail:
    """The last frames of one audio PID's output substream, in file order.

    ``frames`` holds ``(pts, file_offset)`` pairs; ``file_offset`` is the
    source packet that starts the frame's PES. ``frame_duration`` is the
    median PTS step (90 kHz).
    """

    frames: list[tuple[int, int]]
    frame_duration: int

    @property
    def end(self) -> int:
        return max(pts for pts, _offset in self.frames) + self.frame_duration


@dataclass
class ClipTail:
    video_end: int | None  # max video PTS + frame duration (90 kHz)
    audio: dict[int, AudioTail] = field(default_factory=dict[int, AudioTail])


@dataclass
class _PesStart:
    pid: int
    offset: int
    pts: int
    substream: int | None


def _read_pts(data: bytes, pos: int) -> int:
    return (
        ((data[pos] >> 1) & 0x07) << 30
        | data[pos + 1] << 22
        | (data[pos + 2] >> 1) << 15
        | data[pos + 3] << 7
        | data[pos + 4] >> 1
    )


def _payload_start(packet: bytes) -> int | None:
    """Offset of the TS payload within a 188-byte packet, or None."""
    adaptation = (packet[3] >> 4) & 0x03
    if not adaptation & 0x01:
        return None
    start = 4
    if adaptation & 0x02:
        start += 1 + packet[4]
    return start if start < len(packet) else None


def _stream_id_extension(pes: bytes) -> int | None:
    """``stream_id_extension`` of an extended-stream-id (0xFD) PES header.

    Walks the optional fields selected by PTS_DTS_flags ... PES_CRC_flag,
    then the PES_extension flags, per ISO/IEC 13818-1 2.4.3.7.
    """
    if len(pes) < 9 or pes[3] != 0xFD or not pes[7] & 0x01:
        return None
    flags = pes[7]
    pos = 9
    pos += {0x80: 5, 0xC0: 10}.get(flags & 0xC0, 0)
    for mask, size in ((0x20, 6), (0x10, 3), (0x08, 1), (0x04, 1), (0x02, 2)):
        if flags & mask:
            pos += size
    if pos >= len(pes):
        return None
    ext_flags = pes[pos]
    pos += 1
    if ext_flags & 0x80:  # PES_private_data
        pos += 16
    if ext_flags & 0x40:  # pack_header_field
        if pos >= len(pes):
            return None
        pos += 1 + pes[pos]
    if ext_flags & 0x20:  # program_packet_sequence_counter
        pos += 2
    if ext_flags & 0x10:  # P-STD_buffer
        pos += 2
    if not ext_flags & 0x01 or pos + 1 >= len(pes):
        return None
    return pes[pos + 1] & 0x7F  # skip marker + PES_extension_field_length


def _packet_pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def _find_alignment(data: bytes) -> int | None:
    """Offset of the first source packet in *data* (sync bytes line up)."""
    for offset in range(SOURCE_PACKET_SIZE):
        positions = range(
            offset + _TS_OFFSET,
            min(len(data), offset + 8 * SOURCE_PACKET_SIZE),
            SOURCE_PACKET_SIZE,
        )
        if positions and all(data[pos] == _SYNC_BYTE for pos in positions):
            return offset
    return None


def _scan_pes_starts(data: bytes, base_offset: int, pids: set[int]) -> list[_PesStart]:
    """PES starts (with PTS) for *pids* in a block of source packets."""
    align = _find_alignment(data)
    if align is None:
        return []
    starts: list[_PesStart] = []
    for pos in range(align, len(data) - SOURCE_PACKET_SIZE + 1, SOURCE_PACKET_SIZE):
        packet = data[pos + _TS_OFFSET : pos + SOURCE_PACKET_SIZE]
        if packet[0] != _SYNC_BYTE or not packet[1] & 0x40:  # payload_unit_start
            continue
        pid = _packet_pid(packet)
        if pid not in pids:
            continue
        payload = _payload_start(packet)
        if payload is None:
            continue
        pes = packet[payload:]
        if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not pes[7] & 0x80:
            continue
        starts.append(
            _PesStart(
                pid, base_offset + pos, _read_pts(pes, 9), _stream_id_extension(pes)
            )
        )
    return starts


def _median_step(pts_values: list[int]) -> int:
    ordered = sorted(set(pts_values))
    steps = sorted(b - a for a, b in zip(ordered, ordered[1:]))
    return steps[len(steps) // 2] if steps else 0


def _audio_tail(starts: list[_PesStart]) -> AudioTail | None:
    """The substream mkvmerge outputs for one audio PID.

    TrueHD PIDs interleave the TrueHD access units with an embedded AC-3
    core on another stream_id_extension; mkvmerge outputs only the TrueHD
    stream, so when substreams differ in frame duration the finest one is
    used. Otherwise (E-AC-3 core + dependent frames, DTS-HD, LPCM) the
    substreams share PTS and form one frame sequence.
    """
    by_substream: dict[int | None, list[_PesStart]] = {}
    for start in starts:
        by_substream.setdefault(start.substream, []).append(start)
    durations = {
        substream: _median_step([s.pts for s in group])
        for substream, group in by_substream.items()
    }
    if len(set(durations.values())) > 1:
        main = min(durations, key=lambda substream: durations[substream] or 1 << 40)
        chosen = by_substream[main]
        duration = durations[main]
    else:
        chosen = starts
        duration = _median_step([s.pts for s in starts])
    if len(chosen) < 3 or duration <= 0:
        return None
    frames: list[tuple[int, int]] = []
    seen: set[int] = set()
    for start in chosen:
        if start.pts not in seen:  # E-AC-3 dependent frames repeat the PTS
            seen.add(start.pts)
            frames.append((start.pts, start.offset))
    return AudioTail(frames, duration)


def scan_clip_tail(path: Path, audio_pids: set[int]) -> ClipTail:
    """Read-only scan of a clip's tail for the video and audio track ends.

    Starts with a small window and widens it to TAIL_WINDOW when the video
    or a requested audio track has too few frames in it to measure.
    """
    tail = _scan_clip_window(path, audio_pids, TAIL_WINDOW // 8)
    if tail.video_end is None or len(tail.audio) < len(audio_pids):
        tail = _scan_clip_window(path, audio_pids, TAIL_WINDOW)
    return tail


def _scan_clip_window(path: Path, audio_pids: set[int], window: int) -> ClipTail:
    size = path.stat().st_size
    start = max(0, size - window)
    start -= start % SOURCE_PACKET_SIZE
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    starts = _scan_pes_starts(data, start, {PRIMARY_VIDEO_PID, *audio_pids})
    video_pts = [s.pts for s in starts if s.pid == PRIMARY_VIDEO_PID]
    video_step = _median_step(video_pts)
    tail = ClipTail(video_end=max(video_pts) + video_step if video_step > 0 else None)
    for pid in audio_pids:
        audio = _audio_tail([s for s in starts if s.pid == pid])
        if audio is not None:
            tail.audio[pid] = audio
    return tail


def plan_seamless_trim(
    tails: list[ClipTail], audio_pids: set[int], tolerance: float = DRIFT_TOLERANCE
) -> list[dict[int, int]] | None:
    """Per-clip cut offsets that keep every audio track locked to the video.

    Simulates mkvmerge's ``track`` append mode clip by clip: each track's
    offset against the video changes by (audio end - video end) at every
    join. Before each join the clip's trailing frames are dropped while that
    moves the offset closer to zero. Returns, per clip, ``{pid: offset}``
    where every packet of ``pid`` at or after ``offset`` must be nulled; or
    None when a track can't be kept within half a frame plus *tolerance*
    (missing track data, or audio shorter than the video).
    """
    plan: list[dict[int, int]] = [{} for _ in tails]
    for pid in audio_pids:
        drift = 0
        for index, tail in enumerate(tails[:-1]):
            audio = tail.audio.get(pid)
            if tail.video_end is None or audio is None:
                return None
            ends = sorted(audio.frames)
            keep = len(ends)
            while keep > 1:
                current = (
                    drift + ends[keep - 1][0] + audio.frame_duration - tail.video_end
                )
                shorter = (
                    drift + ends[keep - 2][0] + audio.frame_duration - tail.video_end
                )
                if abs(shorter) >= abs(current):
                    break
                keep -= 1
            if keep < len(ends):
                cut_pts = ends[keep][0]
                plan[index][pid] = min(
                    off for pts, off in audio.frames if pts >= cut_pts
                )
            drift += ends[keep - 1][0] + audio.frame_duration - tail.video_end
            limit = audio.frame_duration / 2 + tolerance * PTS_HZ
            if abs(drift) > limit:
                return None
    return plan


def null_packets_from(path: Path, cuts: dict[int, int]) -> int:
    """Rewrite every packet of each PID at or after its cut offset as null.

    Modifies *path* in place (it must be a private copy) and returns the
    number of packets nulled. Null packets (PID 0x1FFF) keep the source
    packet size and arrival time stamps, so the rest of the file is intact.
    """
    if not cuts:
        return 0
    first = min(cuts.values())
    nulled = 0
    with path.open("r+b") as handle:
        handle.seek(first)
        data = bytearray(handle.read())
        for pos in range(0, len(data) - SOURCE_PACKET_SIZE + 1, SOURCE_PACKET_SIZE):
            ts = pos + _TS_OFFSET
            if data[ts] != _SYNC_BYTE:
                continue
            pid = ((data[ts + 1] & 0x1F) << 8) | data[ts + 2]
            cut = cuts.get(pid)
            if cut is None or first + pos < cut:
                continue
            data[ts + 1] = _NULL_PID >> 8
            data[ts + 2] = _NULL_PID & 0xFF
            nulled += 1
        handle.seek(first)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return nulled
