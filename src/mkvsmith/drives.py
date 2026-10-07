"""Optical drive discovery: which drives exist, whether a disc is in, its label.

UI-agnostic (no printing or prompting): the TUI's source picker lists these,
and anything else can too. Linux reads sysfs and asks the drive for its
status; Windows asks the OS for CD/DVD drive letters. Other platforms list
nothing (pick the disc's folder or device path instead).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# linux/cdrom.h
_CDROM_DRIVE_STATUS = 0x5326
_CDROMEJECT = 0x5309
_CDSL_CURRENT = 0x7FFFFFFF
_CDS_DISC_OK = 4

# GetDriveTypeW
_DRIVE_CDROM = 5


@dataclass(frozen=True)
class OpticalDrive:
    path: Path
    # Vendor and model when known, else the device name.
    name: str
    # False when the drive reports no disc (or an open tray); None if unknown.
    has_disc: bool | None


def list_optical_drives() -> list[OpticalDrive]:
    if sys.platform.startswith("linux"):
        return _linux_drives(Path("/sys/block"), Path("/dev"))
    if sys.platform == "win32":
        return _windows_drives()
    return []


def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _linux_drives(sysfs: Path, dev: Path) -> list[OpticalDrive]:
    try:
        entries = sorted(sysfs.glob("sr*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        return []
    drives: list[OpticalDrive] = []
    for entry in entries:
        device = entry / "device"
        name = " ".join(
            part
            for part in (
                _read_sysfs(device / "vendor"),
                _read_sysfs(device / "model"),
            )
            if part
        )
        path = dev / entry.name
        drives.append(OpticalDrive(path, name or entry.name, _linux_has_disc(path)))
    return drives


def _linux_has_disc(path: Path) -> bool | None:
    # A sys.platform test type checkers understand: os.O_NONBLOCK and
    # fcntl.ioctl don't exist on Windows.
    if sys.platform == "win32":
        return None
    try:
        import fcntl

        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (ImportError, OSError):
        return None
    try:
        return fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, _CDSL_CURRENT) == _CDS_DISC_OK
    except OSError:
        return None
    finally:
        os.close(fd)


def _windows_drives() -> list[OpticalDrive]:
    import ctypes

    # getattr: ctypes.windll only exists on Windows.
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return []
    kernel32 = windll.kernel32
    mask: int = kernel32.GetLogicalDrives()
    drives: list[OpticalDrive] = []
    for bit in range(26):
        if not mask & (1 << bit):
            continue
        root = f"{chr(ord('A') + bit)}:\\"
        if kernel32.GetDriveTypeW(root) != _DRIVE_CDROM:
            continue
        label = ctypes.create_unicode_buffer(261)
        has_disc = bool(
            kernel32.GetVolumeInformationW(
                root, label, len(label), None, None, None, None, 0
            )
        )
        drives.append(OpticalDrive(Path(root[:2]), root[:2], has_disc))
    return drives


def disc_label(path: Path) -> str | None:
    """The volume label of the disc in *path* (or of an image), or None.

    Reads the disc's file system, so it can take a few seconds on a drive
    that is spinning up; call it off the UI thread.
    """
    from mkvsmith.isofs import IsoImage, IsoImageError

    try:
        with IsoImage(path) as image:
            return image.label or None
    except (IsoImageError, OSError):
        return None


def can_eject() -> bool:
    return sys.platform.startswith("linux")


def eject(path: Path) -> None:
    """Open the tray of drive *path* (Linux). Raises OSError on failure."""
    if sys.platform == "win32" or not can_eject():
        raise OSError("ejecting is not supported on this platform")
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, _CDROMEJECT)
    finally:
        os.close(fd)
