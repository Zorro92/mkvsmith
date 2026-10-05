# mkvsmith reference

The details the [README](../README.md) leaves out: command-line rules, the
settings file, TheDiscDB, and how mkvsmith treats unusual discs.

## Command line

`--help` lists the everyday options; `--help-all` lists every option,
grouped by section (tracks, titles, TMDB tagging, TheDiscDB, temporary
files, ...).

- **Running it directly:** `main.py` carries a `uv run --script` shebang,
  so after `chmod +x main.py` it runs as `./main.py`.
- **Sources:** an optical drive, an ISO image, a disc folder (`VIDEO_TS`,
  `BDMV`, or `HVDVD_TS` with its XPL playlists), raw `.m2ts`/`.vob`/`.evo`
  files, or a plain video file.
- **One action per run:** `-t`, `-m`, `-a`, `-i`, `-d`, `--multi-edition`
  or `--settings`. Without one, mkvsmith opens its full-screen interface
  (when run at a terminal).
- **`-m` is smart:** the main feature on a movie disc, every episode on a
  series disc. On DVD, that covers episodes grouped within one title set and
  one episode per title set. On Blu-ray, it means one playlist per episode.
- **Output folder:** the second positional argument (`main.py disc.iso -m
  ~/rips`), else the current directory.
- **List values** are one argument, separated by commas (`jpn,eng`) or by
  spaces inside quotes (`"jpn eng"`). Quote anything else that contains a
  space: paths (`"/media/My Disc.iso"`), `--tag-title "The Matrix"`,
  `--set "temp.dir=/mnt/big disk/tmp"`.
- **Every on/off flag has a `--no-` form**, so a flag can override a saved
  setting either way.
- **Streams:** `-s v:0,a:eng,s:all` picks streams by type and index or
  language; `-l jpn,eng` sets the preferred languages, which decide which
  subtitles are kept and which audio track is the default.
- **Saved settings from the command line:** `--settings` shows them,
  `--set KEY=VALUE` saves one (`--set KEY` asks, hidden for secrets), and
  `--reset KEY` restores the default.

## Settings

Settings live in `$XDG_CONFIG_HOME/mkvsmith/config.json` (by default
`~/.config/mkvsmith/config.json`). Set `MKVSMITH_CONFIG` to use another
file, for example from a script. Every option resolves as **flag >
environment variable > settings file > built-in default**, so a flag only
changes the run it's on.

The full-screen interface checks the file each time it starts. The first
run asks every everyday setting; after an update, only the new ones. Some
questions appear only once they matter: the caption format once captions
are on, and the tagging choices once a TMDB key is set. Advanced settings
(temp dir, RAM limit, TMDB region and fields, TheDiscDB server) are never
asked; their defaults are written to the file for editing. Plain
command-line runs never ask: anything missing uses the built-in default.

```json
{
  "version": 2,
  "output": {"overwrite": "ask"},
  "tracks": {"languages": ["eng", "en", "und"], "closed_captions": "ask"},
  "scan": {"split_episodes": "never"},
  "tmdb": {"api_key": "...", "tagging": "never", "art": "ask"}
}
```

Per-disc choices take `never`, `ask` (every time) or `always`:
`output.overwrite`, `tracks.closed_captions`, `scan.split_episodes`,
`tmdb.tagging`, and `tmdb.art` (`none` / `poster` / `backdrop` / `both` /
`ask`). The interface asks these for each disc (captions only when the disc
has them); a command-line run treats `ask` as the built-in default.

The output folder is not a setting: the command line writes to the current
directory unless given one, and the interface asks before its first rip.
Invalid values are reported and asked again, and older flat config files are
migrated automatically.

## TheDiscDB

Lookup is opt-in: `--discdb` for one run, or `discdb.enabled` in the
settings. It sends only identifiers computed from the disc, never playlist
data or media file contents.

```sh
# identify a disc and apply a unique community title mapping
uv run ./main.py movie.iso --discdb --info

# prepare files for TheDiscDB's reviewed contribution flow
uv run ./main.py movie.iso --discdb-contribute=browser
uv run ./main.py movie.iso --discdb-contribute=manual --discdb-bundle-dir ~/discdb
```

**Matching** uses TheDiscDB's legacy Disc Hash, the
[matrix256v1](https://github.com/shitwolfymakes/matrix256) fingerprint, the
Blu-ray AACS Disc ID, or the libdvdread DVD Disc ID. A UPC/EAN is only a
weak hint, and must be backed up by playlist or title/duration data. A
unique match's `MainMovie` outranks mkvsmith's own guess, which sees
through "screen pass" playlist obfuscation. Ambiguous matches never rename
titles or change the main feature. The format-specific Disc IDs need
readable `AACS`/`VIDEO_TS` structures (folder, ISO or mounted image); a raw
`/dev` device doesn't expose them.

**Contributing:** `--discdb-contribute` writes `manifest.json` plus a scan
log (`scan_log.txt`) built from mkvsmith's own playlist and IFO parsing.

- **Browser mode:** open or create a contribution draft, then upload
  `scan_log.txt` where the site asks for a scan log.
- **Direct mode:** attaches the data to an existing draft, given
  `--discdb-contribution-id` and a logged-in browser cookie
  (`--discdb-cookie` or `THEDISCDB_COOKIE`). Treat that cookie like a
  password. Direct mode stops before labelling and review, which stay
  human-approved steps. Use `--discdb-disc-name` when adding more discs to
  the same draft.

Blu-ray segment maps come straight from playlist clip IDs. DVD cell-range
maps are left blank for a person to fill in, because mkvsmith doesn't expose
DVD cell IDs.

## How mkvsmith handles discs

- **Identifiers:** each scan shows the disc's matrix256v1 fingerprint and
  its DVD/Blu-ray identifiers. Each muxed track gets a `SOURCE_ID` tag (the
  Blu-ray PID, or the DVD title set and stream IDs) next to mkvmerge's
  statistics tags.
- **Copy-protected DVDs** that bury the film among dozens of decoy chains
  (Disney's, for one) list only the real versions. Scrambled decoys and
  duplicate chains are hidden (`--show-all` shows them, labelled). Padded
  cell ranges are ripped without their junk, and a version's alternate cuts
  are named `Edition 1`, `Edition 2`, ...
- **HD DVD** titles come from the XPL playlists, with their chapters and
  languages. EVO subpictures are extracted the same way as DVD subtitles.
- **CSS-encrypted DVDs:** a DVD image or folder that is still encrypted is
  flagged when scanned, instead of failing at the mux.
- **Multi-edition MKVs** (`--multi-edition`, or "Combine" in the title list)
  join seamless-branching versions of a film (Blu-ray playlists, or DVD
  program chains sharing cells) into one file. The joins are gapless and the
  chapters exact. Switching editions needs a player with ordered-chapters
  support, such as mpv or VLC.
- **Episode numbers** are zero-padded to the disc's highest
  (`Episode 001` … `Episode 101`), so they line up and sort. A TheDiscDB
  match supplies its own names instead.
- **Packed episodes:** some series Blu-rays (e.g. Sgt. Frog) play a whole
  disc's episodes from one 15-20 hour playlist. mkvsmith spots these from
  the chapter marks, and lists the playlist as usual with a hint; nothing is
  split by default. `--split-episodes`, or "Split" in the title list, turns
  it into one title per episode (plus any trailing extra), each with its own
  range and chapters.
  - Blu-ray video usually has a keyframe (IDR) only at the start of each
    disc clip, so an episode cut mid-clip starts on a recovery-point frame
    instead.
  - Software decoders handle that, but some hardware decoders (e.g.
    Android's, as used by mpv-android) show a black screen with audio.
  - mkvsmith warns when a rip starts this way. Fixing it would need
    re-encoding.
- **Interlaced video:** H.264 rips that are interlaced throughout (e.g.
  1080i extras, SD anime) are marked interlaced with their field order,
  judged from the muxed frames. Soft-telecined film, which Blu-ray clip info
  also calls interlaced, is left progressive, so players don't deinterlace
  real film frames. This needs `mkvpropedit` (part of MKVToolNix).
- **HDR:** HDR10 and HDR10+ metadata travel inside the video and survive a
  remux untouched. The BT.2020/PQ colour signalling is read from the
  playlist.
  - No Dolby Vision Profile 7 (dual-layer UHD) disc has been tested.
  - A remux keeps only the HDR10-compatible base layer; full Dolby Vision
    would need bitstream-level processing.
  - It's unverified whether the enhancement layer can show up as a stray
    extra video track.
- **ISO images** are read by a built-in UDF/ISO9660 reader. Images of burned
  rewritable or recordable media (sparable or virtual UDF partitions) aren't
  supported yet.
- **Temporary files** go to `/var/tmp` when usable, else the system temp
  folder. If the temp folder is RAM-backed (tmpfs, e.g. `--temp-dir /tmp`),
  oversized extractions spill to disk automatically.
  - The RAM budget is `--ram-limit` of the smaller of total RAM and the
    tmpfs size.
  - Extra guards check the RAM currently available and the tmpfs free
    space, since `/tmp` is shared.
- **Platforms:** folder, ISO and video-file sources are written to be
  cross-platform. Windows drive letters (`E:`) are recognised as device
  sources, but `/dev/...` device input is Linux-only.
