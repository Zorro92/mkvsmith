# DVD subpictures and VobSub

The DVD subtitle format (the subpicture unit, SPU), its palette, and the
VobSub `.idx`/`.sub` pair mkvsmith writes for mkvmerge, which reads it into
Matroska `S_VOBSUB`. mkvmerge can't find subpicture streams in a VOB or EVO
itself, so `vobsub.py` reassembles them from the program stream. Blu-ray's
PGS is summarised at the end.

## Sources

| Letter | Source | Where |
|---|---|---|
| M | mpucoder, `spu.html`, `pgc.html` (all rights reserved) | `.agents/specs/dvd/mpucoder/` |
| F | FFmpeg `libavcodec/dvdsubdec.c` (LGPL-2.1+) | [sources/ffmpeg-dvdsubdec.c.md](sources/ffmpeg-dvdsubdec.c.md) |
| R | libdvdread `ifo_types.h` (GPL-2.0+) | [sources/libdvdread-ifo_types.h.md](sources/libdvdread-ifo_types.h.md) |
| V | VLC `modules/codec/spudec/parse.c` (LGPL-2.1+) | https://github.com/videolan/vlc/blob/master/modules/codec/spudec/parse.c |
| X | MPC-HC `src/Subtitles/VobSubFile.cpp` (the VSFilter code; GPL) | https://github.com/clsid2/mpc-hc/blob/develop/src/Subtitles/VobSubFile.cpp |
| K | MKVToolNix `src/input/r_vobsub.cpp`, `src/common/spu.cpp` (GPL-2.0-only) | https://codeberg.org/mbunkus/mkvtoolnix |
| MK | Matroska codec mappings, `S_VOBSUB` | https://www.matroska.org/technical/codec_specs.html |
| FP | FFmpeg `libavcodec/pgssubdec.c` (LGPL-2.1+) | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/pgssubdec.c |

## Carriage in the program stream

Subpictures travel in private stream 1 (`00 00 01 BD`) PES packets whose
first payload byte, the sub-stream ID, is `0x20`–`0x3F` (stream 0–31). An SPU
can span several PES packets; only the first need carry a PTS. The PTS is in
90 kHz ticks. (M, F, K)

Which physical stream a title's logical subtitle *n* uses comes from the
PGC's `PGC_SPST_CTL` (see [dvd-video.md](dvd-video.md)): one byte each for
4:3, wide, letterbox and pan&scan. mkvsmith reads the wide byte on 16:9
titles and the 4:3 byte otherwise. When the four differ, the letterbox and
pan&scan bytes name further physical streams carrying the same subtitles,
which is why listing every `0x20`–`0x3F` stream shows apparent duplicates.

## The subpicture unit

An SPU is one buffer of at most 53220 bytes. Only the header has a fixed
place; everything else is reached by offsets. (M)

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | SPDSZ: size of the whole SPU, which may span packets |
| 2 | 2 | SP_DCSQTA: offset of the control sequence table |
| 4 | … | pixel data (top field, bottom field), by convention |
| SP_DCSQTA | … | control sequences |

There is no further header field: bytes 4–5 are the first pixel data, and
the field addresses come from command `0x06`. (M; F reads only the two
words.) Reassembling an SPU means taking SPDSZ from its first packet and
appending same-stream payloads until that many bytes are in, as
`_SpuAccumulator` does (ending at a new PTS as well, and only there for
the HD DVD layout). ✔ real disc: on three retail NTSC DVDs, splitting by
SPDSZ gave exactly the SPUs, byte for byte, that splitting by PTS had.

### Control sequences

Each sequence starts with two big-endian words:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | SP_DCSQ_STM: delay from the PTS, in units of 1024/90000 s (≈11.38 ms) |
| 2 | 2 | offset of the next sequence; the last one points to itself |
| 4 | … | commands, ending with `0xFF` |

Seconds to STM: multiply by 90000/1024 and truncate (1 s = 87). (M) F
converts back as `(stm << 10) / 90` ms, and K as `stm * 1024` 90 kHz ticks.

| Code | Name | Arguments | Meaning |
|---|---|---|---|
| `0x00` | FSTA_DSP | none | forced start of display |
| `0x01` | STA_DSP | none | start of display |
| `0x02` | STP_DSP | none | stop of display |
| `0x03` | SET_COLOR | 2 bytes | palette index per pixel value, nibbles `e2 e1 \| p b` |
| `0x04` | SET_CONTR | 2 bytes | alpha per pixel value, same order; 0 transparent, 15 opaque |
| `0x05` | SET_DAREA | 6 bytes | x start, x end, y start, y end, 12 bits each |
| `0x06` | SET_DSPXA | 4 bytes | offsets of the top-field and bottom-field pixel data |
| `0x07` | CHG_COLCON | 2-byte size, then data | per-line colour and contrast changes; the size counts itself |
| `0xFF` | CMD_END | none | end of this sequence |

(M; the CHG_COLCON size rule is also how V skips it.) The first sequence
should hold at least SET_COLOR, SET_CONTR, SET_DAREA and SET_DSPXA (M).
✔ real disc: on a retail NTSC DVD, all 273 SPUs read from two streams
carried SET_COLOR and SET_CONTR, with the same indices (7, 2, 15, 2) and
alphas (0, 15, 13, 15) throughout.

CHG_COLCON data is a list of line entries (4 bytes: start line, 1–8 pixel
entries, end line), each followed by its pixel entries (6 bytes: start
column, colour nibbles, contrast nibbles), ended by `0f ff ff ff`. (M)

Forced display: `0x00` marks one SPU forced, independently of the stream.
F sets `AV_SUBTITLE_FLAG_FORCED` from it, V sets `b_forced`, and X shows only
those SPUs under `forced subs: ON`. A normal stream can carry a few forced
SPUs (for foreign-language lines); the IFO's code extension 9 marks a whole
stream forced.

A missing STP_DSP: K times the SPU up to the next `.idx` timestamp (the
last entry gets the average of the others); V uses about 5.5 s.

### Pixel data

Four pixel values: 0 background, 1 pattern, 2 emphasis 1, 3 emphasis 2.
Runs are coded in 4, 8, 12 or 16 bits (M):

| Bits | Form | Run length |
|---|---|---|
| 4 | `nncc` | 1–3 |
| 8 | `00nnnncc` | 4–15 |
| 12 | `0000nnnnnncc` | 16–63 |
| 16 | `000000nnnnnnnncc` | 64–255; count 0 = to the end of the line |

A line that ends off a byte boundary is padded with 4 zero bits. The top
field holds lines 1, 3, 5, …, the bottom field lines 2, 4, 6, …. (M, F)

### HD DVD subpictures

F recognises a variant used in EVO files: SPDSZ = 0 means 4-byte offsets
(size at bytes 2–5, control table offset at bytes 6–9), with extra commands
`0x83` (768-byte palette), `0x84` (256-byte contrast), `0x85` and `0x86`
(8-bit pixel area and 32-bit field offsets). See [hd-dvd.md](hd-dvd.md).

## The palette

Each PGC holds a 16-entry colour lookup table at PGC+`0xA4`, 4 bytes per
entry, ordered `0, Y, Cr, Cb`. (M, R with a "?" in its comment, F
`parse_ifo_palette`.) SET_COLOR's indices point into it.

F converts with the BT.601 limited-range formula, rescaling chroma by
255/224 (`YUV_TO_RGB1_CCIR`, `YUV_TO_RGB2_CCIR`). mkvsmith's `_ycbcr_to_rgb`
uses the same factors in integer form (298, 409, 100, 208, 516 over 256).
✔ real disc: a retail NTSC DVD's palette converted to the same RGB values
with both.

The `.idx` file and `S_VOBSUB` carry the palette already in RGB, and F (so
mpv), K and X take it as is; V keeps the YUV values. X's own IFO reader
(only for `.idx` below version 6) skips the chroma rescale, giving about
12% less saturated colours.

The palette is per PGC, and an `.idx` file has one `palette:` line, so a
title whose PGCs use different palettes can't be represented exactly.
mkvsmith uses the main PGC's palette.

Sources disagree: a 2010 FFmpeg mailing-list post describes the VTS
palette as `0, Y, Cb, Cr`, against M, R and F. It concerned MP4
extradata, and the three sources agree with each other.

## VobSub `.idx`

X's `ReadIdx` is the de facto grammar. The first line must contain
`VobSub index file, v`; `#` lines are comments; other lines are
`key: value`, keys lowercased, and **unknown keys set an error flag**.

| Key | Value |
|---|---|
| `size` | `W x H` canvas, default 720x480 |
| `org` | `x, y` origin |
| `scale` | `h%, v%` |
| `alpha` | `N%` |
| `smooth` | `OFF`, `ON` or `OLD` |
| `fadein/out` | `in, out` in ms |
| `align` | `ON\|OFF [at] LEFT\|CENTER\|RIGHT TOP\|CENTER\|BOTTOM` |
| `time offset` | ms, or `hh:mm:ss.ms`, optionally signed |
| `forced subs` | `ON` shows only forced SPUs |
| `palette` | 16 RGB hex values |
| `custom colors` | `ON\|OFF, tridx: XXXX, colors: c0, c1, c2, c3` |
| `langidx` | default stream |
| `id` | `xx, index: N`: starts a stream, two-letter language |
| `alt` | stream name |
| `delay` | `[-]hh:mm:ss:ms`, cumulative within a stream |
| `timestamp` | `hh:mm:ss:ms, filepos: <hex offset in .sub>` |

F reads only `palette:` and `size:`, and sets the decoder's frame size
from `size:`, so it must be 720x576 for PAL.

### What mkvmerge does with it

K requires version 7. `id:` starts a track (`und` if empty); `alt:` and
`langidx:` are ignored; `timestamp:` needs `filepos:`. Every other line goes
into the CodecPrivate, which is built once per file and **shared by every
track** in it. Nothing in K reads a forced flag, so mkvsmith writes none to
the `.idx` and sets FlagForced with `--forced-track`. Each SPU becomes one Matroska frame (MK: "the
CodecPrivate contains the palette: and size: lines").

## Blu-ray PGS (summary)

PGS is a stream of segments: `0x14` palette, `0x15` object, `0x16`
presentation composition, `0x17` window, `0x80` end. Palette entries are
5 bytes (`id, Y, Cr, Cb, alpha`); FP uses BT.709 above 576 lines and BT.601
otherwise. The forced flag is per object in the composition segment
(`0x40` in its composition flag), so no playlist or clip-info field says a
PGS stream is forced. mkvmerge passes PGS through as `S_HDMV/PGS`. (FP, MK)

## Open parser mismatches

- `_raw_vobsub_spu_scan` and `_try_scan_private2_for_spu` look for `00 04`
  at bytes 2–3 and read bytes 4–5 as an end offset, a header the format
  doesn't have. Private stream 2 carries the navigation packs on DVD-Video.
  The comment in `_build_pes_entry` ("end_off=0") describes the same field.
- `_parse_spu_control_state` accepts next-sequence offsets that go
  backwards (relying on a visited set); F rejects them.
- FSTA_DSP, STA_DSP and STP_DSP aren't read, so there's no per-SPU forced
  detection or start delay.
- EVO subpictures in the 4-byte-offset layout would be misparsed.
