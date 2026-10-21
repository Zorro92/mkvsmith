# Closed captions (line 21, CEA-608)

How DVDs carry line-21 captions, how they decode, and what `cc608.py`
does with them. Matroska has no closed-caption codec ID (MK), so captions
reach the MKV only as text converted from them.

## Sources

| Letter | Source | Where |
|---|---|---|
| CX | CCExtractor `src/lib_ccx/es_userdata.c` (licence header not checked) | https://github.com/CCExtractor/ccextractor/blob/master/src/lib_ccx/es_userdata.c |
| FC | FFmpeg `libavcodec/ccaption_dec.c` (LGPL-2.1+) | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/ccaption_dec.c |
| MP | McPoodle SCC_TOOLS docs, `CC_CODES.HTML`, `SCC_TOOLS.HTML` (no licence stated) | http://www.theneitherworld.com/mcpoodle/SCC_TOOLS/DOCS/ |
| MK | Matroska codec mappings | https://www.matroska.org/technical/codec_specs.html |

47 CFR 15.119, the US regulation that defines the caption grid, parity and
codes, is public domain and the source to cite for the decoding rules; it
hasn't been fetched yet (https://www.law.cornell.edu/cfr/text/47/15.119).

## Carriage on DVD

Line-21 data rides in the MPEG-2 video's GOP user data, not in a stream of
its own. The VTS video attributes say whether field 1 and field 2 carry
captions (byte 1 bits 7 and 6; [dvd-video.md](dvd-video.md)).

| Bytes | Field |
|---|---|
| `00 00 01 B2` | user data start code |
| `43 43 01 F8` | "CC" and the DVD caption header |
| 1 | bit 7 pattern flag (field order), bit 6 reserved, bits 5–1 count, bit 0 truncate flag |
| 3 × … | triplets: a marker byte, then two odd-parity data bytes |

(CX.) Each frame contributes two triplets, field 1 and field 2. A marker of
`0xFF` with the pattern flag's ordering is field 1, `0xFE` field 2; a
triplet is valid when `marker & 0xFE == 0xFE`. The truncate flag adds one
extra triplet to the count, and suppresses it on the last iteration. Some
DVDs append further `0xFE` triplets, which CX consumes. (CX)

This is not the ATSC A/53 `GA94` cc_data used by broadcast and by H.264 SEI
on Blu-ray, whose triplets carry `cc_valid` (bit 2) and `cc_type` (bits
1–0: 0 and 1 are 608 field 1 and 2, 2 and 3 are CEA-708). CX also
recognises SCTE-20, ReplayTV, Dish Network, Divicom and GXF user data.

## Decoding CEA-608

Each field carries one byte pair per frame, with odd parity in bit 7.
Control codes are usually sent twice, and a decoder ignores the second of
an identical pair. (MP, FC)

| First byte | Channel |
|---|---|
| `0x14` | CC1 control (field 1) |
| `0x1C` | CC2 control (field 1) |
| `0x15` | CC3 control (field 2) |
| `0x1D` | CC4 control (field 2) |

Control codes (second byte, with the channel's first byte) (MP):

| Code | Name | Effect |
|---|---|---|
| `0x20` | RCL | pop-on: write to the off-screen buffer |
| `0x21` | BS | back up one column and delete |
| `0x24` | DER | delete to end of row |
| `0x25`–`0x27` | RU2–RU4 | roll-up with 2–4 rows; clears pop-on and paint-on content |
| `0x28` | FON | flash on |
| `0x29` | RDC | paint-on: write to the screen directly |
| `0x2A` | TR | text mode, clears the text screen |
| `0x2B` | RTD | resume text mode |
| `0x2C` | EDM | erase displayed memory |
| `0x2D` | CR | next row; scrolls in roll-up and text modes, nothing in pop-on |
| `0x2E` | ENM | erase non-displayed memory |
| `0x2F` | EOC | swap displayed and off-screen memory |

Tab offsets TO1–TO3 are first byte `0x17` with `0x21`–`0x23`. Preamble
address codes (row and indent) are `0x10` with `0x40`–`0x5F`, or `0x11`–
`0x17` with `0x40`–`0x7F`; mid-row codes (colour, italics, underline) are
`0x11` with `0x20`–`0x2F`. Special characters are `0x11` with
`0x30`–`0x3F`; extended characters `0x12`/`0x13` with `0x20`–`0x3F`
replace the character before them. (MP, FC)

FC handles RCL, DER, RU2–4, RDC, RTD, EDM, CR, ENM (real-time mode only)
and EOC, and ignores BS, FON and TR. It treats `0x14`, `0x15` and `0x1C`
alike as control prefixes, so CC2's controls act on CC1's screen.

## Timing

One GOP's user data bundles a pair per frame for the whole GOP (about 15
frames) under one PTS. Pair *k* belongs to frame *k*: GOP PTS + *k* × 3003
ticks on NTSC (3600 on PAL). (MP; the per-pair offset is our reading.)

## Open parser mismatches

`cc608.py` follows FC's dispatch, with these differences from the format:

- No channel tracking: CC2 controls (`0x1C`) act on the CC1 screen, and
  CC2's text lands in it.
- Only field 1 is read, so CC3, CC4 and XDS (field 2) are never decoded.
- Every pair in a GOP gets the GOP's PTS, so caption times snap to about
  0.5 s steps.
- BS, FON, TR and ENM aren't handled; TO1–TO3 insert spaces rather than
  moving the cursor.
