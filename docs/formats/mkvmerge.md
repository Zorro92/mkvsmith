# mkvmerge

mkvmerge is mkvsmith's one external media tool. This page lists what
mkvsmith reads from it, which of its behaviours mkvsmith works around,
and how each is checked, so a new MKVToolNix release can be checked
against them before it breaks a rip.

Changelog reviewed through: v103.0 (2026-10-18), on 2026-10-22.
Tested with: v96.0 (rips of real discs). The contract tests pass with
v54.0, 81.0, 82.0, 92.0, 96.0 and 103.0, and the whole suite with v54.0,
82.0, 96.0 and 103.0 (`scripts/mkvmerge_versions.py`, 2026-10-22). CI
runs the tests with Ubuntu's packaged mkvtoolnix (x86 and ARM64) and the
latest Homebrew and Chocolatey releases, and the contract tests with the
v54.0, v81.0 and newest AppImages (the `mkvmerge-versions` job).

## Sources

| Key | Source | Where |
|---|---|---|
| NEWS | MKVToolNix changelog, per release | https://mkvtoolnix.download/doc/NEWS.md |
| GIT | MKVToolNix source; its `NEWS.md` on `main` heads the unreleased changes `# Version ?` | https://codeberg.org/mbunkus/mkvtoolnix |
| AI | AppImages of every release since 25.0.0 (Linux x86_64) | https://mkvtoolnix.download/appimage/ |
| MAN | mkvmerge manual | https://mkvtoolnix.download/doc/mkvmerge.html |

The old site, bunkus.org, still serves the same changelog and source
archives but isn't kept up; mkvtoolnix.download is.

**✔ contract test** marks a check in `tests/test_mkvmerge_contract.py`,
which muxes a synthetic stream with the installed mkvmerge; **✔ real
disc** marks a check only a rip of a disc can make.

## Minimum versions

`models.mkvmerge_version()` reads `mkvmerge --version` once; features
that need a newer mkvmerge are gated on it (details in
[matroska-output.md](matroska-output.md)).

| Version | Needed for | Gate |
|---|---|---|
| 54.0 | hearing-impaired, commentary and original-language flags | `MKVMERGE_FLAGS_VERSION` |
| 81.0 | a UHD disc's two Dolby Vision layers as one track | `MKVMERGE_DOLBY_VISION_VERSION` |

## What mkvsmith reads from mkvmerge

| What | Used by | Check |
|---|---|---|
| `-J` JSON: `container.recognized`, each track's `id`, `type`, `codec` and `properties` (`pixel_dimensions`, `audio_channels`, `audio_sampling_frequency`, `language`, `number`, `track_name`, `default_track`, `forced_track`); an MKV's `container.properties.duration`. `identification_format_version` is 20 in v96 | `probe.py`, `mkv.py` | ✔ contract test (the common fields) |
| `-J` codec names, mapped to mkvsmith's (`MPEG-1/2`, `AVC/H.264/MPEG-4p10`, …) | `probe._MKVMERGE_CODEC_MAP` | ✔ contract test (VC-1 only); [codecs.md](codecs.md) |
| `Progress: N%` lines while muxing, read as they arrive | `mkv._read_mkvmerge_output` | ✔ contract test |
| Exit code 0, 1 (finished with warnings, each line starting `Warning:`), 2 (failed) | `mkv._mkvmerge_warnings`, `mkv._validate_mux_result` | ✔ contract test |
| English output: the mux passes `--ui-language en_US`, since in another locale mkvmerge translates the lines above (`Progreso: N%`, `Advertencia:` in its Spanish catalogue) | `mkv._build_mkvmerge_command` | ✔ contract test (the option, back to v54.0); the catalogue read from the v103.0 AppImage |
| An EVO's private stream 1 track IDs, `(sub-stream << 32) \| 0xBD` | `scan._apply_hddvd_languages` | ✔ real disc |

## Behaviour mkvsmith works around

| mkvmerge | mkvsmith | Evidence | Check |
|---|---|---|---|
| EVO VC-1 (frames without PTS): mistimes the first GOP after each chapter's sequence header, runs every frame after an EVO's first 150–167 ms late, and two frames early where a stream drops its B frames; one block per frame, the first on its PTS | rewrites the block times from the EVO (`vc1time.correct_vc1_frame_times`) | [hd-dvd.md](hd-dvd.md#vc-1-frame-timing), five features | ✔ contract test (one block per frame, first on its PTS, still mistimed, rewritten on the cadence) |
| Two EVOs appended: the second's VC-1 lands ~0.3 s behind its audio | reads clock-joined EVOs as one stream, `( a b )` | [hd-dvd.md](hd-dvd.md) | ✔ real disc |
| Skips EVO subpictures (`0x20`–`0x3F`, "not supported yet") and reports no EVO languages | decodes them to PGS (`hdspu.py`); languages from the XPL | [hd-dvd.md](hd-dvd.md), [subpictures.md](subpictures.md) | ✔ real disc |
| Blu-ray VC-1 (VfW mode): a block carries the frame before's time (the DTS), so `track` append mode puts the next clip a frame early | `--sync` of a frame on each appended clip; the join's first block retimed after the mux (`vc1time.retime_vc1_join_frames`) | [bluray.md](bluray.md#vc-1-across-seamless-joins), one 29-clip feature; H.264 unaffected (one 132-clip feature) | ✔ contract test (DTS times, a frame early, on time once fixed) |
| `track` append mode: each audio track follows its own end, so an overhang creeps at every join; `file` mode leaves gaps | trims clip tails' audio before a `track` append (`m2ts.plan_seamless_trim`) | `m2ts.py`, [bluray.md](bluray.md) | ✔ real disc |
| `file` append mode offsets each clip by its real end, past the play item's out time | edition chapters placed on placeholder chapters from `--generate-chapters when-appending` | `mkv.py` (`_RETIME_EPS`) | ✔ real disc |
| Shifts chapters unevenly across `--split parts:` | writes the chapters after the mux | [bluray.md](bluray.md#clips-played-in-part) | ✔ real disc |
| Regenerates `_STATISTICS_TAGS` without `SOURCE_ID`, and puts a mux-time tag's UID after its Simples | rewrites the tags after the mux | `mkv._normalize_tags_for_source_id`, [matroska-output.md](matroska-output.md) | ✔ contract test (both still so, and fixed after) |
| Writes no interlace elements for AVC | flags interlaced H.264 after the mux | `mkv._apply_interlace_flags` | ✔ contract test (none written, then both field orders flagged, progressive left alone) |
| Misses sparse DVD subpictures it doesn't see in the first VOB | scans the VOBs and adds a VobSub input | `vobsub.py`, `mkv.py` | ✔ real disc |
| Outputs TrueHD and its embedded AC-3 core as two tracks on one PID | treats the PID as not trimmable; the trim plan follows the TrueHD units | `m2ts._audio_tail`, `mkv._seamless_audio_pids` | none |
| Can't read scrambled VOBs | refuses them (`disc_reader.py`) | | none |

## Review log

**v97.0–v103.0** (reviewed 2026-10-22, against v96.0):

- v101.0: the TrueHD parser no longer drops a last frame smaller than
  12 bytes. The trim plan counts every TrueHD unit already; at most a
  0.83 ms unit more audio. Nothing to change.
- v100.0: the MPEG TS reader no longer skips the start of the main M2TS
  when reading an MPLS on a disc with TextST subtitles. mkvsmith gives
  mkvmerge clips, never the MPLS: unaffected.
- v103.0: AVC and HEVC stereo mode is now set from a frame-packing SEI,
  in transport streams too. A disc with that SEI gains a `StereoMode`;
  mkvsmith sets none itself. Nothing to change.
- v101.0: libEBML 1.4.7 and libMatroska 1.7.2 are required (distribution
  packaging only).
- Everything else: the GUI, MP4, AVI, AAC, fonts and packaging. mkvmerge's
  VC-1 handling hasn't changed since v17.0.

## Reviewing a new release

1. `uv run python scripts/mkvmerge_news.py` lists the mkvmerge entries
   newer than the "reviewed through" line above, marking those that touch
   what's listed here; `--git` adds the changes merged on `main` for the
   next release.
2. Check each marked entry against the tables. A fix to a behaviour
   mkvsmith works around can make the workaround wrong, not only
   unneeded (the VC-1 `--sync` would put clips a frame late).
3. Run the tests with the release:
   `uv run python scripts/mkvmerge_versions.py latest --all-tests` takes
   its AppImage (unpacked without FUSE into `~/.cache/mkvsmith/mkvtoolnix`,
   about 150 MB a version) and puts it first on PATH; any other versions
   can be listed alongside, such as the minimums above. A failing canary
   (`…still_mistimes…`, `…a_frame_early`) says which workaround to
   revisit.
4. For a behaviour checked only on real discs, rip one such disc.
5. Add the release to the review log and move the "reviewed through"
   line on.
