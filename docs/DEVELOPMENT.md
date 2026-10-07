# Developing mkvsmith

## Running from a clone

```sh
git clone https://github.com/Zorro92/mkvsmith
cd mkvsmith
uv run mkvsmith            # or: uv run ./main.py
```

`uv run` installs the project into `.venv` in editable mode, so your edits
take effect straight away. The code is the `mkvsmith` package in
`src/mkvsmith/`. `main.py` is a small launcher with a `uv run --script`
shebang, so `./main.py` also works without setting anything up.

To try your working copy as an installed tool, the way users get it:

```sh
uv tool install --force .
```

## Checks

Run these from the repository root before sending a change:

```sh
uv run ruff check                       # lint
uv run ruff format --check              # formatting (`uv run ruff format` fixes it)
uv run ty check --config-file ty.toml   # type check
uv run pytest                           # tests
```

Pass `--config-file` to ty explicitly: when ty finds `ty.toml` on its own,
it silently skips `main.py`. [AGENTS.md](../AGENTS.md) covers the project's
design rules: mkvmerge as the only external media tool, and core logic kept
separate from the CLI and TUI.

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

