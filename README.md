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
- **Rips HD DVD discs and ISOs** (`HVDVD_TS/*.evo` + XPL playlists, with
  chapters, XPL languages, and EVO subpicture extraction).
- **Keeps audio, subtitles, and chapters**, including DVD subpicture streams
  that simpler scanners miss.
- **Writes per-track `SOURCE_ID` tags** (Blu-ray PID / DVD
  VTS+stream IDs) alongside mkvmerge's statistics tags.
- **Shows stable disc identifiers**, including the filesystem-level
  [matrix256v1](https://github.com/shitwolfymakes/matrix256) fingerprint and
  DVD/Blu-ray metadata identifiers.
- **Optional [TheDiscDB](https://thediscdb.com/) lookup** — identify discs,
  apply community title names, and resolve obfuscated main-feature playlists.
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

# -m is smart: main feature on movies, all episodes on series discs
# (within-VTS PGC clusters and one-episode-per-VTS authoring both supported)
uv run ./main.py /path/to/disc -m

# write output to a specific directory (second positional argument)
uv run ./main.py /path/to/disc -m ~/rips
```

### Interactive mode

Run without `-t/-m/-a` to drop into the interactive prompt:

```text
mkvsmith> n          # show details for title n
mkvsmith> r 1        # rip title 1
mkvsmith> rm         # rip the main feature (episodes on series discs)
mkvsmith> ra         # rip all titles
mkvsmith> q          # quit
```

### Common options

| Flag | Description |
|---|---|
| `-t, --title N` | Rip a specific title |
| `-m, --main` | Rip the detected main feature (all episodes on series discs) |
| `-a, --all` | Rip all titles |
| `-i, --info` | Just scan and list titles |
| `-s, --streams` | Select streams (e.g. `v:0 a:eng s:all`) |
| `-l, --lang` | Preferred languages (default `eng,en,und`) |
| `--all-audio` / `--no-all-audio` | Keep all audio (default on) |
| `--no-subs` | Drop subtitles |
| `--no-forced` | Drop forced subtitles |
| `--cc-srt` / `--no-cc-srt` | EIA-608 closed captions as a text track (default off) |
| `--cc-format srt\|ass` | CC track format: portable text or positioned ASS |
| `--min-duration N` | Ignore titles shorter than N seconds |
| `--show-all` | Show low-quality titles (menus/trailers) |
| `--temp-dir DIR` | Temp dir (default: /var/tmp; override for tmpfs/RAM or another disk path) |
| `--ram-limit FRAC` | Max fraction of tmpfs capacity for RAM-backed temp dirs |
| `--force` | Overwrite existing output files without asking |
| `--no-sudo` | Skip sudo loop-mounting |
| `--tag` / `--no-tag` | TMDB tagging controls |
| `--discdb` / `--no-discdb` | TheDiscDB lookup (opt-in) |
| `--discdb-contribute[=MODE]` | Write/upload a contribution (`browser`, `manual`, or `direct`) |
| `--discdb-disc-name NAME` | Disc name used by direct contribution mode |
| `--ui-lang LANG` | UI language (e.g. `en`, `es`) |
| `--debug` | Verbose debug logging |

### TheDiscDB

TheDiscDB lookup is opt-in. Enable it per run with `--discdb`, or persist it in
`$XDG_CONFIG_HOME/mkvsmith/config.json` (default `~/.config/...`) under
`"discdb": {"enabled": true}`. Lookup sends
only local disc identifiers — never playlist data or media file contents.

```sh
# identify a disc and apply a unique community title mapping
uv run ./main.py movie.iso --discdb --info

# prepare files for TheDiscDB's reviewed contribution flow
uv run ./main.py movie.iso --discdb-contribute=browser
uv run ./main.py movie.iso --discdb-contribute=manual --discdb-bundle-dir ~/discdb
```

Matching uses TheDiscDB's legacy Disc Hash, Matrix256 fingerprint, the Blu-ray
AACS Disc ID, or the libdvdread DVD Disc ID. A UPC/EAN is only used as a weak
hint and must be corroborated by playlist or title/duration data. A uniquely
matched remote `MainMovie` outranks local heuristics, which resolves "screen
pass" playlist obfuscation without guessing.
Ambiguous matches never rename titles or override main-feature detection.
The format-specific Disc IDs require readable `AACS`/`VIDEO_TS` structures
(folder, ISO, or mounted image); the raw `/dev` fallback does not expose them.

`--discdb-contribute` writes `manifest.json` plus a MakeMKV-compatible scan
log generated from mkvsmith's own MPLS/CLPI/IFO parsing. In browser mode, open
or create a contribution draft and upload `makemkv_compat.txt` where the site
asks for a MakeMKV scan log. Direct mode attaches that data to an existing
contribution draft using
`--discdb-contribution-id` and an authenticated browser cookie supplied with
`--discdb-cookie` or `THEDISCDB_COOKIE`; it stops before item labelling and
review, which remain human-approved steps. Use `--discdb-disc-name` when
attaching additional discs to the same draft. Treat the cookie like a password.
Blu-ray segment maps come directly from playlist clip IDs; DVD cell-range maps
are intentionally left blank for human identification because mkvsmith does
not expose DVD cell IDs.

## Notes

- **Platform support:** Linux is the primary platform. Folder, ISO (via 7z),
  and video-file sources are written to be cross-platform, and Windows drive
  letters (`E:`) are recognised as device sources, but the `sudo mount -o
  loop` ISO fallback and `/dev/...` optical-device input are Linux-only.
  Windows and macOS support is otherwise untested.
- Encrypted commercial discs need `libdvdcss` (DVD) / `libaacs` (Blu-ray) at
  the OS level.
- Temp files default to `/var/tmp` (disk-backed) when usable, falling back to
  the system temp dir. If the effective temp dir is RAM-backed (tmpfs —
  e.g. an explicit `--temp-dir /tmp`), `mkvsmith` detects this and
  transparently spills oversized extractions to disk. The budget is
  `--ram-limit` of the smaller of total RAM and the tmpfs size (a tmpfs is
  frequently capped at a fraction of RAM), with extra guards for
  currently-available RAM and tmpfs free space, since `/tmp` is shared.
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

Optional, disc-specific fixtures (their tests skip when absent):

```sh
# Treasure Planet (2002) R1 DVD9 — alternate-edition PGC detection:
7z e treasure_planet.iso "VIDEO_TS/VTS_01_0.IFO" "VIDEO_TS/VTS_09_0.IFO" -otests/fixtures -y
mv tests/fixtures/VTS_01_0.IFO tests/fixtures/treasure_vts_01_0.ifo
mv tests/fixtures/VTS_09_0.IFO tests/fixtures/treasure_vts_09_0.ifo

# Beauty and the Beast (1991) multi-angle DVD — see
# tests/test_parser_fixtures.py for the files it expects.
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
