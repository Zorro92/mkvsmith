"""Throwaway helper: print parsed output of the captured fixtures.

Run from the project root: `uv run python scripts/inspect_fixtures.py`.
Used to determine the exact values the regression tests should assert.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from bluray import _parse_clpi, _parse_mpls
from dvdifo import (
    _parse_vmg_ifo,
    _parse_vts_ifo_languages,
    _parse_vts_pgc_info,
    _parse_vts_video_attrs,
    _parse_vts_audio_attrs,
)

FIX = Path("tests/fixtures")


def show_mpls(name: str) -> None:
    r = _parse_mpls(FIX / name)
    if r is None:
        print(f"== {name} -> None")
        return
    print(f"== {name} ==")
    print("  play_items:", len(r["play_items"]))
    print("  duration_s:", round(sum(p["duration"] for p in r["play_items"])))
    print("  chapters:", len(r["chapter_times"]))
    print("  first_chapters:", [round(t, 1) for t in r["chapter_times"][:6]])
    print("  streams:", len(r["streams"]))
    for s in r["streams"]:
        st = s["type"].value if s["type"] is not None else "?"
        print(f"    {st} codec={s['codec']} lang={s['lang']} pid={s.get('pid')}")
    print("  subpaths:", len(r.get("subpath_entries", [])))


def show_clpi(name: str) -> None:
    data = (FIX / name).read_bytes()
    r = _parse_clpi(data)
    print(f"== {name} ==")
    print("  pids:", sorted(r))
    for pid in sorted(r):
        info = r[pid]
        print(
            f"    pid={pid} codec={info.get('codec')} "
            f"coding_type={info.get('coding_type')} "
            f"channels={info.get('channels')} height={info.get('height')} "
            f"lang={info.get('language')}"
        )


show_mpls("00800.mpls")
show_clpi("00875.clpi")

print("== bdmt_eng.xml ==")
for elem in ET.parse(FIX / "bdmt_eng.xml").iter():
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
    if elem.text and elem.text.strip():
        print(f"  {tag} = {elem.text.strip()!r}")

print("== dvd_video_ts.ifo (VMG) ==")
vmg = _parse_vmg_ifo(FIX / "dvd_video_ts.ifo")
print("  disc_name:", vmg.get("disc_name"))
print("  barcode:", vmg.get("barcode"))
print("  provider_id:", vmg.get("provider_id"))
print("  title_map:", vmg.get("title_map"))

print("== dvd_vts_01_0.ifo (VTS) ==")
data = (FIX / "dvd_vts_01_0.ifo").read_bytes()
chapters, duration = _parse_vts_pgc_info(data)
print("  chapters:", len(chapters), "duration_s:", round(duration))
print("  first_chapters:", [round(t) for t in chapters[:8]])
audio_lang, sub_lang = _parse_vts_ifo_languages(data)
print("  audio_langs:", audio_lang)
print("  sub_langs:", sub_lang)
va = _parse_vts_video_attrs(data)
print("  video_attrs:", va)
for sid, attrs in sorted(_parse_vts_audio_attrs(data).items()):
    print(f"    audio 0x{sid:02x}: codec={attrs.codec} channels={attrs.channels}")
