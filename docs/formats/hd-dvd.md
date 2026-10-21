# HD DVD structures

Advanced Content playlists (XPL), Standard Content IFOs, and the EVO files
they point at, as `hddvd.py` and its callers in `scan.py`, `mkv.py` and
`vobsub.py` read them.

The HD DVD specification (DVD Forum, "DVD Specifications for High
Definition Video") is licensed and has no public copy. Everything here comes
from secondary sources: authoring documentation, demuxer code and playlist
files people have posted. Claims we could only infer say so.

## Sources

| Letter | Source | Where |
|---|---|---|
| F | FFmpeg `libavformat/mpeg.c` (LGPL-2.1+) | https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/mpeg.c |
| K | MKVToolNix `src/input/r_mpeg_ps.cpp`, `NEWS.md` (GPL-2.0-only) | https://codeberg.org/mbunkus/mkvtoolnix |
| FD | ffmpeg-devel, "support for hddvd .evo files", January 2007 | https://ffmpeg.org/pipermail/ffmpeg-devel/2007-January/027512.html |
| MS | Peter Torr (Microsoft HD DVD team), "Hello World, Part 4", 2006 | https://learn.microsoft.com/en-us/archive/blogs/ptorr/hello-world-part-4-script-and-a-movie-part-i-the-playlist |
| AS | Sonic AuthorScript HD DVD SDK guide, chapter 3 (cites spec section numbers) | http://delancie.net/PD_Samples/AS_HDDVD_SDK_Guide/HD_Guide_Parts/HD_Guide_3.htm |
| D9 | Doom9 forum threads with complete decrypted retail `VPLST000.XPL` files | https://forum.doom9.org/archive/index.php/t-120842.html, `t-121272.html` |
| H | hddvdinfo (no licence: facts only, never copied) | https://github.com/9Oc/hddvdinfo |

## Disc layout

| Path | Holds |
|---|---|
| `HVDVD_TS/*.EVO` | enhanced video objects: the A/V data |
| `HVDVD_TS/*.MAP` | one time map per EVO, same stem (`FEATURE_1.EVO` → `FEATURE_1.MAP`) |
| `HVDVD_TS/HVA00001.VTI` | Advanced Content's title set information |
| `HVDVD_TS/HV###I01.IFO` | Standard Content's title set information |
| `ADV_OBJ/VPLSTxxx.XPL` | playlists, `000`–`999`, upper case |
| `ADV_OBJ/DISCID.DAT` | disc identification |
| `ADV_OBJ/*.xmf`, `*.xmu`, `*.xas`, `*.js` | manifests, markup, Advanced Subtitles, script |

(MS, D9, H.) HD DVD file names are case-sensitive (MS). Standard Content
titles are split as `HV###T##.EVO`, all parts of one title set forming one
title, and menus are `HV###M##.EVO` (H).

`DISCID.DAT`: ✔ real disc, the 128-byte file starts with the magic
`HDDVD-V_CONF`, followed by binary data and a NUL-padded ASCII provider
name. No published source describes it.

### Which playlist

A player "finds an appropriate playlist and loads it" (MS), but no public
source says how it picks among several `VPLST*.XPL`. Real discs don't all
keep the feature in `VPLST000.XPL`: H's sample output shows it in
`VPLST001.XPL` on one disc. Nothing found says what `APLST*.XPL` files are.

## The playlist (XPL)

UTF-8 XML in the namespace `http://www.dvdforum.org/2005/HDDVDVideo/Playlist`
(MS prints `https://`; the posted retail files have `http://`). Matching
elements by local name, as `hddvd.py` does, avoids the difference.

```
Playlist (majorVersion, minorVersion, displayName)
├── Configuration (StreamingBuffer, Aperture, MainVideoDefaultColor)
├── MediaAttributeList
│   ├── VideoAttributeItem (index, codec)
│   ├── AudioAttributeItem (index, codec, channels)
│   └── SubpictureAttributeItem (index, codec)
└── TitleSet (timeBase, tickBase, defaultLanguage)
    ├── FirstPlayTitle, PlaylistApplication
    └── Title (titleNumber, titleDuration, id, displayName, onEnd, …)
        ├── PrimaryAudioVideoClip (src, titleTimeBegin, titleTimeEnd,
        │   │                      dataSource, seamless, description)
        │   ├── Video (track, mediaAttr)
        │   ├── Audio, Subtitle (track, streamNumber, mediaAttr, description)
        │   └── SubVideo, SubAudio (picture-in-picture)
        ├── ChapterList
        │   └── Chapter (titleTimeBegin, displayName)
        └── TrackNavigationList
            └── AudioTrack, SubtitleTrack (track, langcode, selectable, description)
```

(D9, MS, AS.)

### Times

All times are `HH:MM:SS:FF`, with FF counted in `TitleSet@timeBase` frames;
every posted disc has `timeBase="60fps"`, and frame values up to `:54` occur.
`tickBase` (24 or 60 fps) is the markup's tick rate, not the video's. (D9)
Chapter times are relative to the title.

### Media attributes

| Element | `codec` values seen | Notes |
|---|---|---|
| `VideoAttributeItem` | `VC-1`, `AVC`, `MPEG-2` | |
| `AudioAttributeItem` | `DD+`, `MLP`, `AC-3`, `DTS-HD` | `channels` is a count, e.g. `6` |
| `SubpictureAttributeItem` | `8bitRLC` | on every posted retail disc |

Tracks point at an item through `mediaAttr`. The list may be empty on
simple content (MS).

### Clips

AS names six clip types: `PrimaryAudioVideoClip`,
`SubstituteAudioVideoClip`, `SecondaryAudioVideoClip`, `SubstituteAudioClip`,
`ApplicationSegment` (menus and applications) and `AdvancedSubtitleSegment`.
A clip's `src` names the `.MAP`, not the `.EVO` (D9).

A feature is often two clips joined seamlessly (`seamless="true"` on the
second), contiguous on the title timeline, and every feature clip repeats the
same track list. A posted retail title also ends with a third, short clip (a
warning card) that has only a `Video` child; the title's `titleDuration`
covers it. (D9)

### Tracks

`track` is the number the viewer sees, `streamNumber` the stream within the
EVO. `description` is free text in the authoring language: seen are
"English 5.1", "English SDH", "Espanol", "Francais", "Forced Espanol". (D9)

`langcode` is an ISO 639-1 code with a two-digit suffix: `en:01`, `en:05`,
and `ja:00`–`ja:03` on four subtitle tracks of one disc. `selectable="false"`
appears on the track described "Forced Espanol". (D9) We read the suffix as
the HD DVD form of DVD's language code extension (`05` captions, `09`
forced), and `selectable="false"` as a forced track; no source confirms
either.

## EVO streams

An EVO is an MPEG-2 program stream with HD extensions. Audio and
subpictures ride private stream 1 (`0xBD`) with sub-stream IDs that extend
DVD's; VC-1 rides `0xFD` with a `stream_id_extension` read from
PES_extension_2. (F, K, FD)

| Stream | IDs |
|---|---|
| MPEG-2 video | `0xE0` |
| H.264 video | `0xE2` primary, `0xE3` secondary |
| VC-1 video | `0xFD` + extension `0x55` primary, `0x75` secondary |
| Subpictures | `0xBD` `0x20`–`0x3F` |
| AC-3 | `0xBD` `0x80`–`0x87` |
| DTS(-HD) | `0xBD` `0x88`–`0x8F` primary, `0x98`–`0x9F` secondary |
| LPCM, MLP | `0xBD` `0xA0`–`0xAF` |
| TrueHD / MLP | `0xBD` `0xB0`–`0xBF`, 4-byte audio header |
| AC-3, E-AC-3 (DD+) | `0xBD` `0xC0`–`0xCF` |

The H.264 IDs come from a patent on an earlier spec revision, quoted in FD,
which warns that it differs from the final spec. ✔ real disc: an EVO's
video used `0xE2`.

mkvmerge skips every `0x20`–`0x3F` sub-stream ("not supported yet", K
`parse_packet`), so mkvsmith extracts subtitles itself, and `mkvmerge -J`
isn't a full inventory of an EVO. K sorts tracks video, audio, subtitles,
then by ID. A feature EVO can carry a second video track (a 480p VC-1
picture-in-picture stream) and audio the XPL doesn't declare (H).

The `.MAP` files' layout is not public. AS cites "Object Mapping
Information" (spec 6.2.3.2) for how a clip maps onto an EVO, without a
layout.

### Subpictures

Retail XPLs declare `8bitRLC` subpictures, and users of another ripping
tool report its HD DVD subtitles come out as an "extended VobSub" few players
render. F decodes an HD variant of the SPU with 4-byte offsets and an 8-bit
palette (see [subpictures.md](subpictures.md)); whether retail discs use it
is unconfirmed.

### Sources disagree

- **E-AC-3 range:** F takes `0xC0`–`0xCF` as AC-3/E-AC-3, K only
  `0xC0`–`0xC7`, so K would drop `0xC8`–`0xCF` (secondary DD+, by analogy
  with DTS).
- **`0xA0`–`0xAF`:** F tells LPCM from MLP by a header byte, K treats the
  range as PCM; FD puts MLP at `0xB0`–`0xBF`.
- **Language:** `hddvd.py` prefers `langcode` and falls back to the
  description; H does the reverse.

## Standard Content IFOs

Only H describes them, from reverse engineering, so none of this is
confirmed. The file starts `STANDARD-VTS` (DVD: `DVDVIDEO-VTS`). The
part-of-title, PGC and cell address tables sit at the same VTSI_MAT offsets
as DVD's (`0xC8`, `0xCC`, `0xE0`) and have DVD's layout. The attributes
move:

| Field | Standard Content | DVD-Video |
|---|---|---|
| Audio count / first attribute | `0x218` / `0x21A`, 8 × 8 bytes | `0x202` / `0x204` |
| Audio language bytes | 4–5 | 2–3 |
| Subpicture count / first attribute | `0x25A` / `0x25C`, 32 × 6 bytes | `0x254` / `0x256` |

H's audio coding modes: 0 AC-3, 1 MLP, 2 MPEG-1, 3 MPEG-2, 5 LPCM, 6
DTS-HD, 7 DD+. DVD has LPCM at 4.

## Open parser mismatches

- **Subtitles:** XPL subtitles are declared `dvd_subtitle` and muxed
  through the DVD VobSub path, which may not decode `8bitRLC`.
- **Playlist choice:** a folder uses the first `VPLST*.XPL` in name order;
  an ISO the first `.xpl` in its listing, with no `VPLST` filter.
- **Clips:** every clip of a title is appended, including a trailing
  warning card with a different track list. When a clip is missing, the
  chapters and the duration stay on the full timeline.
- **Track matching:** tracks are matched to mkvmerge's by position within
  each type, not by `streamNumber` and ID range, so with mixed codecs a
  language can land on the wrong track.
- **Unused fields:** `MediaAttributeList`, chapter `displayName`, the
  `langcode` suffix and `selectable` aren't read. The description fallback
  knows English words only.
- **Picture-in-picture:** secondary video and audio are muxed as unlabelled
  tracks.
- **Standard Content:** discs without an XPL get one title per EVO.
- **SOURCE_ID:** the tags assume `0xC0` + n for audio and only `0xE0`–`0xEF`
  for video.
- **Detection order:** `VIDEO_TS` is checked before `HVDVD_TS`.
