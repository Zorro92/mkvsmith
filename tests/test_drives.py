"""Optical drive discovery (drives.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

import drives
import isofs


def _fake_drive(sysfs: Path, name: str, vendor: str, model: str) -> None:
    device = sysfs / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n")
    (device / "model").write_text(model + "   \n")


def test_linux_drives_read_names_from_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysfs = tmp_path / "block"
    _fake_drive(sysfs, "sr10", "HL-DT-ST", "BD-RE WH16NS60")
    _fake_drive(sysfs, "sr0", "ASUS", "DRW-24D5MT")
    (sysfs / "sda").mkdir()
    (sysfs / "sr1" / "device").mkdir(parents=True)
    statuses = {"sr0": True, "sr1": False, "sr10": None}
    monkeypatch.setattr(drives, "_linux_has_disc", lambda path: statuses[path.name])

    found = drives._linux_drives(sysfs, Path("/dev"))

    assert found == [
        drives.OpticalDrive(Path("/dev/sr0"), "ASUS DRW-24D5MT", True),
        drives.OpticalDrive(Path("/dev/sr1"), "sr1", False),
        drives.OpticalDrive(Path("/dev/sr10"), "HL-DT-ST BD-RE WH16NS60", None),
    ]


def test_linux_has_disc_is_unknown_for_a_missing_device(tmp_path: Path) -> None:
    assert drives._linux_has_disc(tmp_path / "sr0") is None


def test_disc_label_reads_the_volume_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeImage:
        label = "CATS_DONT_DANCE"

        def __init__(self, path: Path) -> None:
            assert path == tmp_path / "disc.iso"

        def __enter__(self) -> FakeImage:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(isofs, "IsoImage", FakeImage)

    assert drives.disc_label(tmp_path / "disc.iso") == "CATS_DONT_DANCE"


def test_disc_label_is_none_for_an_unreadable_disc(tmp_path: Path) -> None:
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\0" * 4096)

    assert drives.disc_label(junk) is None
    assert drives.disc_label(tmp_path / "missing.iso") is None
