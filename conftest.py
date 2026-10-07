"""Shared pytest fixtures and helpers for mkvsmith parser tests.

Run pytest from the repository root (``uv run pytest``). The
``pythonpath = [".", "src"]`` setting in ``pyproject.toml`` makes this module
(``from conftest import load_fixture``) and the ``mkvsmith`` package in
``src/`` importable even when the project isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory holding captured real disc-structure blobs (.mpls/.clpi/.ifo/.vob)."""
    return FIXTURES_DIR


def load_fixture(name: str) -> bytes:
    """Read a fixture file from ``tests/fixtures/`` as raw bytes."""
    return (FIXTURES_DIR / name).read_bytes()
