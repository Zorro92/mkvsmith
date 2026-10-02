# mkvsmith

> [English](README.md) · [Español](README.es.md)

DVD/Blu-ray ripper that produces MKV files using
[mkvmerge](https://mkvtoolnix.download/) (MKVToolNix).

`mkvsmith` reads disc structures directly — `.mpls` / `.clpi` / `.ifo` / BDMV
metadata — instead of probing media bitstreams. That makes scanning fast and
dependency-light: the only external media tool it needs is `mkvmerge`. It
follows de-facto ripping conventions where those are the sensible
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
- **mkvmerge** (MKVToolNix) — the only external tool, and a hard
  requirement for muxing. ISO images (UDF and ISO9660) are read natively.
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
# (DVD: within-VTS PGC clusters and one-episode-per-VTS authoring;
#  Blu-ray: one playlist per episode, e.g. with a "play all" playlist)
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
mkvsmith> settings   # list saved settings (set KEY VALUE / reset KEY to change)
mkvsmith> q          # quit
```

### Common options

`-h` shows the everyday options; `--help-all` lists every option, grouped
by section (tracks, titles, TMDB tagging, TheDiscDB, temporary files, ...).

| Flag | Description |
|---|---|
| `-t, --title N[,N...]` | Rip one title, or several (`-t 1,3,5`) |
| `-m, --main` | Rip the detected main feature (all episodes on series discs) |
| `-a, --all` | Rip all titles |
| `-i, --info` | Just scan and list titles |
| `-d, --details N` | Show one title's tracks and chapters |
| `--multi-edition N,N,...` | Combine playlist titles into one multi-edition MKV |
| `--settings` | Show the saved settings and exit |
| `--set KEY[=VALUE]` / `--reset KEY` | Save a setting (no value: asked, hidden for secrets) or restore its default, then exit |
| `--split-episodes` / `--no-split-episodes` | Split playlists holding back-to-back episodes into one title per episode |
| `-s, --streams SEL,...` | Select streams (e.g. `v:0,a:eng,s:all`) |
| `-l, --languages LANG,...` | Preferred languages (default `eng,en,und`): keeps only these subtitles, marks the default audio |
| `--all-audio` / `--no-all-audio` | Keep audio in every language (default on) |
| `--subs` / `--no-subs` | Keep subtitles |
| `--all-subs` / `--no-all-subs` | Keep subtitles in every language, not only `-l` (default off) |
| `--forced` / `--no-forced` | Keep forced subtitles |
| `--cc` / `--no-cc` | EIA-608 closed captions as a text track (default off) |
| `--cc-format srt\|ass` | CC track format: portable text or positioned ASS |
| `--min-duration N` | Ignore titles shorter than N seconds |
| `--show-all` / `--no-show-all` | Show low-quality titles (menus/trailers) |
| `--temp-dir DIR` | Temp dir (default: /var/tmp; override for tmpfs/RAM or another disk path) |
| `--ram-limit FRAC` | Max fraction of tmpfs capacity for RAM-backed temp dirs |
| `--overwrite ask\|always\|never` | An existing output file: ask, overwrite, or skip the title |
| `--force` | Same as `--overwrite always` |
| `--tag` / `--no-tag` | TMDB tagging |
| `--tag-art none\|poster\|backdrop\|both\|ask` | Cover art to embed |
| `--tag-confirm` / `--no-tag-confirm` | Confirm the TMDB match before tagging |
| `--tag-metadata PROP,...` | TMDB properties to fetch |
| `--discdb` / `--no-discdb` | TheDiscDB lookup (opt-in) |
| `--discdb-contribute MODE` | Write/upload a contribution (`browser`, `manual`, `direct`, or `off`) |
| `--discdb-disc-name NAME` | Disc name used by direct contribution mode |
| `--ui-lang LANG` | UI language (e.g. `en`, `es`) |
| `--debug` | Verbose debug logging |

List values are one argument, separated by commas (`jpn,eng`) or by spaces
inside quotes (`"jpn eng"`). Quote anything else that contains a space: paths
(`"/media/My Disc.iso"`), `--tag-title "The Matrix"`, `--set "temp.dir=/mnt/big
disk/tmp"`. Only one action
(`-t`, `-m`, `-a`, `-i`, `-d`, `--multi-edition`, `--settings`) is allowed
per run. Every on/off flag has a `--no-` form, so a flag can override a saved
setting either way.

### Settings

Your defaults live in `$XDG_CONFIG_HOME/mkvsmith/config.json` (default
`~/.config/mkvsmith/config.json`; set `MKVSMITH_CONFIG` to use another file,
e.g. from a script). Every option resolves as **flag > environment variable >
settings file > built-in default**, so a flag only changes the run it's on.

The interactive mode checks the file for completeness each time it starts:
the first run asks every everyday setting (Enter keeps the suggestion), and
after an update only the settings that are new. Follow-up questions appear
once they matter (the caption format once captions are on, tagging choices
once a TMDB key is set). Advanced settings (temp dir, RAM limit, TMDB region
and fields, TheDiscDB server) are never asked; their defaults are written to
the file for editing. Plain CLI runs never ask: anything missing uses the
built-in default.

```json
{
  "version": 2,
  "output": {"overwrite": "ask"},
  "tracks": {"languages": ["eng", "en", "und"], "closed_captions": "ask"},
  "scan": {"split_episodes": "never"},
  "tmdb": {"api_key": "...", "tagging": "never", "art": "ask"}
}
```

Per-disc choices take `never`, `ask` (ask every time), or `always`:
`output.overwrite`, `tracks.closed_captions`, `scan.split_episodes`,
`tmdb.tagging`, and `tmdb.art` (`none` / `poster` / `backdrop` / `both` /
`ask`). The interactive mode asks those on each disc (captions only when the
disc has them); a plain CLI run treats `ask` as the built-in default. So a
stored TMDB key with `"tagging": "never"` no longer needs `--no-tag` every
run. The output folder is not a setting: the CLI writes to the current
directory unless given one, and the interactive mode asks before its first
rip. Invalid values are reported and asked again; older flat config files
are migrated automatically.

### TheDiscDB

TheDiscDB lookup is opt-in. Enable it per run with `--discdb`, or persist it in
the [settings file](#settings) under `"discdb": {"enabled": true}`. Lookup sends
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

`--discdb-contribute` writes `manifest.json` plus a compatible scan
log (`scan_log.txt`) generated from mkvsmith's own MPLS/CLPI/IFO parsing. In browser mode, open
or create a contribution draft and upload `scan_log.txt` where the site
asks for a scan log. Direct mode attaches that data to an existing
contribution draft using
`--discdb-contribution-id` and an authenticated browser cookie supplied with
`--discdb-cookie` or `THEDISCDB_COOKIE`; it stops before item labelling and
review, which remain human-approved steps. Use `--discdb-disc-name` when
attaching additional discs to the same draft. Treat the cookie like a password.
Blu-ray segment maps come directly from playlist clip IDs; DVD cell-range maps
are intentionally left blank for human identification because mkvsmith does
not expose DVD cell IDs.

## Notes

- **Platform support:** Linux is the primary platform. Folder, ISO, and
  video-file sources are written to be cross-platform, and Windows drive
  letters (`E:`) are recognised as device sources, but `/dev/...`
  optical-device input is Linux-only. Windows and macOS support is otherwise
  untested.
- ISO images are read with a built-in UDF/ISO9660 reader. Images from burned
  rewritable or recordable media (sparable or virtual UDF partitions) are not
  supported yet; copy such a disc to a folder first.
- Encrypted commercial discs need `libdvdcss` (DVD) / `libaacs` (Blu-ray) at
  the OS level. A DVD image or folder that is still CSS-encrypted is flagged
  when scanned (mkvsmith doesn't decrypt), instead of failing at the mux.
- **Copy-protected DVDs** that bury the film among dozens of decoy chains
  (Disney's, for one) list only the real versions: scrambled decoys and
  duplicate chains are hidden (`--show-all` shows them labelled), padded
  cell ranges are ripped without their junk, and a version's alternate
  cuts read as `Edition 1`, `Edition 2`, ...
- Temp files default to `/var/tmp` (disk-backed) when usable, falling back to
  the system temp dir. If the effective temp dir is RAM-backed (tmpfs —
  e.g. an explicit `--temp-dir /tmp`), `mkvsmith` detects this and
  transparently spills oversized extractions to disk. The budget is
  `--ram-limit` of the smaller of total RAM and the tmpfs size (a tmpfs is
  frequently capped at a fraction of RAM), with extra guards for
  currently-available RAM and tmpfs free space, since `/tmp` is shared.
- **Multi-edition MKV output** (`--multi-edition`, or the interactive `me`
  command) combines seamless-branching playlists into one file with
  gapless joins and exact chapter placement. It needs a player with
  ordered-chapters support (e.g. mpv, VLC) to switch editions.
- **Episode names.** A disc can't know how many episodes earlier discs of
  a set held, so episodes are numbered per disc and named with the season
  and disc number when the disc, folder or release name carries them
  (`Show - S01D02 - Episode 3`), else `<disc name> - Episode N`.
  Numbers are zero-padded to the disc's highest (`Episode 001` …
  `Episode 101`) so they line up and sort. A TheDiscDB match (`--discdb`)
  supplies its own names instead.
- **Packed episodes.** Some series Blu-rays (e.g. Sgt. Frog) play a whole
  disc's episodes from one 15-20 hour playlist. `mkvsmith` detects these
  from the playlist's chapter marks and lists the playlist as usual, with a
  hint; nothing is split by default. Pass `--split-episodes`, or use the
  interactive `se N` command, to turn it into one title per episode (plus any
  trailing extra), each cut to its own range with its own chapters.
  Blu-ray video usually has an IDR frame only at the start of each disc clip,
  so episodes cut mid-clip start on a recovery-point frame instead. Software
  decoders handle that, but some hardware decoders (e.g. Android's, as used by
  mpv-android) show a black screen with audio. `mkvsmith` warns when a rip
  starts this way; play those files with software decoding (fixing it would
  need re-encoding).
- **Interlaced video.** H.264 rips that are interlaced throughout (e.g. 1080i
  extras, SD anime) are marked interlaced with their field order, judged from
  the muxed frames themselves. Soft-telecined film, which Blu-ray clip info
  also calls "interlaced", is left progressive so players don't deinterlace
  real film frames. Needs `mkvpropedit` (part of MKVToolNix).
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
# Blu-ray, from an .iso (playlist/clip numbers are disc-specific):
uv run python scripts/iso_extract.py disc.iso tests/fixtures "BDMV/PLAYLIST/00800.mpls" "BDMV/CLIPINF/00875.clpi" "BDMV/META/DL/bdmt_eng.xml"

# DVD, from an extracted VIDEO_TS folder:
cp VIDEO_TS/VIDEO_TS.IFO tests/fixtures/dvd_video_ts.ifo
cp VIDEO_TS/VTS_01_0.IFO tests/fixtures/dvd_vts_01_0.ifo
```

Optional, disc-specific fixtures (their tests skip when absent):

```sh
# Treasure Planet (2002) R1 DVD9 — alternate-edition PGC detection:
uv run python scripts/iso_extract.py treasure_planet.iso tests/fixtures "VIDEO_TS/VTS_01_0.IFO" "VIDEO_TS/VTS_09_0.IFO"
mv tests/fixtures/VTS_01_0.IFO tests/fixtures/treasure_vts_01_0.ifo
mv tests/fixtures/VTS_09_0.IFO tests/fixtures/treasure_vts_09_0.ifo

# Beauty and the Beast (1991) multi-angle DVD — see
# tests/test_parser_fixtures.py for the files it expects.

# Monsters University (2013) Blu-ray — seamless-connection trimming
# (tests/test_m2ts.py). Header-only carvings of two clip tails; no essence:
uv run python scripts/iso_extract.py disc.iso /tmp/mu "BDMV/STREAM/00875.m2ts" "BDMV/STREAM/00876.m2ts"
for c in 00875 00876; do
  uv run python scripts/carve_m2ts_tail_fixture.py /tmp/mu/$c.m2ts tests/fixtures/${c}_tail_headers.m2ts
done

# STN SubPath / IG entries (tests/test_bluray_stn.py):
uv run python scripts/iso_extract.py disc.iso tests/fixtures "BDMV/PLAYLIST/00307.mpls"   # Monsters University
cp SGT_FROG_S1_D1/BDMV/PLAYLIST/00000.mpls tests/fixtures/sgtfrog_s1d1_00000.mpls
cp SGT_FROG_S7/BDMV/PLAYLIST/00001.mpls tests/fixtures/sgtfrog_s7_00001.mpls

# Packed-episode detection (tests/test_packed_episodes.py):
cp SGT_FROG_S2_D1/BDMV/PLAYLIST/00003.mpls tests/fixtures/sgtfrog_s2d1_00003.mpls

# Built-in ISO reader (tests/test_isofs.py). Sparse captures holding only the
# filesystem sectors plus a few small files; no essence:
uv run python scripts/capture_iso_fixture.py monster_high_welcome.iso \
  tests/fixtures/monster_high_udf250.isofix.gz BDMV/index.bdmv \
  BDMV/MovieObject.bdmv BDMV/PLAYLIST/00800.mpls BDMV/CLIPINF/00800.clpi \
  BDMV/STREAM/00800.m2ts   # Monster High: Welcome to Monster High (2016) BD
uv run python scripts/capture_iso_fixture.py treasure_planet.iso \
  tests/fixtures/treasure_planet_dvd.isofix.gz VIDEO_TS/VIDEO_TS.IFO \
  VIDEO_TS/VTS_01_0.IFO VIDEO_TS/VTS_01_1.VOB
```

To cross-check the reader against 7z on whole images, point
`MKVSMITH_TEST_ISOS` at one or more ISOs (`:`-separated) and run
`uv run pytest tests/test_isofs.py -k real_iso`.

`scripts/inspect_fixtures.py` re-parses whatever is in `tests/fixtures/` and
prints the values the tests expect, which is handy when swapping in a new disc.

## Vibe check

This project was *vibe coded* — mostly described to an LLM and iterated on,
rather than typed out line by line. The disc-format parsing and the
behavioural decisions are deliberate and covered by tests against real
disc images; the rest may have been written with unwarranted confidence.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
