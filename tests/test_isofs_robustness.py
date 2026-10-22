"""The UDF reader on damaged and unusual images.

Each image is built by tests/synth_iso.py (per ECMA-167 and OSTA UDF) and then
altered the way a damaged disc or another authoring tool would leave it:
copies of the volume structures stand in for broken ones (anchors at N-256
and N, the reserve descriptor sequence, the metadata mirror), as libudfread
and libdvdread read them (docs/formats/filesystems.md).
"""

from __future__ import annotations

import struct
from pathlib import Path

from mkvsmith import isofs
from tests.synth_iso import PARTITION_START, SECTOR, _tag, build_image

FILES: dict[str, bytes | int] = {
    "VIDEO_TS/VIDEO_TS.IFO": bytes(range(256)) * 16,
    "VIDEO_TS/VTS_01_0.IFO": bytes(range(255, -1, -1)) * 24,
}


def _sector(image: bytearray, number: int) -> bytearray:
    return image[number * SECTOR : (number + 1) * SECTOR]


def _put(image: bytearray, number: int, data: bytes | bytearray) -> None:
    image[number * SECTOR : (number + 1) * SECTOR] = data.ljust(SECTOR, b"\x00")


def _retag(descriptor: bytes | bytearray, location: int) -> bytes:
    """*descriptor* with its tag rewritten for a new *location*."""
    ident = struct.unpack_from("<H", descriptor, 0)[0]
    length = struct.unpack_from("<H", descriptor, 10)[0]
    return _tag(ident, bytes(descriptor[16 : 16 + length]), location)


def _open(tmp_path: Path, image: bytes) -> isofs.IsoImage:
    path = tmp_path / "disc.iso"
    path.write_bytes(image)
    return isofs.IsoImage(path)


def _listing(image: isofs.IsoImage) -> dict[str, bytes]:
    return {entry.path: image.read(entry) for entry in image.files()}


def _dvd() -> bytearray:
    return bytearray(build_image(FILES, udf="1.02", iso9660=False))


def test_the_anchor_is_found_at_the_last_sector(tmp_path: Path) -> None:
    image = _dvd()
    last = len(image) // SECTOR - 1
    _put(image, last, _retag(_sector(image, 256), last))
    _put(image, 256, b"")

    with _open(tmp_path, bytes(image)) as disc:
        assert disc.filesystem == "udf"
        assert _listing(disc) == FILES


def test_the_reserve_sequence_stands_in_for_a_damaged_main_one(tmp_path: Path) -> None:
    image = _dvd()
    # Copy the sequence (sectors 32-34) to 40-42, point the anchor's reserve
    # extent there, and wipe the main logical volume descriptor.
    for offset in range(3):
        _put(image, 40 + offset, _retag(_sector(image, 32 + offset), 40 + offset))
    anchor = bytearray(_sector(image, 256))
    struct.pack_into("<II", anchor, 24, 16 * SECTOR, 40)
    _put(image, 256, _retag(anchor, 256))
    _put(image, 33, b"")

    with _open(tmp_path, bytes(image)) as disc:
        assert disc.filesystem == "udf"
        assert _listing(disc) == FILES


def test_the_metadata_mirror_stands_in_for_a_damaged_metadata_file(
    tmp_path: Path,
) -> None:
    image = bytearray(
        build_image(
            {
                "BDMV/index.bdmv": b"INDX" * 100,
                "BDMV/STREAM/00800.m2ts": b"\x47" * 5000,
            },
            udf="2.50",
            iso9660=False,
        )
    )
    # The metadata partition map follows the 6-byte type-1 map in the LVD:
    # its metadata file location is at map + 40, the mirror's at map + 44.
    lvd = bytearray(_sector(image, 33))
    metadata_map = 440 + 6
    metadata_lb = struct.unpack_from("<I", lvd, metadata_map + 40)[0]
    # Put the mirror's entry in a spare partition block (they start unused).
    mirror_lb = 1
    _put(
        image,
        PARTITION_START + mirror_lb,
        _sector(image, PARTITION_START + metadata_lb),
    )
    struct.pack_into("<I", lvd, metadata_map + 44, mirror_lb)
    _put(image, 33, _retag(lvd, 33))
    _put(image, PARTITION_START + metadata_lb, b"")

    with _open(tmp_path, bytes(image)) as disc:
        assert sorted(_listing(disc)) == ["BDMV/STREAM/00800.m2ts", "BDMV/index.bdmv"]
        assert disc.skipped == ["<metadata>: metadata file unreadable; used its mirror"]


def _fid_offset(image: bytearray, name: str) -> int:
    """Image offset of the file identifier descriptor naming *name*."""
    name_bytes = b"\x08" + name.encode("latin-1")
    at = image.index(name_bytes)
    return at - 38  # FIDs here carry no implementation use


def test_a_file_linked_under_two_names_is_listed_under_both(tmp_path: Path) -> None:
    image = _dvd()
    first, second = (_fid_offset(image, n) for n in ("VIDEO_TS.IFO", "VTS_01_0.IFO"))
    image[second + 20 : second + 36] = image[first + 20 : first + 36]  # same ICB

    with _open(tmp_path, bytes(image)) as disc:
        listing = _listing(disc)

    assert listing["VIDEO_TS/VTS_01_0.IFO"] == listing["VIDEO_TS/VIDEO_TS.IFO"]
    assert len(listing) == 2


def test_an_indirect_entry_is_followed(tmp_path: Path) -> None:
    image = _dvd()
    fid = _fid_offset(image, "VTS_01_0.IFO")
    target_lb = struct.unpack_from("<I", image, fid + 24)[0]
    # An indirect entry (tag 259) in a spare partition block, pointing on.
    indirect_lb = 1
    body = bytes(20) + struct.pack("<IIH", SECTOR, target_lb, 0) + bytes(6)
    _put(image, PARTITION_START + indirect_lb, _tag(259, body, indirect_lb))
    struct.pack_into("<I", image, fid + 24, indirect_lb)

    with _open(tmp_path, bytes(image)) as disc:
        assert _listing(disc) == FILES


def test_extended_and_empty_first_allocation_descriptors(tmp_path: Path) -> None:
    path = tmp_path / "disc.iso"
    path.write_bytes(bytes(_dvd()))
    with path.open("rb") as handle:
        reader = isofs._UdfReader(handle)

    # An empty short_ad first (as copy protections add) doesn't end the list.
    short_ads = struct.pack("<II", 0, 0) + struct.pack("<II", 4096, 64)
    assert list(reader._allocation_descriptors(short_ads, 0, 0, "f")) == [
        (0, 64, 4096, True)
    ]
    # An ext_ad: length, recorded length, information length, then the block
    # and partition at 12 and 16.
    ext_ad = struct.pack("<IIIIH2x", 4096, 4096, 4096, 64, 0)
    assert list(reader._allocation_descriptors(ext_ad, 2, 0, "f")) == [
        (0, 64, 4096, True)
    ]


def test_descriptor_tags_are_checked() -> None:
    good = _tag(2, bytes(496), 256)
    assert isofs._udf_tag_ok(good, 2, 256, crc=True)
    assert not isofs._udf_tag_ok(good, 2, 257)  # wrong location
    bad_checksum = bytearray(good)
    bad_checksum[4] ^= 1
    assert not isofs._udf_tag_ok(bytes(bad_checksum), 2)
    bad_crc = bytearray(good)
    bad_crc[100] ^= 1
    assert isofs._udf_tag_ok(bytes(bad_crc), 2)  # the CRC isn't checked by default
    assert not isofs._udf_tag_ok(bytes(bad_crc), 2, crc=True)
