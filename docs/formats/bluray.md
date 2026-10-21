# Blu-ray structures

The parts of the Blu-ray format mkvsmith reads (playlists and clip info),
field by field, in our own words. Each table names its source; **✔ real
disc** marks a layout confirmed on a retail disc's bytes (discs are never
named). See [README.md](README.md) for the conventions.

## Sources

| | Source | Where |
|---|---|---|
| **L** | libbluray `bdnav/mpls_parse.c`, `mpls_data.h`, `clpi_parse.c`, `clpi_data.h` (LGPL-2.1-or-later) | [mpls_parse](sources/libbluray-mpls_parse.c.md), [mpls_data](sources/libbluray-mpls_data.h.md), [clpi_parse](sources/libbluray-clpi_parse.c.md), [clpi_data](sources/libbluray-clpi_data.h.md) |
| **E** | libbluray `bluray.h` (LGPL-2.1-or-later): the enums for every code below | [bluray.h](sources/libbluray-bluray.h.md) |
| **K** | MKVToolNix `src/common/bluray/mpls.cpp`, `clpi.cpp` (GPL-2.0-only): an implementation independent of L | local only: `.agents/specs/bluray/mkvtoolnix-*.md` |

The BD-ROM specification itself is licensed and not available to us.

Conventions: offsets are hex, multi-byte values big-endian. Times in
playlists and clip info count a 45 kHz clock (divide by 45,000 for
seconds).

## Playlist (`BDMV/PLAYLIST/xxxxx.mpls`)

### Header (L `_parse_header`)

| Offset | Size | Field |
|---|---|---|
| 00 | 4 | `MPLS` |
| 04 | 4 | version: `0100`, `0200` or `0300` (UHD) |
| 08 | 4 | start of the PlayList section |
| 0C | 4 | start of the PlayListMark section (chapters) |
| 10 | 4 | start of the extension data |
| 14 | 20 | reserved |
| 28 | | AppInfoPlayList: length (4), then playback type, user-operation mask, flags |

### PlayList and PlayItems (L `_parse_playlist`, `_parse_playitem`)

PlayList: length (4), reserved (2), number of PlayItems (2), number of
SubPaths (2), then the PlayItems, then the SubPaths.

PlayItem, after its length (2):

| Size | Field |
|---|---|
| 5 | clip name (`00800` for `STREAM/00800.m2ts`, `CLIPINF/00800.clpi`) |
| 4 | codec identifier `M2TS` |
| 11 bits | reserved |
| 1 bit | multi-angle |
| 4 bits | connection condition: 1 = not seamless, 5 and 6 = seamless (L warns on any other value) |
| 1 | STC id |
| 4 | in time (45 kHz) |
| 4 | out time (45 kHz) |
| 8 | user-operation mask |
| 1 bit + 7 | random access flag, reserved |
| 1 | still mode (+ 2 still time) |
| | when multi-angle: angle count (1), flags (1), then 10 bytes (clip name, codec id, STC id) per extra angle |
| | the STN table |

### STN table (stream number table) (L `_parse_stn`)

Length (2), reserved (2), then one count byte each, in this order: primary
video, primary audio, PG (subtitles), IG (menus), secondary audio,
secondary video, picture-in-picture PG, Dolby Vision enhancement layer;
then 4 reserved bytes.

The entries follow in this order: primary video, primary audio, PG and
PiP PG together, IG, secondary audio (each followed by a list of primary
audio references), secondary video (followed by two reference lists:
secondary audio, then PiP PG), Dolby Vision. A reference list is a count
(1), reserved (1), the references (1 each), padded to an even length.

Each entry is a **stream entry** then a **stream attributes** block, each
preceded by its own length byte (L `_parse_stream`):

Stream entry, by its first byte (stream type):

| Type | Fields |
|---|---|
| 1 | main-clip stream: PID (2) |
| 2 | stream in a SubPath's sub-clip: SubPath id (1), sub-clip id (1), PID (2) |
| 3, 4 | stream in a SubPath: SubPath id (1), PID (2) |

Stream attributes, by coding type (the first byte, `attr[0]`):

| Coding type | Then |
|---|---|
| video `01`, `02`, `1B`, `EA` | video format (4 bits) + frame rate (4 bits) |
| HEVC `24` | video format + frame rate, then **dynamic range (4 bits) + colour space (4 bits)** in `attr[2]`, then CR flag and HDR10+ flag bits ✔ real disc |
| audio `03`, `04`, `80`-`86`, `A1`, `A2` | presentation type (4 bits) + sample rate (4 bits), then a 3-letter ISO 639-2 language |
| PG `90`, IG `91` | 3-letter language |
| text subtitles `92` | character code (1), then a 3-letter language |

✔ real disc (a UHD Blu-ray with Dolby Vision): the HDR10 base layer's
attributes are `24 82 12 00 00` (HEVC, 2160p at 24 Hz, HDR10 in BT.2020),
the enhancement layer's `24 62 22 00 00` (1080p, Dolby Vision, BT.2020).
The block is 5 bytes long.

The language of text subtitles is ASCII whatever their character code: the
character code applies to the subtitle text.

### PlayListMark (chapters) (L `_parse_playlistmark`)

Length (4), number of marks (2), then 14 bytes per mark: reserved (1),
mark type (1: 1 = entry mark, i.e. a chapter; 2 = link point; E `bd_mark_type_e`), PlayItem index (2), time
(4, 45 kHz, on that PlayItem's clock), entry PID (2), duration (4).

## Clip info (`BDMV/CLIPINF/xxxxx.clpi`)

### Header (L `_parse_header`)

| Offset | Size | Field |
|---|---|---|
| 00 | 8 | `HDMV0100` / `0200` / `0300` |
| 08 | 4 | start of SequenceInfo |
| 0C | 4 | start of **ProgramInfo** (the stream list) |
| 10 | 4 | start of CPI (the EP map) |
| 14 | 4 | start of ClipMark |
| 18 | 4 | start of the extension data |

### ProgramInfo (L `_parse_program`)

From its start: length (4), reserved (1), number of programs (1); then per
program: SPN of the program sequence start (4), PMT PID (2), number of
streams (1), number of groups (1); then per stream its PID (2) and an
attributes block (L `_parse_stream_attr`), preceded by a length byte.

Clip-info attributes differ from the playlist's for video:

| Byte | Field |
|---|---|
| 0 | coding type |
| 1 | video format (4 bits) + frame rate (4 bits) |
| 2 | **aspect ratio** (4 bits), reserved (2), OC flag (1), CR flag (1, HEVC) |
| 3 | HEVC: dynamic range (4 bits) + colour space (4 bits) |
| 4 | HEVC: HDR10+ flag, reserved |

Audio is as in the playlist; PG and IG add a reserved byte after the
language; text subtitles are character code then language.

## Code values (E)

| Field | Values |
|---|---|
| video coding type | `01` MPEG-1, `02` MPEG-2, `1B` H.264, `EA` VC-1, `24` HEVC |
| audio coding type | `03` MPEG-1, `04` MPEG-2, `80` LPCM, `81` AC-3, `82` DTS, `83` TrueHD, `84` E-AC-3, `85` DTS-HD High Resolution, `86` DTS-HD Master Audio, `A1` secondary E-AC-3, `A2` secondary DTS-HD |
| video format | 1 480i, 2 576i, 3 480p, 4 1080i, 5 720p, 6 1080p, 7 576p, 8 2160p |
| frame rate | 1 23.976, 2 24, 3 25, 4 29.97, 6 50, 7 59.94 |
| aspect ratio | 2 4:3, 3 16:9 |
| audio presentation type | 1 mono, 3 stereo, 6 multi-channel, 12 stereo core + multi-channel |
| sample rate | 1 48 kHz, 4 96 kHz, 5 192 kHz, 12 192 kHz with a 48 or 96 kHz core (AC-3 / DTS), 14 96 kHz with a 48 kHz core |
| text character code | 1 UTF-8, 2 UTF-16BE, 3 Shift-JIS, 4 EUC-KR, 5 GB18030, 6 GB2312, 7 Big5 |
| dynamic range | 0 SDR, 1 HDR10, 2 Dolby Vision |
| colour space | 1 BT.709, 2 BT.2020 |

## Not yet checked against a source

`bluray.py` has a few values that aren't in E's lists. None has been seen
on a disc yet; they're harmless guesses until one is:

- coding type `14`, labelled H.264 MVC (3D);
- text subtitles (`92`) labelled with the DVD subtitle codec;
- frame rate 5 (30 fps);
- presentation types 2, 4, 5, 7, 8, 9 (dual mono, 3, 4, 6.1, 7.1 channels);
  for multi-channel audio the real channel count comes from mkvmerge at
  mux time anyway;
- sample rates 2 and 3; combo rates 12 and 14 are reported as 48 kHz (the
  core of the commonest case);
- aspect ratio 4 (">2.20:1").

Also: `_parse_clpi` reads only the first program of ProgramInfo, and its
comment calls the block "clip info".

Not read at all yet: the static HDR metadata in the playlist's extension
data (mastering display, MaxCLL, MaxFALL; L parses it), which a rip
carries in the video stream anyway.
