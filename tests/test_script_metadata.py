"""main.py's inline script metadata (PEP 723) must match pyproject.toml.

``./main.py`` and ``uv run main.py`` outside the project resolve only the
dependencies listed in main.py's header, so a dependency added to
pyproject.toml alone is missing there.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _script_metadata(text: str) -> dict[str, Any]:
    match = re.search(r"^# /// script$\n(.*?)^# ///$", text, re.M | re.S)
    assert match, "main.py has no '# /// script' block"
    body = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group(1).splitlines(keepends=True)
    )
    return tomllib.loads(body)


def test_script_dependencies_match_the_project() -> None:
    script = _script_metadata((ROOT / "main.py").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert sorted(script["dependencies"]) == sorted(project["dependencies"])
    assert script["requires-python"] == project["requires-python"]
