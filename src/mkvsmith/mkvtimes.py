"""Read and rewrite one track's block timestamps in a finished MKV, in place.

A (Simple)Block stores its time as a signed 16-bit offset from its
Cluster's Timestamp, so a new time within that range is a 2-byte write:
the file keeps its size and layout. Cue points that index the track's
blocks get the new times too, when they fit the CueTime's width. Used by
``vc1time`` to put right mkvmerge's VC-1 frame times.

References: Matroska/EBML element IDs (RFC 8794, RFC 9559).
"""

from __future__ import annotations

import mmap
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_SEGMENT = 0x18538067
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_CODEC_ID = 0x86
_CODEC_PRIVATE = 0x63A2
_CLUSTER = 0x1F43B675
_CLUSTER_TIMESTAMP = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_CUES = 0x1C53BB6B
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CUE_CLUSTER_POSITION = 0xF1
_CUE_RELATIVE_POSITION = 0xF0

_DEFAULT_TIMESTAMP_SCALE = 1_000_000
_VFW_CODEC_ID = b"V_MS/VFW/FOURCC"
# BITMAPINFOHEADER.biCompression sits 16 bytes in.
_FOURCC_OFFSET = 16
_VC1_FOURCC = b"WVC1"


@dataclass(frozen=True, slots=True)
class BlockTime:
    """One block of the track: where its time is stored and what it is."""

    offset: int  # file offset of the block's 16-bit relative time
    cluster_time: int
    time: int
    element: int  # file offset of the SimpleBlock / BlockGroup element


@dataclass(frozen=True, slots=True)
class _CueTime:
    offset: int  # file offset of the CueTime value
    size: int
    block: int | None  # element offset of the indexed block, when given
    cluster: int  # file offset of the Cluster element
    time: int


@dataclass(slots=True)
class TrackTimes:
    track: int
    timestamp_scale: int
    blocks: list[BlockTime] = field(default_factory=list[BlockTime])
    cues: list[_CueTime] = field(default_factory=list[_CueTime])
    # Cluster element offset -> its data start, for CueRelativePosition.
    cluster_data: dict[int, int] = field(default_factory=dict[int, int])


def _vint(data: mmap.mmap, offset: int, marker: bool) -> tuple[int, int]:
    """(value, length) of the EBML variable-size integer at *offset*.

    *marker* keeps the length marker bit, as element IDs do.
    """
    first = data[offset]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError("bad EBML integer")
    value = first if marker else first & (0xFF >> length)
    for byte in data[offset + 1 : offset + length]:
        value = value << 8 | byte
    if not marker and value == (1 << (7 * length)) - 1:
        raise ValueError("element of unknown size")
    return value, length


def _children(
    data: mmap.mmap, start: int, end: int
) -> Iterator[tuple[int, int, int, int]]:
    """(id, element offset, data offset, data size) of each child element."""
    offset = start
    while offset < end:
        element_id, id_length = _vint(data, offset, True)
        size, size_length = _vint(data, offset + id_length, False)
        data_start = offset + id_length + size_length
        if data_start + size > end:
            raise ValueError("element runs past its parent")
        yield element_id, offset, data_start, size
        offset = data_start + size


def _uint(data: mmap.mmap, start: int, size: int) -> int:
    return int.from_bytes(data[start : start + size], "big")


def is_vc1_track(codec_id: bytes, private: bytes) -> bool:
    """Whether a track entry is VC-1 (VfW mode, ``WVC1`` FourCC)."""
    return (
        codec_id == _VFW_CODEC_ID
        and private[_FOURCC_OFFSET : _FOURCC_OFFSET + 4] == _VC1_FOURCC
    )


def _find_track(
    data: mmap.mmap, start: int, size: int, wanted: Callable[[bytes, bytes], bool]
) -> list[int]:
    numbers: list[int] = []
    for element_id, _offset, entry, entry_size in _children(data, start, start + size):
        if element_id != _TRACK_ENTRY:
            continue
        number = 0
        codec_id = private = b""
        for child, _o, value, value_size in _children(data, entry, entry + entry_size):
            if child == _TRACK_NUMBER:
                number = _uint(data, value, value_size)
            elif child == _CODEC_ID:
                codec_id = bytes(data[value : value + value_size]).rstrip(b"\0")
            elif child == _CODEC_PRIVATE:
                private = bytes(data[value : value + value_size])
        if number and wanted(codec_id, private):
            numbers.append(number)
    return numbers


def _read_cluster(data: mmap.mmap, start: int, size: int, times: TrackTimes) -> None:
    cluster_time = 0
    for element_id, offset, value, value_size in _children(data, start, start + size):
        if element_id == _CLUSTER_TIMESTAMP:
            cluster_time = _uint(data, value, value_size)
            continue
        block = value
        if element_id == _BLOCK_GROUP:
            block = next(
                (
                    child_value
                    for child, _o, child_value, _s in _children(
                        data, value, value + value_size
                    )
                    if child == _BLOCK
                ),
                -1,
            )
        elif element_id != _SIMPLE_BLOCK:
            continue
        if block < 0:
            continue
        number, length = _vint(data, block, False)
        if number != times.track:
            continue
        relative = int.from_bytes(
            data[block + length : block + length + 2], "big", signed=True
        )
        times.blocks.append(
            BlockTime(block + length, cluster_time, cluster_time + relative, offset)
        )


def _read_cues(
    data: mmap.mmap, start: int, size: int, segment_data: int, times: TrackTimes
) -> None:
    for point_id, _o, point, point_size in _children(data, start, start + size):
        if point_id != _CUE_POINT:
            continue
        time_at: tuple[int, int] | None = None
        positions: list[tuple[int, int, int | None]] = []
        for child, _o, value, value_size in _children(data, point, point + point_size):
            if child == _CUE_TIME:
                time_at = (value, value_size)
            elif child == _CUE_TRACK_POSITIONS:
                track = cluster = 0
                relative: int | None = None
                for item, _o, item_value, item_size in _children(
                    data, value, value + value_size
                ):
                    if item == _CUE_TRACK:
                        track = _uint(data, item_value, item_size)
                    elif item == _CUE_CLUSTER_POSITION:
                        cluster = _uint(data, item_value, item_size)
                    elif item == _CUE_RELATIVE_POSITION:
                        relative = _uint(data, item_value, item_size)
                positions.append((track, cluster, relative))
        # Only points that index this track alone: their time is the block's.
        if time_at is None or len(positions) != 1 or positions[0][0] != times.track:
            continue
        _track, cluster, relative = positions[0]
        cluster_offset = segment_data + cluster
        block = None
        if relative is not None and cluster_offset in times.cluster_data:
            block = times.cluster_data[cluster_offset] + relative
        times.cues.append(
            _CueTime(
                time_at[0],
                time_at[1],
                block,
                cluster_offset,
                _uint(data, time_at[0], time_at[1]),
            )
        )


def read_track_times(
    path: Path, wanted: Callable[[bytes, bytes], bool]
) -> TrackTimes | None:
    """Block and cue times of the one track that *wanted* picks.

    *wanted* gets each track's CodecID and CodecPrivate. ``None`` when no
    track, or more than one, matches. Raises ``ValueError`` on a file it
    can't walk (unknown-size elements, damage).
    """
    with (
        path.open("rb") as handle,
        mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        segment = next(
            (
                (value, size)
                for element_id, _o, value, size in _children(data, 0, len(data))
                if element_id == _SEGMENT
            ),
            None,
        )
        if segment is None:
            raise ValueError("no Segment")
        segment_data, segment_size = segment
        times: TrackTimes | None = None
        scale = _DEFAULT_TIMESTAMP_SCALE
        cues: tuple[int, int] | None = None
        for element_id, offset, value, size in _children(
            data, segment_data, segment_data + segment_size
        ):
            if element_id == _INFO:
                for child, _o, child_value, child_size in _children(
                    data, value, value + size
                ):
                    if child == _TIMESTAMP_SCALE:
                        scale = _uint(data, child_value, child_size)
            elif element_id == _TRACKS:
                numbers = _find_track(data, value, size, wanted)
                if len(numbers) != 1:
                    return None
                times = TrackTimes(numbers[0], scale)
            elif element_id == _CLUSTER and times is not None:
                times.cluster_data[offset] = value
                _read_cluster(data, value, size, times)
            elif element_id == _CUES:
                cues = (value, size)
        if times is None:
            return None
        times.timestamp_scale = scale
        if cues is not None:
            _read_cues(data, cues[0], cues[1], segment_data, times)
        return times


def write_block_times(path: Path, times: TrackTimes, new_times: Sequence[int]) -> int:
    """Give the track's blocks *new_times*, in file order; returns changes.

    Raises ``ValueError`` (writing nothing) when the counts differ or a
    time is out of its cluster's 16-bit range.
    """
    if len(new_times) != len(times.blocks):
        raise ValueError("block count mismatch")
    writes: list[tuple[int, bytes]] = []
    by_element: dict[int, int] = {}
    by_cluster_time: dict[tuple[int, int], int] = {}
    clusters = sorted(times.cluster_data)
    index = 0
    for block, new in zip(times.blocks, new_times):
        relative = new - block.cluster_time
        if not -0x8000 <= relative <= 0x7FFF:
            raise ValueError("block time outside its cluster's range")
        if new != block.time:
            writes.append((block.offset, relative.to_bytes(2, "big", signed=True)))
        by_element[block.element] = new
        while index + 1 < len(clusters) and clusters[index + 1] < block.element:
            index += 1
        if clusters:
            by_cluster_time.setdefault((clusters[index], block.time), new)
    for cue in times.cues:
        new = (
            by_element.get(cue.block)
            if cue.block is not None
            else by_cluster_time.get((cue.cluster, cue.time))
        )
        if new is None or new == cue.time or new >= 1 << (8 * cue.size):
            continue
        writes.append((cue.offset, new.to_bytes(cue.size, "big")))
    if writes:
        with path.open("r+b") as handle:
            for offset, value in writes:
                handle.seek(offset)
                handle.write(value)
    return sum(1 for block, new in zip(times.blocks, new_times) if new != block.time)
