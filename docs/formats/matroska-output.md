# Matroska output

What mkvsmith writes into an MKV, the mkvmerge options it uses for it, and
what the Matroska specifications say about each element. Codec details are
in [codecs.md](codecs.md).

## Sources

| Letter | Source | Where |
|---|---|---|
| RFC | RFC 9559, Matroska media container (Matroska v4 elements) | https://www.rfc-editor.org/rfc/rfc9559 |
| MTAG | Matroska tagging (CC BY 4.0) | https://www.matroska.org/technical/tagging.html |
| MBAM | Matroska block additional mappings (CC BY 4.0) | https://www.matroska.org/technical/block_additional_mappings.html |
| NEWS | MKVToolNix NEWS | https://mkvtoolnix.download/doc/NEWS.md |
| MAN | mkvmerge manual | https://mkvtoolnix.download/doc/mkvmerge.html |
| FF | FFmpeg `libavformat/matroska.h`, `matroskadec.c`, `matroskaenc.c`, `libavcodec/defs.h` @ `c50d6fca7ac1` | https://github.com/FFmpeg/FFmpeg |
| MPV | mpv manual, `DOCS/man/options.rst` | https://github.com/mpv-player/mpv |

**✔ mkvmerge v96 experiment** marks a check on synthetic input.

## Track properties

| Element | mkvmerge option | mkvsmith |
|---|---|---|
| Language, LanguageBCP47 | `--language` | always |
| FlagDefault | `--default-track` | always explicit; unset, mkvmerge flags the first track of each type |
| FlagForced | `--forced-track` | forced subtitles |
| FlagHearingImpaired | `--hearing-impaired-flag` | caption subtitles |
| FlagCommentary | `--commentary-flag` | commentary audio and subtitles |
| FlagVisualImpaired | `--visual-impaired-flag` | DVD audio code extension 2 |
| FlagTextDescriptions | `--text-descriptions-flag` | not set |
| FlagOriginal | `--original-flag` | audio in TMDB's original language, when tagging (not commentary or visually-impaired tracks) |
| Name | `--track-name` | codec and channel label |
| Colour | `--color-primaries`, `-transfer-characteristics`, `-matrix-coefficients`, `-range` | from the disc, else by picture height |
| ChromaSiting | `--chroma-siting` | MPEG-2 only: `1,2` (left, half), as FF's encoder writes it |
| MasteringMetadata, MaxCLL, MaxFALL | `--chromaticity-coordinates`, `--white-color-coordinates`, `--max-luminance`, `--min-luminance`, `--max-content-light`, `--max-frame-light` | UHD: from the playlist's static HDR extension, on the base layer |
| FlagInterlaced, FieldOrder | set afterwards with `mkvpropedit` | AVC only, from `h264.py` |

`--default-track` and `--forced-track` were renamed `--default-track-flag`
and `--forced-display-flag` in 65.0, with the old names "supported and
recognized indefinitely" (NEWS). The accessibility, original and commentary
flags arrived in 54.0.

RFC 9559: FlagForced "applies only to subtitles" that a player should show
"even if the user's preferences would not normally enable subtitles"
(foreign-language dialogue, on-screen text).

### Field order: sources disagree

RFC 9559 (Table 4) defines FieldOrder 0 progressive, 1 top field first,
2 undetermined, 6 bottom field first, 9 top first interleaved, 14 bottom
first interleaved. FF maps 9 to "top coded first, bottom displayed first"
and 14 the other way, against the RFC. mkvsmith writes 1 and 6, the only
values both read the same way. Keep it.

## Languages

DVDs store ISO 639-1 (two letters), Blu-ray ISO 639-2 (three). Since 50.0,
mkvmerge accepts BCP 47 in every language option, always writes the BCP 47
element, and also the legacy one (ISO 639-2/B: `fre`, `ger`, `chi`) when
the tag has a 639-2 code. RFC 9559 makes the BCP 47 element win when both
are present.

✔ mkvmerge v96 experiment, `--language` then `-J`:

| Given | Language / BCP 47 |
|---|---|
| `fr`, `fre`, `fra` | `fre` / `fr` |
| `iw` (withdrawn) | `heb` / `he` |
| `sh` | `und` / `sh` |
| `zh`, `chi`, `zho` | `chi` / `zh` |
| `pt-BR`, `es-419` | `por` / `pt-BR`, `spa` / `es-419` |
| `in`, `ji` (withdrawn), `xx` | **error, exit 2, no file** |

So one stray code fails the whole mux. `dvdifo._iso_639_1` maps the
withdrawn codes (`in` → `id`, `iw` → `he`, `ji` → `yi`, `jw` → `jv`, `mo` →
`ro`) and turns any other two letters not in ISO 639-1 into `und`, before
they reach mkvmerge.

## Chapters and editions

### Simple chapters

One default, non-ordered edition. Each atom has start and end time,
"Chapter NN" and `ChapterLanguage` `eng`; an end chapter within 0.5 s of the
end is dropped (`mkv.py` `_write_chapters_xml`). mkvmerge adds the BCP 47
chapter language itself (✔ mkvmerge v96 experiment; NEWS 50.0, 62.0). The
XML element is `ChapLanguageIETF` in mkvmerge's files and
`ChapLanguageBCP47` in RFC 9559.

### Multiple editions

`_write_multi_edition_chapters_xml` (ported from xin1generator) writes one
ordered edition per cut in a single file, the default one flagged, with
hidden atoms (no display) where a chapter spans a branch point. Edition names
are `TITLE` tags aimed at each `EditionUID`. ✔ mkvmerge v96 (in
`tests/test_editions.py`): the chapters and `--global-tags` keep
`EditionUID` targets.

This follows the RFC:

- An enabled ordered edition is played chapter by chapter in stored order,
  and "a single Matroska file can present multiple edits of a film without
  duplicating content" (RFC §20.1.3).
- `ChapterSegmentUUID` "MUST NOT be the SegmentUUID value of the Segment it
  belongs to", so intra-file ordered chapters leave it out.
- Hidden chapters "SHOULD NOT be available to the user interface".
- The first edition flagged default is the default; with none flagged, the
  first edition (§20.1.2).

The atoms are placed on the clip offsets mkvmerge actually used (it
appends each clip at its real end, a little past the play item's out
time), read back from `--generate-chapters when-appending` placeholders,
and an atom that jumps ends 1 ms early so mpv lets no frame of the next
clip in. ✔ real disc: two seamless H.264 cuts (26 play items each, 39
clips in all) muxed into one file; at every play item of both editions
the frame at its atom's start is that clip's first frame, byte for byte.
The cut with a jump at every item comes out 23 ms short of its playlist
(the 1 ms margins), the other within 1 ms; mpv reads both editions at
their playlists' lengths.

mpv plays ordered chapters by default and picks the default edition the same
way (MPV). Support in other players wasn't researched. Matroska v5's
`EditionDisplay`/`EditionString` (mkvmerge 71.0) would name editions
without tags.

### Disc chapter names

Some Blu-rays carry chapter names (`BDMV/META/TN`, see
[bluray.md](bluray.md)); mkvmerge uses them itself when given a playlist
(39.0). mkvsmith writes "Chapter NN" everywhere. Check
`.agents/reference-behavior.md` before changing this.

## Tags

| TargetTypeValue | Meaning (video) |
|---|---|
| 70 | collection |
| 60 | season, sequel, volume |
| 50 | movie, episode, concert |
| 30 | chapter |

(MTAG.) mkvsmith writes the TMDB fields at 50, edition titles targeted by
`EditionUID`, and a `SOURCE_ID` tag per track.

What MTAG requires:

- **`IMDB`:** `tt` and at least 7 digits.
- **`TMDB`:** prefixed `movie/` or `tv/`; mkvsmith writes `movie/<id>`.
- **`TVDB`:** digits, for the `series/{id}` API.
- **`RATING`:** 0–5, "other rating systems … will have to be scaled".
- **Tag names:** upper-case letters, digits and underscores.
- **Lists:** repeated `SimpleTag`s with one value each are RECOMMENDED,
  e.g. for keywords.
- **`LAW_RATING`:** the age rating, read with `COUNTRY`.

## Version floor

What mkvsmith relies on (NEWS):

| Version | Change |
|---|---|
| 39.0 (2019) | chapter names from BD metadata when reading a playlist |
| 50.0 (2020) | BCP 47 languages for tracks, chapters and tags |
| 54.0 (2021) | hearing-impaired, visual-impaired, text-descriptions, original and commentary flags |
| 65.0 (2022) | flag options renamed; old names kept |
| 68.0 (2022) | colour in `-J` output (Matroska and MP4 input only) |
| 71.0 (2022) | Matroska v5 edition names |
| 81.0 (2023) | Dolby Vision from MPEG-TS, two-PID UHD layers merged into one track |
| 96.0 | the version this page's experiments used |

Below 54, a hearing-impaired or commentary stream makes mkvmerge reject the
command line. Below 81, a UHD Dolby Vision playlist shows two video tracks.

`models.mkvmerge_version()` reads `mkvmerge --version` once. Below 54,
mkvsmith warns at startup and leaves those two flags out, so the mux still
works; below 81, scanning a playlist with a Dolby Vision layer warns once.
An unreadable version counts as new enough.

## Open mismatches

- **`RATING` out of range.** It's written on TMDB's 0–10 scale.
- **Tag names outside the spec.** `CONTENT_RATING`, `ORIGINAL_LANGUAGE`
  and `RUNTIME` are written, and the standalone tag XML (`--save-tag-xml`)
  spells `IMDb`.
- **Lists in one tag.** Multi-value fields are comma-joined into a single
  tag.
