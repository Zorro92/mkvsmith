"""Extract files from a disc image with mkvsmith's built-in ISO reader.

Members are written flattened into OUT_DIR under their basenames (like
``7z e``). Handy for capturing test fixtures without any external tool:

    uv run python scripts/iso_extract.py disc.iso tests/fixtures \\
        BDMV/PLAYLIST/00800.mpls BDMV/CLIPINF/00800.clpi

With no MEMBER arguments, lists the image's files and sizes instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mkvsmith import isofs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract files from a disc image (UDF / ISO9660)."
    )
    parser.add_argument("iso", type=Path)
    parser.add_argument("out_dir", type=Path, nargs="?")
    parser.add_argument("members", nargs="*")
    args = parser.parse_args()
    iso: Path = args.iso
    out_dir: Path | None = args.out_dir
    members: list[str] = args.members

    with isofs.IsoImage(iso) as image:
        if out_dir is None or not members:
            for entry in image.files():
                print(f"{entry.size:>14}  {entry.path}")
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        missing = [m for m in members if m not in image.entries]
        for member in members:
            entry = image.entries.get(member)
            if entry is None:
                continue
            dest = out_dir / Path(member).name
            image.copy_to(entry, dest)
            print(f"{dest} ({entry.size} bytes)")
    for member in missing:
        print(f"not found: {member}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
