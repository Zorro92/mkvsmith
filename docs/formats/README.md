# Disc format notes

What mkvsmith knows about the disc formats it parses, in one place, with the
evidence for each claim. Check here before reading or writing a parser,
builder or test, and add to it whenever research settles something.

## What's here

- **Write-ups** (`*.md` in this folder): each structure mkvsmith reads,
  field by field, in our own words, with the source of every table.
  - [dvd-video.md](dvd-video.md): IFO files (VMG, VTS, PGC, cells, address
    tables, time maps, menus, stream attributes), NAV packs, VOB stream IDs
    and DVD VM commands.
  - [bluray.md](bluray.md): playlists (MPLS: play items, STN stream table,
    SubPaths, chapters, extensions), clip info (CLPI: program info, EP
    map), the transport stream's PIDs, and `index.bdmv`, `MovieObject.bdmv`
    and the disc metadata files, with the code tables.
  - [hd-dvd.md](hd-dvd.md): XPL playlists, EVO stream IDs, Standard
    Content IFOs.
  - [subpictures.md](subpictures.md): the DVD subpicture unit, its palette,
    VobSub `.idx` and Matroska `S_VOBSUB`; Blu-ray PGS in brief.
  - [closed-captions.md](closed-captions.md): line-21 captions in DVD GOP
    user data, and CEA-608 decoding.
  - [filesystems.md](filesystems.md): UDF (1.02–2.60, the metadata
    partition) and ISO 9660, and which to trust on a protected DVD.
  - [codecs.md](codecs.md): video and audio bitstream headers (colour,
    HDR, Dolby Vision, AC-3, DTS, TrueHD, LPCM) and what mkvmerge takes
    from them.
  - [matroska-output.md](matroska-output.md): the track flags, languages,
    chapters, editions and tags mkvsmith writes, and the mkvmerge versions
    they need.
  - [mkvmerge.md](mkvmerge.md): what mkvsmith reads from mkvmerge, the
    mkvmerge behaviours it works around and how each is checked, and the
    review of each MKVToolNix release.
- **Sources** ([sources/](sources/)): copies of reference material whose
  licence allows it, unchanged, each headed with its URL, exact commit,
  fetch date and licence.
  - DVD: libdvdread (`ifo_types.h`, `ifo_read.c`, `ifo_print.c`,
    `dvd_reader.c`), libdvdnav
    (`vmcmd.c`), dvdauthor (`dvdifo.c`, `dvdvob.c`), FFmpeg
    (`dvdvideodec.c`, `dvdsubdec.c`), VLC (`dvdread.c`, `dvdnav.c`).
  - Blu-ray: libbluray (`mpls_parse.c`, `mpls_data.h`, `clpi_parse.c`,
    `clpi_data.h`, `bluray.h`).
- **Kept on the developer's machine** (`.agents/specs/`, see its README):
  the mpucoder DVD pages, HandBrake's DVD scan and MKVToolNix's MPLS/CLPI
  parsers (both GPL-2.0-only), and the UDF 1.02 / 2.50 / 2.60, ECMA-167 and
  ECMA-119 (ISO 9660) specifications.

## Two places, by licence

| Kind of source | Where it goes |
|---|---|
| Open source with a licence compatible with mkvsmith's GPL-3.0-or-later (GPL-2.0-or-later, LGPL, MIT, BSD, ...) | `docs/formats/sources/`, unchanged, with its licence noted |
| No licence stated (e.g. personal reference sites), licensed specifications, anything else that isn't clearly ours to republish | `.agents/specs/` on the developer's machine (gitignored), never committed |

The write-ups can cite local-only sources by name and file; anyone without
the local copy can follow the original URL listed in that file's header.

## Conventions

- Cite the source for every table or field ("R `cell_playback_t`", "M
  `ifo.md`"), using the letters in the write-up's source table.
- Mark a layout confirmed on a real disc's bytes with **✔ real disc**, and
  say what was seen. Never name the disc: describe it ("a retail NTSC DVD").
  A test of mkvmerge on synthetic input is **✔ mkvmerge vNN experiment**,
  never ✔ real disc.
- When sources disagree, record both readings under "Sources disagree" and
  what settled it, if anything has.
- When mkvsmith's parser doesn't match a write-up, list it under "Open
  parser mismatches" until it's fixed.
- Test builders (`tests/synth_*.py`) follow these write-ups, not the
  parser, so a test checks the parser against the format.
