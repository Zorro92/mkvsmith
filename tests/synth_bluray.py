"""Build synthetic Blu-ray playlists (MPLS) and clip info (CLPI) for tests.

Layouts follow docs/formats/bluray.md (libbluray's mpls_parse.c,
clpi_parse.c and bluray.h), written independently of mkvsmith's parser so
a test checks the parser against the format rather than against itself.
Only what the tests need is modelled; reserved and unused fields are zero.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

TICKS = 45_000  # playlist and clip times count a 45 kHz clock

# Coding types (bluray.h bd_stream_type_e).
H264, HEVC, MPEG2 = 0x1B, 0x24, 0x02
AC3, DTS, TRUEHD, DTSHD_MA, LPCM = 0x81, 0x82, 0x83, 0x86, 0x80
PG, IG, TEXTST = 0x90, 0x91, 0x92

# Video format / frame rate / presentation type / sample rate codes.
FORMAT_1080P, FORMAT_2160P, FORMAT_1080I, FORMAT_480I = 6, 8, 4, 1
RATE_23976, RATE_24, RATE_2997 = 1, 2, 4
STEREO, MULTI = 3, 6
KHZ_48 = 1
SDR, HDR10, DOLBY_VISION = 0, 1, 2
BT709, BT2020 = 1, 2


def _block(body: bytes) -> bytes:
    """A body preceded by its one-byte length (stream entries and attributes)."""
    return bytes([len(body)]) + body


# --- STN stream entries ------------------------------------------------------------


@dataclass
class Stream:
    """One STN entry: where the stream is (stream entry) and what it is
    (stream attributes).

    *stream_type* 1 = in the main clip, 2 = in a SubPath's sub-clip, 3 / 4 =
    in a SubPath. Video uses *video_format* / *rate*; HEVC adds *dynamic_range*
    and *colour_space*; audio uses *presentation* / *sample_rate* / *lang*;
    PG and IG use *lang*; text subtitles *char_code* and *lang*.
    """

    coding: int
    pid: int
    stream_type: int = 1
    subpath: int = 0
    subclip: int = 0
    video_format: int = FORMAT_1080P
    rate: int = RATE_23976
    dynamic_range: int = SDR
    colour_space: int = BT709
    presentation: int = STEREO
    sample_rate: int = KHZ_48
    lang: str = "eng"
    char_code: int = 1

    def entry(self) -> bytes:
        if self.stream_type == 1:
            body = bytes([1]) + struct.pack(">H", self.pid)
        elif self.stream_type == 2:
            body = bytes([2, self.subpath, self.subclip]) + struct.pack(">H", self.pid)
        else:
            body = bytes([self.stream_type, self.subpath]) + struct.pack(">H", self.pid)
        return _block(body.ljust(9, b"\x00"))

    def attributes(self) -> bytes:
        c = self.coding
        if c in (0x01, MPEG2, H264, 0xEA):
            body = bytes([c, self.video_format << 4 | self.rate])
        elif c == HEVC:
            body = bytes(
                [
                    c,
                    self.video_format << 4 | self.rate,
                    self.dynamic_range << 4 | self.colour_space,
                ]
            )
        elif c in (PG, IG):
            body = bytes([c]) + self.lang.encode("ascii")
        elif c == TEXTST:
            body = bytes([c, self.char_code]) + self.lang.encode("ascii")
        else:  # audio
            body = bytes([c, self.presentation << 4 | self.sample_rate])
            body += self.lang.encode("ascii")
        return _block(body.ljust(5, b"\x00"))


def _refs(refs: list[int]) -> bytes:
    """A secondary stream's reference list: count, reserved, refs, padded to
    an even length."""
    body = bytes([len(refs), 0, *refs])
    return body + (b"\x00" if len(body) % 2 else b"")


@dataclass
class Stn:
    """An STN table. Secondary audio carries a list of primary audio
    references; the lists are empty here."""

    video: list[Stream] = field(default_factory=list)
    audio: list[Stream] = field(default_factory=list)
    pg: list[Stream] = field(default_factory=list)
    ig: list[Stream] = field(default_factory=list)
    secondary_audio: list[Stream] = field(default_factory=list)
    pip_pg: list[Stream] = field(default_factory=list)
    dolby_vision: list[Stream] = field(default_factory=list)

    def encode(self) -> bytes:
        counts = bytes(
            [
                len(self.video),
                len(self.audio),
                len(self.pg),
                len(self.ig),
                len(self.secondary_audio),
                0,  # secondary video
                len(self.pip_pg),
                len(self.dolby_vision),
            ]
        )

        def plain(streams: list[Stream]) -> bytes:
            return b"".join(s.entry() + s.attributes() for s in streams)

        body = (
            b"\x00\x00"
            + counts
            + b"\x00" * 4
            + plain(self.video)
            + plain(self.audio)
            + plain(self.pg + self.pip_pg)
            + plain(self.ig)
            + b"".join(
                s.entry() + s.attributes() + _refs([]) for s in self.secondary_audio
            )
            + plain(self.dolby_vision)
        )
        return struct.pack(">H", len(body)) + body


# --- play items, SubPaths, marks -------------------------------------------------------


@dataclass
class PlayItem:
    """A PlayItem: a clip played from *in_seconds* to *out_seconds*."""

    clip: str
    in_seconds: float
    out_seconds: float
    stn: Stn = field(default_factory=Stn)
    connection: int = 1
    angles: list[str] = field(default_factory=list)  # clips of angles 2+

    def encode(self) -> bytes:
        flags = (0x10 if self.angles else 0) | self.connection  # multi-angle bit 4
        body = (
            self.clip.encode("ascii")
            + b"M2TS"
            + struct.pack(">H", flags)
            + bytes([0])  # STC id
            + struct.pack(
                ">II",
                round(self.in_seconds * TICKS),
                round(self.out_seconds * TICKS),
            )
            + bytes(8)  # user-operation mask
            + bytes([0, 0])  # random access flag, still mode
            + bytes(2)  # still time
        )
        if self.angles:
            body += bytes([len(self.angles) + 1, 0])
            body += b"".join(
                clip.encode("ascii") + b"M2TS" + bytes([0]) for clip in self.angles
            )
        body += self.stn.encode()
        return struct.pack(">H", len(body)) + body


@dataclass
class SubPlayItem:
    clip: str
    in_seconds: float
    out_seconds: float
    connection: int = 1
    sync_item: int = 0

    def encode(self) -> bytes:
        body = (
            self.clip.encode("ascii")
            + b"M2TS"
            + struct.pack(
                ">I", self.connection << 1
            )  # 27 reserved, cc(4), multi-clip(1)
            + bytes([0])
            + struct.pack(
                ">IIHI",
                round(self.in_seconds * TICKS),
                round(self.out_seconds * TICKS),
                self.sync_item,
                0,
            )
        )
        return struct.pack(">H", len(body)) + body


@dataclass
class SubPath:
    """A SubPath: *kind* 2 = secondary audio presentation, 3 = interactive
    graphics menu, 4 = text subtitles, ..."""

    kind: int
    items: list[SubPlayItem]

    def encode(self) -> bytes:
        body = bytes([0, self.kind, 0, 0, 0, len(self.items)])
        body += b"".join(item.encode() for item in self.items)
        return struct.pack(">I", len(body)) + body


def mpls(
    items: list[PlayItem],
    *,
    chapters: list[tuple[int, float]] | None = None,
    subpaths: list[SubPath] | None = None,
    static_hdr: list[tuple[int, list[int]]] | None = None,
) -> bytes:
    """A playlist. *chapters* are (PlayItem index, seconds on that item's
    clock) entry marks; by default one at the start of each item.

    *static_hdr* adds the HDR static metadata extension (ID1 3, ID2 5): per
    entry a dynamic range type and its 12 raw 16-bit values (green, blue, red
    x/y, white x/y, max and min mastering luminance, MaxCLL, MaxFALL).
    """
    subpaths = subpaths or []
    if chapters is None:
        chapters = [(n, item.in_seconds) for n, item in enumerate(items)]

    appinfo = struct.pack(">I", 14) + bytes(14)
    playlist_body = (
        b"\x00\x00"
        + struct.pack(">HH", len(items), len(subpaths))
        + b"".join(item.encode() for item in items)
        + b"".join(sp.encode() for sp in subpaths)
    )
    playlist = struct.pack(">I", len(playlist_body)) + playlist_body
    marks_body = struct.pack(">H", len(chapters)) + b"".join(
        struct.pack(">BBHIHI", 0, 1, item, round(seconds * TICKS), 0xFFFF, 0)
        for item, seconds in chapters
    )
    marks = struct.pack(">I", len(marks_body)) + marks_body

    list_pos = 0x28 + len(appinfo)
    mark_pos = list_pos + len(playlist)
    extension = b""
    if static_hdr is not None:
        block_body = bytes([len(static_hdr), 0, 0, 0]) + b"".join(
            bytes([kind << 4, 0, 0, 0]) + struct.pack(">12H", *values)
            for kind, values in static_hdr
        )
        block = struct.pack(">I", len(block_body)) + block_body
        # Container: length, data start, 3 padding bytes, entry count, then
        # one 12-byte entry (ID1, ID2, start from the container, length).
        start = 12 + 12
        entries = struct.pack(">HHII", 3, 5, start, len(block))
        body = struct.pack(">I", start) + bytes(3) + b"\x01" + entries + block
        extension = struct.pack(">I", len(body)) + body
    ext_pos = mark_pos + len(marks) if extension else 0
    header = b"MPLS0300" if static_hdr is not None else b"MPLS0200"
    header += struct.pack(">III", list_pos, mark_pos, ext_pos) + bytes(20)
    return header + appinfo + playlist + marks + extension


# --- clip info ------------------------------------------------------------------------


@dataclass
class ClipStream:
    """A CLPI ProgramInfo stream. Video attributes differ from the playlist's:
    an aspect-ratio nibble comes before the HEVC dynamic-range byte."""

    pid: int
    coding: int
    video_format: int = FORMAT_1080P
    rate: int = RATE_23976
    aspect: int = 3  # 16:9
    presentation: int = STEREO
    sample_rate: int = KHZ_48
    lang: str = "eng"
    char_code: int = 1

    def encode(self) -> bytes:
        c = self.coding
        if c in (0x01, MPEG2, H264, 0xEA, HEVC):
            body = bytes([c, self.video_format << 4 | self.rate, self.aspect << 4])
        elif c in (PG, IG):
            body = bytes([c]) + self.lang.encode("ascii") + b"\x00"
        elif c == TEXTST:
            body = bytes([c, self.char_code]) + self.lang.encode("ascii")
        else:
            body = bytes([c, self.presentation << 4 | self.sample_rate])
            body += self.lang.encode("ascii")
        return struct.pack(">H", self.pid) + _block(body.ljust(5, b"\x00"))


def clpi(
    streams: list[ClipStream], presentation: tuple[float, float] | None = None
) -> bytes:
    """A clip info file with one program holding *streams*.

    *presentation* adds a SequenceInfo with one ATC and one STC sequence
    (id 0) presented from start to end seconds (docs/formats/bluray.md).
    """
    program_body = (
        bytes([0, 1])  # reserved, number of programs
        + struct.pack(">IHBB", 0, 0x0100, len(streams), 0)
        + b"".join(s.encode() for s in streams)
    )
    program = struct.pack(">I", len(program_body)) + program_body
    program_pos = 40  # after the header and an empty ClipInfo
    sequence = b""
    if presentation is not None:
        start, end = (round(seconds * TICKS) for seconds in presentation)
        sequence_body = bytes([0, 1]) + struct.pack(">IBB", 0, 1, 0)
        sequence_body += struct.pack(">HIII", 0x1001, 0, start, end)
        sequence = struct.pack(">I", len(sequence_body)) + sequence_body
    sequence_pos = program_pos + len(program) if sequence else 0
    header = b"HDMV0200" + struct.pack(">IIIII", sequence_pos, program_pos, 0, 0, 0)
    return header.ljust(program_pos, b"\x00") + program + sequence


def bdmt(name: str, lang: str = "eng") -> str:
    """A BDMV/META/DL/bdmt_<lang>.xml disc-library file naming the disc."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<disclib xmlns="urn:BDA:bdmv;disclib" '
        'xmlns:di="urn:BDA:bdmv;discinfo">\n'
        "  <di:discinfo>\n"
        f"    <di:title><di:name>{name}</di:name></di:title>\n"
        f"    <di:language>{lang}</di:language>\n"
        "  </di:discinfo>\n"
        "</disclib>\n"
    )
