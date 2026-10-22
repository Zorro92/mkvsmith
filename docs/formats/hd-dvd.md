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
| V | FFmpeg `libavcodec/vc1.c` (`ff_vc1_decode_sequence_header`, `ff_vc1_parse_frame_header_adv`; LGPL-2.1+), for SMPTE 421M's advanced-profile headers | https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/vc1.c |

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
`hddvd.py` reads every `VPLSTnnn.XPL` directly in `ADV_OBJ`, in any case,
and keeps the one with the longest title, the lowest number on a tie;
`APLST*.XPL` and backups are never read. ✔ real discs: both have only
`VPLST000.XPL`.

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

✔ real discs: "60fps" is 60/1.001. The time counts frames like
non-drop-frame timecode (`HH:MM:SS` of 60 frames a second, then `FF`),
and the frames run at 59.94 Hz. On two retail discs, each a feature in two
seamless EVOs, the first EVO's video PTS run exactly 1.001 times its XPL
length (3313.143 s for `00:55:09:50`, 2791.956 s for `00:46:29:10`), up to
where the second EVO's PTS continue. Read at 60, every chapter falls
3.6 s early per hour. Other time bases are taken as they are.

### Media attributes

| Element | `codec` values seen | Notes |
|---|---|---|
| `VideoAttributeItem` | `VC-1`, `AVC`, `MPEG-2` | |
| `AudioAttributeItem` | `DD+`, `MLP`, `AC-3`, `DTS-HD` | `channels` is a count, e.g. `6` |
| `SubpictureAttributeItem` | `8bitRLC` | on every posted retail disc |

Tracks point at an item through `mediaAttr`. The list may be empty on
simple content (MS). ✔ real discs: two retail discs list the same five
items, VC-1 and MPEG-2 video, DD+ 6 ch, MLP 6 ch and DD+ 2 ch audio, and
`8bitRLC`; `hddvd.py` reads the audio codec to find each track's
sub-stream (below).

### Clips

AS names six clip types: `PrimaryAudioVideoClip`,
`SubstituteAudioVideoClip`, `SecondaryAudioVideoClip`, `SubstituteAudioClip`,
`ApplicationSegment` (menus and applications) and `AdvancedSubtitleSegment`.
A clip's `src` names the `.MAP`, not the `.EVO` (D9).

A feature is often two clips joined seamlessly (`seamless="true"` on the
second), contiguous on the title timeline, and every feature clip repeats the
same track list. A posted retail title also ends with a third, short clip (a
warning card) that has only a `Video` child; the title's `titleDuration`
covers it. (D9) ✔ real disc: a bonus documentary ends with a 2-minute
credits clip whose XPL lists only `Video`, yet its EVO carries the same
MPEG-2 video and AC-3 audio as the clip before, and mkvmerge appends it
cleanly. A video-only track list doesn't mean a clip is silent or isn't
part of the programme, so every clip is kept. When a clip's EVO is
missing, the title's timeline closes up: the duration becomes the
remaining clips' total, the missing clip's chapters go and later ones move
back.

### Tracks

`track` is the number the viewer sees, `streamNumber` the stream within the
EVO. `description` is free text in the authoring language: seen are
"English 5.1", "English SDH", "Espanol", "Francais", "Forced Espanol". (D9)

`streamNumber` counts across codecs, not within one: on a real disc the
MLP track is stream 1 and the two DD+ tracks streams 2 and 3, and the EVO
carries them at `0xB0`, `0xC1` and `0xC2`. So a track's sub-stream is its
codec's first ID (below) + `streamNumber` − 1. ✔ real discs (DD+, MLP);
DTS-HD at `0x88` follows the same rule unconfirmed.

`langcode` is an ISO 639-1 code with a two-digit suffix: `en:01`, `en:05`,
and `ja:00`–`ja:03` on four subtitle tracks of one disc. `selectable="false"`
appears on the track described "Forced Espanol". (D9) The suffix is the HD
DVD form of DVD's language code extension: ✔ real disc, `en:03` on the
audio described "Commentary" and `en:05` on the subtitles described
"English for the Hearing Impaired", with `00` (unspecified) and `01`
(normal) on the rest. `hddvd.py` reads audio `03`–`04` as commentary and
subtitle `05`–`07` as captions, `09` as forced and `13`–`15` as
commentary, as on DVD. `SubtitleTrack` also carries `forced="true"` or
`"false"` (✔ real disc, `false` on every track seen), read as forced.
`selectable="false"` isn't read; no source says what it means beyond the
one disc. The navigation entry's `description` repeats the clip track's,
and stands in when the clip track has none.

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
palette (see [subpictures.md](subpictures.md)). ✔ real discs: the
subpictures of two retail discs are that variant, starting `00 00`, then a
4-byte size and a 4-byte control offset, so the DVD decoder can't read
them. Decoded with F's 8-bit run codes, they render as clean anti-aliased
text, each unit carrying its own 256-entry palette (details in
[subpictures.md](subpictures.md#hd-dvd-subpictures)).

### VC-1 frame timing

A feature's VC-1 stream stamps a PTS on the first frame of each GOP only
(✔ real disc: 942 PTSs for 12,770 frames). Every other frame's time
follows from decode order and the pulldown flags (V):

- **Sequence header** (start code `0x0F`): `PULLDOWN`, `INTERLACE`,
  `TFCNTRFLAG`, `PSF`, and in the display extension the frame rate
  (`FRAMERATENR` 1–7 = 24, 25, 30, 50, 60, 48, 72; `FRAMERATEDR` 1 =
  /1000, 2 = /1001).
- **Frame header** (`0x0D`): `FCM` when interlaced, then `PTYPE` (`0` P,
  `10` B, `110` I, `1110` BI, `1111` skipped) or, for field pictures,
  `FPTYPE`; `TFCNTR`; then with pulldown `RPTFRM` (2 bits, progressive
  or PSF) or `TFF` and `RFF`.
- B and BI frames show when decoded; I, P and skipped frames show when
  the next one of them is decoded. A frame shows for 2 fields, plus 1
  with `RFF`, or 2 × (1 + `RPTFRM`) progressive.

✔ real disc: interlaced, pulldown, 30000/1001. Timed that way, every
GOP's predicted start hits its PTS within a millisecond, and a frame's
time matches the decoder's.

mkvmerge (K) gets the first GOP after each chapter's sequence header
wrong. ✔ real disc: at every one of a feature's 37 chapters, two frames
get duplicate times and a 117–151 ms hole follows 0.45 s in, which plays
as a stutter; the 1.1 GB start of the EVO muxed alone does the same, so
it isn't the join. Its `--timestamps` files can't fix it: their times
must be sorted, and mkvmerge hands them out by its own frame order, so
the wrong frames get them (a frame two places late, the timer stepping
back). mkvsmith therefore rewrites the finished MKV's VC-1 block times
in place (`vc1time.py`, `mkvtimes.py`). VC-1 in Matroska is VfW mode,
where block times rise in file order and a decoder shows each frame at
the time of the block after it (✔ the decoder's output stamps are the
next block's time), so block *i* gets the presentation time of frame
*i* − 1 and the first block repeats the first. mkvmerge times Blu-ray
VC-1 that way (✔ real disc: an M2TS that stamps every frame comes out
`0, 0, 42, 83, …` ms, each frame on its PTS). On HD DVD it puts an
EVO's first frame on its PTS (✔ real disc, with the audio at 0), but
every later frame shows 150–167 ms late, and its cadence is often a
field (16.7 ms) out of phase. Read as one stream at a seamless join, the
second EVO's first PTS goes to the block before its first frame (✔ real
disc: the time is right to the millisecond, one block early). So the
first EVO's times start at mkvmerge's time for its first block, and an
EVO that carries on the previous one's clock (its first PTS within
0.5 s after the previous EVO's last frame) starts where the PTSs put it;
any other EVO starts at its first block. The median offset from
mkvmerge's times only checks that the cadence agrees.

An EVO can end on a skipped frame, 5 bytes with nothing after it (✔ real
disc), so the frame count includes a last frame shorter than a full
header.

✔ real discs, measured against the disc clock (each frame shown at the
next block's time, against its PTS-derived time): on five features (two
EVOs each, 132,552 to 198,893 frames, all interlaced with pulldown)
every frame lands on the clock to the millisecond and every step between
block times becomes 33–34 or 50–51 ms, from 110 duplicates and 37 holes
on one, 1,708 bad steps (some backwards) on another and 33 on a third.
Every cue still matches its block, and the stutter is gone. The audio's
first block lands within 10 ms of where the stream start times put it.

Where a stream drops its B frames (✔ real disc: I and P frames only for
1,373 frames), mkvmerge steps back 34 ms and then runs two frames early
for the whole stretch; 11% of that feature's frames lie more than 20 ms
from its times, while every one of its 9,601 GOP PTSs lies on the
cadence. A decoder shows the stretch on the cadence once the times are
rewritten.

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

- **Subtitles:** XPL-declared subtitles are decoded from the EVOs and
  converted to PGS (`hdspu.py`), one `.sup` per track. Only the 8-bit HD
  layout is read; a 2-bit HD unit (`0x05` instead of `0x85`) is skipped
  and counted. Each EVO's times start at its first video PTS; the next
  EVO follows on the shared clock when it continues it, else after the
  previous EVO's video PTS span plus one GOP. ✔ real disc: a feature in
  two EVOs (seamless) runs its first EVO 3.31 s past the XPL's
  `titleTimeEnd` read at 60 fps (exactly its length at 59.94, see
  Times), and the second EVO's first video PTS continues the first's
  clock. Timing by those durations put the second EVO's subtitles 3.31 s
  early; by the clock, a sign's translation lands on the sign's shot.
- **Seamless joins:** a clip marked `seamless="true"` is read with the
  one before it as one stream (mkvmerge's `( a b )`), not appended. ✔
  real disc: appended in track mode, the second EVO's audio started where
  the shared clock says but its VC-1 video 0.31 s later (a 0.24 s jump in
  the video timestamps at the join); concatenated, both follow the clock
  (a cut at 4379.198 s moves to 4378.891 s, the second EVO starts at
  3313.143 s). A clip after a missing one is appended as before.
- **VC-1 frame times:** rewritten after the mux when the EVOs' frame
  count matches the MKV's VC-1 blocks and at least 80% of the frames lie
  within 20 ms of mkvmerge's times (after the median offset), and each
  EVO's first block lies within 0.5 s of that offset; otherwise
  mkvmerge's times stay, with a warning. Not done for a cut (`--split`) mux.
- **Track matching:** audio of a codec outside DD+, MLP and DTS-HD, and
  video, are matched to mkvmerge's tracks by position within each type.
- **Unused fields:** chapter `displayName` and `selectable` aren't read.
  The description fallback knows English words only.
- **Picture-in-picture:** secondary video (any video after the first) and
  audio (a sub-stream no primary track has) are muxed, named from the
  XPL's `SubVideo`/`SubAudio` description or "Picture-in-picture", never
  default. No real disc with them was available to confirm the IDs.
- **Standard Content:** discs without an XPL get one title per EVO.
- **Detection order:** `VIDEO_TS` is checked before `HVDVD_TS`.
