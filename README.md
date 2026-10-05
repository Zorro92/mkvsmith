# mkvsmith

> [English](README.md) · [Español](README.es.md)

Turn your DVDs, Blu-rays and HD DVDs into MKV files, with all their audio,
subtitles and chapters.

mkvsmith reads the disc's own structure to work out what's on it: which title
is the movie, which are episodes, which are extras, and which are menus or
filler. Then it hands the actual copying to
[mkvmerge](https://mkvtoolnix.download/). Scanning a disc takes seconds, and
mkvmerge is the only other program you need.

## What it does

- **Rips DVDs, Blu-rays and HD DVDs**: from a drive, an ISO image, or a disc
  folder (`VIDEO_TS`, `BDMV`, `HVDVD_TS`).
- **Finds the main feature for you**, or every episode on a TV series disc,
  and hides menus, trailers and decoy titles.
- **Keeps what matters**: every audio track you want, subtitles (including
  DVD subtitles other tools miss), chapters, and optional closed captions.
- **Combines a film's alternate cuts** (theatrical, extended, …) into a
  single MKV whose editions you can switch between in your player.
- **Optional extras**: movie metadata and cover art from
  [TMDB](https://www.themoviedb.org/), and disc and episode names from
  [TheDiscDB](https://thediscdb.com/).

## Getting started

You need:

1. **Python 3.12 or newer**.
2. **MKVToolNix**, for `mkvmerge`: `sudo apt install mkvtoolnix` on
   Debian/Ubuntu, `brew install mkvtoolnix` on macOS, or the installer from
   [mkvtoolnix.download](https://mkvtoolnix.download/).
3. **[uv](https://docs.astral.sh/uv/)**, which installs everything else for
   you.

Then:

```sh
git clone https://github.com/Zorro92/mkvsmith
cd mkvsmith
uv run ./main.py
```

That opens mkvsmith. The first time, it asks a few questions (your
languages, whether to keep subtitles, and so on). Press Enter to keep each
suggestion. After that, pick a disc and rip.

> **Encrypted discs:** like any ripper, reading a store-bought disc needs
> `libdvdcss` (DVD) or `libaacs` (Blu-ray) installed on your system.
> mkvsmith doesn't decrypt anything itself, and tells you when a disc is
> still encrypted.

## Using mkvsmith

Everything is a list. Move with the arrow keys or the mouse wheel, and pick a
line with Enter or a click. **Esc** goes back and **q** quits. The bar at the
bottom shows the keys for each screen, and you can click them too.

1. **Choose a disc.** Pick a drive, or browse to an ISO or disc folder. You
   can also open one straight away: `uv run ./main.py movie.iso`.
2. **Pick what to rip.** The top of the list offers the likely choice: the
   main feature on a movie, or every episode on a series disc. Below it are
   all the titles. **Space** marks several for one batch, and Enter on a
   title shows its audio and subtitle tracks, so you can choose which to
   keep.
3. **Rip.** mkvsmith asks where to save the first time, then shows each
   file's progress. Esc asks before stopping, then deletes the unfinished
   file.

Your choices are saved, and you can change them any time under **Settings**
in the main menu.

## Optional extras

### Movie info and cover art (TMDB)

mkvsmith can tag each rip with the movie's title, year, cast, plot and cover
art from TMDB. You need a free
[TMDB API key](https://www.themoviedb.org/settings/api). Enter it under
**Settings → tmdb.api_key**, then set **tmdb.tagging** to `ask` (asks for
each disc) or `always`.

### Disc names (TheDiscDB)

[TheDiscDB](https://thediscdb.com/) is a community database of discs. With
**discdb.enabled** on, mkvsmith looks your disc up and uses its titles and
episode names. That helps most on discs that hide the real movie among
dozens of fake ones. Only the disc's ID numbers are sent, never its
contents. mkvsmith can also prepare the files for
[contributing a disc](https://thediscdb.com/) you've scanned
(`--discdb-contribute`).

## The command line

For scripts, or if you just prefer typing, mkvsmith also works without its
interface. Any action flag skips straight to the work:

```sh
uv run ./main.py movie.iso -m           # rip the main feature (or all episodes)
uv run ./main.py movie.iso -t 1,3       # rip titles 1 and 3
uv run ./main.py movie.iso -a ~/rips    # rip every title into ~/rips
uv run ./main.py movie.iso -i           # just list the titles
```

`--help` shows the everyday options and `--help-all` shows every one. Flags
override your saved settings for that run only. The
[reference](docs/REFERENCE.md) has the details.

## Good to know

The [reference](docs/REFERENCE.md) covers all of this in more depth.

- **Where settings live:** `~/.config/mkvsmith/config.json`. Set
  `MKVSMITH_CONFIG` to use another file.
- **Platforms:** made and tested on Linux. Discs from ISOs and folders should
  work anywhere. Reading straight from a drive works on Linux, and drive
  letters are recognised on Windows, but Windows and macOS are largely
  untested.
- **Disc images from burned discs** (rewritable or recordable media) can't
  be opened yet. Copy the disc to a folder first.
- **Switching editions** in a combined multi-edition MKV needs a player that
  supports ordered chapters, such as mpv or VLC.
- **Episode names** look like `Show - S01D02 - Episode 3` when the disc or
  folder name includes the season and disc number, otherwise
  `<disc name> - Episode 3`. Discs can't know how many episodes came before
  them, so numbering starts over on each disc.
- **Long series Blu-rays** that play a whole disc as one 15–20 hour video
  (e.g. Sgt. Frog) can be split into one file per episode. mkvsmith offers
  this when it spots one. Some hardware video players show a black screen
  for a split episode that starts mid-clip; mkvsmith warns you when that
  happens, and software playback is fine.
- **HDR:** HDR10 and HDR10+ come through untouched. Dolby Vision hasn't
  been tested; only the HDR10 base layer is kept.
- **Temporary files** go to `/var/tmp`. Big extractions avoid filling a RAM
  disk. Change the folder with `temp.dir` if space is tight.

## Contributing

Bug reports and pull requests are welcome. See
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for running the checks and tests,
including the optional disc-based test fixtures.

## Vibe check

This project was *vibe coded*: mostly described to an LLM and iterated on,
rather than typed out line by line. The disc-format parsing and the
behavioural decisions are deliberate and covered by tests against real
disc images; the rest may have been written with unwarranted confidence.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
