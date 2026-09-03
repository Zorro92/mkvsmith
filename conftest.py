"""Shared pytest fixtures and helpers for mkvsmith parser tests.

Run pytest from the ``mkvsmith/`` directory (``uv run pytest``). The
``pythonpath = ["."]`` setting in ``pyproject.toml`` puts the project root on
``sys.path`` so sibling imports (``main``, ``dvdifo``, ``models``, ``i18n``)
resolve the same way they do when ``main.py`` runs as a script.
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
