"""Tests for the minimal Matroska reader's first-frame IDR check.

The MKVs are built byte by byte, so the tests need no disc fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mkvsmith import mkv
from mkvsmith.mkvread import first_video_frame_is_idr

_UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def _el(element_id: str, payload: bytes, *, unknown: bool = False) -> bytes:
    """One EBML element with an 8-byte size field."""
    size = _UNKNOWN if unknown else b"\x01" + len(payload).to_bytes(7, "big")
    return bytes.fromhex(element_id) + size + payload


def _track(number: int, codec: bytes, private: bytes = b"") -> bytes:
    body = _el("D7", bytes([number])) + _el("86", codec)
    if private:
        body += _el("63A2", private)
    return _el("AE", body)


# avcC: version 1, High profile, level 4.1, lengthSizeMinusOne = 3, and no
# SPS/PPS (the IDR check doesn't need them).
_AVCC = bytes([1, 0x64, 0x00, 0x29, 0xFF, 0xE0, 0x00])


def _frame(*nal_headers: int) -> bytes:
    """Length-prefixed NAL units (4-byte lengths), each with a byte of payload."""
    return b"".join(
        len(bytes([h, 0])).to_bytes(4, "big") + bytes([h, 0]) for h in nal_headers
    )


def _block(track: int, frame: bytes) -> bytes:
    return bytes([0x80 | track]) + b"\x00\x00" + b"\x80" + frame


def _mkv(
    tmp_path: Path,
    blocks: list[bytes],
    *,
    tracks: bytes | None = None,
    before_clusters: bytes = b"",
    unknown_cluster: bool = False,
) -> Path:
    if tracks is None:
        tracks = _track(1, b"V_MPEG4/ISO/AVC", _AVCC) + _track(2, b"A_AC3")
    cluster = _el(
        "1F43B675", _el("E7", b"\x00") + b"".join(blocks), unknown=unknown_cluster
    )
    segment = _el("1654AE6B", tracks) + before_clusters + cluster
    data = _el("1A45DFA3", _el("4282", b"matroska")) + _el("18538067", segment)
    path = tmp_path / "out.mkv"
    path.write_bytes(data)
    return path


AUD, SEI, IDR, SLICE = 0x09, 0x06, 0x65, 0x41


def test_idr_first_frame(tmp_path: Path) -> None:
    path = _mkv(tmp_path, [_el("A3", _block(1, _frame(AUD, SEI, IDR, IDR)))])
    assert first_video_frame_is_idr(path) is True


def test_recovery_point_first_frame_is_not_idr(tmp_path: Path) -> None:
    path = _mkv(tmp_path, [_el("A3", _block(1, _frame(AUD, SEI, SLICE, SLICE)))])
    assert first_video_frame_is_idr(path) is False


def test_skips_attachments_and_audio_and_reads_block_groups(tmp_path: Path) -> None:
    attachments = _el("1941A469", b"\x00" * 200_000)  # cover art before clusters
    path = _mkv(
        tmp_path,
        [
            _el("A3", _block(2, b"\x0b\x77audio")),
            _el("A0", _el("A1", _block(1, _frame(AUD, SLICE)))),
        ],
        before_clusters=attachments,
    )
    assert first_video_frame_is_idr(path) is False


def test_unknown_size_cluster(tmp_path: Path) -> None:
    path = _mkv(tmp_path, [_el("A3", _block(1, _frame(IDR)))], unknown_cluster=True)
    assert first_video_frame_is_idr(path) is True


def test_no_avc_track_or_unreadable_file(tmp_path: Path) -> None:
    mpeg2 = _mkv(
        tmp_path,
        [_el("A3", _block(1, b"\x00\x00\x01\xb3"))],
        tracks=_track(1, b"V_MPEG2"),
    )
    assert first_video_frame_is_idr(mpeg2) is None
    garbage = tmp_path / "garbage.mkv"
    garbage.write_bytes(b"\xff" * 64)
    assert first_video_frame_is_idr(garbage) is None
    assert first_video_frame_is_idr(tmp_path / "missing.mkv") is None


@pytest.mark.parametrize(
    ("is_idr", "warned"), [(False, True), (True, False), (None, False)]
)
def test_warning_only_for_a_non_idr_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    is_idr: bool | None,
    warned: bool,
) -> None:
    def fake_check(_path: Path) -> bool | None:
        return is_idr

    monkeypatch.setattr("mkvsmith.mkvread.first_video_frame_is_idr", fake_check)

    mkv._warn_if_starts_without_idr(Path("Episode 6.mkv"))

    assert ("non-IDR" in capsys.readouterr().out) is warned
