"""Unit tests for mux-time colour resolution (mkv._resolve_video_color)."""

from models import Stream
from mkv import _COLOR_CICP, _COLOR_RANGE, _resolve_video_color


def _video(height: int | None = None, **kwargs: object) -> Stream:
    s = Stream(index=0, codec="mpeg2video")
    s.height = height
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


def test_no_color_no_height_returns_none() -> None:
    assert _resolve_video_color(_video()) is None


def test_hd_1080_falls_back_to_bt709() -> None:
    assert _resolve_video_color(_video(height=1080)) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )


def test_hd_720_falls_back_to_bt709() -> None:
    assert _resolve_video_color(_video(height=720)) == ("bt709", "bt709", "bt709", "tv")


def test_uhd_2160_without_signalling_falls_back_to_sdr_bt709() -> None:
    # HDR is only inferred at scan time from the STN dynamic_range_type;
    # unmarked 2160p content defaults to SDR BT.709.
    assert _resolve_video_color(_video(height=2160)) == (
        "bt709",
        "bt709",
        "bt709",
        "tv",
    )


def test_sd_576_falls_back_to_pal_bt601() -> None:
    assert _resolve_video_color(_video(height=576)) == (
        "bt470bg",
        "bt709",
        "bt470bg",
        "tv",
    )


def test_sd_480_falls_back_to_ntsc_bt601() -> None:
    assert _resolve_video_color(_video(height=480)) == (
        "smpte170m",
        "bt709",
        "smpte170m",
        "tv",
    )


def test_explicit_values_are_respected() -> None:
    s = _video(height=1080)
    s.color_primaries = "bt709"
    s.color_transfer = "bt709"
    s.color_space = "bt709"
    s.color_range = "limited"
    assert _resolve_video_color(s) == ("bt709", "bt709", "bt709", "limited")


def test_explicit_hdr_values_are_passed_through() -> None:
    s = _video(height=2160)
    s.color_primaries = "bt2020"
    s.color_transfer = "smpte2084"
    s.color_space = "bt2020nc"
    s.color_range = "limited"
    assert _resolve_video_color(s) == ("bt2020", "smpte2084", "bt2020nc", "limited")


def test_partial_signalling_pads_with_unknown() -> None:
    s = _video(height=1080)
    s.color_primaries = "bt709"
    assert _resolve_video_color(s) == ("bt709", "unknown", "unknown", "tv")


def test_full_range_is_passed_through() -> None:
    s = _video(height=1080)
    s.color_range = "pc"
    assert _resolve_video_color(s) == ("bt709", "bt709", "bt709", "pc")


def test_every_resolvable_value_has_a_mkvmerge_code() -> None:
    """The muxer maps every string _resolve_video_color can return."""
    for value in (
        "unknown",
        "bt709",
        "bt470bg",
        "smpte170m",
        "bt2020",
        "bt2020nc",
        "smpte2084",
    ):
        assert value in _COLOR_CICP, value
    for value in ("tv", "limited", "pc", "full"):
        assert value in _COLOR_RANGE, value
