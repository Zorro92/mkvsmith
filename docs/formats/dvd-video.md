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

F, V and H read the IFO through libdvdread, so they show how a field is
*interpreted*, not an independent reading of its *position*. A and M are
independent of R.

Conventions: offsets are hex, multi-byte values big-endian, bits numbered 7
(most significant) to 0. C bitfields in **R** are declared most significant
bits first. Tables start on a 2048-byte sector; a "sector pointer" is a
sector number relative to the start of the IFO file.

## Time values (`dvd_time_t`)

4 bytes: hours, minutes, seconds as BCD, then a frame byte whose bits 7-6
give the rate (`01` = 25 fps, `11` = 30 fps, i.e. NTSC 29.97) and bits 5-0
the frame count in BCD. (R `dvd_time_t`, M `pgc.md`)

## VIDEO_TS.IFO (VMGI_MAT)

| Offset | Size | Field | Source |
|---|---|---|---|
| 00 | 12 | `DVDVIDEO-VMG` | R, M `ifo.md` |
| 3E | 2 | number of title sets | R, M |
| 40 | 32 | provider identifier (text) | R, M |
| C4 | 4 | sector pointer to TT_SRPT | R, M |
| D4 | 4 | sector pointer to TXTDT_MG (text data, disc name) | R, M |

### TT_SRPT (title search pointers)

Header 8 bytes: number of titles (2), reserved (2), last byte (4). Then one
12-byte entry per title (R `title_info_t`, M `ifo_vmg.md`):

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | playback type |
| 1 | 1 | number of angles |
| 2 | 2 | number of chapters (PTTs) |
| 4 | 2 | parental management mask |
| 6 | 1 | title set (VTS) number |
| 7 | 1 | title number within the VTS (VTS_TTN) |
| 8 | 4 | start sector of the VTS |

## VTS_xx_0.IFO (VTSI_MAT)

| Offset | Size | Field | Source |
|---|---|---|---|
| 00 | 12 | `DVDVIDEO-VTS` | R, M `ifo.md` |
| C4 | 4 | sector of the title VOBs (VTSTT_VOBS) | R, M |
| C8 | 4 | sector pointer to VTS_PTT_SRPT | R, M |
| CC | 4 | sector pointer to VTS_PGCIT | R, M |
| D0 | 4 | sector pointer to VTSM_PGCI_UT (menus) | R, M |
| E0 | 4 | sector pointer to VTS_C_ADT | R, M |
| E4 | 4 | sector pointer to VTS_VOBU_ADMAP | R, M |
| 200 | 2 | video attributes | R, M |
| 202 | 2 | number of audio streams | R, M |
| 204 | 8 × 8 | audio attributes | R, M |
| 254 | 2 | number of subpicture streams | R, M |
| 256 | 32 × 6 | subpicture attributes | R, M |

### Video attributes (2 bytes)

Byte 0 (sources agree): bits 7-6 coding (0 MPEG-1, 1 MPEG-2), 5-4 standard
(0 NTSC, 1 PAL), 3-2 aspect (0 = 4:3, 3 = 16:9), bit 1 automatic pan-scan
disallowed, bit 0 automatic letterbox disallowed (M; A writes 0x200 for "no
pan-scan" and 0x100 for "no letterbox" in the 16-bit value; R calls bits 1-0
`permitted_df`, and P prints 1 as "only pan&scan", 2 as "only letterboxed").

Byte 1: bit 7 / bit 6 Line 21 closed captions in field 1 / field 2 (sources
agree). The rest differs; see [Sources disagree](#sources-disagree).

### Audio attributes (8 bytes)

(R `audio_attr_t`, M `ifo.md`)

| Byte | Bits | Field |
|---|---|---|
| 0 | 7-5 | coding: 0 AC-3, 2 MPEG-1, 3 MPEG-2 ext, 4 LPCM, 6 DTS |
| 0 | 4 | multichannel extension present |
| 0 | 3-2 | language type: 1 = language code in bytes 2-3 |
| 0 | 1-0 | application mode (karaoke / surround) |
| 1 | 7-6 | quantization (LPCM) |
| 1 | 5-4 | sample rate: 0 = 48 kHz |
| 1 | 2-0 | channels minus one |
| 2-3 | | ISO 639-1 language code |
| 5 | | code extension: 1 normal, **2 for visually impaired**, 3 director's comments, 4 alternate director's comments (R `dvd_audio_code_ext_t`, M `sprm.md` SPRM 17; F and H flag 2 as visually impaired and 3-4 as commentary) |

### Subpicture attributes (6 bytes)

(R `subp_attr_t`, M `ifo.md`) ✔ real disc: `01 00 65 6e 00 00` is an
English stream with language type 1. Across 10 retail DVDs (168 title-set
IFOs, 225 subpicture streams), 137 streams have language type 1, and only 17
of them have code extension 5 (captions); the rest are 0, 1, 9 (forced) or
13 (director's comments).

| Byte | Bits | Field |
|---|---|---|
| 0 | 7-5 | coding mode: 0 = 2-bit RLE |
| 0 | 1-0 | **language type**: 1 = language code in bytes 2-3 (not a hearing-impaired flag) |
| 2-3 | | ISO 639-1 language code |
| 4 | | language code extension |
| 5 | | code extension: 1 normal, 2 large, 3 children, **5 / 6 / 7 captions** (normal / large / children), 9 forced, 13 / 14 / 15 director's comments (R `dvd_subp_code_ext_t`, M `sprm.md` SPRM 19) |

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
| 02 | 1 | number of programs |
| 03 | 1 | number of cells |
| 04 | 4 | playback time (`dvd_time_t`) |
| 08 | 4 | prohibited user operations |
| 0C | 8 × 2 | audio stream control: bit 15 = available, bits 10-8 = physical stream |
| 1C | 32 × 4 | subpicture stream control: bit 31 = available; byte 0 bits 4-0 = stream for 4:3, byte 1 = wide, byte 2 = letterbox, byte 3 = pan-scan |
| 9C | 2 × 3 | next / previous / go-up PGC number |
| A2 | 1 | PG playback mode |
| A3 | 1 | still time |
| A4 | 16 × 4 | palette (0, Y, Cr, Cb) |
| E4 | 2 | offset to the command table (0 = none) |
| E6 | 2 | offset to the program map |
| E8 | 2 | offset to the cell playback table |
| EA | 2 | offset to the cell position table |

**Command table**: counts of pre-, post- and cell commands (2 each), last
byte (2), then the 8-byte commands in that order. (R `pgc_command_tbl_t`)

**Program map**: one byte per program, its first cell number.

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

Independent evidence so far:

- **Resolution:** A (a writer) puts the resolution code in bits 5-3
  (`(vres - 1) << 3`), as M describes. R reads a 2-bit picture size from
  bits 3-2 (P `read_video_attr`), and F uses that value for the width, but F
  reads through R, so it isn't independent. Two independent sources (M, A)
  against one (R). mkvsmith follows M and A.
- **Letterboxed:** A sets bit 1 for "crop" (letterboxed), as R does; M says
  bit 2. Two against one, the other way round. mkvsmith doesn't use the
  letterboxed value.
- **Real discs:** on 168 title-set IFOs of 10 retail DVDs, byte 1 was
  `00000000`, `10000000` or `11000000` (only the caption bits set), so every
  one is a full-width, non-letterboxed title under all readings. Settling it
  needs a disc with a 704- or 352-wide title, or a letterboxed 4:3 one.

## Open parser mismatches

Places where `dvdifo.py` doesn't match this document yet. Remove an entry
when it's fixed.

1. **Subpicture "hearing impaired"** reads byte 0 bit 0, which is the
   language type, so every subtitle with a language is flagged hearing
   impaired (and muxed with `--hearing-impaired-flag`). Should be code
   extension 5-7.
2. **VTS_C_ADT** is read with a 4-byte header instead of 8, so entries are
   misaligned (53 garbage entries instead of 81 on the disc above). Used by
   `_lookup_main_feature_range`.
3. **Video "letterboxed"** is read from byte 0 bit 1 (automatic pan-scan
   disallowed), which no source calls letterboxed. Harmless today: nothing
   uses the value.
4. **Audio commentary** is any code extension of 2 or more, so a track for
   the visually impaired (2) counts as commentary.
