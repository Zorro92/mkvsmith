"""Just enough H.264 header parsing to tell how a stream's frames are scanned.

Blu-ray clip info only says a video stream is "interlaced", which is also
true of soft-telecined film: progressive 24 fps frames carried as 29.97i
with pulldown flags. Writing Matroska's interlaced flag for those would make
players deinterlace real film frames, so the decision is made per frame from
the bitstream instead, following ITU-T H.264:

- SPS (7.3.2.1) incl. VUI (E.1.1) and HRD parameters (E.1.2): whether field
  coding (``frame_mbs_only_flag``, MBAFF) is possible, and whether pictures
  carry timing SEI (``pic_struct_present_flag``) and its field lengths.
- PPS (7.3.2.2): which SPS a slice uses.
- Slice header (7.3.3): field picture or frame, top or bottom field.
- Picture timing SEI (D.1.3): ``pic_struct`` and the clock timestamp's
  ``ct_type`` (Table D-1, D-2).

A frame counts as interlaced the way FFmpeg's ``h264_slice.c`` decides it:
``ct_type`` when present, else single fields or field/MBAFF-coded frames
shown as two fields. Repeated-field pictures (``pic_struct`` 5/6) are
pulldown, i.e. progressive. Without ``pic_struct``, field order comes from
the first field of each field pair, or from a frame's top vs bottom field
picture order counts (8.2.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

# profile_idc values whose SPS carries chroma format / bit depth / scaling.
_HIGH_PROFILES = frozenset(
    {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
)
_NAL_SLICE = 1
_NAL_IDR = 5
_NAL_SEI = 6
_NAL_SPS = 7
_NAL_PPS = 8
_SEI_PIC_TIMING = 1
# pic_struct -> number of clock timestamps that follow it (Table D-1).
_CLOCK_TIMESTAMPS = (1, 1, 1, 2, 2, 3, 3, 2, 3)
_EXTENDED_SAR = 255

# A file is only flagged interlaced when nearly every sampled frame is
# interlaced and all of them agree on which field leads.
_MIN_INTERLACED_SHARE = 0.95
_MIN_FRAMES = 24


class ScanType(Enum):
    PROGRESSIVE = "progressive"  # incl. soft-telecined / pulldown film
    INTERLACED_TFF = "tff"
    INTERLACED_BFF = "bff"


@dataclass(frozen=True)
class FrameScan:
    interlaced: bool
    top_field_first: bool | None  # None when the stream doesn't say
    # A lone field picture (Matroska may store each field as its own block);
    # top_field_first then says which field this one is.
    single_field: bool = False


class _BitReader:
    def __init__(self, rbsp: bytes) -> None:
        self._data = rbsp
        self._pos = 0  # in bits

    def bit(self) -> int:
        byte = self._data[self._pos >> 3]
        value = (byte >> (7 - (self._pos & 7))) & 1
        self._pos += 1
        return value

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-Golomb code")
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        code = self.ue()
        return (code + 1) // 2 if code & 1 else -(code // 2)


def _rbsp(nal: bytes) -> bytes:
    """NAL payload without its header byte and emulation-prevention bytes."""
    out = bytearray()
    zeros = 0
    for byte in nal[1:]:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


# =============================================================================
# Parameter sets
# =============================================================================


@dataclass(frozen=True)
class _Poc:
    """Picture order count parameters needed for a frame's field order."""

    poc_type: int
    log2_max_poc_lsb: int = 0
    delta_always_zero: bool = False
    offset_top_to_bottom: int = 0


@dataclass(frozen=True)
class _Pps:
    sps_id: int
    bottom_poc_present: bool  # bottom_field_pic_order_in_frame_present_flag


@dataclass(frozen=True)
class _Sps:
    log2_max_frame_num: int
    frame_mbs_only: bool
    mbaff: bool
    separate_colour_plane: bool
    pic_struct_present: bool
    poc: _Poc
    # Lengths of the picture timing SEI fields; delays only when HRD present.
    cpb_dpb_delays_present: bool = False
    cpb_removal_delay_length: int = 24
    dpb_output_delay_length: int = 24
    time_offset_length: int = 24


@dataclass(frozen=True)
class _Hrd:
    cpb_removal_delay_length: int
    dpb_output_delay_length: int
    time_offset_length: int


def _skip_scaling_list(r: _BitReader, size: int) -> None:
    last, next_scale = 8, 8
    for _ in range(size):
        if next_scale:
            next_scale = (last + r.se() + 256) % 256
        last = next_scale or last


def _parse_hrd(r: _BitReader) -> _Hrd:
    cpb_count = r.ue() + 1
    r.bits(8)  # bit_rate_scale, cpb_size_scale
    for _ in range(cpb_count):
        r.ue()  # bit_rate_value_minus1
        r.ue()  # cpb_size_value_minus1
        r.bit()  # cbr_flag
    r.bits(5)  # initial_cpb_removal_delay_length_minus1
    cpb_removal = r.bits(5) + 1
    dpb_output = r.bits(5) + 1
    time_offset = r.bits(5)
    return _Hrd(cpb_removal, dpb_output, time_offset)


def _parse_vui(r: _BitReader) -> tuple[bool, _Hrd | None]:
    """(pic_struct_present_flag, HRD parameters if any) from VUI."""
    if r.bit():  # aspect_ratio_info_present_flag
        if r.bits(8) == _EXTENDED_SAR:
            r.bits(32)  # sar_width, sar_height
    if r.bit():  # overscan_info_present_flag
        r.bit()
    if r.bit():  # video_signal_type_present_flag
        r.bits(4)  # video_format, video_full_range_flag
        if r.bit():  # colour_description_present_flag
            r.bits(24)
    if r.bit():  # chroma_loc_info_present_flag
        r.ue()
        r.ue()
    if r.bit():  # timing_info_present_flag
        r.bits(32)  # num_units_in_tick
        r.bits(32)  # time_scale
        r.bit()  # fixed_frame_rate_flag
    hrd: _Hrd | None = None
    nal_hrd = r.bit()
    if nal_hrd:
        hrd = _parse_hrd(r)
    vcl_hrd = r.bit()
    if vcl_hrd:
        hrd = hrd or _parse_hrd(r)
    if nal_hrd or vcl_hrd:
        r.bit()  # low_delay_hrd_flag
    return bool(r.bit()), hrd


def _parse_sps(nal: bytes) -> tuple[int, _Sps]:
    r = _BitReader(_rbsp(nal))
    profile = r.bits(8)
    r.bits(16)  # constraint flags, level_idc
    sps_id = r.ue()
    separate = False
    if profile in _HIGH_PROFILES:
        chroma_format = r.ue()
        if chroma_format == 3:
            separate = bool(r.bit())
        r.ue()  # bit_depth_luma_minus8
        r.ue()  # bit_depth_chroma_minus8
        r.bit()  # qpprime_y_zero_transform_bypass_flag
        if r.bit():  # seq_scaling_matrix_present_flag
            for i in range(12 if chroma_format == 3 else 8):
                if r.bit():
                    _skip_scaling_list(r, 16 if i < 6 else 64)
    log2_max_frame_num = r.ue() + 4
    poc_type = r.ue()
    poc = _Poc(poc_type)
    if poc_type == 0:
        poc = _Poc(poc_type, log2_max_poc_lsb=r.ue() + 4)
    elif poc_type == 1:
        always_zero = bool(r.bit())
        r.se()  # offset_for_non_ref_pic
        poc = _Poc(poc_type, delta_always_zero=always_zero, offset_top_to_bottom=r.se())
        for _ in range(r.ue()):
            r.se()
    r.ue()  # max_num_ref_frames
    r.bit()  # gaps_in_frame_num_value_allowed_flag
    r.ue()  # pic_width_in_mbs_minus1
    r.ue()  # pic_height_in_map_units_minus1
    frame_mbs_only = bool(r.bit())
    mbaff = False if frame_mbs_only else bool(r.bit())
    r.bit()  # direct_8x8_inference_flag
    if r.bit():  # frame_cropping_flag
        for _ in range(4):
            r.ue()
    pic_struct_present = False
    hrd: _Hrd | None = None
    if r.bit():  # vui_parameters_present_flag
        pic_struct_present, hrd = _parse_vui(r)
    sps = _Sps(
        log2_max_frame_num, frame_mbs_only, mbaff, separate, pic_struct_present, poc
    )
    if hrd is not None:
        sps = _Sps(
            log2_max_frame_num,
            frame_mbs_only,
            mbaff,
            separate,
            pic_struct_present,
            poc,
            True,
            hrd.cpb_removal_delay_length,
            hrd.dpb_output_delay_length,
            hrd.time_offset_length,
        )
    return sps_id, sps


def _parse_pps(nal: bytes) -> tuple[int, _Pps]:
    r = _BitReader(_rbsp(nal))
    pps_id = r.ue()
    sps_id = r.ue()
    r.bit()  # entropy_coding_mode_flag
    return pps_id, _Pps(sps_id, bool(r.bit()))


# =============================================================================
# Per-frame analysis
# =============================================================================


@dataclass(frozen=True)
class _PicTiming:
    pic_struct: int | None
    ct_type: int | None


def _parse_pic_timing(payload: bytes, sps: _Sps) -> _PicTiming:
    r = _BitReader(payload)
    if sps.cpb_dpb_delays_present:
        r.bits(sps.cpb_removal_delay_length)
        r.bits(sps.dpb_output_delay_length)
    if not sps.pic_struct_present:
        return _PicTiming(None, None)
    pic_struct = r.bits(4)
    ct_type: int | None = None
    if pic_struct < len(_CLOCK_TIMESTAMPS) and r.bit():  # clock_timestamp_flag
        ct_type = r.bits(2)
    return _PicTiming(pic_struct, ct_type)


def _sei_payloads(nal: bytes) -> Iterable[tuple[int, bytes]]:
    data = _rbsp(nal)
    pos = 0
    while pos < len(data) and data[pos] != 0x80:  # rbsp trailing bits
        payload_type = 0
        while data[pos] == 0xFF:
            payload_type += 255
            pos += 1
        payload_type += data[pos]
        pos += 1
        size = 0
        while data[pos] == 0xFF:
            size += 255
            pos += 1
        size += data[pos]
        pos += 1
        yield payload_type, data[pos : pos + size]
        pos += size


@dataclass(frozen=True)
class _Slice:
    sps: _Sps
    field_pic: bool
    bottom_field: bool
    # For a frame: whether its top field precedes its bottom field in
    # picture order count; None when they are equal or unknown.
    poc_top_first: bool | None = None


def _parse_slice(nal: bytes, sps: dict[int, _Sps], pps: dict[int, _Pps]) -> _Slice:
    r = _BitReader(_rbsp(nal))
    r.ue()  # first_mb_in_slice
    r.ue()  # slice_type
    picture_params = pps[r.ue()]
    params = sps[picture_params.sps_id]
    if params.frame_mbs_only:
        return _Slice(params, False, False)
    if params.separate_colour_plane:
        r.bits(2)  # colour_plane_id
    r.bits(params.log2_max_frame_num)  # frame_num
    field_pic = bool(r.bit())
    bottom = bool(r.bit()) if field_pic else False
    if field_pic:
        return _Slice(params, True, bottom)
    if nal[0] & 0x1F == _NAL_IDR:
        r.ue()  # idr_pic_id
    # Bottom POC - top POC for this frame (8.2.1.1 / 8.2.1.2).
    poc = params.poc
    if poc.poc_type == 0:
        r.bits(poc.log2_max_poc_lsb)  # pic_order_cnt_lsb
        delta = r.se() if picture_params.bottom_poc_present else 0
    elif poc.poc_type == 1 and not poc.delta_always_zero:
        r.se()  # delta_pic_order_cnt[0]
        bottom_delta = r.se() if picture_params.bottom_poc_present else 0
        delta = poc.offset_top_to_bottom + bottom_delta
    elif poc.poc_type == 1:
        delta = poc.offset_top_to_bottom
    else:
        delta = 0
    return _Slice(params, False, False, None if delta == 0 else delta > 0)


def _frame_scan(slice_: _Slice, timing: _PicTiming | None) -> FrameScan:
    field_or_mbaff = slice_.field_pic or slice_.sps.mbaff
    pic_struct = timing.pic_struct if timing else None
    if pic_struct is None:
        if slice_.field_pic:
            return FrameScan(True, not slice_.bottom_field, single_field=True)
        return FrameScan(field_or_mbaff, slice_.poc_top_first)
    if pic_struct in (1, 2):  # a single field
        interlaced = True
    elif pic_struct in (3, 4):  # two fields of one frame
        interlaced = field_or_mbaff
    else:  # frame, pulldown (5/6) or frame doubling/tripling: progressive
        interlaced = False
    if timing is not None and timing.ct_type in (0, 1):
        interlaced = timing.ct_type == 1
    top_first = {1: True, 2: False, 3: True, 4: False, 5: True, 6: False}.get(
        pic_struct
    )
    return FrameScan(interlaced, top_first)


class FrameAnalyzer:
    """Feeds one frame's NAL units at a time; tracks in-band parameter sets."""

    def __init__(self, sps_nals: Sequence[bytes], pps_nals: Sequence[bytes]) -> None:
        self._sps: dict[int, _Sps] = dict(_parse_sps(n) for n in sps_nals)
        self._pps: dict[int, _Pps] = dict(_parse_pps(n) for n in pps_nals)

    def frame(self, nals: Sequence[bytes]) -> FrameScan | None:
        """Scan type of one frame, or ``None`` if it has no slice."""
        sei: list[bytes] = []
        for nal in nals:
            nal_type = nal[0] & 0x1F
            if nal_type == _NAL_SPS:
                self._sps.update([_parse_sps(nal)])
            elif nal_type == _NAL_PPS:
                self._pps.update([_parse_pps(nal)])
            elif nal_type == _NAL_SEI:
                sei.append(nal)
            elif nal_type in (_NAL_SLICE, _NAL_IDR):
                slice_ = _parse_slice(nal, self._sps, self._pps)
                timing = None
                for message in sei:
                    for payload_type, payload in _sei_payloads(message):
                        if payload_type == _SEI_PIC_TIMING:
                            timing = _parse_pic_timing(payload, slice_.sps)
                return _frame_scan(slice_, timing)
        return None


def classify_scan(frames: Sequence[FrameScan]) -> ScanType | None:
    """One scan type for a stream from its sampled frames, or ``None``.

    Interlaced only when nearly every frame is interlaced and every frame
    that states a field order agrees; progressive when no frame is
    interlaced. Anything in between (or too few frames) is left undecided.
    """
    if len(frames) < _MIN_FRAMES:
        return None
    interlaced = [f for f in frames if f.interlaced]
    if not interlaced:
        return ScanType.PROGRESSIVE
    if len(interlaced) < _MIN_INTERLACED_SHARE * len(frames):
        return None
    orders = {f.top_field_first for f in interlaced if f.top_field_first is not None}
    if orders == {True}:
        return ScanType.INTERLACED_TFF
    if orders == {False}:
        return ScanType.INTERLACED_BFF
    return None


def stream_scan_type(
    sps_nals: Sequence[bytes],
    pps_nals: Sequence[bytes],
    frames: Iterable[Sequence[bytes]],
) -> ScanType | None:
    """Scan type of a stream from its parameter sets and leading frames."""
    try:
        analyzer = FrameAnalyzer(sps_nals, pps_nals)
        scans = [s for s in (analyzer.frame(nals) for nals in frames) if s]
    except (IndexError, ValueError, KeyError):
        return None
    return classify_scan(_pair_fields(scans))


def _pair_fields(scans: Sequence[FrameScan]) -> list[FrameScan]:
    """Merge consecutive opposite single fields into one frame each.

    The pair's first field gives its field order. Unpaired fields stay as
    they are, so a misaligned start just reads as a mixed order (undecided).
    """
    merged: list[FrameScan] = []
    i = 0
    while i < len(scans):
        scan = scans[i]
        nxt = scans[i + 1] if i + 1 < len(scans) else None
        if (
            scan.single_field
            and nxt is not None
            and nxt.single_field
            and nxt.top_field_first != scan.top_field_first
        ):
            merged.append(FrameScan(True, scan.top_field_first))
            i += 2
            continue
        merged.append(scan)
        i += 1
    return merged
