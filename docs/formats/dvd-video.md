# DVD-Video structures

The parts of the DVD-Video format mkvsmith reads, field by field, in our own
words. Each table names its source; **✔ real disc** marks a field whose
layout was confirmed on a retail disc's bytes (discs are never named).
Where sources disagree, see [Sources disagree](#sources-disagree). Known
places where mkvsmith's parser doesn't match yet are listed under
[Open parser mismatches](#open-parser-mismatches).

## Sources

| | Source | Where |
|---|---|---|
| **R** | libdvdread `src/dvdread/ifo_types.h` (GPL-2.0-or-later) | [sources/libdvdread-ifo_types.h.md](sources/libdvdread-ifo_types.h.md) |
| **N** | libdvdnav `src/vm/vmcmd.c` (GPL-2.0-or-later) | [sources/libdvdnav-vmcmd.c.md](sources/libdvdnav-vmcmd.c.md) |
| **M** | mpucoder.com DVD pages (no licence stated) | local only: `.agents/specs/dvd/mpucoder/` (`ifo.md`, `ifo_vmg.md`, `ifo_vts.md`, `pgc.md`, `sprm.md`, `vmi*.md`, ...) |
| **P** | libdvdread `src/ifo_read.c` and `src/ifo_print.c` (GPL-2.0-or-later): how R's fields are read, checked and printed | [read](sources/libdvdread-ifo_read.c.md), [print](sources/libdvdread-ifo_print.c.md) |
| **A** | dvdauthor `src/dvdifo.c`, `src/dvdvob.c` (GPL-2.0-or-later): an authoring tool, so which bits a *writer* sets | [dvdifo](sources/dvdauthor-dvdifo.c.md), [dvdvob](sources/dvdauthor-dvdvob.c.md) |
| **F** | FFmpeg `libavformat/dvdvideodec.c`, `libavcodec/dvdsubdec.c` (LGPL-2.1-or-later), built on R | [demuxer](sources/ffmpeg-dvdvideodec.c.md), [subtitles](sources/ffmpeg-dvdsubdec.c.md) |
| **V** | VLC `modules/access/dvdread.c`, `dvdnav.c` (LGPL-2.1-or-later), built on R | [dvdread](sources/vlc-dvdread.c.md), [dvdnav](sources/vlc-dvdnav.c.md) |
| **H** | HandBrake `libhb/dvd.c`, `dvdnav.c` (GPL-2.0-only, so not in the repo), built on R | local only: `.agents/specs/dvd/handbrake-*.md` |
| **NT** | libdvdread `src/dvdread/nav_types.h`, `src/nav_read.c` (GPL-2.0-or-later): the NAV packs | https://code.videolan.org/videolan/libdvdread |
| **DN** | libdvdnav `src/dvdnav.c`, `searching.c`, `vm/play.c` (GPL-2.0-or-later) | https://code.videolan.org/videolan/libdvdnav |
| **LX** | Linux DVB API `include/uapi/linux/dvb/video.h`, `audio.h` (v4.18; LGPL-2.1+) | https://raw.githubusercontent.com/torvalds/linux/v4.18/include/uapi/linux/dvb/video.h |
| **PT** | Toshiba patent EP0883125 A2: the audio attribute table | https://data.epo.org/publication-server/rest/v1.2/patents/EP0883125NWA2/document.html |

F, V and H read the IFO through libdvdread, so they show how a field is
*interpreted*, not an independent reading of its *position*. A, M, LX and
PT are independent of R. The local Markdown copies of M lose the HTML
column spans that place multi-bit fields; bit positions here were read from
the live pages.

Conventions: offsets are hex, multi-byte values big-endian, bits numbered 7
(most significant) to 0. C bitfields in **R** are declared most significant
bits first. Tables start on a 2048-byte sector; a "sector pointer" is a
sector number relative to the start of the IFO file.

## Time values (`dvd_time_t`)

4 bytes: hours, minutes, seconds as BCD, then a frame byte whose bits 7-6
give the rate (`01` = 25 fps, `11` = 30 fps, i.e. NTSC 29.97; `00` and
`10` are illegal, M) and bits 5-0 the frame count in BCD (tens digit in
bits 5-4). (R `dvd_time_t`, M `pgc.md`)

How to read a 30 fps value is disputed; see
[Sources disagree](#sources-disagree).

## VIDEO_TS.IFO (VMGI_MAT)

| Offset | Size | Field | Source |
|---|---|---|---|
| 00 | 12 | `DVDVIDEO-VMG` | R, M `ifo.md` |
| 0C | 4 | last sector of the VMG set (end of the BUP) | R, M |
| 1C | 4 | last sector of the VMGI (the IFO) | R, M |
| 20 | 2 | version: byte 21 is major, minor nibbles (✔ real disc: `11` = 1.1) | R, M |
| 22 | 4 | category; **byte 23 = regions the disc may not play in** (bit *n* set: region *n*+1 prohibited) | M, M `sprm.md` SPRM 20 |
| 26 | 2 | number of volumes | R, M |
| 28 | 2 | volume number | R, M |
| 2A | 1 | side (1 or 2; P checks it) | R, M, P |
| 3E | 2 | number of title sets | R, M |
| 40 | 32 | provider identifier (text) | R, M |
| 60 | 8 | VMG POS code (meaning unknown) | R |
| 80 | 4 | last byte of VMGI_MAT | R, M |
| 84 | 4 | start byte of the First Play PGC (✔ real disc: `0x400`) | R, M |
| C0 | 4 | sector of the menu VOBs (VMGM_VOBS) | R, M |
| C4 | 4 | sector pointer to TT_SRPT | R, M |
| C8 | 4 | sector pointer to VMGM_PGCI_UT (menus) | R, M |
| CC | 4 | sector pointer to PTL_MAIT (parental levels) | R, M |
| D0 | 4 | sector pointer to VTS_ATRT (every title set's attributes) | R, M |
| D4 | 4 | sector pointer to TXTDT_MG (text data, disc name) | R, M |
| D8 | 4 | sector pointer to VMGM_C_ADT | R, M |
| DC | 4 | sector pointer to VMGM_VOBU_ADMAP | R, M |
| 100 | 2 | menu video attributes | R, M |
| 102 | 2 | number of menu audio streams (0 or 1) | R, M, P |
| 104 | 8 | menu audio attributes | R, M |
| 154 | 2 | number of menu subpicture streams (0 or 1) | R, M |
| 156 | 6 | menu subpicture attributes | R, M |

Menu audio attributes carry only coding mode, quantization, sample rate and
channels; menu subpicture attributes only the coding mode (M).

### TT_SRPT (title search pointers)

Header 8 bytes: number of titles (2), reserved (2), last byte (4). Then one
12-byte entry per title (R `title_info_t`, M `ifo_vmg.md`):

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | playback type (below) |
| 1 | 1 | number of angles |
| 2 | 2 | number of chapters (PTTs) |
| 4 | 2 | parental management mask |
| 6 | 1 | title set (VTS) number |
| 7 | 1 | title number within the VTS (VTS_TTN) |
| 8 | 4 | start sector of the VTS, counted from the start of the disc (VIDEO_TS.IFO is sector 0) |

Playback type (R `playback_type_t`, M `ifo_vmg.md`): bit 6 = 0 for one
sequential PGC, 1 for anything else (random, shuffle, stills, loops,
several PGCs); bits 5-2 = jump, link or call commands exist in cells,
pre/post commands, buttons, the title domain; bit 1 = chapter search
prohibited; bit 0 = time search prohibited. ✔ real disc: titles of type
`0x14` and `0x34`, both one sequential PGC.

### VTS_ATRT, PTL_MAIT and TXTDT_MG

**VTS_ATRT:** number of title sets (2), reserved (2), last byte (4), an
offset per title set (4 each); each entry is its last byte (4), the VTS
category (4) and a copy of that VTSI_MAT from `0x100` on, so every title
set's stream attributes can be read from VIDEO_TS.IFO alone. (M
`ifo_vmg.md`, R `vts_atrt_t`)

**PTL_MAIT:** number of countries (2), number of title sets (2), last byte
(4), then 8 bytes per country: country code (2), reserved (2), offset of
its table (2), reserved (2). Each table holds, for levels 8 down to 1, a
2-byte mask for the VMG and each title set. (M, R `ptl_mait_t`)

**TXTDT_MG** (R calls its own layout "Incomplete"): a 12-byte disc name,
2 unknown bytes, number of language units (2), last byte (4); then per
language unit (8 bytes): language code (2), reserved (1), character set
(1), start byte of its text (4). Character sets: 0x00 reserved (Unicode),
0x01 ISO 646, 0x10 JIS Roman and Kanji, 0x11 ISO 8859-1, 0x12 Shift JIS
Kanji. (R `txtdt_mgi_t`, `txtdt_lu_t`)

## VTS_xx_0.IFO (VTSI_MAT)

| Offset | Size | Field | Source |
|---|---|---|---|
| 00 | 12 | `DVDVIDEO-VTS` | R, M `ifo.md` |
| 0C, 1C, 20 | | as in VMGI_MAT | R, M |
| 22 | 4 | category: 0 unspecified, 1 karaoke | M |
| 80 | 4 | last byte of VTSI_MAT | R, M |
| C0 | 4 | sector of the menu VOBs (VTSM_VOBS) | R, M |
| C4 | 4 | sector of the title VOBs (VTSTT_VOBS) | R, M |
| C8 | 4 | sector pointer to VTS_PTT_SRPT | R, M |
| CC | 4 | sector pointer to VTS_PGCIT | R, M |
| D0 | 4 | sector pointer to VTSM_PGCI_UT (menus) | R, M |
| D4 | 4 | sector pointer to VTS_TMAPTI (time maps; ✔ real disc: set on all 12 sampled) | R, M |
| D8 | 4 | sector pointer to VTSM_C_ADT | R, M |
| DC | 4 | sector pointer to VTSM_VOBU_ADMAP | R, M |
| E0 | 4 | sector pointer to VTS_C_ADT | R, M |
| E4 | 4 | sector pointer to VTS_VOBU_ADMAP | R, M |
| 100–157 | | menu video, audio and subpicture attributes, as in VMGI_MAT | R, M |
| 200 | 2 | video attributes | R, M |
| 202 | 2 | number of audio streams (R: a reserved byte, then the count) | R, M |
| 204 | 8 × 8 | audio attributes | R, M |
| 254 | 2 | number of subpicture streams (R: likewise) | R, M |
| 256 | 32 × 6 | subpicture attributes | R, M |
| 318 | 8 × 24 | multichannel extension, per audio stream (karaoke guide channels) | R, M |

### Video attributes (2 bytes)

Byte 0 (sources agree): bits 7-6 coding (0 MPEG-1, 1 MPEG-2), 5-4 standard
(0 NTSC, 1 PAL), 3-2 aspect (0 = 4:3, 3 = 16:9), bit 1 automatic pan-scan
disallowed, bit 0 automatic letterbox disallowed (M; A writes 0x200 for "no
pan-scan" and 0x100 for "no letterbox" in the 16-bit value; R calls bits 1-0
`permitted_df`, and P prints 1 as "only pan&scan", 2 as "only letterboxed").

Byte 1: bit 7 / bit 6 Line 21 closed captions in field 1 / field 2, bit 0
film (1) or camera (0), PAL only (sources agree). The rest differs; see
[Sources disagree](#sources-disagree). A 4:3 title should carry permitted
display 3 (dvdauthor's `dvdunauthor` warns otherwise).

### Audio attributes (8 bytes)

(R `audio_attr_t`, M `ifo.md`, PT, LX)

| Byte | Bits | Field |
|---|---|---|
| 0 | 7-5 | coding: 0 AC-3, 2 MPEG-1 (or MPEG-2 without extension), 3 MPEG-2 with extension, 4 LPCM, 6 DTS, 7 SDDS; 1 and 5 reserved (PT, M, LX, P) |
| 0 | 4 | multichannel extension present |
| 0 | 3-2 | language type: 1 = language code in bytes 2-3 |
| 0 | 1-0 | application mode: 1 karaoke, 2 surround |
| 1 | 7-6 | quantization / DRC: LPCM 0 / 1 / 2 = 16 / 20 / 24 bits; AC-3 and DTS always 3; MPEG 0 no DRC, 1 DRC (✔ real disc: 3 on every AC-3 stream of 12 title sets) |
| 1 | 5-4 | sample rate: 0 = 48 kHz, 1 = 96 kHz |
| 1 | 3 | reserved |
| 1 | 2-0 | channels minus one |
| 2-3 | | ISO 639-1 language code |
| 4 | | reserved (language code extension) |
| 5 | | code extension: 1 normal, **2 for visually impaired**, 3 director's comments, 4 alternate director's comments (R `dvd_audio_code_ext_t`, M `sprm.md` SPRM 17; F and H flag 2 as visually impaired and 3-4 as commentary) |
| 6 | | reserved |
| 7 | | application info: in karaoke mode, channel assignment (bits 6-4), karaoke version, intro and solo/duet flags; **in surround mode, bit 3 = Dolby Surround encoded** (R `app_info`, M, A) |

The sub-stream that carries audio stream *n* depends on its coding: AC-3
`0x80` + *n*, DTS `0x88` + *n*, LPCM `0xA0` + *n* (all in private stream 1),
MPEG audio stream ID `0xC0` + *n*. (F `dvdvideo_audio_stream_analyze`, M
`dvdmpeg.md`) ✔ real disc: a retail NTSC DVD's first audio stream is DTS
stream 0, sub-stream `0x88`, beside AC-3 streams `0x81` and `0x82`.
mkvmerge numbers such a track `(sub-stream << 32) | 0xBD`
(`0x88000000BD`), and video by its stream ID (`0xE0`).

### Subpicture attributes (6 bytes)

(R `subp_attr_t`, M `ifo.md`) ✔ real disc: `01 00 65 6e 00 00` is an
English stream with language type 1. Across 10 retail DVDs (168 title-set
IFOs, 225 subpicture streams), 137 streams have language type 1, and only 17
of them have code extension 5 (captions); the rest are 0, 1, 9 (forced) or
13 (director's comments).

| Byte | Bits | Field |
|---|---|---|
| 0 | 7-5 | coding mode: 0 = 2-bit RLE (R adds 1 extended, 2 other) |
| 0 | 4-2 | reserved |
| 0 | 1-0 | **language type**: 1 = language code in bytes 2-3 (not a hearing-impaired flag) |
| 2-3 | | ISO 639-1 language code |
| 4 | | language code extension |
| 5 | | code extension: 0 unspecified, 1 normal, 2 large, 3 children, **5 / 6 / 7 captions** (normal / large / children), 9 forced, 13 / 14 / 15 director's comments (normal / large / children); 4, 8, 10-12 reserved (R `dvd_subp_code_ext_t`, M `sprm.md` SPRM 19) |

Hearing-impaired (captions) subtitles are the ones with code extension 5-7:
H labels 5 / 6 / 7 "Closed Caption" (plain / large type / children), 9
"Forced" and 13-15 "Director's Commentary"; V marks 9 as forced. None of R,
F, V or H treats the type bits as a hearing-impaired flag. F alone tests
byte 4 (the language extension) for 9 to mark a stream forced, where M, R,
H and V use byte 5.

Audio code extensions seen on the same discs: 0, 1 and 3 (director's
comments, 15 tracks); none used 2.

## VTS_PTT_SRPT (chapters → PGC)

Header 8 bytes: number of titles (2), reserved (2), last byte (4); then a
4-byte offset (from the table start) per title. Each title's entries are 4
bytes: PGC number (2), program number (2), one per chapter. (R
`ptt_info_t`, M `ifo_vts.md`)

## VTS_PGCIT and the PGC

Header 8 bytes: number of PGCs (2), reserved (2), last byte (4). Then 8 bytes
per PGC: category (4: byte 0 bit 7 = entry PGC, bits 6-0 = title number;
bytes 2-3 parental mask) and the PGC's offset from the table start (4).
(R `pgci_srp_t`, M `ifo_vts.md`)

PGC header, 0xEC bytes (R `pgc_t`, M `pgc.md`):

| Offset | Size | Field |
|---|---|---|
| 00 | 2 | reserved (✔ real disc: `00 00` in every PGC sampled) |
| 02 | 1 | number of programs |
| 03 | 1 | number of cells |
| 04 | 4 | playback time (`dvd_time_t`) |
| 08 | 4 | prohibited user operations |
| 0C | 8 × 2 | audio stream control: bit 15 = available, bits 10-8 = stream number, added to the codec's base ID (see the audio attributes) |
| 1C | 32 × 4 | subpicture stream control: bit 31 = available; byte 0 bits 4-0 = stream for 4:3, then bits 4-0 of byte 1 = wide, byte 2 = letterbox, byte 3 = pan-scan |
| 9C | 2 × 3 | next / previous / go-up PGC number (a menu's go-up may be `FFFF`, resume) |
| A2 | 1 | PG playback mode: 0 sequential; otherwise bit 7 = shuffle (1) or random (0), bits 6-0 = program count − 1 |
| A3 | 1 | still time, 255 = infinite |
| A4 | 16 × 4 | palette (0, Y, Cr, Cb) |
| E4 | 2 | offset to the command table (0 = none) |
| E6 | 2 | offset to the program map |
| E8 | 2 | offset to the cell playback table |
| EA | 2 | offset to the cell position table |

F and H offer the letterbox stream only when automatic pan-scan isn't the
only permitted display, and the pan-scan stream likewise.

**Command table**: counts of pre-, post- and cell commands (2 each), last
byte (2), then the 8-byte commands in that order; at most 128 in all (M).
(R `pgc_command_tbl_t`)

**Program map**: one byte per program, its first cell number (numbering
from 1), padded with zero to a word boundary (M).

**Cell playback**, 24 bytes per cell (R `cell_playback_t`):

| Byte | Bits | Field |
|---|---|---|
| 0 | 7-6 | block mode: 0 not in a block, 1 first, 2 inside, 3 last |
| 0 | 5-4 | block type: 1 = angle block |
| 0 | 3 | seamless playback |
| 0 | 2 | interleaved |
| 0 | 1 | STC discontinuity |
| 0 | 0 | seamless angle change |
| 1 | 6 | playback mode (still after each VOBU) |
| 1 | 5 | restricted |
| 1 | 4-0 | karaoke cell type (0 none, 1 title picture, 2 introduction, …) |
| 2 | | still time |
| 3 | | cell command number |
| 4-7 | | playback time (`dvd_time_t`) |
| 8-11 | | first VOBU start sector |
| 12-15 | | first ILVU end sector |
| 16-19 | | last VOBU start sector |
| 20-23 | | last VOBU end sector |

**Cell position**, 4 bytes per cell: VOB ID (2), reserved (1), cell ID (1).

## VTS_C_ADT (cell address table)

✔ real disc: a retail DVD's table has header `00 0a 00 00 00 00 03 d3` (10
VOBs, last byte 979) and 81 entries, the first VOB 1 / cell 1 at sectors 0-54.
On all 168 title-set IFOs of 10 retail DVDs, (last byte + 1 - 8) is a whole
number of 12-byte entries.

mkvsmith once read this table 4 bytes off. Most tables then came out
empty and titles fell back to their PGC's own cells, but on 2 of the 10
discs the misread produced valid-looking entries, and 7 short titles were
cut from the wrong part of the VOBs. Read correctly, every title's range
matches its PGC's own cells.

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | number of VOBs |
| 2 | 2 | reserved |
| 4 | 4 | last byte of the table |
| 8 | 12 each | VOB ID (2), cell ID (1), reserved (1), start sector (4), last sector (4) |

Entries = (last byte + 1 - 8) / 12. (R `c_adt_t` / `cell_adr_t`, M `ifo.md`)

## VTS_VOBU_ADMAP

Last byte of the table (4), then each VOBU's start sector (4 each).
Entries = (last byte + 1 - 4) / 4. (R `vobu_admap_t`, M `ifo.md`)

## VTS_TMAPTI (time maps)

Number of PGCs (2), reserved (2), last byte (4), an offset per PGC (4
each). Each map: time unit in seconds (1), reserved (1), entry count (2, at
most 2048), then a sector per time unit (4 each; bit 31 = the next entry is
in another cell). Every PGC has one, even if empty. (M `ifo_vts.md`, R
`vts_tmapt_t`)

## Menus (VMGM_PGCI_UT, VTSM_PGCI_UT)

A language-unit table: per unit a language code (2), its extension (1) and
which menus exist (`0x80` root or title, `0x40` subpicture, `0x20` audio,
`0x10` angle, `0x08` chapter), each unit pointing at its own PGC table.
A menu PGC's category byte 0 has bit 7 = entry and bits 3-0 = menu type
(2 title, 3 root, 4 subpicture, 5 audio, 6 angle, 7 chapter). Menus have
their own cell and VOBU address tables. (M, R `pgci_ut_t`)

## NAV packs

Each VOBU starts with a navigation pack: one sector with the pack header
(14 bytes), a system header, then private stream 2 (`0xBF`) twice: the PCI
(sub-stream `0x00` at byte `0x2C`, data from `0x2D`) and the DSI
(sub-stream `0x01` at `0x406`, data from `0x407`). (M `pci_pkt.md`,
`dsi_pkt.md`; NT)

| Packet | Offset | Field |
|---|---|---|
| PCI | 00 | this pack's sector |
| | 04 | VOBU category (analogue protection bits) |
| | 08 | prohibited user operations |
| | 0C, 10 | VOBU start and end PTM (90 kHz) |
| | 14 | end PTM of a sequence end code |
| | 18 | cell elapsed time (`dvd_time_t`) |
| | 3C | non-seamless angle destinations, 9 × 4 |
| | 60 | highlight (buttons) |
| DSI | 00, 04 | SCR, this pack's sector |
| | 08 | the VOBU's last sector, relative |
| | 0C–14 | ends of the first three reference frames |
| | 18 | VOB ID (2) |
| | 1B | cell ID (1) |
| | 1C | cell elapsed time |
| | 20 | seamless playback: ILVU flags, ILVU end, next ILVU start and size, the VOB's first and last video PTM, audio gaps for 8 streams |
| | B4 | seamless angle destinations, 9 × (offset 4, size 2) |
| | EA | VOBU search: next video, 19 forward, next, previous, 19 backward, previous video |
| | 192 | sync: 8 audio, 32 subpicture offsets |

mkvsmith reads the VOB and cell IDs (to tell which branch an interleaved
VOBU belongs to) and the start and end PTMs (for durations).

## Stream IDs in the VOBs

| ID | Stream |
|---|---|
| `0xBA`, `0xBB` | pack header, system header |
| `0xE0` | video |
| `0xC0`–`0xC7` | MPEG audio |
| `0xBD` | private stream 1: sub-streams `0x20`–`0x3F` subpictures, `0x80`–`0x87` AC-3, `0x88`–`0x8F` DTS, `0x90`–`0x97` reserved for SDDS, `0xA0`–`0xA7` LPCM |
| `0xBF` | private stream 2: NAV packs |

(M `dvdmpeg.md`, F `mpeg.c`.) In private stream 1, AC-3, DTS and LPCM
packets follow the sub-stream ID with 3 bytes: the number of frame headers
and a 2-byte pointer to the first access unit; LPCM then has its own
3-byte header (see [codecs.md](codecs.md)). Packs are always 2048 bytes and
MPEG-2.

## mkvsmith's own heuristics

These come from real discs, not from a source:

- **Padded cells:** a cell whose sectors exceed 1.5 × what the DVD-Video
  maximum mux rate (10.08 Mbit/s, 615 sectors/s) allows for its time is
  padding (`_cell_is_padded`).
- **Decoy chains:** at least 8 film-length PGC chains and 3 backward jumps
  mark a protected disc's decoys (`_settle_dvd_protection`).
- **Decoy angle:** a SetSTN that selects angle 11 marks a decoy
  (`_scan_pgc_angle_commands`).

## VM commands (8 bytes)

`vm_getbits(command, start, count)` in **N** numbers bits from 63 (byte 0,
bit 7) down to 0, so bit *b* is in byte (63 - *b*) / 8.

| Bytes | Command | Fields |
|---|---|---|
| `51 ..` / `41 ..` | SetSTN, immediate / from register | byte 3 audio, byte 4 subpicture, byte 5 angle; bit 7 of each = "set this one" (N `print_system_set`) ✔ real disc |
| `30 02 .. ..` | JumpTT | title number in byte 5 |
| `30 06 .. ..` | JumpSS | byte 5 bits 7-6: 0 first play, 1 VMGM menu, 2 VTSM menu, 3 VMGM PGC (number in bytes 2-3) |
| `30 08 .. ..` | CallSS | as JumpSS; byte 4 = resume cell ✔ real disc |
| `2x ..` | Link with a comparison | byte 1 bit 7 = compare with an immediate, bits 6-4 the comparison (2 = equal), bits 3-0 the link: 4 LinkPGCN, 5 LinkPTTN, 6 LinkPGN, 7 LinkCN; target in bytes 6-7 ✔ real disc |

## Sources disagree

**Video attributes, byte 1, bits 5-0.**

| Bits | M `ifo.md` | R `video_attr_t` |
|---|---|---|
| 5-3 | resolution, 3 bits (0 = 720, 1 = 704, 2 = 352 × full height, 3 = 352 × half) | 5 unknown, 4 bit rate, 3 picture size (high bit) |
| 2 | letterboxed | picture size (low bit) |
| 1 | reserved | letterboxed |
| 0 | film (PAL only) | film mode |

LX (`video.h`) reads like M: bits 5-3 resolution, bit 2 letterboxed, bit 1
not listed.

Independent evidence so far:

- **Resolution:** A (a writer) puts the resolution code in bits 5-3
  (`(vres - 1) << 3`), as M and LX describe. R reads a 2-bit picture size
  from bits 3-2 (P `read_video_attr`), and F uses that value for the width,
  but F reads through R, so it isn't independent. Three independent sources
  (M, A, LX) against one (R). mkvsmith follows M, A and LX. Under R's
  reading a 704-wide title would come out 352. M marks values 4-7 reserved.
- **Letterboxed:** A sets bit 1 for "crop" (letterboxed), as R does; M and
  LX say bit 2. Two against two. mkvsmith doesn't use the letterboxed
  value.
- **Real discs:** on 168 title-set IFOs of 10 retail DVDs, byte 1 was
  `00000000`, `10000000` or `11000000` (only the caption bits set), so every
  one is a full-width, non-letterboxed title under all readings. Settling it
  needs a disc with a 704- or 352-wide title, or a letterboxed 4:3 one.

**Time values at 30 fps.** DN (`dvdnav_convert_time`) and H
(`dvdtime2msec`) take the seconds as real seconds plus frames / 29.97.
mkvsmith (`_bcd_playback_seconds`) reads the value as a 30-frame timecode
played at 30000/1001, so every second is 1.001 s. ✔ real disc: a retail NTSC
cell's IFO time is 304.868 s, and its NAV packs' PTMs span 305.172 s, which
is the ×1.001 reading.

**DSI audio gaps.** M lists 16 stop PTMs, then 16 gap lengths; NT groups
them per stream (`stp_ptm1`, `stp_ptm2`, `gap_len1`, `gap_len2` × 8). Same
128 bytes, different order. mkvsmith doesn't read them.

**ILVU size.** M counts it in sectors, NT's comment in bytes. Not read.

## Open parser mismatches

Places where `dvdifo.py` doesn't match this document yet. Remove an entry
when it's fixed.

1. **Video "letterboxed"** is read from byte 0 bit 1 (automatic pan-scan
   disallowed), which no source calls letterboxed. Harmless today: nothing
   uses the value.
2. **PGC still time and playback mode** are swapped (`_PGCOffset`: `A2`
   still, `A3` mode). Nothing reads them yet.
3. **"Offset mode" stream control.** `_parse_pgc_stream_languages` treats
   PGC bytes 0-1 as a flag that turns the stream control tables into
   offsets. No source describes it; those bytes are reserved, and `00 00`
   on every real PGC sampled, so the path never runs.
4. **TXTDT_MG** is parsed with a layout and character-set codes that don't
   match R's. No sampled disc has the table, so it has never run on real
   bytes.
5. **Unsourced values.** `RES_MAP` has widths for resolution codes 4 (544)
   and 5 (480), which M calls reserved. Subpicture coding mode 3 is
   labelled "line-21 CC". Command counts are masked with `0x3F`.

Not modelled yet:

- **Visually impaired audio** (code extension 2) could be muxed with
  mkvmerge's `--visual-impaired-flag`. mkvsmith has no such stream flag,
  and none of the 10 retail DVDs checked uses it.
- **BUP fallback:** P opens the `.BUP` copy when an IFO won't read or
  validate (its `CHECK_VALUE`s), and mkvsmith doesn't.
- **STC discontinuities:** the per-cell flag isn't read; mkvmerge's
  timestamp handling covers the joins.
- **Still cells:** still times (PGC, cell, VOBU still) aren't modelled, so
  a slideshow title comes out a few frames long.
- **Random and shuffle PGCs** are muxed in program order.
- **Angles:** angle blocks are reduced to the selected angle; there's no
  per-angle output.
- **Not read at all:** parental levels, menus, time maps and the region
  mask.
- **Other disc types:** DVD-Audio (`AUDIO_TS`) and DVD-VR (`DVD_RTAV`)
  discs aren't told apart from DVD-Video.
