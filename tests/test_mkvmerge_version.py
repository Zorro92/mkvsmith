"""The mkvmerge version check and what an old mkvmerge changes."""

from __future__ import annotations

import pytest

from mkvsmith import cli, mkv, models, scan
from mkvsmith.bluray import MplsStreamInfo
from mkvsmith.models import Stream, StreamType


def test_version_line_is_parsed() -> None:
    parse = models._parse_mkvmerge_version
    assert parse("mkvmerge v96.0 ('It's My Life') 64-bit") == (96, 0)
    assert parse("mkvmerge v9.6.0 ('Slave To Love') 64-bit") == (9, 6)
    assert parse("not mkvmerge") is None


def test_an_unknown_version_counts_as_new_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "mkvmerge_version", lambda: None)
    assert models.mkvmerge_at_least((999, 0))
    monkeypatch.setattr(models, "mkvmerge_version", lambda: (53, 0))
    assert not models.mkvmerge_at_least(models.MKVMERGE_FLAGS_VERSION)
    assert models.mkvmerge_at_least((53, 0))


def _flagged_subtitle() -> Stream:
    return Stream(
        index=2,
        stream_type=StreamType.SUBTITLE,
        codec="dvd_subtitle",
        language="en",
        is_hearing_impaired=True,
        is_commentary=True,
    )


def test_old_mkvmerge_gets_no_accessibility_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before v54 mkvmerge rejects these options, failing the whole mux.
    monkeypatch.setattr(mkv, "mkvmerge_at_least", lambda version: False)
    cmd: list[str] = []

    mkv._append_track_state_options(cmd, 0, _flagged_subtitle())

    assert "--hearing-impaired-flag" not in cmd
    assert "--commentary-flag" not in cmd
    assert cmd[:2] == ["--language", "0:en"]


def test_current_mkvmerge_gets_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mkv, "mkvmerge_at_least", lambda version: True)
    cmd: list[str] = []

    mkv._append_track_state_options(cmd, 0, _flagged_subtitle())

    assert cmd[-4:] == [
        "--hearing-impaired-flag",
        "0:yes",
        "--commentary-flag",
        "0:yes",
    ]


@pytest.mark.parametrize(("version", "warned"), [((53, 4), True), ((54, 0), False)])
def test_startup_warns_about_an_old_mkvmerge(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int], warned: bool
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(cli, "mkvmerge_version", lambda: version)
    monkeypatch.setattr(cli, "log_warn", warnings.append)

    cli._warn_about_old_mkvmerge()

    assert bool(warnings) == warned
    if warned:
        assert warnings[0].startswith("mkvmerge v53.4 is older than v54")


def _dolby_vision_streams() -> list[MplsStreamInfo]:
    return [
        {"type": StreamType.VIDEO, "codec": "hevc", "lang": "und", "channels": None,
         "pid": 0x1011, "height": 2160,
         "dynamic_range_type": "hdr10", "colorspace": "bt2020"},
        {"type": StreamType.VIDEO, "codec": "hevc", "lang": "und", "channels": None,
         "pid": 0x1015, "height": 1080,
         "dynamic_range_type": "dolby_vision", "colorspace": "bt2020",
         "dolby_vision_layer": True},
    ]  # fmt: skip


@pytest.mark.parametrize(("new_enough", "warned"), [(False, True), (True, False)])
def test_dolby_vision_warns_once_on_an_old_mkvmerge(
    monkeypatch: pytest.MonkeyPatch, new_enough: bool, warned: bool
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(scan, "mkvmerge_at_least", lambda version: new_enough)
    monkeypatch.setattr(scan, "log_warn", warnings.append)
    scan._warn_dolby_vision_needs_newer_mkvmerge.cache_clear()

    scan._streams_from_mpls(_dolby_vision_streams())
    scan._streams_from_mpls(_dolby_vision_streams())  # a second playlist

    assert len(warnings) == (1 if warned else 0)
    scan._warn_dolby_vision_needs_newer_mkvmerge.cache_clear()
