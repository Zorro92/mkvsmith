"""MPLS STN stream_entry parsing: SubPath streams, IG, and entry ordering.

Synthetic tables cover every stream_entry type and category layout from
libbluray's ``_parse_stn`` / ``_parse_stream``; the disc fixtures pin the
real cases that exposed the bugs (skipped when not captured locally):

- ``sgtfrog_s1d1_00000.mpls``: Sgt. Frog S1 D1 episode playlist, whose IG
  (menu) stream is a type-2 SubPath entry that used to be listed as a third
  PGS subtitle with PID 0x0000.
- ``sgtfrog_s7_00001.mpls``: first PlayItem video-only, second with audio.
- ``00307.mpls``: Monsters University playlist whose audio lives entirely in
  SubPath clips.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import bluray, mkv
from mkvsmith.bluray import (
    MplsStreamInfo,
    _merge_clpi_into_mpls,
    _parse_mpls,
    _parse_stn_table_languages,
    _parse_stn_table_streams,
    _parse_subpath_entries,
)
from mkvsmith.cli import _non_video_stream_line
from mkvsmith.models import Config, Stream, StreamType, Title

_FIXTURES = Path(__file__).parent / "fixtures"


def _needs(*names: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        not all((_FIXTURES / name).exists() for name in names),
        reason="disc fixtures not present; capture them locally (see docs/DEVELOPMENT.md)",
    )


# --- synthetic STN builders ----------------------------------------------------


def _entry(stream_type: int, pid: int, subpath: int = 0, subclip: int = 0) -> bytes:
    if stream_type == 1:
        body = bytes([1, pid >> 8, pid & 0xFF])
    elif stream_type == 2:
        body = bytes([2, subpath, subclip, pid >> 8, pid & 0xFF])
    else:
        body = bytes([stream_type, subpath, pid >> 8, pid & 0xFF])
    body = body.ljust(9, b"\x00")
    return bytes([len(body)]) + body


def _attr(coding: int, lang: str = "eng") -> bytes:
    if coding in (0x90, 0x91):
        body = bytes([coding]) + lang.encode() + b"\x00"
    elif coding in (0x1B, 0x02, 0xEA, 0x24):
        body = bytes([coding, 0x61, 0, 0, 0])
    else:
        body = bytes([coding, 0x61]) + lang.encode()
    return bytes([len(body)]) + body


def _refs(*refs: int) -> bytes:
    """Secondary-stream reference list: count, reserved, refs, pad to 16 bits."""
    return bytes([len(refs), 0, *refs]) + (b"\x00" if len(refs) % 2 else b"")


def _stn(
    video: list[bytes] | None = None,
    audio: list[bytes] | None = None,
    pg: list[bytes] | None = None,
    ig: list[bytes] | None = None,
    sec_audio: list[bytes] | None = None,
    pip_pg: list[bytes] | None = None,
) -> bytes:
    groups = [video or [], audio or [], pg or [], ig or [], sec_audio or []]
    counts = bytes(
        [len(groups[0]), len(groups[1]), len(groups[2]), len(groups[3])]
        + [len(groups[4]), 0, len(pip_pg or []), 0]
    )
    # Order per libbluray: video, audio, PG + PiP PG, IG, secondary audio.
    body = b"".join(groups[0] + groups[1] + groups[2] + (pip_pg or []) + groups[3])
    body += b"".join(groups[4])
    table = b"\x00\x00" + counts + b"\x00" * 4 + body
    return len(table).to_bytes(2, "big") + table


VIDEO = _entry(1, 0x1011) + _attr(0x1B)


# --- stream_entry types ------------------------------------------------------------


def test_subpath_entry_pids_read_at_their_type_offsets() -> None:
    stn = _stn(
        video=[VIDEO],
        audio=[
            _entry(1, 0x1100) + _attr(0x81),
            _entry(2, 0x1100, subpath=3, subclip=1) + _attr(0x81, "fra"),
            _entry(3, 0x1101, subpath=4) + _attr(0x84, "spa"),
        ],
        pg=[_entry(4, 0x1200, subpath=5) + _attr(0x90)],
    )

    streams = _parse_stn_table_streams(stn)

    assert [(s["pid"], s.get("subpath_id"), s["lang"]) for s in streams] == [
        (0x1011, None, "und"),
        (0x1100, None, "eng"),
        (0x1100, 3, "fra"),
        (0x1101, 4, "spa"),
        (0x1200, 5, "eng"),
    ]


def test_ig_streams_are_not_listed() -> None:
    stn = _stn(
        video=[VIDEO],
        pg=[_entry(1, 0x1200) + _attr(0x90)],
        ig=[_entry(2, 0x1400) + _attr(0x91)],
    )

    streams = _parse_stn_table_streams(stn)

    assert [s["pid"] for s in streams] == [0x1011, 0x1200]


def test_pip_pg_follows_primary_pg_and_precedes_ig() -> None:
    stn = _stn(
        video=[VIDEO],
        pg=[_entry(1, 0x1200) + _attr(0x90)],
        pip_pg=[_entry(1, 0x1A00) + _attr(0x90, "jpn")],
        ig=[_entry(1, 0x1400) + _attr(0x91)],
    )

    streams = _parse_stn_table_streams(stn)

    assert [(s["pid"], s["lang"]) for s in streams] == [
        (0x1011, "und"),
        (0x1200, "eng"),
        (0x1A00, "jpn"),
    ]


def test_secondary_audio_reference_lists_are_skipped() -> None:
    # Two secondary audio entries, each followed by a primary-audio ref list
    # (odd length -> padded). Misreading the list shifts the second entry.
    stn = _stn(
        video=[VIDEO],
        sec_audio=[
            _entry(1, 0x1A00) + _attr(0x81, "eng") + _refs(0),
            _entry(1, 0x1A01) + _attr(0x81, "fra") + _refs(0, 1),
        ],
    )

    streams = _parse_stn_table_streams(stn)

    assert [(s["pid"], s["lang"]) for s in streams] == [
        (0x1011, "und"),
        (0x1A00, "eng"),
        (0x1A01, "fra"),
    ]


def test_stn_languages_skip_subpath_entries() -> None:
    # The fallback language lists align with the main clip's tracks by
    # position, so SubPath entries must not occupy a slot.
    stn = _stn(
        video=[VIDEO],
        audio=[
            _entry(2, 0x1100, subpath=0) + _attr(0x81, "fra"),
            _entry(1, 0x1100) + _attr(0x81, "eng"),
        ],
        pg=[
            _entry(3, 0x1200, subpath=1) + _attr(0x90, "spa"),
            _entry(1, 0x1200) + _attr(0x90),
        ],
    )

    assert _parse_stn_table_languages(stn) == (["eng"], ["eng"])


def test_clpi_attributes_not_merged_into_subpath_streams() -> None:
    streams: list[MplsStreamInfo] = [
        {
            "type": StreamType.AUDIO,
            "codec": "ac3",
            "lang": "eng",
            "channels": None,
            "pid": 0x1100,
        },
        {
            "type": StreamType.AUDIO,
            "codec": "ac3",
            "lang": "eng",
            "channels": None,
            "pid": 0x1100,
            "subpath_id": 0,
        },
    ]
    clpi = {0x1100: {"codec": "truehd", "channels": 8}}

    _merge_clpi_into_mpls(streams, clpi)

    assert (streams[0]["codec"], streams[0]["channels"]) == ("truehd", 8)
    assert (streams[1]["codec"], streams[1]["channels"]) == ("ac3", None)


# --- muxing / selection / display ------------------------------------------------


def _title_with_subpath_audio() -> Title:
    title = Title(0, Path("/disc/00000.m2ts"), "T", 60.0)
    title.streams = [
        Stream(0, StreamType.VIDEO, "h264", "und", pid=0x1011),
        Stream(1, StreamType.AUDIO, "ac3", "eng", type_index=0, pid=0x1100),
        Stream(
            2, StreamType.AUDIO, "ac3", "eng", type_index=1, pid=0x1100, subpath_id=0
        ),
    ]
    return title


def test_default_selection_skips_subpath_streams() -> None:
    title = _title_with_subpath_audio()
    selected = mkv.select_streams(title, config=Config())
    assert [s.index for s in selected] == [0, 1]


def test_subpath_stream_is_never_matched_to_a_main_clip_track() -> None:
    title = _title_with_subpath_audio()
    ident = [
        {"id": 0, "type": "video", "properties": {"number": 0x1011}},
        {"id": 1, "type": "audio", "properties": {"number": 0x1100}},
    ]

    mapped = mkv._map_streams_to_ident_tracks(title.streams, ident)

    by_index = {entry["stream"].index: entry["input_id"] for entry in mapped}
    assert by_index == {0: 0, 1: 1, 2: -1}


def test_subpath_stream_display_notes_it_is_not_muxed() -> None:
    title = _title_with_subpath_audio()
    assert "not muxed" not in _non_video_stream_line(title, title.streams[1])
    assert "(sub-path, not muxed)" in _non_video_stream_line(title, title.streams[2])


# --- real playlists ----------------------------------------------------------------


@_needs("sgtfrog_s1d1_00000.mpls")
def test_sgt_frog_ig_is_not_listed_as_a_subtitle() -> None:
    info = _parse_mpls(_FIXTURES / "sgtfrog_s1d1_00000.mpls")
    assert info is not None
    subs = [s for s in info["streams"] if s["type"] == StreamType.SUBTITLE]
    assert [(s["pid"], s.get("subpath_id")) for s in subs] == [
        (0x1200, None),
        (0x1201, None),
    ]
    assert [s["pid"] for s in info["streams"] if s["type"] == StreamType.AUDIO] == [
        0x1100,
        0x1101,
    ]


@_needs("00307.mpls")
def test_monsters_university_subpath_audio() -> None:
    info = _parse_mpls(_FIXTURES / "00307.mpls")
    assert info is not None
    audio = [s for s in info["streams"] if s["type"] == StreamType.AUDIO]
    assert [(s["pid"], s.get("subpath_id")) for s in audio] == [
        (0x1100, n) for n in range(7)
    ]


@_needs("sgtfrog_s7_00001.mpls", "sgtfrog_s1d1_00000.mpls")
def test_differing_playitem_streams_are_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []

    def record(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(bluray, "log_debug", record)

    info = _parse_mpls(_FIXTURES / "sgtfrog_s7_00001.mpls")
    assert info is not None
    assert [s["type"] for s in info["streams"]] == [StreamType.VIDEO]
    assert any(
        "stream tables differ from the first at item(s) 1" in m for m in messages
    )

    messages.clear()
    _parse_mpls(_FIXTURES / "sgtfrog_s1d1_00000.mpls")
    assert not any("stream tables differ" in m for m in messages)


# --- SubPath entries -----------------------------------------------------------------


def _sub_play_item(
    clip: str,
    in_time: int = 524280,
    out_time: int = 3226980,
    connection_condition: int = 1,
    extra_clips: list[str] | None = None,
) -> bytes:
    """One SubPlayItem record (libbluray ``_parse_subplayitem`` layout)."""
    bitfield = bytes(
        [0x00, 0x00, 0x00, (connection_condition << 1) | (1 if extra_clips else 0)]
    )
    body = (
        clip.encode()
        + b"M2TS"
        + bitfield
        + b"\x00"
        + in_time.to_bytes(4, "big")
        + out_time.to_bytes(4, "big")
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    if extra_clips:
        body += bytes([len(extra_clips) + 1]) + b"".join(
            c.encode() + b"M2TS\x00" for c in extra_clips
        )
    return len(body).to_bytes(2, "big") + body


def _subpath(
    sp_type: int,
    items: list[bytes],
    is_repeat: bool = False,
) -> bytes:
    header = bytes([0x00, sp_type, 0x00, 0x01 if is_repeat else 0x00, 0x00, len(items)])
    return (len(header + b"".join(items))).to_bytes(4, "big") + header + b"".join(items)


def test_subpath_entries_parse_type_clips_and_timing() -> None:
    data = _subpath(2, [_sub_play_item("00819")], is_repeat=True) + _subpath(
        4, [_sub_play_item("00830", connection_condition=5)]
    )

    entries, pos = _parse_subpath_entries(data, 0, 2)

    assert pos == len(data)
    assert [(e["type"], e["is_repeat"], e["clips"]) for e in entries] == [
        (2, True, ["00819"]),
        (4, False, ["00830"]),
    ]
    assert entries[0]["items"][0] == {
        "clip": "00819",
        "connection_condition": 1,
        "in_time": 524280,
        "out_time": 3226980,
        "sync_play_item_id": 0,
        "sync_pts": 0,
    }
    assert entries[1]["items"][0]["connection_condition"] == 5


def test_subpath_multi_clip_item_lists_every_clip() -> None:
    data = _subpath(2, [_sub_play_item("00819", extra_clips=["00820", "00821"])])

    (entry,), _ = _parse_subpath_entries(data, 0, 1)

    assert entry["clips"] == ["00819", "00820", "00821"]
    assert [item["clip"] for item in entry["items"]] == ["00819"]


def test_subpath_truncated_records_stop_parsing() -> None:
    entries, pos = _parse_subpath_entries(b"\x00\x00\x02", 0, 1)
    assert entries == [] and pos == 0

    full = _subpath(2, [_sub_play_item("00819")])
    entries, _ = _parse_subpath_entries(full[:-10], 0, 1)
    assert entries == []

    two = _subpath(2, [_sub_play_item("00819")]) + _subpath(
        2, [_sub_play_item("00820")]
    )
    entries, pos = _parse_subpath_entries(two[:-5], 0, 2)
    assert [e["clips"] for e in entries] == [["00819"]]
    assert pos <= len(two)


@_needs("00307.mpls")
def test_monsters_university_subpath_entries() -> None:
    info = _parse_mpls(_FIXTURES / "00307.mpls")
    assert info is not None
    entries = info["subpath_entries"]
    assert [(e["type"], e["clips"]) for e in entries] == [
        (2, [f"0081{n}"]) for n in range(9, 10)
    ] + [(2, [f"0082{n}"]) for n in range(0, 6)]
    first = entries[0]["items"][0]
    assert (first["clip"], first["in_time"], first["connection_condition"]) == (
        "00819",
        524280,
        1,
    )


@_needs("sgtfrog_s1d1_00000.mpls")
def test_sgt_frog_subpath_entry_is_the_ig_menu() -> None:
    info = _parse_mpls(_FIXTURES / "sgtfrog_s1d1_00000.mpls")
    assert info is not None
    assert [(e["type"], e["clips"]) for e in info["subpath_entries"]] == [
        (3, ["00013"])
    ]
