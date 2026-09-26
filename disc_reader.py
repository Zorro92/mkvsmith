"""
Disc-reading helpers for mkvsmith.

Provides functions for detecting source types, listing and extracting files
from ISO images using 7z, direct loop-mount mounting via sudo, and other
low-level disc I/O.  Extracted / mounted resources are tracked in the global
runtime cleanup registries. Callers may inject registry lists; standalone
calls fall back to the process-wide ``RUNTIME_STATE``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from models import (
    Config,
    RUNTIME_STATE,
    UserPrompts,
    log_debug,
    log_error,
    log_info,
    log_warn,
)
from i18n import tr


# Loop-mounting an ISO with sudo is a Linux-only fallback; macOS mounts
# images via hdiutil and Windows has no sudo/loop mount at all.
_IS_LINUX = sys.platform.startswith("linux")


# =============================================================================
# Source type detection
# =============================================================================


class SourceType(Enum):
    DVD = "dvd"
    DVD_RAW = "dvd_raw"
    BLURAY = "bluray"
    BLURAY_RAW = "bluray_raw"
    HDDVD = "hddvd"
    HDDVD_RAW = "hddvd_raw"
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
    except OSError:
        return False


def _has_file_with_ext(root: Path, exts: tuple[str, ...]) -> bool:
    """Case-insensitive, short-circuiting search for any file with a given suffix."""
    lowered = {e.lower() for e in exts}
    for p in root.rglob("*"):
        if p.suffix.lower() in lowered:
            return True
    return False


_VIDEO_FILE_EXTENSIONS = (
    ".m2ts",
    ".vob",
    ".evo",
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
)


def _directory_source_type(source: Path) -> SourceType | None:
    if not source.is_dir():
        return None
    if (source / "VIDEO_TS").is_dir():
        return SourceType.DVD
    if (source / "BDMV").is_dir() or (source / "bdmv").is_dir():
        return SourceType.BLURAY
    if (source / "HVDVD_TS").is_dir():
        return SourceType.HDDVD
    if _has_file_with_ext(source, (".m2ts",)):
        return SourceType.BLURAY_RAW
    if _has_file_with_ext(source, (".vob",)):
        return SourceType.DVD_RAW
    if _has_file_with_ext(source, (".evo",)):
        return SourceType.HDDVD_RAW
    if _has_file_with_ext(source, (".iso",)):
        return SourceType.ISO_UNKNOWN
    return None


def _file_source_type(source: Path) -> SourceType | None:
    if not source.is_file():
        return None
    if source.suffix.lower() == ".iso":
        return SourceType.ISO_UNKNOWN
    if source.suffix.lower() in _VIDEO_FILE_EXTENSIONS:
        return SourceType.VIDEO_FILE
    return None


def _is_device_path(source: Path) -> bool:
    """True for optical-device style sources on any supported platform.

    Linux and macOS expose block devices as ``/dev/...`` nodes (e.g.
    ``/dev/sr0``, ``/dev/disk4``); Windows exposes drives as bare
    ``E:`` / ``E:\\`` style paths. Browsable Windows/macOS discs are usually
    detected earlier as DVD/Blu-ray directories, so this only needs to catch
    the device-node cases.
    """
    text = str(source)
    if text.startswith("/dev/"):
        return True
    return bool(re.fullmatch(r"[A-Za-z]:[\\\\/]?", text))


def detect_source_type(source: Path) -> SourceType:
    directory_type = _directory_source_type(source)
    if directory_type is not None:
        return directory_type

    file_type = _file_source_type(source)
    if file_type is not None:
        return file_type

    if _is_device_path(source):
        return SourceType.DEVICE
    return SourceType.UNKNOWN


# =============================================================================
# Temp-dir selection and RAM-backed budgeting
# -----------------------------------------------------------------------------
# Temp files default to the conventional disk-backed ``/var/tmp`` (which by
# definition survives reboots, so it is never tmpfs) instead of the system
# temp dir, which on Linux is often a RAM-backed tmpfs (``/tmp``). Ripping a
# large title extracts its multi-GB raw streams into temp, which on tmpfs
# consumes real RAM — exhausting tmpfs can trigger the OOM killer or freeze
# the machine (a full tmpfs is a full memory, not just a full "disk").
#
# When the effective temp dir *is* RAM-backed (explicit ``--temp-dir``/``TMPDIR``
# pointing at tmpfs, or no usable ``/var/tmp``), we cap extraction at
# ``ram_limit`` of the constraining capacity — total RAM vs. the tmpfs size,
# whichever is smaller (a tmpfs is frequently capped at a fraction of RAM, so
# RAM alone is not a safe basis) — and any title expected to exceed that, or
# to exceed currently-free RAM / tmpfs space, transparently spills to a
# disk-backed temp dir instead. Disk-backed temp dirs are left uncapped.
# =============================================================================


def _is_usable_dir(path: Path) -> bool:
    """True when *path* exists and is writable (never raises)."""
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False


def default_temp_dir() -> Path:
    """Default base dir for temp files.

    Prefers an explicitly exported ``TMPDIR``, then the conventional
    disk-backed ``/var/tmp``, falling back to the system temp dir. ``/var/tmp``
    is skipped when it is missing, unwritable, or itself RAM-backed, so the
    default never silently lands on tmpfs.
    """
    env = os.environ.get("TMPDIR")
    if env and _is_usable_dir(Path(env)):
        return Path(env)
    var_tmp = Path("/var/tmp")
    if _is_usable_dir(var_tmp) and not _is_ram_backed_dir(var_tmp):
        return var_tmp
    return Path(tempfile.gettempdir())


def _fs_sizes(path: Path) -> tuple[int, int] | None:
    """``(total, free)`` bytes of the filesystem holding *path*, or ``None``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.total, usage.free


def _total_ram_bytes() -> int | None:
    """Total physical RAM in bytes, or ``None`` if it cannot be determined."""
    # Linux: /proc/meminfo is authoritative and dependency-free.
    try:
        with open("/proc/meminfo", "rb") as f:
            for line in f:
                if line.startswith(b"MemTotal:"):
                    return int(line.split()[1]) * 1024  # KiB -> bytes
    except (OSError, ValueError, IndexError):
        pass
    # macOS / *BSD: POSIX sysconf. os.sysconf is absent on Windows; resolve
    # it through getattr so the code stays valid on every platform's types.
    sysconf: Callable[[str], int] | None = getattr(os, "sysconf", None)
    if sysconf is not None:
        try:
            return sysconf("SC_PAGE_SIZE") * sysconf("SC_PHYS_PAGES")
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
    except OSError:
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
    except (OSError, ValueError, IndexError):
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
    except OSError:
        resolved = str(path)
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
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


def init_ram_budget(config: Config | None = None) -> None:
    """Compute the RAM-backed temp-dir budget on the supplied config.

    Sets ``ram_budget_bytes`` to ``ram_limit`` * the constraining capacity
    (total RAM vs. the tmpfs size, whichever is smaller) when the *effective*
    temp dir (``--temp-dir`` if given, else the default temp dir) is
    RAM-backed, otherwise leaves it ``None`` (no limit enforced).
    """
    effective_config = config or RUNTIME_STATE.config
    effective_config.ram_budget_bytes = None
    if effective_config.ram_limit <= 0:
        return
    effective = effective_config.temp_dir or default_temp_dir()
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
    sizes = _fs_sizes(effective)
    if sizes is not None and sizes[0] < total:
        # The tmpfs is capped below total RAM (commonly at a fraction of it),
        # so its size — not RAM — is the binding constraint.
        basis = sizes[0]
        basis_kind = "tmpfs"
    else:
        basis = total
        basis_kind = "RAM"
    budget = int(basis * effective_config.ram_limit)
    effective_config.ram_budget_bytes = budget
    log_info(
        tr(
            "Temp dir '{dir}' is RAM-backed; limiting extracts to {gb:.1f} GB "
            "({pct:.0%} of {total_gb:.1f} GB {kind}). Oversized titles spill to disk.",
            dir=effective,
            gb=budget / 1e9,
            pct=effective_config.ram_limit,
            total_gb=basis / 1e9,
            kind=basis_kind,
        )
    )


def _should_spill_to_disk(
    estimated_bytes: int, config: Config | None = None
) -> str | None:
    """Why an extraction of *estimated_bytes* must avoid the RAM temp dir.

    Returns a reason string (``"budget"``, ``"available"``, or ``"space"``)
    when the extraction should spill to disk, or ``None`` when it's safe to
    use the RAM-backed temp dir.
    """
    effective_config = config or RUNTIME_STATE.config
    budget = effective_config.ram_budget_bytes
    if not budget:
        return None
    if estimated_bytes > budget:
        return "budget"
    # Second guard: even under the static budget, don't consume nearly all the
    # RAM that is actually free right now (other apps use memory too).
    avail = _available_ram_bytes()
    if avail and estimated_bytes > int(avail * 0.9):
        return "available"
    # Third guard: the tmpfs is shared (e.g. other users of /tmp), so even an
    # extraction under budget must spill when the filesystem itself is nearly
    # full right now.
    effective = effective_config.temp_dir or default_temp_dir()
    sizes = _fs_sizes(effective)
    if sizes is not None and estimated_bytes > int(sizes[1] * 0.9):
        return "space"
    return None


def _disk_temp_base(config: Config | None = None) -> Path:
    """A writable, disk-backed directory for oversized extractions.

    Considers the user's ``--temp-dir`` (if set and disk-backed), then the
    conventional disk-backed ``/var/tmp``, then the output directory and the
    home directory, preferring disk-backed candidates. Falls back to the system
    temp dir if nothing better is found.
    """
    effective_config = config or RUNTIME_STATE.config
    candidates: list[Path] = []
    if effective_config.temp_dir:
        candidates.append(effective_config.temp_dir)
    candidates.append(Path("/var/tmp"))
    if effective_config.output_dir:
        candidates.append(Path(effective_config.output_dir))
    candidates.append(Path.home())
    for c in candidates:
        try:
            if c.exists() and os.access(c, os.W_OK) and not _is_ram_backed_dir(c):
                return c
        except OSError:
            continue
    return default_temp_dir()


def temp_base_for_title(
    estimated_bytes: int, config: Config | None = None
) -> Path | None:
    """Temp-file base dir for a title's extraction, or ``None`` for the default.

    Returns a disk-backed path when the title is expected to exceed the RAM
    budget (so it spills off tmpfs), otherwise ``None`` to use the normal
    (possibly RAM-backed) temp dir. Emits a single warning per spill.
    """
    effective_config = config or RUNTIME_STATE.config
    reason = _should_spill_to_disk(estimated_bytes, effective_config)
    if not reason:
        return None
    base = _disk_temp_base(effective_config)
    budget = effective_config.ram_budget_bytes or 0
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
    elif reason == "space":
        effective = effective_config.temp_dir or default_temp_dir()
        free = _fs_sizes(effective)
        log_warn(
            tr(
                "Title estimated at {est:.1f} GB fits the RAM budget of {budget:.1f} GB "
                "but the temp filesystem is low on space ({avail:.1f} GB free); "
                "using disk-backed temp '{dir}' for this title.",
                est=estimated_bytes / 1e9,
                budget=budget / 1e9,
                avail=(free[1] if free is not None else 0) / 1e9,
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


def _get_safe_7z_path(
    iso_path: Path, symlinks: list[Path] | None = None
) -> tuple[Path, Path | None]:
    """Return a safe 7z-compatible path for *iso_path*, creating a symlink
    if the filename contains characters that confuse 7z (e.g. spaces, parens).

    The link lives in a private per-process temp dir, never next to the ISO
    itself: a stray symlink in a media folder confuses folder watchers (media
    servers, sync tools) and can look broken from containers or over network
    filesystems.

    Returns ``(safe_path, symlink_or_None)``.  The second element is the
    symlink path when one was created, so callers can schedule cleanup.
    """
    safe_name = re.sub(r"[^\w\.\-]", "_", iso_path.name)
    if safe_name == iso_path.name:
        return iso_path, None
    base = _safe_link_dir()
    if base is None:
        return iso_path, None
    try:
        target = iso_path.resolve()
    except OSError:
        return iso_path, None
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    for attempt in range(100):
        candidate = base / (safe_name if attempt == 0 else f"{stem}_{attempt}{suffix}")
        try:
            if candidate.is_symlink() and candidate.resolve() == target:
                _register_safe_link(candidate, symlinks)
                return candidate, candidate
            candidate.symlink_to(target)
            _register_safe_link(candidate, symlinks)
            return candidate, candidate
        except FileExistsError:
            continue
        except OSError:
            return iso_path, None
    return iso_path, None


def _register_safe_link(link: Path, symlinks: list[Path] | None) -> None:
    if symlinks is None:
        RUNTIME_STATE.cleanup.register_symlink(link)
    else:
        symlinks.append(link)


_SAFE_LINK_DIR: Path | None = None


def _safe_link_dir() -> Path | None:
    """Private per-process temp dir for 7z-safe symlinks (never raises)."""
    global _SAFE_LINK_DIR
    if _SAFE_LINK_DIR is None:
        try:
            _SAFE_LINK_DIR = Path(tempfile.mkdtemp(prefix="mkv_safepath_"))
            RUNTIME_STATE.cleanup.register_temp_dir(_SAFE_LINK_DIR)
        except OSError:
            return None
    return _SAFE_LINK_DIR


# =============================================================================
# ISO listing
# =============================================================================


_ISO_MEDIA_PREFIXES = (
    "BDMV/STREAM/",
    "BDMV/PLAYLIST/",
    "BDMV/CLIPINF/",
    "VIDEO_TS/",
    "BDMV/META/",
    "HVDVD_TS/",
    "ADV_OBJ/",
)

_ISO_MEDIA_EXTENSIONS = (
    ".mpls",
    ".m2ts",
    ".vob",
    ".evo",
    ".xpl",
    ".map",
    ".clpi",
    ".xml",
    ".ifo",
    ".bup",
    ".bdmv",
)


def _run_7z_listing(target_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["7z", "l", "-slt", str(target_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _is_iso_media_path(internal_path: str) -> bool:
    return internal_path.upper().startswith(_ISO_MEDIA_PREFIXES) and (
        internal_path.lower().endswith(_ISO_MEDIA_EXTENSIONS)
    )


def _parse_7z_listing(stdout: str) -> tuple[list[str], dict[str, int]]:
    paths: list[str] = []
    sizes: dict[str, int] = {}
    current_path: str | None = None
    current_path_is_file = False

    # 7z -slt prints one block per entry; Path precedes Folder and Size.
    for line in stdout.splitlines():
        if line.startswith("Path = "):
            internal_path = line[7:].strip()
            if internal_path.startswith("/"):
                internal_path = internal_path[1:]
            current_path = internal_path
            current_path_is_file = False
        elif line.startswith("Folder = "):
            current_path_is_file = line[9:].strip() == "-"
            if current_path_is_file and current_path is not None:
                paths.append(current_path)
        elif (
            line.startswith("Size = ")
            and current_path is not None
            and current_path_is_file
        ):
            try:
                sizes[current_path] = int(line[7:].strip())
            except ValueError:
                pass

    return paths, sizes


@dataclass(frozen=True)
class _IsoFileMetadata:
    path: str
    size: int
    modified: str | None = None


def _parse_7z_metadata_listing(
    stdout: str,
) -> list[_IsoFileMetadata]:
    """Parse path, size, and 7z local Modified timestamps from ``l -slt``."""
    entries: list[_IsoFileMetadata] = []
    current_path: str | None = None
    current_size = 0
    current_modified: str | None = None
    current_is_file = False

    def finish_entry() -> None:
        nonlocal current_path, current_size, current_modified, current_is_file
        if current_is_file and current_path is not None:
            entries.append(
                _IsoFileMetadata(current_path, current_size, current_modified)
            )
        current_path = None
        current_size = 0
        current_modified = None
        current_is_file = False

    for line in stdout.splitlines():
        if line.startswith("Path = "):
            finish_entry()
            path = line[7:].strip()
            current_path = path[1:] if path.startswith("/") else path
        elif line.startswith("Folder = "):
            current_is_file = line[9:].strip() == "-"
        elif line.startswith("Size = ") and current_path is not None:
            try:
                current_size = int(line[7:].strip())
            except ValueError:
                current_size = 0
        elif line.startswith("Modified = ") and current_path is not None:
            current_modified = line[11:].strip() or None
    finish_entry()
    return entries


def _list_iso_file_metadata_7z(
    iso_path: Path, symlinks: list[Path] | None = None
) -> list[_IsoFileMetadata]:
    """List ISO files with sizes and 7z-reported local modification timestamps."""
    target_path, _ = _get_safe_7z_path(iso_path, symlinks)
    try:
        result = _run_7z_listing(target_path)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        log_debug(f"7z metadata listing failed: {exc}")
        return []
    if result.returncode != 0:
        log_debug(f"7z metadata listing failed: {result.stderr.strip()}")
        return []
    return _parse_7z_metadata_listing(result.stdout)


def _list_iso_files_7z(
    iso_path: Path, symlinks: list[Path] | None = None
) -> tuple[list[str], dict[str, int]]:
    """List the regular files inside an ISO using ``7z l -slt``.

    Returns a ``(paths, sizes)`` pair. *paths* are all internal ISO file paths
    and *sizes* maps each path to its uncompressed byte size. The complete
    listing supports matrix256 fingerprinting; callers select media paths.
    Returns ``([], {})`` on failure.
    """
    target_path, _ = _get_safe_7z_path(iso_path, symlinks)
    try:
        result = _run_7z_listing(target_path)
    except FileNotFoundError:
        log_error(tr("7z missing. Install with: sudo apt install p7zip-full"))
        return [], {}
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        log_error(tr("7z exception: {err}", err=exc))
        return [], {}

    if result.returncode != 0:
        log_error(
            tr(
                "7z failed: {err}",
                err=(result.stdout + " " + result.stderr).strip(),
            )
        )
        return [], {}
    return _parse_7z_listing(result.stdout)


# =============================================================================
# Extraction
# =============================================================================


def _extract_with_7z(
    iso_path: Path,
    internal_paths: list[str],
    out_dir: Path,
    symlinks: list[Path] | None = None,
) -> list[Path]:
    """Extract *internal_paths* from *iso_path* into *out_dir* using 7z.

    Returns the list of successfully-extracted files on disk.
    """
    if not internal_paths:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path, _ = _get_safe_7z_path(iso_path, symlinks)
    res = subprocess.run(
        ["7z", "e", str(target_path), f"-o{out_dir}"] + internal_paths + ["-y"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def _registered_temp_file(temp_files: list[Path] | None) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as temp_handle:
        temp_path = Path(temp_handle.name)
    if temp_files is None:
        RUNTIME_STATE.cleanup.register_temp_file(temp_path)
    else:
        temp_files.append(temp_path)
    return temp_path


def _start_7z_pipe(target_path: Path, internal_path: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["7z", "e", "-so", str(target_path), internal_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _stop_7z_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait()
    except OSError:
        pass


def _copy_bounded_stdout(stdout, output_file, limit: int) -> None:
    bytes_read = 0
    while True:
        chunk = stdout.read(1024 * 1024)
        if not chunk:
            break
        bytes_read += len(chunk)
        output_file.write(chunk)
        if bytes_read >= limit:
            break


def _extract_partial_7z(
    iso_path: Path,
    internal_path: str,
    size_mb: int = 256,
    *,
    temp_files: list[Path] | None = None,
    symlinks: list[Path] | None = None,
) -> Path | None:
    """Extract a bounded prefix from one ISO member using a 7z pipe."""
    target_path, _ = _get_safe_7z_path(iso_path, symlinks)
    temp_path = _registered_temp_file(temp_files)
    try:
        process = _start_7z_pipe(target_path, internal_path)
        try:
            stdout = process.stdout
            if stdout is None:
                _stop_7z_process(process)
                temp_path.unlink(missing_ok=True)
                return None
            with temp_path.open("wb") as output_file:
                _copy_bounded_stdout(stdout, output_file, size_mb * 1024 * 1024)
        except BaseException:
            _stop_7z_process(process)
            raise
        _stop_7z_process(process)
        return temp_path
    except (OSError, subprocess.SubprocessError):
        temp_path.unlink(missing_ok=True)
        return None


# =============================================================================
# Direct mounting via sudo
# =============================================================================


def _confirm_direct_mount(iso_path: Path, prompts: UserPrompts | None = None) -> bool:
    hooks = prompts or RUNTIME_STATE.prompts
    return hooks.confirm(
        tr(
            "[INFO] Attempt to mount '{path}' via 'sudo mount -o loop,ro'? [y/N]:",
            path=iso_path,
        )
    )


def _run_direct_mount(
    iso_path: Path, mountpoint: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "mount", "-o", "loop,ro", str(iso_path), str(mountpoint)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _register_direct_mount(mountpoint: Path, direct_mounts: list[Path] | None) -> None:
    if direct_mounts is None:
        RUNTIME_STATE.cleanup.register_direct_mount(mountpoint)
    else:
        direct_mounts.append(mountpoint)


def _remove_empty_mountpoint(mountpoint: Path) -> None:
    try:
        mountpoint.rmdir()
    except OSError:
        pass


def _try_direct_mount(
    iso_path: Path,
    config: Config | None = None,
    *,
    direct_mounts: list[Path] | None = None,
    prompts: UserPrompts | None = None,
) -> Path | None:
    """Attempt to mount *iso_path* via ``sudo mount -o loop,ro``.

    Returns the mount-point ``Path`` on success, ``None`` on failure or
    when disabled (``--no-sudo``).
    """
    if (config or RUNTIME_STATE.config).no_sudo:
        log_info(tr("Skipping direct mount (--no-sudo is set)"))
        return None
    if not _IS_LINUX:
        # 7z has already handled the ISO upstream, so skip instead of
        # prompting for a sudo command that cannot work on this platform.
        log_debug("Skipping direct mount (Linux-only fallback)")
        return None
    if not _confirm_direct_mount(iso_path, prompts):
        return None

    log_info(tr("Attempting direct mount via 'sudo mount -o loop,ro'..."))
    mountpoint = Path(tempfile.mkdtemp(prefix="mkv_mount_"))
    try:
        result = _run_direct_mount(iso_path, mountpoint)
    except FileNotFoundError:
        log_error(tr("mount/sudo not found on PATH."))
        _remove_empty_mountpoint(mountpoint)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        log_error(tr("mount exception: {err}", err=exc))
        _remove_empty_mountpoint(mountpoint)
        return None

    if result.returncode != 0:
        log_error(
            tr(
                "mount failed (rc={rc}): {err}",
                rc=result.returncode,
                err=(result.stdout + result.stderr).strip(),
            )
        )
        _remove_empty_mountpoint(mountpoint)
        return None

    _register_direct_mount(mountpoint, direct_mounts)
    return mountpoint


# =============================================================================
# Convenience: extract all files for muxing
# =============================================================================


def _extract_full_for_muxing(
    iso_path: Path,
    internals: Sequence[str],
    *,
    temp_base: Path | None = None,
    temp_dirs: list[Path] | None = None,
    symlinks: list[Path] | None = None,
) -> list[Path]:
    """Extract the full set of internal ISO files for muxing into a temp dir.

    Creates a temp directory (registered in the supplied registry) and extracts
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
    if temp_dirs is None:
        RUNTIME_STATE.cleanup.register_temp_dir(out_dir)
    else:
        temp_dirs.append(out_dir)
    return _extract_with_7z(iso_path, list(internals), out_dir, symlinks)
