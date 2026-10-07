#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich>=15.0.0",
#   "textual>=8.2.8",
#   "xdg-base-dirs>=6.0.3",
# ]
# ///
"""Run mkvsmith from a source checkout: ``uv run ./main.py`` or ``./main.py``.

Installed copies use the ``mkvsmith`` command instead; the code lives in
``src/mkvsmith``.
"""

# Licensed under GPL-3.0-or-later

from __future__ import annotations

import sys
from pathlib import Path

# As a standalone script (``./main.py``), the project isn't installed.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mkvsmith.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
