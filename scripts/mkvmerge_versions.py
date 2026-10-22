"""Maintenance helper: run the tests against other mkvmerge versions.

Linux x86_64 only. Run from the project root:

    uv run python scripts/mkvmerge_versions.py 81 96 latest [--all-tests]

For each version, downloads MKVToolNix's AppImage from
https://mkvtoolnix.download/appimage/ (cached in
~/.cache/mkvsmith/mkvtoolnix, or --cache), unpacks it without FUSE
(``--appimage-extract``), puts wrappers for its mkvmerge, mkvpropedit,
mkvextract and mkvinfo first on PATH, and runs
tests/test_mkvmerge_contract.py (the whole suite with --all-tests).
Versions are given as 81, 81.0 or 54.0.0; ``latest`` is the newest
AppImage. Prints a pass/fail line per version and exits non-zero if any
failed. See docs/formats/mkvmerge.md.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

APPIMAGES = "https://mkvtoolnix.download/appimage/"
_ROOT = Path(__file__).resolve().parent.parent
_NAME = re.compile(r"MKVToolNix_GUI-([0-9.]+)-x86_64\.AppImage(?=\")")
_TOOLS = ("mkvmerge", "mkvpropedit", "mkvextract", "mkvinfo")


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _available() -> list[str]:
    with urllib.request.urlopen(APPIMAGES, timeout=30) as response:
        page = response.read()
    assert isinstance(page, bytes)
    return sorted(set(_NAME.findall(page.decode("utf-8"))), key=_key)


def _resolve(wanted: str, available: list[str]) -> str:
    if wanted == "latest":
        return available[-1]
    # 81 and 81.0 both name 81.0; 54 and 54.0 name 54.0.0.
    target = _key(wanted)
    for version in available:
        key = _key(version)
        if key[: len(target)] == target and not any(key[len(target) :]):
            return version
    sys.exit(f"no AppImage for mkvtoolnix {wanted} at {APPIMAGES}")


def _install(version: str, cache: Path) -> Path:
    """The unpacked version's wrapper directory, downloading it if needed."""
    home = cache / version
    bin_dir = home / "bin"
    if (bin_dir / "mkvmerge").exists():
        return bin_dir
    # A fresh directory per download: the image is run only to unpack it.
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    image = home / "mkvtoolnix.AppImage"
    url = f"{APPIMAGES}MKVToolNix_GUI-{version}-x86_64.AppImage"
    print(f"downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, image.open("wb") as f:
        shutil.copyfileobj(response, f)
    image.chmod(0o755)
    subprocess.run(
        [str(image), "--appimage-extract"],
        cwd=home,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    image.unlink()
    usr = home / "squashfs-root" / "usr"
    bin_dir.mkdir()
    for tool in _TOOLS:
        wrapper = bin_dir / tool
        wrapper.write_text(
            f'#!/bin/sh\nLD_LIBRARY_PATH="{usr / "lib"}" exec "{usr / "bin" / tool}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return bin_dir


def _run_tests(bin_dir: Path, all_tests: bool) -> bool:
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    version = subprocess.run(
        ["mkvmerge", "--version"], env=env, capture_output=True, text=True, check=False
    )
    print(version.stdout.strip() or version.stderr.strip(), flush=True)
    target = [] if all_tests else ["tests/test_mkvmerge_contract.py"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *target],
        cwd=_ROOT,
        env=env,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", nargs="+", help="e.g. 81 96 latest")
    parser.add_argument(
        "--all-tests",
        action="store_true",
        help="run the whole suite, not only the contract tests",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path.home() / ".cache" / "mkvsmith" / "mkvtoolnix",
        help="where unpacked versions are kept",
    )
    args = parser.parse_args()
    if sys.platform != "linux":
        sys.exit("AppImages run on Linux only")

    available = _available()
    results: list[tuple[str, bool]] = []
    for wanted in args.versions:
        version = _resolve(wanted, available)
        print(f"\n== mkvtoolnix {version}", flush=True)
        results.append(
            (version, _run_tests(_install(version, args.cache), args.all_tests))
        )
    print()
    for version, passed in results:
        print(f"{version:>8}  {'passed' if passed else 'FAILED'}")
    sys.exit(0 if all(passed for _version, passed in results) else 1)


if __name__ == "__main__":
    main()
