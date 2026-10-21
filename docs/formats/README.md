# Disc format notes

What mkvsmith knows about the disc formats it parses, in one place, with the
evidence for each claim. Check here before reading or writing a parser,
builder or test, and add to it whenever research settles something.

## What's here

- **Write-ups** (`*.md` in this folder): each structure mkvsmith reads,
  field by field, in our own words, with the source of every table.
  - [dvd-video.md](dvd-video.md): IFO files (VMG, VTS, PGC, cells, address
    tables, stream attributes) and DVD VM commands.
  - [bluray.md](bluray.md): playlists (MPLS: play items, STN stream table,
    chapters) and clip info (CLPI: program info), with the code tables.
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
- When sources disagree, record both readings under "Sources disagree" and
  what settled it, if anything has.
- When mkvsmith's parser doesn't match a write-up, list it under "Open
  parser mismatches" until it's fixed.
- Test builders (`tests/synth_*.py`) follow these write-ups, not the
  parser, so a test checks the parser against the format.
