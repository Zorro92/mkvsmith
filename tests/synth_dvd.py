"""Build synthetic DVD-Video IFO files and VOB packets for tests.

Layouts follow the DVD-Video structures as libdvdread declares them
(``ifo_types.h``: vtsi_mat_t, pgc_t, cell_playback_t, c_adt_t, ...) and the
mpucoder.com pages, written independently of mkvsmith's parser so a test
checks the parser against the format rather than against itself. Every
table starts on its own 2048-byte sector, as on real discs.

Only what the tests need is modelled; unused fields are zero.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SECTOR = 2048

# --- small field encoders -----------------------------------------------------


def bcd(value: int) -> int:
    return (value // 10) << 4 | value % 10


def dvd_time(seconds: float, fps: int = 30) -> bytes:
    """dvd_time_t: BCD hours, minutes, seconds, then frames with the rate.

    The frame byte's top two bits give the rate (01 = 25 fps, 11 = 30 fps).
    """
    whole = int(seconds)
    frames = round((seconds - whole) * fps)
    rate = 0b11 if fps == 30 else 0b01
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return bytes([bcd(hours), bcd(minutes), bcd(secs), rate << 6 | bcd(frames)])


def pad_to_sector(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % SECTOR)


# --- VM commands (8 bytes each) ----------------------------------------------


def set_angle(angle: int) -> bytes:
    """SetSTN with an immediate angle: audio, subpicture, angle in bytes 3-5,
    each with bit 7 set when it changes (libdvdnav vmcmd.c)."""
    return bytes([0x51, 0, 0, 0, 0, 0x80 | angle, 0, 0])


def nop() -> bytes:
    return bytes(8)


# --- attributes ---------------------------------------------------------------


@dataclass
class Video:
    """video_attr_t, 2 bytes: mpeg_version(2) video_format(2) aspect(2) ...

    mpeg 1 = MPEG-2; standard 0 = NTSC, 1 = PAL; aspect 0 = 4:3, 3 = 16:9.
    Byte 1 bit 7 / 6: Line 21 closed captions in field 1 / 2. The picture
    size is left 0 (720 wide), where the published layouts agree.
    """

    mpeg: int = 1
    standard: int = 0
    aspect: int = 0
    cc_field_1: bool = False
    letterboxed: bool = False  # byte 1 bit 2 (mpucoder, Linux DVB)
    resolution: int = 0  # byte 1 bits 5-3: 0 720, 1 704, 2 352, 3 352 half

    def encode(self) -> bytes:
        b0 = self.mpeg << 6 | self.standard << 4 | self.aspect << 2
        b1 = (0x80 if self.cc_field_1 else 0) | self.resolution << 3
        b1 |= 0x04 if self.letterboxed else 0
        return bytes([b0, b1])


@dataclass
class Audio:
    """audio_attr_t, 8 bytes.

    Byte 0: audio_format(3) multichannel(1) lang_type(2) app_mode(2); format
    0 = AC-3, 2/3 = MPEG, 4 = LPCM, 6 = DTS, 7 = SDDS; lang_type 1 = a
    language code follows. Byte 1: quantization(2) sample_frequency(2)
    reserved(1) channels-1 (3); quantization is 0/1/2 = 16/20/24-bit for
    LPCM and 3 for AC-3 and DTS, frequency 0 = 48 kHz, 1 = 96 kHz.
    Bytes 2-3: ISO 639-1 code. Byte 5: code_extension (2-4: visually
    impaired / director's comments). Byte 7: application info; in app_mode
    2, bit 3 = Dolby Surround. Quantization defaults to 0, not the 3 real
    AC-3 streams carry, to keep the bytes a disc-ID test was computed on.
    """

    lang: str = "en"
    channels: int = 2
    audio_format: int = 0
    code_extension: int = 1
    quantization: int = 0
    sample_frequency: int = 0
    app_mode: int = 0
    app_info: int = 0

    def encode(self) -> bytes:
        b0 = self.audio_format << 5 | 1 << 2 | self.app_mode
        b1 = self.quantization << 6 | self.sample_frequency << 4
        b1 |= (self.channels - 1) & 7
        lang = self.lang.encode("ascii")
        return bytes([b0, b1, *lang, 0, self.code_extension, 0, self.app_info])


@dataclass
class Subpicture:
    """subp_attr_t, 6 bytes: code_mode(3) zero(3) type(2), zero, lang(2),
    lang_extension, code_extension (9 = forced, 13-15 = commentary)."""

    lang: str = "en"
    code_extension: int = 1
    language_type: int = 1

    def encode(self) -> bytes:
        return bytes(
            [self.language_type, 0, *self.lang.encode("ascii"), 0, self.code_extension]
        )


# --- PGCs ----------------------------------------------------------------------


@dataclass
class Cell:
    """One cell_playback_t (24 bytes) plus its cell_position_t (4 bytes).

    block_mode: 0 not in a block, 1 first, 2 inside, 3 last; block_type
    1 = angle block. Sectors are relative to the title VOBs.
    """

    seconds: float
    first_sector: int
    last_sector: int
    vob_id: int = 1
    cell_id: int = 1
    block_mode: int = 0
    block_type: int = 0
    interleaved: bool = False
    seamless: bool = True
    still_time: int = 0
    command: int = 0

    def playback(self) -> bytes:
        b0 = (
            self.block_mode << 6
            | self.block_type << 4
            | (0x08 if self.seamless else 0)
            | (0x04 if self.interleaved else 0)
        )
        return (
            bytes([b0, 0, self.still_time, self.command])
            + dvd_time(self.seconds)
            + struct.pack(
                ">IIII",
                self.first_sector,
                self.first_sector if self.interleaved else 0,
                self.last_sector,
                self.last_sector,
            )
        )

    def position(self) -> bytes:
        return struct.pack(">HBB", self.vob_id, 0, self.cell_id)


@dataclass
class Pgc:
    """pgc_t. *programs* lists each program's first cell (1-based); by
    default every cell starts a program (a chapter).

    *audio*: physical audio stream per attribute slot, None for a slot the
    PGC doesn't enable. *subpictures*: per slot, (4:3, wide) physical
    streams, or None.
    """

    cells: list[Cell]
    programs: list[int] | None = None
    seconds: float | None = None
    audio: list[int | None] = field(default_factory=lambda: [0])
    subpictures: list[tuple[int, int] | None] = field(default_factory=list)
    pre: list[bytes] = field(default_factory=list)
    post: list[bytes] = field(default_factory=list)
    cell_commands: list[bytes] = field(default_factory=list)
    title: int = 0  # VTS title number, for the PGCIT category (0 = none)
    entry: bool = True

    def encode(self) -> bytes:
        programs = self.programs or list(range(1, len(self.cells) + 1))
        seconds = (
            self.seconds
            if self.seconds is not None
            else sum(cell.seconds for cell in self.cells)
        )
        header = bytearray(0xEC)
        header[2] = len(programs)
        header[3] = len(self.cells)
        header[4:8] = dvd_time(seconds)
        for slot, physical in enumerate(self.audio[:8]):
            if physical is not None:
                struct.pack_into(">H", header, 0x0C + 2 * slot, 0x8000 | physical << 8)
        for slot, streams in enumerate(self.subpictures[:32]):
            if streams is not None:
                narrow, wide = streams
                header[0x1C + 4 * slot : 0x20 + 4 * slot] = bytes(
                    [0x80 | narrow, wide, 0, 0]
                )

        body = bytearray()
        offsets = {}
        commands = self.pre + self.post + self.cell_commands
        if commands:
            offsets["commands"] = 0xEC + len(body)
            table = struct.pack(
                ">HHHH",
                len(self.pre),
                len(self.post),
                len(self.cell_commands),
                8 + 8 * len(commands) - 1,
            ) + b"".join(commands)
            body += table
        offsets["programs"] = 0xEC + len(body)
        body += bytes(programs)
        body += b"\x00" * (len(body) % 2)
        offsets["playback"] = 0xEC + len(body)
        body += b"".join(cell.playback() for cell in self.cells)
        offsets["position"] = 0xEC + len(body)
        body += b"".join(cell.position() for cell in self.cells)
        struct.pack_into(
            ">HHHH",
            header,
            0xE4,
            offsets.get("commands", 0),
            offsets["programs"],
            offsets["playback"],
            offsets["position"],
        )
        return bytes(header) + bytes(body)


# --- whole files ---------------------------------------------------------------


def _pgcit(pgcs: list[Pgc]) -> bytes:
    """VTS_PGCIT: count, zero, last byte, then 8-byte search pointers
    (category with the entry flag and title number; offset from the table)."""
    encoded = [pgc.encode() for pgc in pgcs]
    offset = 8 + 8 * len(pgcs)
    pointers = b""
    for pgc, data in zip(pgcs, encoded, strict=True):
        category = (0x80 if pgc.entry else 0) | (pgc.title & 0x7F)
        pointers += struct.pack(">BBHI", category, 0, 0, offset)
        offset += len(data)
    body = pointers + b"".join(encoded)
    return struct.pack(">HHI", len(pgcs), 0, 8 + len(body) - 1) + body


def _ptt_srpt(titles: list[list[tuple[int, int]]]) -> bytes:
    """VTS_PTT_SRPT: per title, its chapters as (PGC number, program)."""
    offset = 8 + 4 * len(titles)
    pointers = b""
    units = b""
    for chapters in titles:
        pointers += struct.pack(">I", offset + len(units))
        units += b"".join(struct.pack(">HH", pgcn, pgn) for pgcn, pgn in chapters)
    body = pointers + units
    return struct.pack(">HHI", len(titles), 0, 8 + len(body) - 1) + body


def _c_adt(cells: list[Cell]) -> bytes:
    """VTS_C_ADT: VOB count, zero, last byte, then 12-byte cell addresses."""
    seen: dict[tuple[int, int], Cell] = {}
    for cell in cells:
        seen.setdefault((cell.vob_id, cell.cell_id), cell)
    entries = b"".join(
        struct.pack(">HBBII", vob, cell_id, 0, cell.first_sector, cell.last_sector)
        for (vob, cell_id), cell in sorted(seen.items())
    )
    vobs = len({vob for vob, _cell in seen})
    return struct.pack(">HHI", vobs, 0, 8 + len(entries) - 1) + entries


def _vobu_admap(sectors: list[int]) -> bytes:
    """VTS_VOBU_ADMAP: last byte, then each VOBU's start sector."""
    entries = b"".join(struct.pack(">I", sector) for sector in sectors)
    return struct.pack(">I", 4 + len(entries) - 1) + entries


def vts_ifo(
    pgcs: list[Pgc],
    *,
    video: Video | None = None,
    audio: list[Audio] | None = None,
    subpictures: list[Subpicture] | None = None,
    titles: list[list[tuple[int, int]]] | None = None,
    vobu_sectors: list[int] | None = None,
) -> bytes:
    """A VTS_xx_0.IFO. *titles* defaults to one title per PGC, chapters per
    program; *vobu_sectors* to every cell's first sector."""
    audio = audio if audio is not None else [Audio()]
    subpictures = subpictures or []
    all_cells = [cell for pgc in pgcs for cell in pgc.cells]
    if titles is None:
        titles = [
            [(n, program) for program in range(1, len(pgc.programs or pgc.cells) + 1)]
            for n, pgc in enumerate(pgcs, start=1)
        ]
    if vobu_sectors is None:
        vobu_sectors = sorted({cell.first_sector for cell in all_cells})

    tables = [
        ("ptt", _ptt_srpt(titles)),
        ("pgcit", _pgcit(pgcs)),
        ("c_adt", _c_adt(all_cells)),
        ("vobu", _vobu_admap(vobu_sectors)),
    ]
    sector = 1
    placed: dict[str, int] = {}
    blobs = []
    for name, data in tables:
        placed[name] = sector
        padded = pad_to_sector(data)
        blobs.append(padded)
        sector += len(padded) // SECTOR

    mat = bytearray(SECTOR)
    mat[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", mat, 0x0C, sector * 2 - 1)  # vts_last_sector
    struct.pack_into(">I", mat, 0x1C, sector - 1)  # vtsi_last_sector
    struct.pack_into(">I", mat, 0x80, SECTOR - 1)  # vtsi_last_byte
    struct.pack_into(">I", mat, 0xC4, sector)  # vtstt_vobs
    struct.pack_into(">I", mat, 0xC8, placed["ptt"])
    struct.pack_into(">I", mat, 0xCC, placed["pgcit"])
    struct.pack_into(">I", mat, 0xE0, placed["c_adt"])
    struct.pack_into(">I", mat, 0xE4, placed["vobu"])
    mat[0x200:0x202] = (video or Video()).encode()
    struct.pack_into(">H", mat, 0x202, len(audio))
    for n, attrs in enumerate(audio[:8]):
        mat[0x204 + 8 * n : 0x20C + 8 * n] = attrs.encode()
    struct.pack_into(">H", mat, 0x254, len(subpictures))
    for n, attrs in enumerate(subpictures[:32]):
        mat[0x256 + 6 * n : 0x25C + 6 * n] = attrs.encode()
    return bytes(mat) + b"".join(blobs)


def vmg_ifo(titles: list[tuple[int, int]], provider: str = "") -> bytes:
    """A VIDEO_TS.IFO whose TT_SRPT maps title k to *titles*[k-1] =
    (VTS number, VTS title number)."""
    entries = b"".join(
        struct.pack(">BBHHBBI", 0, 1, 1, 0, vts, ttn, 0) for vts, ttn in titles
    )
    tt_srpt = struct.pack(">HHI", len(titles), 0, 8 + len(entries) - 1) + entries
    mat = bytearray(SECTOR)
    mat[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">H", mat, 0x3E, max((vts for vts, _ttn in titles), default=0))
    mat[0x40 : 0x40 + len(provider)] = provider.encode("ascii")
    struct.pack_into(">I", mat, 0x80, SECTOR - 1)
    struct.pack_into(">I", mat, 0xC4, 1)  # TT_SRPT in sector 1
    return bytes(mat) + pad_to_sector(tt_srpt)


# --- VOB packets -----------------------------------------------------------------


def _pts_field(pts: int, marker: int = 0x2) -> bytes:
    """The 5-byte PES PTS field: '0010' / '0011' marker, 33 bits, marker bits."""
    return bytes(
        [
            marker << 4 | ((pts >> 29) & 0x0E) | 1,
            (pts >> 22) & 0xFF,
            ((pts >> 14) & 0xFE) | 1,
            (pts >> 7) & 0xFF,
            ((pts << 1) & 0xFE) | 1,
        ]
    )


def _pack_header() -> bytes:
    """An MPEG-2 program stream pack header (SCR 0, mux rate 10.08 Mbit/s)."""
    return bytes.fromhex("000001ba4400040004010189c3f8")


def pes_pack(stream_id: int, payload: bytes, pts: int | None = None) -> bytes:
    """One 2048-byte VOB pack holding a single PES packet, padded with a
    padding stream so the pack is a whole sector."""
    header_data = _pts_field(pts) if pts is not None else b""
    flags = bytes([0x81, 0x80 if pts is not None else 0x00, len(header_data)])
    body = flags + header_data + payload
    packet = bytes([0, 0, 1, stream_id]) + struct.pack(">H", len(body)) + body
    pack = _pack_header() + packet
    remaining = SECTOR - len(pack)
    if remaining < 0:
        raise ValueError("payload too large for one pack")
    if remaining:
        if remaining < 6:
            raise ValueError("leftover space too small for a padding packet")
        pack += bytes([0, 0, 1, 0xBE]) + struct.pack(">H", remaining - 6)
        pack += b"\xff" * (remaining - 6)
    return pack


def subpicture_pack(substream: int, spu: bytes, pts: int) -> bytes:
    """A private-stream-1 pack carrying one subpicture unit (SPU)."""
    return pes_pack(0xBD, bytes([substream]) + spu, pts)


def video_pack(pts: int, size: int = 64) -> bytes:
    """A video pack with a PTS and a dummy elementary stream payload."""
    return pes_pack(0xE0, bytes([0, 0, 1, 0xB3]) + bytes(size - 4), pts)


def spu(size: int = 64) -> bytes:
    """A minimal SPU: its size, the control sequence offset, and a control
    sequence (start display, end) after some empty pixel data."""
    control = bytes([0x00, 0x00]) + struct.pack(">H", size - 6) + bytes([0x01, 0xFF])
    pixels = bytes(size - 4 - len(control))
    return struct.pack(">HH", size, 4 + len(pixels)) + pixels + control


# --- Line 21 closed captions (DVD-CC user data) ----------------------------------


def odd_parity(value: int) -> int:
    """A 7-bit EIA-608 byte with its parity bit (bit 7) set to make it odd."""
    value &= 0x7F
    return value | (0x80 if bin(value).count("1") % 2 == 0 else 0)


def text_pairs(text: str) -> list[tuple[int, int]]:
    """EIA-608 basic characters, two per pair (padded with a null)."""
    raw = text.encode("ascii")
    raw += b"\x00" * (len(raw) % 2)
    return [(raw[n], raw[n + 1]) for n in range(0, len(raw), 2)]


def cc_user_data(pairs: list[tuple[int, int]]) -> bytes:
    """MPEG-2 user data carrying Field-1 caption pairs in the DVD-CC wrapper.

    Layout as FFmpeg's mpeg12dec.c documents it: user_data_start_code, "CC",
    type 0x01, block size 0xF8, an attributes byte (bit 7: odd field first;
    bits 5-1: block count), then per block a Field-1 triplet (marker 0xFF,
    the pair) and a Field-2 triplet (marker 0xFE, a null pair: 0x80 0x80).
    """
    attributes = 0x80 | (len(pairs) & 0x1F) << 1
    blocks = b"".join(
        bytes([0xFF, odd_parity(a), odd_parity(b), 0xFE, 0x80, 0x80]) for a, b in pairs
    )
    return b"\x00\x00\x01\xb2CC\x01\xf8" + bytes([attributes]) + blocks


def video_cc_pack(pts: int, pairs: list[tuple[int, int]]) -> bytes:
    """A video pack: a GOP header, its caption user data, then a picture
    start code, in a PES packet with a PTS."""
    gop = b"\x00\x00\x01\xb8" + bytes([0x00, 0x08, 0x00, 0x00])
    picture = b"\x00\x00\x01\x00" + bytes([0x00, 0x0F, 0xFF, 0xF8])
    return pes_pack(0xE0, gop + cc_user_data(pairs) + picture, pts)
