"""Minimal read-only Matroska reader for post-mux checks.

Walks just enough EBML structure to find the first H.264 video frame of a
finished MKV and its first few hundred frames: whether playback starts on an
IDR picture, and (with ``h264``) whether the video is interlaced. Elements that are not
needed (attachments, cues, other tracks) are skipped by size, so only a few
kilobytes are read regardless of file size.

Why it matters: Blu-ray AVC streams usually carry a single IDR at the start
of each clip and rely on recovery-point I-frames afterwards. A file cut
mid-clip (a packed-episode split) therefore starts on a non-IDR frame, which
some hardware decoders (Android MediaCodec, as used by mpv-android) refuse
to start from: audio plays over a black screen until the player is switched
to software decoding.

References: Matroska/EBML element IDs (RFC 8794, RFC 9559); AVC
configuration record (ISO/IEC 14496-15) for the NAL length size.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from h264 import ScanType, stream_scan_type

_SEGMENT = 0x18538067
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_CODEC_ID = 0x86
_CODEC_PRIVATE = 0x63A2
_CLUSTER = 0x1F43B675
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_SIMPLE_BLOCK = 0xA3

_AVC_CODEC_ID = b"V_MPEG4/ISO/AVC"
_NAL_IDR = 5
_NAL_NON_IDR = 1
_UNKNOWN_SIZE = -1
# Give up after this many elements: a sane file reaches the frames we sample
# long before, and a corrupt one must not loop forever.
_MAX_ELEMENTS = 200_000


class _Reader:
    def __init__(self, f: BinaryIO) -> None:
        self._f = f

    def tell(self) -> int:
        return self._f.tell()

    def seek(self, pos: int) -> None:
        self._f.seek(pos)

    def read(self, n: int) -> bytes:
        data = self._f.read(n)
        if len(data) != n:
            raise EOFError
        return data

    def element_id(self) -> int:
        first = self.read(1)[0]
        length = _vint_length(first)
        if length > 4:
            raise ValueError("bad element id")
        return int.from_bytes(bytes([first]) + self.read(length - 1), "big")

    def element_size(self) -> int:
        first = self.read(1)[0]
        length = _vint_length(first)
        value = first & (0xFF >> length)
        rest = self.read(length - 1)
        all_ones = value == (0xFF >> length) and all(b == 0xFF for b in rest)
        for byte in rest:
            value = (value << 8) | byte
        return _UNKNOWN_SIZE if all_ones else value


def _vint_length(first: int) -> int:
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length
    raise ValueError("bad vint")


def _block_track(block: bytes) -> tuple[int, int, bool]:
    """(track number, frame data offset, laced?) for a (Simple)Block body.

    Audio blocks are often laced (several frames per block); only the video
    track's blocks are read, and those are never laced in practice.
    """
    length = _vint_length(block[0])
    track = block[0] & (0xFF >> length)
    for byte in block[1:length]:
        track = (track << 8) | byte
    laced = bool(block[length + 2] & 0x06)
    return track, length + 3, laced


@dataclass(frozen=True)
class _AvcConfig:
    """The parts of an AVCDecoderConfigurationRecord (CodecPrivate) we use."""

    length_size: int
    sps: list[bytes]
    pps: list[bytes]


def _parse_avc_config(private: bytes) -> _AvcConfig:
    length_size = (private[4] & 0x03) + 1
    pos = 5
    sets: list[list[bytes]] = []
    for count_mask in (0x1F, 0xFF):  # SPS count (5 bits), then PPS count
        count = private[pos] & count_mask
        pos += 1
        units: list[bytes] = []
        for _ in range(count):
            size = int.from_bytes(private[pos : pos + 2], "big")
            units.append(private[pos + 2 : pos + 2 + size])
            pos += 2 + size
        sets.append(units)
    return _AvcConfig(length_size, sets[0], sets[1])


def _split_nals(frame: bytes, length_size: int) -> list[bytes]:
    nals: list[bytes] = []
    pos = 0
    while pos + length_size <= len(frame):
        size = int.from_bytes(frame[pos : pos + length_size], "big")
        pos += length_size
        if size <= 0 or pos + size > len(frame):
            break
        nals.append(frame[pos : pos + size])
        pos += size
    return nals


def _avc_frames(path: Path, limit: int) -> tuple[_AvcConfig | None, list[list[bytes]]]:
    """The AVC config and up to *limit* leading frames (as NAL lists)."""
    config: _AvcConfig | None = None
    frames: list[list[bytes]] = []
    try:
        with path.open("rb") as f:
            for found_config, nals in _scan(_Reader(f)):
                config = found_config
                frames.append(nals)
                if len(frames) >= limit:
                    break
    except (OSError, EOFError, ValueError, IndexError):
        pass
    return config, frames


def first_video_frame_is_idr(path: Path) -> bool | None:
    """Whether *path*'s first H.264 video frame is an IDR picture.

    ``None`` when there is no H.264 track or the file can't be read.
    """
    _config, frames = _avc_frames(path, 1)
    for nal in frames[0] if frames else []:
        nal_type = nal[0] & 0x1F
        if nal_type == _NAL_IDR:
            return True
        if nal_type == _NAL_NON_IDR:
            return False
    return None


def video_scan_type(path: Path, sample_frames: int = 300) -> ScanType | None:
    """Progressive / interlaced (with field order) for *path*'s H.264 video.

    Judged from the first *sample_frames* frames (see ``h264.classify_scan``);
    ``None`` when undecided or there is no H.264 track.
    """
    config, frames = _avc_frames(path, sample_frames)
    if config is None:
        return None
    return stream_scan_type(config.sps, config.pps, frames)


def _scan(r: _Reader) -> Iterator[tuple[_AvcConfig, list[bytes]]]:
    """Yield the first AVC track's frames, in file order, as NAL lists."""
    # EBML header, then the Segment whose children we walk.
    r.element_id()
    header_size = r.element_size()  # read before tell(): it advances the file
    r.seek(r.tell() + header_size)
    if r.element_id() != _SEGMENT:
        return
    segment_size = r.element_size()
    segment_end = None if segment_size == _UNKNOWN_SIZE else r.tell() + segment_size
    avc: dict[int, _AvcConfig] = {}  # track number -> its AVC config
    stack: list[int | None] = [segment_end]
    for _ in range(_MAX_ELEMENTS):
        end = stack[-1]
        if end is not None and r.tell() >= end:
            stack.pop()
            if not stack:
                return
            continue
        element = r.element_id()
        size = r.element_size()
        if element in (_TRACKS, _CLUSTER, _BLOCK_GROUP):
            stack.append(None if size == _UNKNOWN_SIZE else r.tell() + size)
        elif element == _TRACK_ENTRY:
            avc.update(_track_entry(r.read(size)))
        elif element in (_SIMPLE_BLOCK, _BLOCK):
            if not avc:
                return
            block = r.read(size)
            track, offset, laced = _block_track(block)
            config = avc.get(track)
            if config is not None and track == min(avc):
                if laced:
                    return
                yield config, _split_nals(block[offset:], config.length_size)
        elif size == _UNKNOWN_SIZE:
            return
        else:
            r.seek(r.tell() + size)


def _track_entry(data: bytes) -> dict[int, _AvcConfig]:
    """``{track number: AVC config}`` if this entry is an AVC track."""
    r = _BytesReader(data)
    number: int | None = None
    codec: bytes | None = None
    private: bytes | None = None
    while r.remaining():
        element = r.element_id()
        size = r.element_size()
        value = r.read(size)
        if element == _TRACK_NUMBER:
            number = int.from_bytes(value, "big")
        elif element == _CODEC_ID:
            codec = value.rstrip(b"\0")
        elif element == _CODEC_PRIVATE:
            private = value
    if number is None or codec != _AVC_CODEC_ID or not private or len(private) < 7:
        return {}
    return {number: _parse_avc_config(private)}


class _BytesReader(_Reader):
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise EOFError
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def remaining(self) -> int:
        return len(self._data) - self._pos
