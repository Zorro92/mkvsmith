"""
Disc-reading helpers for mkvsmith.

Provides functions for detecting source types, listing and extracting files
from ISO images using 7z, direct loop-mount mounting via sudo, and other
low-level disc I/O.  Extracted / mounted resources are tracked in the global
``_TEMP_DIRS`` / ``_TEMP_FILES`` / ``_SYMLINK_CLEANUP`` /
``_DIRECT_MOUNT_CLEANUP`` lists from ``main`` so they are cleaned up on exit.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from enum import Enum
from pathlib import Path

from models import (
    CONFIG,
    _DIRECT_MOUNT_CLEANUP,
    _SYMLINK_CLEANUP,
    _TEMP_DIRS,
    _TEMP_FILES,
    log_debug,
    log_error,
    log_info,
    log_warn,
)
from i18n import tr


# =============================================================================
# Source type detection
# =============================================================================


class SourceType(Enum):
    DVD = "dvd"
    DVD_RAW = "dvd_raw"
    BLURAY = "bluray"
    BLURAY_RAW = "bluray_raw"
    VIDEO_FILE = "video_file"
    ISO_UNKNOWN = "iso_unknown"
    DEVICE = "device"
    UNKNOWN = "unknown"


def _probe_has_iso9660_pvd(iso_path: Path) -> bool:
    try:
        with open(iso_path, "rb") as f:
            f.seek(16 * 2048)
            ident = f.read(2048)[1:6]
            return ident == b"CD001"
    except Exception:
        return False


def _has_file_with_ext(root: Path, exts: tuple[str, ...]) -> bool:
    """Case-insensitive, short-circuiting search for any file with a given suffix."""
    lowered = {e.lower() for e in exts}
    for p in root.rglob("*"):
        if p.suffix.lower() in lowered:
            return True
    return False


def detect_source_type(s: Path) -> SourceType:
    if s.is_dir():
        if (s / "VIDEO_TS").is_dir():
            return SourceType.DVD
        if (s / "BDMV").is_dir() or (s / "bdmv").is_dir():
            return SourceType.BLURAY
        if _has_file_with_ext(s, (".m2ts",)):
            return SourceType.BLURAY_RAW
        if _has_file_with_ext(s, (".vob",)):
            return SourceType.DVD_RAW
        if _has_file_with_ext(s, (".iso",)):
            return SourceType.ISO_UNKNOWN
    if s.is_file():
        if s.suffix.lower() == ".iso":
            return SourceType.ISO_UNKNOWN
        if s.suffix.lower() in (
            ".m2ts",
            ".vob",
            ".mkv",
            ".mp4",
            ".avi",
            ".mov",
            ".wmv",
            ".ts",
        ):
            return SourceType.VIDEO_FILE
    if str(s).startswith("/dev/"):
        return SourceType.DEVICE
    return SourceType.UNKNOWN


# =============================================================================
# RAM-backed temp-dir budgeting
# -----------------------------------------------------------------------------
# The default system temp dir is often a tmpfs mount (RAM-backed) on Linux.
# Ripping a large title extracts its multi-GB raw streams there, which consumes
# real RAM — exhausting tmpfs can trigger the OOM killer or freeze the machine
# (a full tmpfs is a full memory, not just a full "disk").
#
# To stay safe we cap RAM-backed extraction at ``ram_limit`` of installed RAM
# (default 80%); any title expected to exceed that transparently spills to a
# disk-backed temp dir instead. Disk-backed temp dirs are left uncapped.
# =============================================================================


def _total_ram_bytes() -> int | None:
    """Total physical RAM in bytes, or ``None`` if it cannot be determined."""
    # Linux: /proc/meminfo is authoritative and dependency-free.
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f:
                if line.startswith(b"MemTotal:"):
                    return int(line.split()[1]) * 1024  # KiB -> bytes
    except Exception:
        pass
    # macOS / *BSD: POSIX sysconf.
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    # Windows.
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):  # type: ignore[type-arg]
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullTotalPhys)
    except Exception:
        return None


def _available_ram_bytes() -> int | None:
    """Currently-available RAM in bytes, or ``None`` if unknown.

    Used as an extra guard on top of the static total-RAM budget: even a
    title "under budget" can OOM the box if most RAM is already in use.
    """
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f:
                if line.startswith(b"MemAvailable:"):
                    return int(line.split()[1]) * 1024  # KiB -> bytes
    except Exception:
        pass
    return None


def _unescape_mount_point(token: str) -> str:
    """Decode ``\040``-style octal escapes used in /proc/mounts."""
    out: list[str] = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 3 < len(token) + 1 and token[i + 1 : i + 4].isdigit():
            try:
                out.append(chr(int(token[i + 1 : i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def _is_ram_backed_dir(path: Path) -> bool:
    """Return ``True`` if *path* lives on a RAM-backed filesystem.

    Resolves the path and walks ``/proc/mounts``, selecting the longest-prefix
    mount point (handles nested mounts such as ``/tmp`` tmpfs over ``/`` disk)
    and checking its filesystem type. Returns ``False`` when undeterminable
    (so disk-backed is the safe default).
    """
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except Exception:
        return False
    best_mp = ""
    best_fs = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mp = _unescape_mount_point(parts[1])
        fs = parts[2]
        is_prefix = (
            mp == "/" or resolved == mp or resolved.startswith(mp.rstrip("/") + "/")
        )
        if is_prefix and len(mp) > len(best_mp):
            best_mp, best_fs = mp, fs
    return best_fs in ("tmpfs", "ramfs")


def init_ram_budget() -> None:
    """Compute the RAM-backed temp-dir budget and store it on ``CONFIG``.

    Sets ``CONFIG.ram_budget_bytes`` to ``ram_limit`` * total RAM when the
    *effective* temp dir (``--temp-dir`` if given, else the system temp) is
    RAM-backed, otherwise leaves it ``None`` (no limit enforced).
    """
    CONFIG.ram_budget_bytes = None
    if CONFIG.ram_limit <= 0:
        return
    effective = CONFIG.temp_dir or Path(tempfile.gettempdir())
    if not _is_ram_backed_dir(effective):
        log_debug(f"Temp dir '{effective}' is disk-backed; no RAM budget enforced.")
        return
    total = _total_ram_bytes()
    if not total:
        log_warn(
            tr(
                "Temp dir '{dir}' is RAM-backed but installed RAM could not be "
                "detected; large rips may exhaust memory. Use --temp-dir to point "
                "at a disk-backed path.",
                dir=effective,
            )
        )
        return
    budget = int(total * CONFIG.ram_limit)
    CONFIG.ram_budget_bytes = budget
    log_info(
        tr(
            "Temp dir '{dir}' is RAM-backed; limiting extracts to {gb:.1f} GB "
            "({pct:.0%} of {total_gb:.1f} GB RAM). Oversized titles spill to disk.",
            dir=effective,
            gb=budget / 1e9,
            pct=CONFIG.ram_limit,
            total_gb=total / 1e9,
        )
    )


def _should_spill_to_disk(estimated_bytes: int) -> str | None:
    """Why an extraction of *estimated_bytes* must avoid the RAM temp dir.

    Returns a reason string (``"budget"`` or ``"available"``) when the
    extraction should spill to disk, or ``None`` when it's safe to use the
    RAM-backed temp dir.
    """
    budget = CONFIG.ram_budget_bytes
    if not budget:
        return None
    if estimated_bytes > budget:
        return "budget"
    # Second guard: even under the static budget, don't consume nearly all the
    # RAM that is actually free right now (other apps use memory too).
    avail = _available_ram_bytes()
    if avail and estimated_bytes > int(avail * 0.9):
        return "available"
    return None


def _disk_temp_base() -> Path:
    """A writable, disk-backed directory for oversized extractions.

    Considers the user's ``--temp-dir`` (if set and disk-backed), then the
    conventional disk-backed ``/var/tmp``, then the output directory and the
    home directory, preferring disk-backed candidates. Falls back to the system
    temp dir if nothing better is found.
    """
    candidates: list[Path] = []
    if CONFIG.temp_dir:
        candidates.append(CONFIG.temp_dir)
    candidates.append(Path("/var/tmp"))
    if CONFIG.output_dir:
        candidates.append(Path(CONFIG.output_dir))
    candidates.append(Path.home())
    for c in candidates:
        try:
            if c.exists() and os.access(c, os.W_OK) and not _is_ram_backed_dir(c):
                return c
        except Exception:
            continue
    return Path(tempfile.gettempdir())


def temp_base_for_title(estimated_bytes: int) -> Path | None:
    """Temp-file base dir for a title's extraction, or ``None`` for the default.

    Returns a disk-backed path when the title is expected to exceed the RAM
    budget (so it spills off tmpfs), otherwise ``None`` to use the normal
    (possibly RAM-backed) temp dir. Emits a single warning per spill.
    """
    reason = _should_spill_to_disk(estimated_bytes)
    if not reason:
        return None
    base = _disk_temp_base()
    budget = CONFIG.ram_budget_bytes or 0
    if reason == "budget":
        log_warn(
            tr(
                "Title estimated at {est:.1f} GB exceeds the RAM budget of {budget:.1f} GB; "
                "using disk-backed temp '{dir}' for this title.",
                est=estimated_bytes / 1e9,
                budget=budget / 1e9,
                dir=base,
            )
        )
    else:
        avail = _available_ram_bytes() or 0
        log_warn(
            tr(
                "Title estimated at {est:.1f} GB fits the RAM budget of {budget:.1f} GB "
                "but available memory is low ({avail:.1f} GB free); "
                "using disk-backed temp '{dir}' for this title.",
                est=estimated_bytes / 1e9,
                budget=budget / 1e9,
                avail=avail / 1e9,
                dir=base,
            )
        )
    return base


# =============================================================================
# Safe path handling for 7z
# =============================================================================


def _get_safe_7z_path(iso_path: Path) -> tuple[Path, Path | None]:
    """Return a safe 7z-compatible path for *iso_path*, creating a symlink
    if the filename contains characters that confuse 7z (e.g. spaces, parens).

    Returns ``(safe_path, symlink_or_None)``.  The second element is the
    symlink path when one was created, so callers can schedule cleanup.
    """
    safe_name = re.sub(r"[^\w\.\-]", "_", iso_path.name)
    if safe_name == iso_path.name:
        return iso_path, None
    safe_path = iso_path.parent / safe_name
    try:
        safe_path.symlink_to(iso_path.resolve())
        _SYMLINK_CLEANUP.append(safe_path)
        return safe_path, safe_path
    except Exception:
        return iso_path, None


# =============================================================================
# ISO listing
# =============================================================================


def _list_iso_files_7z(iso_path: Path) -> tuple[list[str], dict[str, int]]:
    """List the Blu-ray / DVD paths inside an ISO using ``7z l -slt``.

    Returns a ``(paths, sizes)`` pair. *paths* are the internal ISO paths
    (e.g. ``BDMV/STREAM/00001.m2ts``) matching known disc directory structures;
    *sizes* maps each of those paths to its (uncompressed) byte size, used to
    estimate extraction cost for RAM-budget spill decisions. Returns
    ``([], {})`` on failure.
    """
    target_path, _ = _get_safe_7z_path(iso_path)
    try:
        res = subprocess.run(
            ["7z", "l", "-slt", str(target_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode != 0:
            log_error(
                tr("7z failed: {err}", err=(res.stdout + " " + res.stderr).strip())
            )
            return [], {}
        paths: list[str] = []
        sizes: dict[str, int] = {}
        valid_prefixes = [
            "BDMV/STREAM/",
            "BDMV/PLAYLIST/",
            "BDMV/CLIPINF/",
            "VIDEO_TS/",
            "BDMV/META/",
        ]
        valid_exts = (".mpls", ".m2ts", ".vob", ".clpi", ".xml", ".ifo", ".bup")
        cur_path: str | None = None
        cur_ok = False
        # 7z -slt prints one block per entry; within a block "Path =" precedes
        # "Size =", so we track the current path and attach its size.
        for line in res.stdout.splitlines():
            if line.startswith("Path = "):
                p = line[7:].strip()
                if p.startswith("/"):
                    p = p[1:]
                cur_path = p
                cur_ok = any(
                    p.upper().startswith(pf) for pf in valid_prefixes
                ) and p.lower().endswith(valid_exts)
                if cur_ok:
                    paths.append(p)
            elif line.startswith("Size = ") and cur_path is not None and cur_ok:
                try:
                    sizes[cur_path] = int(line[7:].strip())
                except ValueError:
                    pass
        return paths, sizes
    except FileNotFoundError:
        log_error(tr("7z missing. Install with: sudo apt install p7zip-full"))
        return [], {}
    except Exception as e:
        log_error(tr("7z exception: {err}", err=e))
        return [], {}


# =============================================================================
# Extraction
# =============================================================================


def _extract_with_7z(
    iso_path: Path, internal_paths: list[str], out_dir: Path
) -> list[Path]:
    """Extract *internal_paths* from *iso_path* into *out_dir* using 7z.

    Returns the list of successfully-extracted files on disk.
    """
    if not internal_paths:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path, _ = _get_safe_7z_path(iso_path)
    res = subprocess.run(
        ["7z", "e", str(target_path), f"-o{out_dir}"] + internal_paths + ["-y"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if res.returncode != 0:
        log_error(
            tr(
                "7z extraction failed: {err}",
                err=(res.stdout + " " + res.stderr).strip(),
            )
        )
    return [
        out_dir / Path(p).name
        for p in internal_paths
        if (out_dir / Path(p).name).exists()
    ]


def _extract_partial_7z(
    iso_path: Path, internal_path: str, size_mb: int = 256
) -> Path | None:
    """Extract a prefix of a single file from an ISO via 7z pipe.

    Reads the first *size_mb* MiB of *internal_path* into a temp file and
    returns its path.  This is used for lightweight probing (e.g. reading
    the first sector of an M2TS for chapter data) without extracting the
    entire multi-GB stream.
    """
    tmp = Path(tempfile.NamedTemporaryFile(suffix=".tmp", delete=False).name)
    _TEMP_FILES.append(tmp)
    target_path, _ = _get_safe_7z_path(iso_path)
    try:
        proc = subprocess.Popen(
            ["7z", "e", "-so", str(target_path), internal_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = proc.stdout
        if stdout is None:
            proc.terminate()
            tmp.unlink(missing_ok=True)
            return None
        bytes_read, limit = 0, size_mb * 1024 * 1024
        with open(tmp, "wb") as f:
            while True:
                chunk = stdout.read(1024 * 1024)
                if not chunk:
                    break
                bytes_read += len(chunk)
                f.write(chunk)
                if bytes_read >= limit:
                    break
        proc.kill()
        proc.wait()
        return tmp
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


# =============================================================================
# Direct mounting via sudo
# =============================================================================


def _try_direct_mount(iso_path: Path) -> Path | None:
    """Attempt to mount *iso_path* via ``sudo mount -o loop,ro``.

    Returns the mount-point ``Path`` on success, ``None`` on failure or
    when disabled (``--no-sudo``).
    """
    if CONFIG.no_sudo:
        log_info(tr("Skipping direct mount (--no-sudo is set)"))
        return None
    try:
        ans = (
            input(
                tr(
                    "[INFO] Attempt to mount '{path}' via 'sudo mount -o loop,ro'? [y/N]:",
                    path=iso_path,
                )
                + " "
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        return None
    if ans not in ("y", "yes"):
        return None
    log_info(tr("Attempting direct mount via 'sudo mount -o loop,ro'..."))
    mnt = Path(tempfile.mkdtemp(prefix="mkv_mount_"))
    try:
        res = subprocess.run(
            ["sudo", "mount", "-o", "loop,ro", str(iso_path), str(mnt)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0:
            log_error(
                tr(
                    "mount failed (rc={rc}): {err}",
                    rc=res.returncode,
                    err=(res.stdout + res.stderr).strip(),
                )
            )
            try:
                mnt.rmdir()
            except Exception:
                pass
            return None
        _DIRECT_MOUNT_CLEANUP.append(mnt)
        return mnt
    except FileNotFoundError:
        log_error(tr("mount/sudo not found on PATH."))
        try:
            mnt.rmdir()
        except Exception:
            pass
        return None
    except Exception as e:
        log_error(tr("mount exception: {err}", err=e))
        try:
            mnt.rmdir()
        except Exception:
            pass
        return None


# =============================================================================
# Convenience: extract all files for muxing
# =============================================================================


def _extract_full_for_muxing(
    iso_path: Path,
    internals: Sequence[str],
    *,
    temp_base: Path | None = None,
) -> list[Path]:
    """Extract the full set of internal ISO files for muxing into a temp dir.

    Creates a temp directory (registered in ``_TEMP_DIRS``) and extracts
    *internals* into it via ``_extract_with_7z``. *temp_base*, when given,
    overrides the parent of the temp directory — used to spill oversized
    titles off a RAM-backed temp dir onto disk (see ``temp_base_for_title``).
    """
    out_dir = Path(
        tempfile.mkdtemp(
            prefix="mkv_mux_",
            dir=str(temp_base) if temp_base else None,
        )
    )
    _TEMP_DIRS.append(out_dir)
    return _extract_with_7z(iso_path, list(internals), out_dir)
