# Codec headers

The bitstream headers that say what a stream is when the disc's own
metadata doesn't, or says too little: video colour, HDR and interlacing,
and audio layout and extensions. mkvsmith parses only H.264 itself
(`h264.py`); the rest is here so a cheap header check can be added where it
pays, without probing streams with an external tool.

## Sources

| Letter | Source | Where |
|---|---|---|
| FF | FFmpeg source (LGPL-2.1+ for the files below) @ `c50d6fca7ac1` | https://github.com/FFmpeg/FFmpeg |
| LBR | libbluray `mpls_parse.c`, `mpls_data.h`, `clpi_data.h` (LGPL-2.1+) | [sources/](sources/) |
| MCOD | Matroska codec mappings (CC BY 4.0) | https://www.matroska.org/technical/codec_specs.html |
| MBAM | Matroska block additional mappings (CC BY 4.0) | https://www.matroska.org/technical/block_additional_mappings.html |
| NEWS | MKVToolNix NEWS | https://mkvtoolnix.download/doc/NEWS.md |
| MAN | mkvmerge manual | https://mkvtoolnix.download/doc/mkvmerge.html |
| ITU | ITU-T H.262, H.264, H.265 (free download, no redistribution) | https://www.itu.int/rec/T-REC-H.264 etc. |
| ETSI | ETSI TS 102 366 (AC-3, E-AC-3), TS 102 114 (DTS) | https://www.etsi.org/ |

The ITU, ETSI, SMPTE and Dolby documents weren't fetched for this page;
layouts come from FF and the clause numbers cited in `h264.py`.

Marks: **✔ mkvmerge v96 experiment** is a test on synthetic streams (made
with test tools outside the shipped code), not a real disc.

## Video

### H.264 (BD, HD DVD)

`h264.py` parses the SPS (H.264 7.3.2.1) with its VUI (E.1.1) and HRD
(E.1.2), the PPS, slice headers and picture-timing SEI (D.1.3), and decides
interlaced, progressive or pulldown per frame the way FF's `h264_slice.c`
does. Frames with `pic_struct` 5 or 6 (a repeated field) are pulldown,
so progressive. A file is interlaced when at least 95% of at least 24
sampled frames are, with one field order.

VUI fields, in order: `aspect_ratio_idc` (255: explicit `sar_width`,
`sar_height`), overscan, `video_format` (3 bits), `video_full_range_flag`,
`colour_primaries`, `transfer_characteristics`, `matrix_coefficients`
(8 bits each), chroma location (two ue(v)), timing (`num_units_in_tick`,
`time_scale`, `fixed_frame_rate_flag`), HRD, `pic_struct_present_flag`,
bitstream restrictions. `_parse_vui` walks past the aspect, colour and
chroma fields without keeping them.

The colour codes are CICP (H.273), the numbering mkvmerge's `--color-*`
options take. Transfer characteristics: 1 BT.709, 6 SMPTE 170M, 14 BT.2020
10-bit, 15 BT.2020 12-bit, 16 SMPTE ST 2084 (PQ), 18 ARIB STD-B67 (HLG).
(MAN)

### HEVC (UHD BD)

The VUI (H.265 E.2.1) has the same signal-type, colour and chroma fields,
plus `field_seq_flag` and `frame_field_info_present_flag` (FF
`hevc/ps.c`). HDR travels in SEI (FF `sei.h`, `h2645_sei.c`):

| SEI | Payload | Holds |
|---|---|---|
| 137 | mastering display colour volume (SMPTE ST 2086) | three primaries and the white point (x, y in 0.00002), max and min luminance (0.0001 cd/m²) |
| 144 | content light level | MaxCLL, MaxFALL (u16, cd/m²) |
| 147 | alternative transfer characteristics | HLG compatibility |
| 4 | user data registered ITU-T T.35 | HDR10+ (country `0xB5`, provider `0x003C`); Dolby uses provider `0x003B` |

The disc itself carries the static HDR10 values too: libbluray parses a
playlist extension (ID1 3, ID2 5) whose entries hold the dynamic range
type, the display primaries and white point, max and min mastering
luminance, MaxCLL and MaxFALL, as u16 each (LBR `_parse_static_metadata`;
see [bluray.md](bluray.md)). Their units haven't been checked against a real
disc's SEI.

HDR10+ on the disc side is only a flag (`hdr_plus_flag`) in the stream
table; the metadata itself is per frame, in SEI.

### Dolby Vision

The decoder configuration record (FF `dovi_isom.c`): `dv_version_major`,
`dv_version_minor` (u8 each), then `dv_profile` (7 bits), `dv_level` (6),
`rpu_present_flag`, `el_present_flag`, `bl_present_flag` (1 each), and from
the fifth byte `dv_bl_signal_compatibility_id` (4) and `dv_md_compression`
(2).

Matroska keeps it in a `BlockAdditionMapping`: type `dvcC` for profiles
0–7, `dvvC` for 8–10 and 20, `dvwC` for 11–19; `hvcE` holds the HEVC
enhancement layer's configuration (MBAM).

On UHD BD the base layer and the enhancement layer are separate PIDs in one
M2TS (`0x1011` and `0x1015`; see [bluray.md](bluray.md)). mkvmerge 81.0
(2023-12-02) and later merge the two into one track and no longer list the
enhancement layer (NEWS). Earlier versions show two video tracks.

### MPEG-2 (DVD, some BD)

Sequence header: `aspect_ratio_information` (4 bits: 1 square, 2 4:3, 3
16:9, 4 2.21:1, as display aspect) and `frame_rate_code` (4 bits). Sequence
extension: `progressive_sequence`. Picture coding extension:
`top_field_first`, `repeat_first_field`, `progressive_frame`. The optional
sequence display extension carries colour and the display size. (FF
`mpeg12dec.c`; H.262 6.2.2–6.2.3)

Soft telecine is `progressive_sequence` 0 with `progressive_frame` 1 and
`repeat_first_field` alternating; hard interlace has `progressive_frame` 0.

### VC-1 (BD, HD DVD)

The advanced-profile sequence header gives profile, level, chroma format,
`pulldown`, `interlace`, `tfcntrflag`, `finterpflag`, `psf`, then optional
display size, aspect ratio, frame rate and colour (`color_prim`,
`transfer_char`, `matrix_coef`). (FF `vc1.c`; SMPTE 421M not consulted)

### What mkvmerge takes from video headers

✔ mkvmerge v96 experiment:

- **H.264 and HEVC from MPEG-TS:** no `Colour` element, not even the VUI's
  primaries, transfer and matrix.
- **HEVC HDR10 from MPEG-TS:** no colour and no mastering metadata either,
  with the VUI set to BT.2020/PQ and both HDR SEIs present.
- **Passing the values on the command line:** `--max-content-light`,
  `--max-frame-light`, `--max-luminance`, `--min-luminance`,
  `--chromaticity-coordinates` and `--white-color-coordinates` write the
  full mastering metadata.
- **Hard-interlaced MPEG-2:** `FlagInterlaced` 1, with no field order.

mkvmerge reports colour in `-J` output only for Matroska and MP4 input
(68.0). So the colour in mkvsmith's output is the colour it passes:
`mkv.py` chooses it from the disc's dynamic range and colour space, or
from the picture height.

Since 103.0 mkvmerge sets StereoMode from an H.264 or HEVC frame-packing
SEI (payload type 45), TS input included (NEWS).

## Audio

### AC-3 and E-AC-3

Sync word `0x0B77`. `bsid` (5 bits at bit 29) up to 10 is AC-3, 11–16
E-AC-3. (FF `ac3_parser.c`)

AC-3: `crc1`, `fscod` (2), `frmsizecod` (6), `bsid` (5), `bsmod` (3),
`acmod` (3), then `dsurmod` when `acmod` is 2 or the mix levels, then
`lfeon`. Channels = the `acmod` count + `lfeon`.

| `acmod` | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| layout | 1+1 | 1/0 | 2/0 | 3/0 | 2/1 | 3/1 | 2/2 | 3/2 |

(FF `ac3defs.h`.) E-AC-3 adds `strmtyp` (0 independent, 1 dependent,
2 AC-3 converted) and `substreamid`; a dependent substream with a channel
map adds channels to the independent one (7.1 as 5.1 + dependent).
Atmos (joint object coding): the low bit of the first `addbsi` byte is
`flag_ec3_extension_type_a` (FF's comment; ETSI Annex E not checked).
Matroska: `A_AC3` and `A_EAC3` (MCOD).

### DTS

Sync words (FF `dca_syncwords.h`):

| Sync word | Part |
|---|---|
| `7FFE8001` | core (14-bit forms exist) |
| `64582025` | extension substream (DTS-HD) |
| `41A29547` | XLL: lossless (Master Audio) |
| `655E315E` | XBR (High Resolution) |
| `1D95F262` | X96 |
| `5A5A5A5A`, `47004A03` | XCH, XXCH |
| `0A801921` | LBR (Express) |
| `02000850` | at the start of XLL extra data: DTS:X |
| `F14000D0` | the same place: DTS:X IMAX |

The extension substream's asset descriptor can carry a speaker mask, the
true layout beyond the core (FF `dca_exss.c`). Matroska: one `A_DTS` for
core, ES, 96/24, HR and MA; `A_DTS/EXPRESS`, `A_DTS/LOSSLESS` (MCOD).

### TrueHD and MLP

Major sync `F8726FBA` is TrueHD, `F8726FBB` MLP. TrueHD then gives
`ratebits` (4), the 6-channel presentation's modifier and arrangement, and
the 8-channel presentation's (13 bits); later `substream_info`. (FF
`mlp_parse.c`) Atmos: a fourth substream with the top bit of
`substream_info` set (a 16-channel presentation; FF `mlpdec.c`). On BD,
TrueHD carries an AC-3 core in the same PID, which mkvmerge splits into a
second track.

### LPCM

**BD** (a 4-byte header on each PES payload; FF `pcm-bluray.c`):

| Bits | Field |
|---|---|
| byte 2, high nibble | channel assignment: 1 mono, 3 stereo, 4 3/0, 5 2/1, 6 3/1, 7 2/2, 8 3/2 (5.0), 9 3/2+LFE (5.1), 10 3/4 (7.0), 11 3/4+LFE (7.1); 2, 12–15 reserved |
| byte 2, low nibble | sample rate: 1 48 kHz, 4 96 kHz, 5 192 kHz |
| byte 3, bits 7–6 | bits: 1 16, 2 20, 3 24 |

Odd channel counts are padded to even. The clip info has no 5.0 code (a
5.0 mix is stored as 5.1), so this header is where the exact layout is.

**DVD** (private stream 1, `0xA0`–`0xA7`; after the sub-stream ID, the
frame count and first-access-unit pointer, then 3 bytes; FF `pcm-dvd.c`):
byte 0 emphasis, mute, reserved, frame number (5); byte 1 quantization
(2: 16, 20, 24 bits), frequency (2: 48, 96 kHz), reserved, channels − 1
(3); byte 2 dynamic range control (`0x80` off). FF tells DVD-Video LPCM from
DVD-Audio LPCM and MLP by the sixth header byte being `0x80`.

### MPEG audio

DVD `0xC0`–`0xC7`, rare. Matroska `A_MPEG/L1`, `L2`, `L3`.

### What mkvmerge reports

✔ mkvmerge v96 experiment, `codec` in `mkvmerge -J` output:

| Input | `codec` |
|---|---|
| MPEG-2 video in TS | `MPEG-1/2` |
| H.264 in TS | `AVC/H.264/MPEG-4p10` |
| HEVC in TS or raw | `HEVC/H.265/MPEG-H` |
| AC-3, E-AC-3, DTS, TrueHD | `AC-3`, `E-AC-3`, `DTS`, `TrueHD` |
| BD LPCM in TS | `PCM` |
| MPEG-1 layer II | `MP2` |

mkvmerge also reports `TrueHD Atmos` (since 8.7.0) and names the DTS
family "DTS-HD Master Audio", "DTS-HD High Resolution", "DTS Express".
Nothing in NEWS mentions identifying DTS:X or E-AC-3 Atmos.

## Open parser mismatches

- `probe._MKVMERGE_CODEC_MAP` lacks `MPEG-1/2`, `AVC/H.264/MPEG-4p10`,
  `HEVC/H.265/MPEG-H`, `MP2` and `TrueHD Atmos`. Where the DVD title
  builder falls back to `mkvmerge -J`, the raw string becomes the codec, and
  MPEG-2 loses its chroma siting.
- The H.264 VUI colour and chroma location are read past and dropped; AVC
  colour comes from the picture height instead.
- No header check for Atmos or DTS:X, so track names can't say so.
