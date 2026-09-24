"""Tests for EIA-608 closed-caption extraction (cc608).

The fixture captures the pack-aligned opening of Treasure Planet (2002,
R1 DVD9) VTS_01_1.VOB, whose GOP user data decodes to the film's first
caption ("[MUSIC PLAYING]"). The decoder cases below follow FFmpeg's
ccaption_dec.c semantics (pop-on buffering, PAC addressing, doubled-pair
suppression).
"""

from __future__ import annotations

from pathlib import Path

import cc608
import pytest

from cc608 import (
    CC608_CODEC_ASS,
    CC608_CODEC_SRT,
    CC608_CODECS,
    CcCaptionEvent,
    CcStyledLine,
    _Cc608Decoder,
    _cc_pairs_from_block,
    _format_srt_timestamp,
    _has_cc608_data,
    extract_cc608_captions,
    write_cc608_ass,
    write_cc608_srt,
)


def _odd(value: int) -> int:
    """Set the parity bit when needed (transmitter side of EIA-608)."""
    return value | 0x80 if bin(value).count("1") % 2 == 0 else value


def _feed(decoder: _Cc608Decoder, pairs: list[tuple[int, int]], pts: int = 1000):
    """Feed parity-stripped pairs; return the emitted events."""
    events = []
    for hi, lo in pairs:
        event = decoder.feed(hi, lo, pts)
        if event is not None:
            events.append(event)
    return events


def test_decoder_popon_caption_cycle() -> None:
    decoder = _Cc608Decoder()
    # RCL, PAC (row 13, indent 0), text, EOC. Doubled *control* pairs are
    # suppressed (prev_cmd); standard character pairs are not, per
    # ccaption_dec.c.
    events = _feed(
        decoder,
        [
            (0x14, 0x20),
            (0x13, 0x40),
            (0x54, 0x45),
            (0x53, 0x54),
            (0x14, 0x2F),
            (0x14, 0x2F),  # doubled EOC is suppressed
        ],
    )

    assert [event.rows for event in events if event.rows] == [("TEST",)]
    # EDM after display clears the screen.
    clear = _feed(decoder, [(0x14, 0x2C)])
    assert [event.rows for event in clear] == [()]


def test_decoder_rollup_carriage_return() -> None:
    decoder = _Cc608Decoder()
    events = _feed(
        decoder,
        [
            (0x14, 0x25),  # roll-up 2
            (0x13, 0x40),  # bottom row
            (0x48, 0x49),  # HI
            (0x14, 0x2D),  # carriage return
        ],
    )

    assert [event.rows for event in events if event.rows] == [("HI",)]


def test_decoder_special_characters() -> None:
    decoder = _Cc608Decoder()
    _feed(decoder, [(0x14, 0x20), (0x13, 0x40)])
    _feed(decoder, [(0x11, 0x30), (0x20, 0x20)])  # (R) then two spaces
    events = _feed(decoder, [(0x14, 0x2F)])

    assert [event.rows for event in events if event.rows] == [("®",)]


def test_cc_pairs_from_block_reads_both_segment_patterns() -> None:
    """Segments are [field marker][pair][field marker][pair]; the pattern
    flag (attributes bit 7) says whether Field 1 comes first or second."""

    def segment(field1: tuple[int, int], field2: tuple[int, int], f1_first: bool):
        if f1_first:
            first_marker, first, second_marker, second = (
                0xFF,
                field1,
                0xFE,
                field2,
            )
        else:
            first_marker, first, second_marker, second = (
                0xFE,
                field2,
                0xFF,
                field1,
            )
        return bytes(
            [
                first_marker,
                _odd(first[0]),
                _odd(first[1]),
                second_marker,
                _odd(second[0]),
                _odd(second[1]),
            ]
        )

    real = (0x14, 0x2C)  # wire form 94 2c (both already odd parity)
    text = (0x54, 0x45)
    nulls = (0x00, 0x00)  # Field 2 nulls with broken parity

    # Pattern 1 (Field 1 first), two segments.
    pattern1 = (
        b"\x43\x43\x01\xf8"
        + bytes([0x80 | (2 << 1)])
        + segment(real, nulls, f1_first=True)
        + segment(text, nulls, f1_first=True)
    )
    # Pattern 0 (Field 2 first), same Field 1 data.
    pattern0 = (
        b"\x43\x43\x01\xf8"
        + bytes([(2 << 1)])
        + segment(real, (0x80, 0x80), f1_first=False)
        + segment(text, (0x80, 0x80), f1_first=False)
    )

    assert _cc_pairs_from_block(pattern1) == [real, text]
    assert _cc_pairs_from_block(pattern0) == [real, text]

    # Extra field (attributes bit 0) appends one 3-byte Field 1 triplet.
    with_extra = (
        b"\x43\x43\x01\xf8"
        + bytes([0x80 | (1 << 1) | 0x01])
        + segment(real, nulls, f1_first=True)
        + bytes([0xFF, _odd(0x14), _odd(0x2F)])
    )
    assert _cc_pairs_from_block(with_extra) == [real, (0x14, 0x2F)]

    # A marker that is not Field 1's 0xff is skipped, as are bad headers
    # and truncated segments.
    bad_marker = (
        b"\x43\x43\x01\xf8"
        + bytes([0x80 | (1 << 1)])
        + bytes([0xFE, _odd(0x14), _odd(0x2C), 0xFE, 0x00, 0x00])
    )
    assert _cc_pairs_from_block(bad_marker) == []
    assert _cc_pairs_from_block(b"\x43\x43\x01") == []
    assert _cc_pairs_from_block(b"") == []


def test_format_srt_timestamp() -> None:
    assert _format_srt_timestamp(0) == "00:00:00,000"
    assert _format_srt_timestamp(90000 * 67 + 4500) == "00:01:07,050"
    assert _format_srt_timestamp(3600 * 90000) == "01:00:00,000"


def test_write_cc608_srt_builds_cues_with_offset(tmp_path: Path) -> None:
    events = [
        CcCaptionEvent(pts=90000, rows=("HELLO",)),
        CcCaptionEvent(pts=180000, rows=()),
        CcCaptionEvent(pts=270000, rows=("WORLD",)),
    ]

    text = write_cc608_srt(
        events, tmp_path / "test_cc.srt", pts_offset=45000
    ).read_text()

    assert "1\n00:00:00,500 --> 00:00:01,500\nHELLO\n" in text
    assert "2\n00:00:02,500 --> 00:00:07,500\nWORLD\n" in text


def test_has_cc608_data_detects_prefix(tmp_path: Path) -> None:
    carrier = tmp_path / "carrier.vob"
    carrier.write_bytes(
        b"\x00" * 8192 + b"\x00\x00\x01\xb2\x43\x43\x01\xf8\x9e" + b"\x00" * 8192
    )
    plain = tmp_path / "plain.vob"
    plain.write_bytes(b"\x00" * 65536)

    assert _has_cc608_data(carrier, max_bytes=1024 * 1024) is True
    assert _has_cc608_data(plain, max_bytes=1024 * 1024) is False


def test_extract_captions_rebases_cell_clock_resets(monkeypatch) -> None:
    """Non-seamless cells restart the PTS clock mid-movie; each reset is
    snapped to the end of the previous timeline (the re-basing mkvmerge
    applies to appended program-stream segments)."""
    cell_one = [
        (90_000 * 10, (0x14, 0x20)),
        (90_000 * 10, (0x13, 0x40)),
        (90_000 * 11, (0x54, 0x45)),  # TE
        (90_000 * 12, (0x14, 0x2F)),
    ]
    cell_two = [  # raw clock restarts at 1s after cell one ended at 12s
        (90_000 * 1, (0x14, 0x20)),
        (90_000 * 1, (0x13, 0x40)),
        (90_000 * 2, (0x53, 0x54)),  # ST
        (90_000 * 3, (0x14, 0x2F)),
    ]
    monkeypatch.setattr(
        cc608, "_scan_vob_cc608_file", lambda _path: cell_one + cell_two
    )

    events = extract_cc608_captions([Path("fake.vob")])

    assert [(event.pts, event.rows) for event in events if event.rows] == [
        (90_000 * 12, ("TE",)),
        # Cell two's raw clock (1s..3s) is re-based onto the end of cell
        # one: raw 1s -> 12s, so the EOC at raw 3s lands at 14s.
        (90_000 * 14, ("ST",)),
    ]


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_01_cc.vob").is_file(),
    reason="Treasure Planet closed-caption VOB fixture not present",
)
def test_extract_captions_from_vob_fixture(fixtures_dir: Path, tmp_path: Path) -> None:
    """Regression: the film's first caption decodes with correct timing."""
    fixture = fixtures_dir / "treasure_vts_01_cc.vob"

    assert _has_cc608_data(fixture) is True
    events = extract_cc608_captions([fixture])

    assert len(events) == 7
    assert events[3].pts == 295003
    assert events[3].rows == ("[MUSIC PLAYING]",)
    # The styled view carries the same caption with its grid placement.
    assert len(events[3].styled) == 1
    assert events[3].styled[0].plain() == "[MUSIC PLAYING]"

    srt = write_cc608_srt(events, tmp_path / "fixture_cc.srt")
    text = srt.read_text()
    assert "1\n00:00:03,278 --> 00:00:05,146\n[MUSIC PLAYING]\n" in text


@pytest.mark.skipif(
    not (Path(__file__).parent / "fixtures" / "treasure_vts_01_cc.vob").is_file(),
    reason="Treasure Planet closed-caption VOB fixture not present",
)
def test_cc608_codecs_mark_text_subtitle_tracks() -> None:
    # The declared stream codecs mark the track for the muxer's sidecar
    # path rather than the VobSub fallback.
    assert CC608_CODEC_SRT == "subrip"
    assert CC608_CODEC_ASS == "ass"
    assert CC608_CODECS == {"subrip", "ass"}


def test_decoder_styled_lines_carry_position_and_italics() -> None:
    """The styled view keeps the CC grid row/column and italic runs that
    the plain-text view flattens."""
    decoder = _Cc608Decoder()
    events = _feed(
        decoder,
        [
            (0x14, 0x20),  # RCL (pop-on)
            (0x13, 0x60),  # PAC: row 12 (0-indexed), column 0, plain
            (0x54, 0x45),  # TE
            (0x14, 0x5A),  # PAC: row 13, indent 20
            (0x11, 0x2E),  # mid-row: italics on
            (0x4D, 0x55),  # MU
            (0x53, 0x49),  # SI (still italic)
            (0x14, 0x2F),  # EOC
        ],
    )

    assert len(events) == 1
    styled = events[0].styled
    assert [(line.row, line.column) for line in styled] == [(12, 0), (13, 21)]
    assert styled[0].segments == (("TE", False),)
    assert styled[1].segments == (("MUSI", True),)
    assert events[0].rows == ("TE", "MUSI")


def test_write_cc608_ass_maps_grid_onto_video(tmp_path: Path) -> None:
    events = [
        CcCaptionEvent(
            pts=90000,
            rows=("HELLO",),
            styled=(CcStyledLine(row=13, column=8, segments=(("HELLO", False),)),),
        ),
        CcCaptionEvent(
            pts=180000,
            rows=(),
        ),
        CcCaptionEvent(
            pts=270000,
            rows=("MUSIC",),
            styled=(CcStyledLine(row=1, column=0, segments=(("MUSIC", True),)),),
        ),
    ]

    text = write_cc608_ass(
        events, tmp_path / "test_cc.ass", pts_offset=45000
    ).read_text()

    assert "PlayResX: 720" in text
    assert "PlayResY: 480" in text
    # 720/32 = 22.5 px per column; (13 + 0.8) / 15 * 480 = 441.6 -> 442.
    assert "Dialogue: 0,0:00:00.50,0:00:01.50,CC,,0,0,0,,{\\pos(180,442)}HELLO" in text
    # Italic runs carry \i tags; top rows map near the top of the frame.
    assert (
        "Dialogue: 0,0:00:02.50,0:00:07.50,CC,,0,0,0,,{\\pos(0,58)}{\\i1}MUSIC{\\i0}"
        in text
    )
