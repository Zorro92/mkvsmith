# Agent Notes

## No ffmpeg/ffprobe in the main code

Do NOT shell out to `ffmpeg` or `ffprobe` from the production code
(`main.py`, `cli.py`, `probe.py`, `bluray.py`, `dvdbuild.py`, `scan.py`,
`mkv.py`, `dvdifo.py`, `vobsub.py`, `disc_reader.py`, `tagger.py`,
`models.py`, `settings.py`, `i18n.py`). This is a hard project constraint, not
a preference. The project depends on **mkvtoolnix** (`mkvmerge`) as its only
external media tool.

`ffmpeg` / `ffprobe` are acceptable as **debugging or testing tools that live
outside the main code** — a throwaway script under `scripts/`, a one-off shell
command, or a test that cross-checks a parser's output against ffprobe on a real
disc image. They must not become a runtime dependency of the shipped tool.

### Why

This tool deliberately parses disc structures directly (`.mpls` / `.clpi` /
`.ifo` / BDMV metadata) instead of probing media streams. Probing every M2TS/VOB
via ffprobe was the old approach and made scanning orders of magnitude slower
(see the comments around the CLPI-merge code in `main.py`). That dependency was
removed on purpose; do not reintroduce it.

### What to use instead

- **`mkvmerge -J`** is the project's only external media tool and is already a
  hard dependency (`_HAS_MKVMERGE`). It is acceptable for probing when a probe is
  genuinely needed (e.g. the mux-time channel-count refinement in `create_mkv`),
  because it is already required and reads only headers.
- **Parse the structured files yourself.** MPLS, CLPI, IFO, and bdmt.xml carry
  codecs, languages, channel config, chapters, durations, and disc names — read
  the binary/structured data directly rather than probing the bitstream.
- If you find yourself wanting ffprobe for something (channel *layout* vs
  channel *count*, stream duration on a raw file, etc.), stop and prefer either
  mkvmerge `-J` output or direct parsing. Ask the user before introducing an
  ffprobe/ffmpeg call into the main code.

## MakeMKV is the behavioural reference (but output is not 1:1)

This tool deliberately mirrors MakeMKV where behaviour is ambiguous: track
naming, normalising chapters to start at 0, stripping the trailing
end-of-movie chapter, exposing seamless-branching editions, and
de-duplicating identical playlists. When a "how should this behave?" question
arises and nothing else decides it, match MakeMKV. Comments that say "matches
MakeMKV" are intentional — don't "improve" on them.

That said, the two are NOT byte-identical by design. mkvsmith intentionally
diverges in muxing details — e.g. it does NOT split DTS-HD MA into a separate
core track, it does NOT duplicate playlist-referenced subs that share a PID,
and it zlib-compresses PGS subtitles. Don't "fix" mkvsmith to match MakeMKV on
these; the divergences are deliberate.

MakeMKV is closed-source. To compare behaviour, ask the user to run
`makemkvcon --robot ...` (or describe the expected output) — don't guess at
what MakeMKV does.

## Reference implementations & specs (read, don't depend on)

When extending a parser (a new CLPI/IFO/MPLS field, a codec's channel layout,
container details, etc.), consult these before reverse-engineering a binary
format from scratch. They are **reference sources to READ / cross-check**, not
runtime dependencies to add. The canonical attributions live in the header of
`main.py`.

### Blu-ray (CLPI / MPLS / BDMV)

- **libbluray** — https://code.videolan.org/videolan/libbluray — the canonical
  BD-ROM reference implementation (CLPI/MPLS parsing, disc structure). Also see
  its docs: https://videolan.videolan.me/libbluray/ (BD-J, UDF, metadata).
- **ace20022/libbluray** — https://github.com/ace20022/libbluray — a clean,
  Pythonic CLPI/MPLS parsing reference; closest in style to this codebase.
- **pyparsebluray** — https://github.com/Ichunjo/pyparsebluray — STN table
  parsing, CHARACTER_CODE, HEVC HDR metadata.
- **bluinfo** — https://github.com/SavSanta/bluinfo — CLPI format layout and
  stream attribute constants.
- **BD-ROM spec** — the authoritative format definition for anything ambiguous.
- **xin1generator** — https://github.com/RollingStar/xin1generator — the
  multi-edition ordered-chapters algorithm (segment-boundary hidden atoms,
  edition TITLE tags) ported in `scan.py`'s `_edition_atoms` /
  `build_multi_edition_title` and `mkv.py`'s `_write_multi_edition_chapters_xml`.

### DVD (IFO / VOB / PGC)

- **dvdutils** — https://pypi.org/project/dvdutils/ — IFO attribute structs
  (VideoAttrs / AudioAttrs / SubpictureAttrs / CellPlaybackInfo).
- **pyparsedvd** — https://github.com/Ichunjo/pyparsedvd — DVD parsing.
- **libdvdread** — struct layout reference (`ifo_types.h`, `cell_playback_t`,
  VTS_C_ADT); cited inline in `dvdifo.py` for sector/byte offsets.
- **mpucoder.com** — DVD-Video spec reference pages (e.g.
  `mpucoder.com/DVD/cell-pbi.html`, `/DVD/pgc.html`); cited inline.

### Codec / bitstream

- **FFmpeg source** — e.g. `libavcodec/dvdsubdec.c` (`parse_ifo_palette`),
  `ac3dec.c`, `dcadec.c`. Read it as a **reference for header/channel-layout
  parsing**; do NOT invoke the `ffmpeg`/`ffprobe` binary (see the hard
  constraint above — the source is fair game, the tool is not).

## Validation (lint & typecheck)

Run from the `mkvsmith/` directory before declaring a change done:

```sh
uv run ruff check                 # lint
uv run ruff format --check        # format check (use `uv run ruff format` to apply)
uv run ty check --config-file ty.toml   # type check — the gate
```

`ty` (Astral's type checker) is in the `dev` dependency group and is the
**primary type checker**. The strict-ish rule set lives in `ty.toml`
(`missing-type-argument`, `possibly-unresolved-reference`,
`unsound-return-statement`).

### ty 0.0.71 config-discovery bug (workaround required)

When ty *discovers* its config from the project root (a `ty.toml` found by
walking up the tree, or a `[tool.ty]` table in `pyproject.toml`), it silently
drops all diagnostics for `main.py`. The same config passed explicitly works
correctly, so always pass it explicitly:

```sh
uv run ty check --config-file ty.toml
```

`ty server` (the LSP) takes no flags, so the editor's built-in ty language
server hits the same bug — don't rely on it in Zed until this is fixed upstream.

### Pyright is a secondary cross-check, not the gate

`pyright` remains available via `uvx pyright` as a second opinion. It is
configured in `pyrightconfig.json` (`typeCheckingMode: strict`, with
`reportPrivateUsage` / `reportUnusedFunction` disabled because cross-module
`_`-prefixed imports are a deliberate convention here — the tests do the same).
Zed's default Python language server (basedpyright) reads that file, so the
editor currently shows pyright diagnostics, not ty's. To switch the editor to
ty instead, set
`{"languages": {"Python": {"language_servers": ["ty", "ruff"]}}}` in Zed's
settings — but see the LSP bug above.

Fix errors and warnings in code you added or changed. Do not fix unrelated
pre-existing errors elsewhere — flag them to the user instead. If a diagnostic
points at a real root-cause issue in your change, fix the root cause rather than
papering over it: don't blanket-ignore ty rules (prefer narrow, documented
suppressions) and don't add a broad `Any` just to pass the checker. Parser
output dicts should be typed with `TypedDict`s, not bare `dict`/`list` — see
`bluray.py`'s `MplsStreamInfo`/`MplsPlayItem` and `dvdifo.py`'s
`VmgInfo`/`CadtCell` as the pattern.

## Tests

Parser changes should be guarded by a fixture-based regression test capturing a
real binary blob (`.mpls` / `.clpi` / `.ifo` / `.vob`). The pytest harness is
configured in `pyproject.toml` (`[tool.pytest.ini_options]`,
`pythonpath = ["."]`, `testpaths = ["tests"]`); fixtures live in
`tests/fixtures/` and `conftest.py` at the project root provides a
`load_fixture` helper. Run tests from the `mkvsmith/` directory with
`uv run pytest`. See the `parser-regression-test` skill for the full procedure.
