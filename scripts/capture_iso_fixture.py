"""Capture a sparse sector fixture of a disc image for tests/test_isofs.py.

Records every 2048-byte sector the ``isofs`` readers touch while listing the
image (UDF and, when present, ISO9660) and while reading the given members,
then writes those sectors plus the expected listing to a gzipped fixture. No
other sectors are kept, so the fixture holds filesystem structures and the
named small files only -- no audio or video essence.

    uv run python scripts/capture_iso_fixture.py SRC.iso OUT.isofix.gz \
        MEMBER [MEMBER ...]

Fixture layout (inside gzip): a JSON header line, then records of
``<u32 sector LE><2048 bytes>``. Each MEMBER is read in full unless it is
larger than 64 KiB, in which case only its first 4 KiB is captured.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import BinaryIO

from mkvsmith import isofs

_PREFIX_ONLY_ABOVE = 64 * 1024
_PREFIX = 4096


class _RecordingFile:
    def __init__(self, inner: BinaryIO) -> None:
        self._inner = inner
        self.sectors: set[int] = set()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._inner.seek(offset, whence)

    def tell(self) -> int:
        return self._inner.tell()

    def read(self, size: int = -1) -> bytes:
        start = self._inner.tell()
        data = self._inner.read(size)
        if data:
            first = start // isofs.SECTOR
            last = (start + len(data) - 1) // isofs.SECTOR
            self.sectors.update(range(first, last + 1))
        return data


def _listing(entries: list[isofs.IsoEntry]) -> list[list[object]]:
    return [
        [e.path, e.size, e.modified.isoformat() if e.modified else None]
        for e in entries
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture a sparse sector fixture of a disc image."
    )
    parser.add_argument("src", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("members", nargs="+")
    args = parser.parse_args()
    out_path: Path = args.out

    with args.src.open("rb") as raw:
        rec = _RecordingFile(raw)
        header: dict[str, object] = {"size": args.src.stat().st_size}
        udf = isofs._UdfReader(rec)
        header["udf"] = {"label": udf.label, "files": _listing(udf.files())}
        try:
            iso = isofs._Iso9660Reader(rec)
            header["iso9660"] = {"label": iso.label, "files": _listing(iso.files())}
        except isofs.IsoImageError:
            header["iso9660"] = None

        with isofs.IsoImage(args.src) as image:
            entries = image.entries
            content: dict[str, list[object]] = {}
            for member in args.members:
                entry = entries[member]
                length = entry.size if entry.size <= _PREFIX_ONLY_ABOVE else _PREFIX
                # Re-read through the recorder so these sectors are captured.
                pos = 0
                for run in entry.runs:
                    if pos >= length:
                        break
                    if run.offset is not None:
                        rec.seek(run.offset)
                        rec.read(min(run.length, length - pos))
                    pos += run.length
                data = image.read(entry, 0, length)
                content[member] = [length, hashlib.sha256(data).hexdigest()]
            header["content"] = content

    with gzip.GzipFile(out_path, "wb", compresslevel=9) as out:
        out.write(json.dumps(header, separators=(",", ":")).encode() + b"\n")
        with args.src.open("rb") as raw:
            for sector in sorted(rec.sectors):
                raw.seek(sector * isofs.SECTOR)
                out.write(sector.to_bytes(4, "little") + raw.read(isofs.SECTOR))
    print(f"{out_path}: {len(rec.sectors)} sectors, {out_path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
