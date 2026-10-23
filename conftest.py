"""Shared pytest fixtures and helpers for mkvsmith parser tests.

Run pytest from the repository root (``uv run pytest``). The
``pythonpath = [".", "src"]`` setting in ``pyproject.toml`` makes this module
(``from conftest import load_fixture``) and the ``mkvsmith`` package in
``src/`` importable even when the project isn't installed.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _temp_files_in_tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Each test's temp files go in its own pytest folder.

    Scans and rips register temp files with the RuntimeState they were
    given, and a test's state is never cleaned up at exit like the
    process's; left in the system temp dir (often RAM-backed), they pile up.
    """
    saved = tempfile.tempdir
    tempfile.tempdir = str(tmp_path_factory.mktemp("tempfile"))
    try:
        yield
    finally:
        tempfile.tempdir = saved


@pytest.fixture(autouse=True)
def _no_update_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks GitHub for the latest version (updates.DISABLE_ENV)."""
    monkeypatch.setenv("MKVSMITH_NO_UPDATE_CHECK", "1")


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory holding captured real disc-structure blobs (.mpls/.clpi/.ifo/.vob)."""
    return FIXTURES_DIR


def load_fixture(name: str) -> bytes:
    """Read a fixture file from ``tests/fixtures/`` as raw bytes."""
    return (FIXTURES_DIR / name).read_bytes()
