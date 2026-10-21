# Blu-ray structures

The parts of the Blu-ray format mkvsmith reads (playlists, clip info, the
disc library metadata), field by field, in our own words, and the files
around them that a rip could use. Each table names its source; **✔ real
disc** marks a layout confirmed on a retail disc's bytes (discs are never
named). See [README.md](README.md) for the conventions.

## Sources

| | Source | Where |
|---|---|---|
| **L** | libbluray `bdnav/mpls_parse.c`, `mpls_data.h`, `clpi_parse.c`, `clpi_data.h` (LGPL-2.1-or-later); also `index_parse.c`, `meta_parse.c`, `extdata_parse.c`, `navigation.c`, `bdid_parse.c`, `bdmv_parse.c`, `hdmv/mobj_parse.c` from master | [mpls_parse](sources/libbluray-mpls_parse.c.md), [mpls_data](sources/libbluray-mpls_data.h.md), [clpi_parse](sources/libbluray-clpi_parse.c.md), [clpi_data](sources/libbluray-clpi_data.h.md); the rest at https://code.videolan.org/videolan/libbluray |
| **E** | libbluray `bluray.h` (LGPL-2.1-or-later): the enums for every code below | [bluray.h](sources/libbluray-bluray.h.md) |
| **LP** | libbluray `decoders/hdmv_pids.h` (LGPL-2.1-or-later) | https://github.com/ace20022/libbluray/blob/master/src/libbluray/decoders/hdmv_pids.h |
| **K** | MKVToolNix `src/common/bluray/mpls.cpp`, `clpi.cpp` (GPL-2.0-only): an implementation independent of L | local only: `.agents/specs/bluray/mkvtoolnix-*.md` |
| **KN** | MKVToolNix `NEWS.md` | https://mkvtoolnix.download/doc/NEWS.md |
| **F** | FFmpeg `libavformat/mpegts.c`, `mpegts.h`, `libavcodec/pgssubdec.c` (LGPL-2.1-or-later) | https://github.com/FFmpeg/FFmpeg |

The BD-ROM specification itself is licensed and not available to us.

Conventions: offsets are hex, multi-byte values big-endian. Times in
playlists and clip info count a 45 kHz clock (divide by 45,000 for
seconds).

Every BDMV file (`MPLS`, `HDMV`, `INDX`, `MOBJ`, `BDID`, `BCLK`) starts
with a 4-character type and a 4-character version; L accepts `0100`,
`0200`, `0240` (3D, profile 5) and `0300` (UHD) (L `bdmv_parse.c`).

## Playlist (`BDMV/PLAYLIST/xxxxx.mpls`)

### Header (L `_parse_header`)

| Offset | Size | Field |
|---|---|---|
| 00 | 4 | `MPLS` |
| 04 | 4 | version |
| 08 | 4 | start of the PlayList section |
| 0C | 4 | start of the PlayListMark section (chapters) |
| 10 | 4 | start of the extension data |
| 14 | 20 | reserved |
| 28 | | AppInfoPlayList |

AppInfoPlayList (L `_parse_appinfo`): length (4), reserved (1), playback
type (1), then playback count (2) when the type is 2 or 3, else reserved
(2); user-operation mask (8); flag bits random access, audio mix, lossless
bypass, MVC base view R, SDR conversion notification; 11 reserved bits.

### PlayList and PlayItems (L `_parse_playlist`, `_parse_playitem`)

PlayList: length (4), reserved (2), number of PlayItems (2), number of
SubPaths (2), then the PlayItems, then the SubPaths.

PlayItem, after its length (2):

| Size | Field |
|---|---|
| 5 | clip name (`00800` for `STREAM/00800.m2ts`, `CLIPINF/00800.clpi`) |
| 4 | codec identifier `M2TS`, or `FMTS` (L then opens `xxxxx.fmts`) |
| 11 bits | reserved |
| 1 bit | multi-angle |
| 4 bits | connection condition: 1 = not seamless, 5 and 6 = seamless (L warns on any other value) |
| 1 | STC id |
| 4 | in time (45 kHz) |
| 4 | out time (45 kHz) |
| 8 | user-operation mask |
| 1 bit + 7 | random access flag, reserved |
| 1 | still mode: 0 none, 1 timed, 2 infinite (E `bd_still_mode_e`) |
| 2 | still time in seconds when the mode is 1, else reserved |
| | when multi-angle: angle count (1), 6 reserved bits, different-audio (1 bit), seamless-angle (1 bit), then 10 bytes per angle after the first (clip name 5, codec id 4, STC id 1) |
| | the STN table |

L's `_fill_clip` treats conditions 5 and 6 alike as seamless; for a
seamless clip it starts at the first packet instead of looking up the in
time in the clip's EP map.

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
After each entry L seeks to the end its length gives.

Each entry is a **stream entry** then a **stream attributes** block, each
preceded by its own length byte (L `_parse_stream`):

Stream entry, by its first byte (stream type):

| Type | Fields | Used by |
|---|---|---|
| 1 | PID (2) | the PlayItem's own clip |
| 2 | SubPath id (1), sub-clip id (1), PID (2) | SubPath types 2–6 |
| 3 | SubPath id (1), PID (2) | SubPath type 7: picture-in-picture inside the main clip, so the PID is in the main TS |
| 4 | SubPath id (1), PID (2) | L parses it like 3; K doesn't know it |

(K `stream_type_e`, citing the spec's table 5-8.)

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

MVC (`20`, the 3D second view) doesn't appear in this table: it sits in the
STN_SS extension, which no open source parses.

### SubPaths (L `_parse_subpath`, `_parse_subplayitem`)

SubPath: length (4), reserved (1), type (1), 15 reserved bits, repeat
(1 bit), reserved (1), number of SubPlayItems (1).

SubPlayItem: length (2), clip name (5), codec id (4), 27 reserved bits,
connection condition (4 bits), multi-clip (1 bit), STC id (1), in time (4),
out time (4), sync PlayItem id (2), sync PTS (4), then when multi-clip a
clip count (1) and 10 bytes per further clip.

| Type | SubPath (L `mpls_sub_path_type`) |
|---|---|
| 2 | primary audio of a browsable slideshow |
| 3 | interactive graphics presentation menu |
| 4 | text subtitles |
| 5 | out-of-mux synchronous elementary stream |
| 6 | out-of-mux asynchronous picture-in-picture |
| 7 | in-mux synchronous picture-in-picture |
| 8 | stereoscopic video (MVC) |
| 10 | Dolby Vision enhancement layer |

K lists 0–7 only.

### PlayListMark (chapters) (L `_parse_playlistmark`)

Length (4), number of marks (2), then 14 bytes per mark: reserved (1),
mark type (1: 1 = entry mark, i.e. a chapter; 2 = link point; E `bd_mark_type_e`), PlayItem index (2), time
(4, 45 kHz, on that PlayItem's clock), entry PID (2), duration (4).
L counts only entry marks as chapters.

A mark's time on the playlist timeline is its time minus its PlayItem's in
time, plus that PlayItem's start on the timeline (K and mkvsmith).

### Extension data (L `extdata_parse.c`, `_parse_mpls_extension`)

Shared by playlists, clip info and `index.bdmv`: length (4), data start (4),
3 padding bytes, entry count (1), then per entry ID1 (2), ID2 (2), start
(4), length (4).

| ID1, ID2 | Playlist extension |
|---|---|
| 1, 1 | picture-in-picture metadata (positions, scaling: 1 none, 2 1:2, 3 1:4, 4 3:2, 5 full screen) |
| 2, 1 | STN_SS: 3D streams (L recognises it, doesn't parse it) |
| 2, 2 | further SubPaths, same layout (3D) |
| 3, 5 | static HDR metadata |

Static HDR metadata (L `_parse_static_metadata`): length (4), count (1),
reserved (3), then 28 bytes per entry: dynamic range type (4 bits),
28 reserved bits, three display primaries (x, y; 2 bytes each), white point
x and y, max and min mastering luminance, MaxCLL, MaxFALL (2 bytes each).
Their units haven't been checked against a disc's SEI (see
[codecs.md](codecs.md)).

If a playlist won't parse, L retries `BDMV/BACKUP/PLAYLIST/` (L `mpls_get`).

## Clip info (`BDMV/CLIPINF/xxxxx.clpi`)

### Header (L `_parse_header`)

| Offset | Size | Field |
|---|---|---|
| 00 | 8 | `HDMV` + version |
| 08 | 4 | start of SequenceInfo |
| 0C | 4 | start of **ProgramInfo** (the stream list) |
| 10 | 4 | start of CPI (the EP map) |
| 14 | 4 | start of ClipMark |
| 18 | 4 | start of the extension data |

### ClipInfo (L `_parse_clipinfo`, at byte 40)

Length (4), reserved (2), clip stream type (1), application type (1),
31 reserved bits, ATC-delta flag (1 bit), TS recording rate (4), number of
source packets (4), 128 reserved bytes, the TS type info block, the
optional ATC deltas, and for application type 6 (the sub-TS of a text
subtitle SubPath) a font list: reserved (1), font count (1), then a 5-byte
file id and a reserved byte per font. The fonts are
`BDMV/AUXDATA/<id>.otf`.

### SequenceInfo (L `_parse_sequence`)

Length (4), reserved (1), number of ATC sequences (1); per ATC sequence:
first source packet (4), number of STC sequences (1), STC id offset (1);
per STC sequence: PCR PID (2), first source packet (4), presentation start
and end time (4 each).

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
| 2 | **aspect ratio** (4 bits), reserved (2), OC flag (1), then the CR flag for HEVC, reserved otherwise |
| 3 | HEVC: dynamic range (4 bits) + colour space (4 bits) |
| 4 | HEVC: HDR10+ flag, reserved |

Audio is as in the playlist; PG and IG add a reserved byte after the
language; text subtitles are character code then language. Every block
ends with a 12-byte ISRC, usually zero, and L then seeks to the length the
block gave. L also accepts coding type `20` (MVC) for video and `A0` beside
PG and IG; no source names `A0`.

### CPI and the EP map (L `_parse_cpi`, `_parse_ep_map_stream`)

Length (4), 12 reserved bits, CPI type (4 bits); then the EP map:
reserved (1), number of PIDs (1); per PID: PID (2), 10 reserved bits, EP
stream type (4 bits), coarse entry count (2), fine entry count (18 bits),
the map's start (4, relative to the EP map).

| Entry | Fields |
|---|---|
| coarse | fine entry reference (18 bits), PTS (14 bits), source packet (4) |
| fine | angle change point (1 bit), I-frame end offset (3 bits), PTS (11 bits), source packet (17 bits) |

This maps a 45 kHz time to a source packet number (L `clpi_lookup_spn`),
which is how L starts a non-seamless PlayItem at its in time and stops at
its out time.

ClipMark: L doesn't parse it.

| ID1, ID2 | Clip-info extension |
|---|---|
| 1, 2 | LPCM down-mix coefficients (L skips them) |
| 2, 4 | extent start points, for `.ssif` interleaving (3D) |
| 2, 5 | ProgramInfo for 3D, ProgramInfo's layout |
| 2, 6 | CPI for 3D |

## Code values (E)

| Field | Values |
|---|---|
| video coding type | `01` MPEG-1, `02` MPEG-2, `1B` H.264, `EA` VC-1, `24` HEVC, `20` MVC (F `STREAM_TYPE_VIDEO_MVC`; clip info only) |
| audio coding type | `03` MPEG-1, `04` MPEG-2, `80` LPCM, `81` AC-3, `82` DTS, `83` TrueHD, `84` E-AC-3, `85` DTS-HD (K: all but XLL; High Resolution), `86` DTS-HD Master Audio (K: XLL), `A1` secondary E-AC-3, `A2` secondary DTS-HD (F: DTS Express) |
| graphics and text | `90` PG, `91` IG, `92` text subtitles (TextST; F `AV_CODEC_ID_HDMV_TEXT_SUBTITLE`, Matroska `S_HDMV/TEXTST`, muxed by mkvmerge since 9.6.0) |
| video format | 1 480i, 2 576i, 3 480p, 4 1080i, 5 720p, 6 1080p, 7 576p, 8 2160p |
| frame rate | 1 23.976, 2 24, 3 25, 4 29.97, 6 50, 7 59.94; 5 reserved (L's `index.bdmv` enum calls it so) |
| aspect ratio | 2 4:3, 3 16:9 |
| audio presentation type | 1 mono, 3 stereo, 6 multi-channel, 12 stereo core + multi-channel |
| sample rate | 1 48 kHz, 4 96 kHz, 5 192 kHz, 12 192 kHz with a 48 or 96 kHz core (AC-3 / DTS), 14 96 kHz with a 48 kHz core |
| text character code | 1 UTF-8, 2 UTF-16BE, 3 Shift-JIS, 4 EUC-KR, 5 GB18030, 6 GB2312, 7 Big5 |
| dynamic range | 0 SDR, 1 HDR10, 2 Dolby Vision; HDR10+ is the separate flag bit |
| colour space | 1 BT.709, 2 BT.2020 |

No source lists presentation types 2, 4, 5, 7–9, sample rates 2 and 3, or
aspect ratio 4. LPCM (`80`) is HDMV LPCM, with its own header and
big-endian samples (F `PCM_BLURAY`; see [codecs.md](codecs.md)).

## Transport stream

M2TS files hold 192-byte source packets: a 4-byte header (2-bit copy
permission, 30-bit arrival time stamp) before each 188-byte TS packet.
F reads HDMV stream types only when the PMT's registration descriptor is
`HDMV` (or `HDPR`).

| PID | Stream (LP; F for `1015`) |
|---|---|
| `0000`, `0100`, `1001` | PAT, PMT, PCR |
| `1011` | primary video |
| `1012` | MVC dependent view |
| `1015` | Dolby Vision enhancement layer |
| `1100`–`111F` | primary audio |
| `1200`–`121F` | PG; `1220`–`123F` and `1240`–`125F` for 3D base and enhanced views |
| `1400`–`141F` | IG |
| `1800` | text subtitles |
| `1A00`–`1A1F` | secondary audio |
| `1B00`–`1B1F` | secondary video |

TrueHD carries an AC-3 core on its own PID, told apart by the PES stream ID
extension `0x76` (F); mkvmerge outputs the two as separate tracks. L blanks
a packet by setting its PID to `1FFF`, as `m2ts.null_packets_from` does.

On UHD discs with Dolby Vision the enhancement layer is PID `1015` in the
same M2TS, with stream type `24` and the HDMV registration descriptor,
not Dolby's TS signalling (F `detect_bdmv_dovi_group`). mkvmerge 81.0 and
later merge the two layers into one track (KN).

## Other BDMV files

### `index.bdmv` (L `index_parse.c`)

Index start (4), extension start (4); at byte 40 the AppInfoBDMV: length
(4, always 34), reserved bit, initial output mode (0 2D, 1 3D), 3D content
flag, reserved bit, initial dynamic range type (4 bits), video format
(4 bits), frame rate (4 bits), then 32 bytes of provider data.

The index: length (4), the First Play object, the Top Menu object, the
number of titles (2), then per title an object type (2 bits: 1 HDMV,
2 BD-J), an access type (2 bits: 0 permitted, 1 prohibited, 3 hidden),
28 reserved bits and the object. An HDMV object names a movie object
(playback type 0 movie, 1 interactive); a BD-J object a `BDMV/BDJO/*.bdjo`
file (types 2 and 3). The UHD extension (3, 1) carries reverse-engineered
flags for 4K, HDR10+ and Dolby Vision.

### `MovieObject.bdmv` (L `hdmv/mobj_parse.c`)

At byte 40: length (4), reserved (4), number of objects (2); per object
the resume, menu-call and title-search flags, then a count (2) of 12-byte
navigation commands. Which playlist a title plays is decided by running
these commands (L `hdmv_vm.c`); BD-J titles need Java.

### Disc library metadata (`BDMV/META/DL/bdmt_<lang>.xml`)

L reads `title/name`, `title/alternative`, `title/numSets`,
`title/setNumber`, `tableOfContents/titleName[@titleNumber]` and
`description/thumbnail[@href, @size]`, with `eng` as the default language
(L `meta_parse.c`).

### Chapter names (`BDMV/META/TN/tnmt_<lang>_<playlist>.xml`)

`chapters/name` lists per playlist (L `_parseTnManifestNode`). mkvmerge
names chapters from these when it reads a playlist (KN 39.0).

### `CERTIFICATE/id.bdmv`, `AUXDATA/sound.bdmv`, `BACKUP/`

`id.bdmv` (`BDID`): data and extension starts, then a 4-byte organisation
ID and a 16-byte disc ID. `sound.bdmv` (`BCLK`) holds menu sound effects.
`BACKUP/` mirrors `index.bdmv`, `MovieObject.bdmv`, `PLAYLIST` and
`CLIPINF`, and L falls back to it.

## Sources disagree

- **STN counts:** L reads 8 counts (with Dolby Vision) and 4 reserved
  bytes; K 7 counts and 5. The byte positions agree.
- **Multi-clip SubPlayItems:** K reads a reserved byte after the clip count
  and L doesn't; `bluray.py` follows L. Needs a disc with one.
- **Last chapter:** K drops a last chapter within 5 s of the playlist's
  end; mkvsmith drops it only at or past the end, merges marks closer than
  1 s and starts at 0, following the reference behaviour.

## Open parser mismatches

- **PlayItem in/out times aren't honoured when muxing.** mkvmerge is given
  whole clip files (`mkv.py`), so a PlayItem that uses part of a clip would
  bring the rest along, and the chapters would drift. mkvmerge trims to
  each PlayItem when given the playlist itself (KN 46.0, 48.0).
- **Version `0240` is refused.** `_parse_clpi` accepts only versions
  `0100`, `0200` and `0300`, so a 3D disc's clip info merge does nothing.
  Its `HDBD` magic has no source.
- **Channel counts.** Presentation types 6 and 12 say no count; 6 is
  taken as 5.1 and 12 gives none until mkvmerge's count at mux time.
- **Picture-in-picture.** In-mux PiP streams (entry type 3) are skipped in
  the clip info merge, though their PIDs are in the main clip.
- **Unread attributes.** The playlist's audio format and rate are never
  read. The clip info's HEVC dynamic range, colour space and HDR10+ bytes
  aren't read either.
- **Stale comments.**
  - `_merge_clpi_into_mpls`'s docstring swaps TrueHD (`83`) and DTS-HD
    (`85`).
  - `_parse_clpi`'s comments misname its blocks and only fit the first
    program.
  - The module header calls libbluray GPL-2.0; it is LGPL-2.1-or-later.
- **Dolby Vision tracks.** The STN's Dolby Vision entry is listed as a
  further video stream, while mkvmerge 81.0+ shows one video track.
- **Not supported.**
  - Only the first angle is used.
  - SubPath streams (text subtitles, out-of-mux audio) are listed but not
    muxed.
  - The static HDR metadata and the chapter names aren't read.
  - There's no `BACKUP` fallback.
  - Repeated-segment playlists aren't filtered: L drops playlists that
    repeat a segment more than twice; mkvmerge's GUI treats five repeats
    as a menu.
