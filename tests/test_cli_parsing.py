"""Tests for CLI argument parsing and action selection."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

import cli
import models


@pytest.fixture
def preserved_cli_state():
    runtime_state = models.RUNTIME_STATE
    config_state = copy.deepcopy(runtime_state.config.__dict__)
    tag_state = copy.deepcopy(runtime_state.tag_options.__dict__)
    yield
    runtime_state.config.__dict__.clear()
    runtime_state.config.__dict__.update(config_state)
    runtime_state.tag_options.__dict__.clear()
    runtime_state.tag_options.__dict__.update(tag_state)


@pytest.mark.usefixtures("preserved_cli_state")
def test_parse_args_preserves_source_and_selects_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "movie.iso"
    output = tmp_path / "rips"
    monkeypatch.setattr(
        sys,
        "argv",
        ["mkvsmith", str(source), str(output), "--info", "--no-forced"],
    )

    assert cli.parse_args() == (source, "info", None, None, None)
    assert models.RUNTIME_STATE.config.output_dir == output
    assert models.RUNTIME_STATE.config.include_forced is False


@pytest.mark.usefixtures("preserved_cli_state")
def test_parse_args_parses_multi_edition_indices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "movie.iso"
    monkeypatch.setattr(
        sys,
        "argv",
        ["mkvsmith", str(source), "--debug", "--multi-edition", "1,2"],
    )

    assert cli.parse_args() == (
        source,
        "rip_multi_edition",
        None,
        None,
        [1, 2],
    )
