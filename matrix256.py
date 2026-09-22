"""Implementation of the matrix256v1 filesystem fingerprint."""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Iterable, Iterator
from pathlib import Path


def _utf8_path_bytes(path: str) -> bytes:
    cleaned = "".join(
        "\ufffd" if "\ud800" <= character <= "\udfff" else character
        for character in path
    )
    return cleaned.encode("utf-8")


def _normalized_path(root: Path, path: Path) -> str:
    return unicodedata.normalize("NFC", path.relative_to(root).as_posix())


def _scan_files(root: Path, current: Path) -> Iterator[tuple[str, int]]:
    with os.scandir(current) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _scan_files(root, Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            path = Path(entry.path)
            yield (
                _normalized_path(root, path),
                entry.stat(follow_symlinks=False).st_size,
            )


def fingerprint_entries(entries: Iterable[tuple[str, int]]) -> str:
    normalized = ((unicodedata.normalize("NFC", path), size) for path, size in entries)
    ordered = sorted(normalized, key=lambda entry: _utf8_path_bytes(entry[0]))
    digest = hashlib.sha256()
    for path, size in ordered:
        digest.update(_utf8_path_bytes(path))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def fingerprint(root: Path) -> str:
    return fingerprint_entries(_scan_files(root, root))
