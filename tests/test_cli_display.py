"""Tests for detailed title display formatting."""

from __future__ import annotations

from pathlib import Path

import cli
import dvdifo
from models import Stream, StreamType, Title


def make_detail_title(tmp_path: Path) -> Title:
    title = Title(
        index=3,
        source_file=tmp_path / "movie.m2ts",
        name="Test Movie",
        duration_seconds=3661.0,
    )
    video = Stream(
        index=0,
        stream_type=StreamType.VIDEO,
        codec="h264",
        language="eng",
        width=1920,
        height=1080,
        is_default=True,
    )
    audio = Stream(
        index=1,
        stream_type=StreamType.AUDIO,
        codec="truehd",
        language="eng",
        channels=8,
        is_forced=True,
    )
    subtitle = Stream(
        index=2,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="fre",
        type_index=0,
        sub_id=0x21,
        is_hearing_impaired=True,
    )
    title.streams = [video, audio, subtitle]
    title.dvd_subp_attrs[0x21] = dvdifo._IFOSubpictureAttrs(
        coding_mode="run-length",
        code_extension=9,
        lang_code="fre",
        is_hearing_impaired=True,
    )
    return title


def test_stream_flag_formatting() -> None:
    unflagged = Stream(index=0)
    default = Stream(index=0, is_default=True)
    forced = Stream(index=0, is_forced=True)
    both = Stream(index=0, is_default=True, is_forced=True)

    assert cli._stream_flags(unflagged) == ""
    assert cli._stream_flags(default) == " [DEF]"
    assert cli._stream_flags(forced) == " [FOR]"
    assert cli._stream_flags(both) == " [DEF,FOR]"


def test_stream_lines_include_dimensions_channels_and_extensions() -> None:
    title = make_detail_title(Path("/tmp"))
    video, audio, subtitle = title.streams

    assert cli._video_stream_line(video) == (
        f"  {'H.264':<14} {'1920x1080':<10} English (eng) [DEF]"
    )
    assert cli._non_video_stream_line(title, audio) == (
        f"  {'a:0':<5} {'TrueHD':<14} {'8ch':<6} English (eng) [FOR]"
    )
    assert cli._subtitle_extension_info(title, subtitle) == " (forced)"
    assert cli._non_video_stream_line(title, subtitle) == (
        f"  {'s:0':<5} {'DVD Sub':<14} {'-':<6} French (fre) (forced)"
    )


def test_display_title_details_prints_groups_in_order(
    monkeypatch, tmp_path: Path, capsys
):
    monkeypatch.setattr(cli, "get_terminal_width", lambda: 80)
    title = make_detail_title(tmp_path)

    cli.display_title_details(title)

    output = capsys.readouterr().out
    assert "Title 3: Test Movie" in output
    assert "Source: movie.m2ts" in output
    assert "Duration: 01:01:01" in output
    assert output.index("[VIDEO]") < output.index("[AUDIO]")
    assert output.index("[AUDIO]") < output.index("[SUBS]")
    assert "1920x1080" in output
    assert "8ch" in output
    assert "(forced)" in output
