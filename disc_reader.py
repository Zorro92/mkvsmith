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
import subprocess
import tempfile
from collections.abc import Sequence
from enum import Enum
from pathlib import Path

from models import (
    Config,
    RUNTIME_STATE,
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
    if _has_file_with_ext(source, (".m2ts",)):
        return SourceType.BLURAY_RAW
    if _has_file_with_ext(source, (".vob",)):
        return SourceType.DVD_RAW
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


def detect_source_type(source: Path) -> SourceType:
    directory_type = _directory_source_type(source)
    if directory_type is not None:
        return directory_type

    file_type = _file_source_type(source)
    if file_type is not None:
        return file_type

    if str(source).startswith("/dev/"):
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
    except (OSError, ValueError, IndexError):
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

    Sets ``ram_budget_bytes`` to ``ram_limit`` * total RAM when the
    *effective* temp dir (``--temp-dir`` if given, else the system temp) is
    RAM-backed, otherwise leaves it ``None`` (no limit enforced).
    """
    effective_config = config or RUNTIME_STATE.config
    effective_config.ram_budget_bytes = None
    if effective_config.ram_limit <= 0:
        return
    effective = effective_config.temp_dir or Path(tempfile.gettempdir())
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
    budget = int(total * effective_config.ram_limit)
    effective_config.ram_budget_bytes = budget
    log_info(
        tr(
            "Temp dir '{dir}' is RAM-backed; limiting extracts to {gb:.1f} GB "
            "({pct:.0%} of {total_gb:.1f} GB RAM). Oversized titles spill to disk.",
            dir=effective,
            gb=budget / 1e9,
            pct=effective_config.ram_limit,
            total_gb=total / 1e9,
        )
    )


def _should_spill_to_disk(
    estimated_bytes: int, config: Config | None = None
) -> str | None:
    """Why an extraction of *estimated_bytes* must avoid the RAM temp dir.

    Returns a reason string (``"budget"`` or ``"available"``) when the
    extraction should spill to disk, or ``None`` when it's safe to use the
    RAM-backed temp dir.
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
    return Path(tempfile.gettempdir())


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

    Returns ``(safe_path, symlink_or_None)``.  The second element is the
    symlink path when one was created, so callers can schedule cleanup.
    """
    safe_name = re.sub(r"[^\w\.\-]", "_", iso_path.name)
    if safe_name == iso_path.name:
        return iso_path, None
    safe_path = iso_path.parent / safe_name
    try:
        safe_path.symlink_to(iso_path.resolve())
        if symlinks is None:
            RUNTIME_STATE.cleanup.register_symlink(safe_path)
        else:
            symlinks.append(safe_path)
        return safe_path, safe_path
    except OSError:
        return iso_path, None


# =============================================================================
# ISO listing
# =============================================================================


_ISO_MEDIA_PREFIXES = (
    "BDMV/STREAM/",
    "BDMV/PLAYLIST/",
    "BDMV/CLIPINF/",
    "VIDEO_TS/",
    "BDMV/META/",
)

_ISO_MEDIA_EXTENSIONS = (
    ".mpls",
    ".m2ts",
    ".vob",
    ".clpi",
    ".xml",
    ".ifo",
    ".bup",
)


def _run_7z_listing(target_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["7z", "l", "-slt", str(target_path)],
        capture_output=True,
        text=True,
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
    current_path_is_media = False

    # 7z -slt prints one block per entry; "Path =" precedes "Size =".
    for line in stdout.splitlines():
        if line.startswith("Path = "):
            internal_path = line[7:].strip()
            if internal_path.startswith("/"):
                internal_path = internal_path[1:]
            current_path = internal_path
            current_path_is_media = _is_iso_media_path(internal_path)
            if current_path_is_media:
                paths.append(internal_path)
        elif (
            line.startswith("Size = ")
            and current_path is not None
            and current_path_is_media
        ):
            try:
                sizes[current_path] = int(line[7:].strip())
            except ValueError:
                pass

    return paths, sizes


def _list_iso_files_7z(
    iso_path: Path, symlinks: list[Path] | None = None
) -> tuple[list[str], dict[str, int]]:
    """List the Blu-ray / DVD paths inside an ISO using ``7z l -slt``.

    Returns a ``(paths, sizes)`` pair. *paths* are internal ISO media paths and
    *sizes* maps each path to its uncompressed byte size for RAM-budget
    estimates. Returns ``([], {})`` on failure.
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


def _confirm_direct_mount(iso_path: Path) -> bool:
    try:
        answer = (
            input(
                tr(
                    "[INFO] Attempt to mount '{path}' via "
                    "'sudo mount -o loop,ro'? [y/N]:",
                    path=iso_path,
                )
                + " "
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _run_direct_mount(
    iso_path: Path, mountpoint: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "mount", "-o", "loop,ro", str(iso_path), str(mountpoint)],
        capture_output=True,
        text=True,
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
) -> Path | None:
    """Attempt to mount *iso_path* via ``sudo mount -o loop,ro``.

    Returns the mount-point ``Path`` on success, ``None`` on failure or
    when disabled (``--no-sudo``).
    """
    if (config or RUNTIME_STATE.config).no_sudo:
        log_info(tr("Skipping direct mount (--no-sudo is set)"))
        return None
    if not _confirm_direct_mount(iso_path):
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
