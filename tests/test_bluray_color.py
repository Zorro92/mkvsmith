"""Unit tests for scan-time Blu-ray colour inference (bluray._set_video_color_from_info)."""

from typing import Any

from bluray import _set_video_color_from_info
from models import Stream


def _video(**kwargs: object) -> Stream:
    s = Stream(index=0, codec="hevc")
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


def _info(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def test_hd_1080_infers_bt709() -> None:
    s = _video()
    _set_video_color_from_info(s, _info(height=1080, codec="hevc"))
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt709",
        "bt709",
        "bt709",
    )
    assert s.color_range == "limited"


def test_uhd_without_dr_byte_defaults_to_sdr_bt709() -> None:
    s = _video()
    _set_video_color_from_info(s, _info(height=2160, codec="hevc"))
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt709",
        "bt709",
        "bt709",
    )


def test_uhd_sdr_defaults_to_bt709() -> None:
    s = _video()
    _set_video_color_from_info(
        s, _info(height=2160, codec="hevc", dynamic_range_type="SDR")
    )
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt709",
        "bt709",
        "bt709",
    )


def test_uhd_hdr10_gets_bt2020_pq() -> None:
    s = _video()
    _set_video_color_from_info(
        s, _info(height=2160, codec="hevc", dynamic_range_type="hdr10")
    )
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt2020",
        "smpte2084",
        "bt2020nc",
    )


def test_uhd_dolby_vision_gets_bt2020_pq() -> None:
    s = _video()
    _set_video_color_from_info(
        s, _info(height=2160, codec="hevc", dynamic_range_type="dolby_vision")
    )
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt2020",
        "smpte2084",
        "bt2020nc",
    )


def test_explicit_colorspace_bt2020_wins_over_missing_dr() -> None:
    s = _video()
    _set_video_color_from_info(s, _info(height=2160, codec="hevc", colorspace="bt2020"))
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt2020",
        "smpte2084",
        "bt2020nc",
    )


def test_explicit_colorspace_bt709_wins() -> None:
    s = _video()
    _set_video_color_from_info(s, _info(height=2160, codec="hevc", colorspace="bt709"))
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt709",
        "bt709",
        "bt709",
    )


def test_already_set_color_is_left_untouched() -> None:
    s = _video()
    s.color_primaries = "bt2020"
    s.color_transfer = "smpte2084"
    s.color_space = "bt2020nc"
    _set_video_color_from_info(s, _info(height=2160, dynamic_range_type="SDR"))
    assert (s.color_primaries, s.color_transfer, s.color_space) == (
        "bt2020",
        "smpte2084",
        "bt2020nc",
    )
