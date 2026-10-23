"""Build synthetic H.264 streams (headers only) for tests.

Sequence and picture parameter sets, slice headers and picture timing
SEI, written bit by bit (ITU-T H.264 7.3), with no picture data. A raw
Annex B stream (``annexb``) is enough for mkvmerge to mux, so the streams
also show what mkvmerge does with interlaced AVC.
"""

from __future__ import annotations


class BitWriter:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def u(self, n: int, value: int) -> BitWriter:
        self.bits += [(value >> (n - 1 - i)) & 1 for i in range(n)]
        return self

    def ue(self, value: int) -> BitWriter:
        code = value + 1
        length = code.bit_length()
        return self.u(length - 1, 0).u(length, code)

    def se(self, value: int) -> BitWriter:
        return self.ue(2 * value - 1 if value > 0 else -2 * value)

    def rbsp(self) -> bytes:
        bits = [*self.bits, 1]  # rbsp_stop_one_bit
        bits += [0] * (-len(bits) % 8)
        return bytes(
            int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)
        )


def nal(header: int, rbsp: bytes) -> bytes:
    """A NAL unit with emulation prevention applied."""
    out = bytearray([header])
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def sps(*, frame_mbs_only: bool, mbaff: bool = False, pic_struct: bool) -> bytes:
    w = BitWriter().u(8, 77).u(16, 0x401E).ue(0)  # Main profile, sps_id 0
    w.ue(0)  # log2_max_frame_num_minus4 -> 4-bit frame_num
    w.ue(0).ue(2)  # poc type 0, 6-bit pic_order_cnt_lsb
    w.ue(1).u(1, 0).ue(44).ue(14)  # refs, gaps, size
    w.u(1, int(frame_mbs_only))
    if not frame_mbs_only:
        w.u(1, int(mbaff))
    w.u(1, 1).u(1, 0)  # direct_8x8_inference, no cropping
    w.u(1, 1)  # vui_parameters_present_flag
    w.u(1, 0).u(1, 0).u(1, 0).u(1, 0).u(1, 0)  # no AR/overscan/signal/chroma/timing
    w.u(1, 0).u(1, 0)  # no NAL / VCL HRD
    w.u(1, int(pic_struct)).u(1, 0)  # pic_struct_present, no bitstream_restriction
    return nal(0x67, w.rbsp())


def pps(*, bottom_poc: bool = False) -> bytes:
    w = BitWriter().ue(0).ue(0).u(1, 0).u(1, int(bottom_poc))
    w.ue(0).ue(0).ue(0).u(1, 0).u(2, 0).se(0).se(0).se(0).u(1, 0).u(1, 0).u(1, 0)
    return nal(0x68, w.rbsp())


def slice_header(
    *, field: bool | None = None, bottom: bool = False, delta_bottom: int | None = None
) -> bytes:
    """Non-IDR I-slice header; field=None for a frame_mbs_only stream."""
    w = BitWriter().ue(0).ue(7).ue(0).u(4, 1)  # first_mb, I, pps 0, frame_num
    if field is not None:
        w.u(1, int(field))
        if field:
            w.u(1, int(bottom))
    w.u(6, 4)  # pic_order_cnt_lsb
    if delta_bottom is not None:
        w.se(delta_bottom)
    w.ue(0)  # (rest of the header; not parsed)
    return nal(0x41, w.rbsp())


def pic_timing(pic_struct: int, ct_type: int | None = None) -> bytes:
    w = BitWriter().u(4, pic_struct)
    if ct_type is None:
        w.u(1, 0)  # clock_timestamp_flag
    else:
        w.u(1, 1).u(2, ct_type).u(1, 0).u(5, 0).u(1, 1).u(1, 0).u(1, 0).u(8, 0)
        w.u(6, 0).u(6, 0).u(5, 0)
    payload = w.rbsp()
    return nal(0x06, bytes([1, len(payload)]) + payload + b"\x80")


def idr_slice(*, field: bool | None = None) -> bytes:
    """IDR I-slice header, as a stream must start for mkvmerge."""
    w = BitWriter().ue(0).ue(7).ue(0).u(4, 0)  # first_mb, I, pps 0, frame_num
    if field is not None:
        w.u(1, int(field))
        if field:
            w.u(1, 0)
    w.ue(0).u(6, 0)  # idr_pic_id, pic_order_cnt_lsb
    w.ue(0)  # (rest of the header; not parsed)
    return nal(0x65, w.rbsp())


def annexb(parameter_sets: list[bytes], frames: list[list[bytes]]) -> bytes:
    """A raw stream: the parameter sets, then each frame's NAL units."""
    start_code = b"\x00\x00\x00\x01"
    units = [*parameter_sets, *(unit for frame in frames for unit in frame)]
    return b"".join(start_code + unit for unit in units)
