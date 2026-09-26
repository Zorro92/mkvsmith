"""Tests for the overwrite confirmation on existing outputs.

create_mkv never silently clobbers an existing .mkv: unless --force is set,
the user must confirm (declining raises RipError, which batch flows report
through the usual channel).
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

import cli
import mkv
import models
from models import (
    Config,
    RipError,
    RuntimeState,
    Stream,
    StreamType,
    Title,
    UserPrompts,
)


def _title() -> Title:
    return Title(
        index=0,
        source_file=Path("movie.m2ts"),
        name="Movie",
        duration_seconds=100.0,
    )


def _streams() -> list[Stream]:
    return [Stream(index=0, stream_type=StreamType.VIDEO, codec="h264")]


def test_confirm_overwrite_accepts_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    for answer in ("y", "Y", "yes", " YES "):
        monkeypatch.setattr(builtins, "input", lambda _p: answer)
        assert mkv._confirm_overwrite(Path("out.mkv")) is True


def test_confirm_overwrite_declines_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for answer in ("n", "", "no"):
        monkeypatch.setattr(builtins, "input", lambda _p: answer)
        assert mkv._confirm_overwrite(Path("out.mkv")) is False


def test_confirm_overwrite_treats_closed_stdin_as_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", closed)
    assert mkv._confirm_overwrite(Path("out.mkv")) is False


def test_missing_output_needs_no_confirmation(tmp_path: Path) -> None:
    creator = mkv.MKVCreator(tmp_path, runtime_state=RuntimeState())
    # Would raise if input() were called (pytest closes stdin).
    creator._ensure_overwrite_allowed(tmp_path / "Movie_t00.mkv")


def test_force_overwrite_skips_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "Movie_t00.mkv"
    out.write_bytes(b"existing")
    config = Config(force_overwrite=True)
    creator = mkv.MKVCreator(tmp_path, config=config, runtime_state=RuntimeState())
    monkeypatch.setattr(
        builtins, "input", lambda _p: (_ for _ in ()).throw(AssertionError())
    )
    creator._ensure_overwrite_allowed(out)


def test_declined_overwrite_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "Movie_t00.mkv"
    out.write_bytes(b"existing")
    creator = mkv.MKVCreator(tmp_path, runtime_state=RuntimeState())
    monkeypatch.setattr(mkv, "_confirm_overwrite", lambda _p, *a, **k: False)

    with pytest.raises(RipError, match="not overwriting"):
        creator._ensure_overwrite_allowed(out)


def test_create_mkv_checks_before_any_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "Movie_t00.mkv"
    out.write_bytes(b"existing")
    creator = mkv.MKVCreator(tmp_path, runtime_state=RuntimeState())
    monkeypatch.setattr(mkv, "_confirm_overwrite", lambda _p, *a, **k: False)

    with pytest.raises(RipError, match="not overwriting"):
        creator.create_mkv(_title(), _streams())


def test_confirm_overwrite_uses_injected_prompts() -> None:
    assert (
        mkv._confirm_overwrite(Path("o.mkv"), UserPrompts(confirm=lambda _m: True))
        is True
    )
    assert (
        mkv._confirm_overwrite(Path("o.mkv"), UserPrompts(confirm=lambda _m: False))
        is False
    )


def test_injected_confirm_allows_overwrite_without_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A GUI-style injected hook answers without touching stdin."""
    out = tmp_path / "Movie_t00.mkv"
    out.write_bytes(b"existing")
    prompts = UserPrompts(confirm=lambda _message: True)
    creator = mkv.MKVCreator(tmp_path, runtime_state=RuntimeState(prompts=prompts))
    monkeypatch.setattr(
        builtins, "input", lambda _p: (_ for _ in ()).throw(AssertionError())
    )
    creator._ensure_overwrite_allowed(out)


def test_injected_confirm_decline_raises_without_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "Movie_t00.mkv"
    out.write_bytes(b"existing")
    prompts = UserPrompts(confirm=lambda _message: False)
    creator = mkv.MKVCreator(tmp_path, runtime_state=RuntimeState(prompts=prompts))
    monkeypatch.setattr(
        builtins, "input", lambda _p: (_ for _ in ()).throw(AssertionError())
    )
    with pytest.raises(RipError, match="not overwriting"):
        creator._ensure_overwrite_allowed(out)


def test_force_flag_reaches_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    runtime_state = RuntimeState()
    monkeypatch.setattr(
        sys, "argv", ["mkvsmith", str(tmp_path / "movie.iso"), "--force"]
    )
    cli._apply_parsed_args(cli._build_arg_parser().parse_args(), runtime_state)

    assert runtime_state.config.force_overwrite is True


def test_force_defaults_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys

    runtime_state = RuntimeState()
    monkeypatch.setattr(sys, "argv", ["mkvsmith", str(tmp_path / "movie.iso")])
    cli._apply_parsed_args(cli._build_arg_parser().parse_args(), runtime_state)

    assert runtime_state.config.force_overwrite is False
    assert models.Config().force_overwrite is False
