"""Build synthetic Blu-ray M2TS clips (headers only) for tests.

A Blu-ray clip is a run of 192-byte source packets: a 4-byte TP_extra_header
(copy permission, arrival time stamp) then a 188-byte MPEG transport stream
packet (ISO/IEC 13818-1 2.4.3). Each frame here is one PES packet with a
PTS in one TS packet, padded with adaptation-field stuffing; there is no
real audio or video, as in the header-only fixtures captured from discs.
Written from the standard, not from mkvsmith's m2ts.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SOURCE_PACKET = 192
VIDEO_PID = 0x1011

# PES stream ids and Blu-ray stream_id_extension values.
VIDEO_STREAM = 0xE0
PRIVATE_1 = 0xBD  # AC-3, DTS, LPCM on Blu-ray
EXTENDED = 0xFD  # TrueHD and E-AC-3 use extended stream ids
TRUEHD_EXT, TRUEHD_AC3_CORE_EXT = 0x72, 0x76
EAC3_EXT = 0x71

# Frame durations in 90 kHz ticks.
FILM_FRAME = 3754  # 1001/24000 s, rounded: 23.976 fps
AC3_FRAME = 2880  # 1536 samples at 48 kHz
TRUEHD_UNIT = 75  # 40 samples at 48 kHz


def _pts(pts: int) -> bytes:
    """The 5-byte PTS field ('0010' marker, 33 bits, marker bits)."""
    return bytes(
        [
            0x20 | ((pts >> 29) & 0x0E) | 1,
            (pts >> 22) & 0xFF,
            ((pts >> 14) & 0xFE) | 1,
            (pts >> 7) & 0xFF,
            ((pts << 1) & 0xFE) | 1,
        ]
    )


def pes(stream_id: int, pts: int, extension: int | None = None) -> bytes:
    """A PES packet header with a PTS, plus a few payload bytes.

    With *extension*, the header carries a PES_extension whose
    PES_extension_field holds stream_id_extension (13818-1 2.4.3.7).
    """
    optional = _pts(pts)
    flags = 0x80  # PTS only
    if extension is not None:
        flags |= 0x01  # PES_extension_flag
        # PES_extension_flag_2 set; then marker bit + field length 1, then
        # stream_id_extension_flag 0 + the 7-bit stream_id_extension.
        optional += bytes([0x01, 0x81, extension & 0x7F])
    header = bytes([0x80, flags, len(optional)]) + optional
    payload = b"\x0b\x77" + bytes(6)  # a few bytes of (empty) elementary stream
    body = header + payload
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", len(body)) + body


def source_packet(
    pid: int, payload: bytes, *, start: bool = True, counter: int = 0
) -> bytes:
    """One 192-byte source packet carrying *payload* on *pid*."""
    if len(payload) > 184:
        raise ValueError("payload too large for one TS packet")
    header = bytes(
        [
            0x47,
            (0x40 if start else 0) | (pid >> 8) & 0x1F,
            pid & 0xFF,
        ]
    )
    stuffing = 184 - len(payload)
    if stuffing:
        # adaptation_field_control 11: adaptation field, then payload.
        header += bytes([0x30 | counter & 0x0F])
        if stuffing == 1:
            adaptation = bytes([0])
        else:
            adaptation = bytes([stuffing - 1, 0]) + b"\xff" * (stuffing - 2)
        packet = header + adaptation + payload
    else:
        packet = header + bytes([0x10 | counter & 0x0F]) + payload
    return bytes(4) + packet


def null_packet() -> bytes:
    return bytes(4) + bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


@dataclass
class Track:
    """An audio track on *pid* with frames of *duration* ticks.

    Like a real track it starts with the video and runs on its own frame
    grid until it reaches the video's end, so its last frame overhangs the
    video by less than one frame; *frames* overrides the count (e.g. audio
    that stops early). A second track on the same PID with another
    *extension* models TrueHD's embedded AC-3 core; *repeat* > 1 gives each
    PTS several PES packets (E-AC-3 core + dependent frames).
    """

    pid: int
    duration: int
    stream_id: int = PRIVATE_1
    extension: int | None = None
    frames: int | None = None
    repeat: int = 1

    def timestamps(self, video_start: int, video_end: int) -> list[int]:
        count = self.frames
        if count is None:
            count = -(-(video_end - video_start) // self.duration)
        return [video_start + n * self.duration for n in range(count)]

    def end(self, video_start: int, video_end: int) -> int:
        """Where this track's last frame ends (90 kHz)."""
        return self.timestamps(video_start, video_end)[-1] + self.duration


def clip(video_end: int, tracks: list[Track], *, video_start: int = 0) -> bytes:
    """A clip whose video frames run from *video_start* up to *video_end*
    (90 kHz, a whole number of frames) and whose audio is *tracks*, packets
    in PTS order (video before audio at equal PTS), as a muxer interleaves
    them."""
    events: list[tuple[int, int, bytes]] = []
    video = range(video_start, video_end, FILM_FRAME)
    if (video_end - video_start) % FILM_FRAME:
        raise ValueError("video_end must be a whole number of frames")
    for pts in video:
        events.append((pts, 0, source_packet(VIDEO_PID, pes(VIDEO_STREAM, pts))))
    for order, track in enumerate(tracks, start=1):
        for pts in track.timestamps(video_start, video_end):
            for _ in range(track.repeat):
                packet = pes(track.stream_id, pts, track.extension)
                events.append((pts, order, source_packet(track.pid, packet)))
    events.sort(key=lambda event: (event[0], event[1]))
    return b"".join(packet for _pts, _order, packet in events) + null_packet()
