# mkvsmith

> [English](README.md) · [Español](README.es.md)

MakeMKV-style DVD/Blu-ray ripper that produces MKV files using
[mkvmerge](https://mkvtoolnix.download/) (MKVToolNix).

`mkvsmith` reads disc structures directly — `.mpls` / `.clpi` / `.ifo` / BDMV
metadata — instead of probing media bitstreams. That makes scanning fast and
dependency-light: the only external media tool it needs is `mkvmerge`. It
deliberately mirrors MakeMKV's behaviour where that behaviour is the sensible
default, but it is an independent, GPL-licensed reimplementation.

## Features

- **Rips DVD (VIDEO_TS) and Blu-ray (BDMV) discs, ISOs, raw `.m2ts`/`.vob`,
  and plain video files** to Matroska (`.mkv`).
- **Keeps audio, subtitles, and chapters**, including DVD subpicture streams
  that simpler scanners miss.
- **Optional TMDB tagging** — metadata and cover art embedded directly in the
  mux.

## Requirements

- **Python 3.12+**
- **mkvmerge** (MKVToolNix) — the only external media tool, and a hard
  requirement for muxing.
- **7z** (`p7zip-full` on Debian/Ubuntu) — for reading ISO images.
- **sudo + mount** — *optional*, only for loop-mounting ISOs.
- **libdvdcss / libaacs** — needed by your OS to read *encrypted* commercial
  discs (the same as any ripper). `mkvsmith` does not ship or bypass DRM.

## Install

`mkvsmith` is a single-file script plus a few modules. The easiest way to run
it is with [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/Zorro92/mkvsmith
cd mkvsmith
uv run ./main.py --help
```

`main.py` also carries a `uv run --script` shebang, so once it is executable it
can be run directly:

```sh
chmod +x main.py
./main.py --help
```

## Usage

```sh
# scan a disc folder / ISO and enter interactive mode
uv run ./main.py /path/to/disc
uv run ./main.py movie.iso

# rip the main feature straight to the current directory
uv run ./main.py /path/to/disc -m

# rip a specific title
uv run ./main.py /path/to/disc -t 1

# rip all titles
uv run ./main.py /path/to/disc -a

# rip all detected TV episodes
uv run ./main.py /path/to/disc -e

# write output to a specific directory (second positional argument)
uv run ./main.py /path/to/disc -m ~/rips
```

### Interactive mode

Run without `-t/-m/-a/-e` to drop into the interactive prompt:

```text
mkvsmith> n          # show details for title n
mkvsmith> r 1        # rip title 1
mkvsmith> rm         # rip the main feature
mkvsmith> re         # rip all episodes
mkvsmith> ra         # rip all titles
mkvsmith> q          # quit
```

### Common options

| Flag | Description |
|---|---|
| `-t, --title N` | Rip a specific title |
| `-m, --main` | Rip the detected main feature |
| `-a, --all` | Rip all titles |
| `-e, --episodes` | Rip all detected TV episodes |
| `-i, --info` | Just scan and list titles |
| `-s, --streams` | Select streams (e.g. `v:0 a:eng s:all`) |
| `-l, --lang` | Preferred languages (default `eng,en,und`) |
| `--all-audio` / `--no-all-audio` | Keep all audio (default on) |
| `--no-subs` | Drop subtitles |
| `--no-forced` | Drop forced subtitles |
| `--min-duration N` | Ignore titles shorter than N seconds |
| `--show-all` | Show low-quality titles (menus/trailers) |
| `--temp-dir DIR` | Temp dir (use a disk path for large ISOs) |
| `--ram-limit FRAC` | Max fraction of RAM for RAM-backed temp dirs |
| `--no-sudo` | Skip sudo loop-mounting |
| `--tag` / `--no-tag` | TMDB tagging controls |
| `--ui-lang LANG` | UI language (e.g. `en`, `es`) |
| `--debug` | Verbose debug logging |

## Notes

- Encrypted commercial discs need `libdvdcss` (DVD) / `libaacs` (Blu-ray) at
  the OS level.
- The default temp dir is often a RAM-backed tmpfs on Linux. `mkvsmith`
  detects this and transparently spills oversized extractions to disk
  (`--ram-limit` controls the threshold).
- Direct ISO loop-mounting uses `sudo`; pass `--no-sudo` to disable it.
- **Multi-edition MKV output is experimental.** It is disabled by default and
  gated behind `--debug` (which exposes `--multi-edition` and the interactive
  `me` command). Playback across the seams where editions are stitched
  together may not work in every player.
- **Dolby Vision has not been fully tested.** HDR10 and HDR10+ need no special
  handling (their metadata travels inside the video bitstream and survives a
  remux untouched), and the BT.2020/PQ colour signalling for HDR and DV
  Blu-rays is parsed from the playlist and covered by unit tests. However,
  no Dolby Vision Profile 7 (dual-layer UHD Blu-ray) disc has been available
  to test against: a remux keeps only the HDR10-compatible base layer (full
  DV would require bitstream-level processing, which a remuxer deliberately
  does not do), and it is unverified whether the disc's enhancement-layer
  entry can show up as a stray extra video track.

## Disc fixtures

The parser regression tests (`tests/test_parser_fixtures.py`) parse real
`.mpls` / `.clpi` / `.ifo` files captured from specific discs. Those blobs are
**not committed** (to avoid redistributing disc metadata), so the tests are
skipped on a fresh clone.

To run them locally, capture the fixtures into `tests/fixtures/` yourself:

```sh
# Blu-ray, from an .iso via 7z (playlist/clip numbers are disc-specific):
7z e disc.iso "BDMV/PLAYLIST/00800.mpls" "BDMV/CLIPINF/00875.clpi" "BDMV/META/DL/bdmt_eng.xml" -otests/fixtures -y

# DVD, from an extracted VIDEO_TS folder:
cp VIDEO_TS/VIDEO_TS.IFO tests/fixtures/dvd_video_ts.ifo
cp VIDEO_TS/VTS_01_0.IFO tests/fixtures/dvd_vts_01_0.ifo
```

`scripts/inspect_fixtures.py` re-parses whatever is in `tests/fixtures/` and
prints the values the tests expect, which is handy when swapping in a new disc.

## Vibe check

This project was *vibe coded* — mostly described to an LLM and iterated on,
rather than typed out line by line. The disc-format parsing and the
MakeMKV-behaviour decisions are deliberate and covered by tests against real
disc images; the rest may have been written with unwarranted confidence.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

`mkvsmith` is not affiliated with, or endorsed by, MakeMKV.
