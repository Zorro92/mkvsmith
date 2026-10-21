"""Build small synthetic disc images (UDF and/or ISO 9660) for tests.

Written from ECMA-167 3rd edition (descriptor tags, volume and file
structures), OSTA UDF 1.02 / 2.50 (the DVD and Blu-ray profiles, including
2.50's metadata partition) and ECMA-119 (ISO 9660), independently of
mkvsmith's isofs.py. Kept on the developer's machine: .agents/specs/udf/.

Each file's data is laid out once and both file systems point at it, as on
a real UDF bridge disc. Descriptor tags carry a correct checksum and CRC.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

SECTOR = 2048
PARTITION_START = 272  # first sector of the UDF partition (after the anchor)

# ECMA-167 tag identifiers.
TAG_PVD, TAG_AVDP, TAG_PD, TAG_LVD, TAG_USD, TAG_TD = 1, 2, 5, 6, 7, 8
TAG_FSD, TAG_FID, TAG_FE, TAG_EFE = 256, 257, 261, 266

FILE_TYPE_DIRECTORY, FILE_TYPE_FILE, FILE_TYPE_METADATA = 4, 5, 250


# --- low-level encoders --------------------------------------------------------------


def _crc_itu(data: bytes) -> int:
    """CRC-ITU-T (x^16 + x^12 + x^5 + 1, initial 0), ECMA-167 1/7.2.6."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _tag(ident: int, body: bytes, location: int, version: int = 2) -> bytes:
    """A 16-byte descriptor tag (ECMA-167 3/7.2) followed by *body*."""
    crc = _crc_itu(body)
    head = bytearray(
        struct.pack("<HHBBHHHI", ident, version, 0, 0, 1, crc, len(body), location)
    )
    head[4] = sum(head[i] for i in range(16) if i != 4) & 0xFF
    return bytes(head) + body


def _dchars(text: str) -> bytes:
    """OSTA CS0: compression id 8 (one byte per character) or 16 (UTF-16BE)."""
    try:
        return b"\x08" + text.encode("latin-1")
    except UnicodeEncodeError:
        return b"\x10" + text.encode("utf-16-be")


def _dstring(text: str, size: int) -> bytes:
    """A fixed-size dstring: CS0 characters, zero padding, length last."""
    raw = _dchars(text) if text else b""
    return raw.ljust(size - 1, b"\x00") + bytes([len(raw)])


def _regid(identifier: bytes, suffix: bytes = b"") -> bytes:
    """An EntityID (regid): flags, 23-byte identifier, 8-byte suffix."""
    return b"\x00" + identifier.ljust(23, b"\x00") + suffix.ljust(8, b"\x00")


def _timestamp(when: datetime) -> bytes:
    """ECMA-167 1/7.3: type 1 (local time) with the UTC offset in minutes."""
    offset = when.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    type_tz = 1 << 12 | (minutes & 0x0FFF)
    return struct.pack(
        "<HhBBBBBBBB",
        type_tz,
        when.year,
        when.month,
        when.day,
        when.hour,
        when.minute,
        when.second,
        when.microsecond // 10_000,
        when.microsecond // 100 % 100,
        when.microsecond % 100,
    )


def _long_ad(length: int, lb: int, partition: int) -> bytes:
    return struct.pack("<IIH", length, lb, partition) + bytes(6)


def _short_ad(length: int, lb: int) -> bytes:
    return struct.pack("<II", length, lb)


def _both16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def _both32(value: int) -> bytes:
    return struct.pack("<I", value) + struct.pack(">I", value)


# --- the tree -------------------------------------------------------------------------


EPOCH = datetime(2005, 6, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Node:
    name: str
    children: dict[str, _Node] = field(default_factory=dict)
    data: bytes | None = None  # None for a directory
    size: int = 0
    modified: datetime = EPOCH
    # Filled in by the layout:
    data_sector: int = 0
    fe_block: int = 0
    dir_block: int = 0  # UDF: first block of the directory's FIDs
    iso_sector: int = 0  # ISO 9660: the directory's extent

    @property
    def is_dir(self) -> bool:
        return self.data is None


def _tree(files: dict[str, bytes | int], modified: datetime) -> _Node:
    """Build the directory tree; a file given as an int is that many zero
    bytes (written sparsely)."""
    root = _Node("")
    for path, content in files.items():
        node = root
        *parents, leaf = path.split("/")
        for part in parents:
            node = node.children.setdefault(part, _Node(part, modified=modified))
        if isinstance(content, int):
            node.children[leaf] = _Node(leaf, data=b"", size=content, modified=modified)
        else:
            node.children[leaf] = _Node(
                leaf, data=content, size=len(content), modified=modified
            )
    return root


def _walk(node: _Node) -> list[_Node]:
    out = [node]
    for child in sorted(node.children.values(), key=lambda n: n.name):
        out.extend(_walk(child) if child.is_dir else [child])
    return out


# --- the image ----------------------------------------------------------------------


class _Writer:
    def __init__(self) -> None:
        self.sectors: dict[int, bytes] = {}
        self.sparse: list[tuple[int, int]] = []  # (sector, bytes) of zero files

    def put(self, sector: int, data: bytes) -> None:
        for n in range(0, max(len(data), 1), SECTOR):
            self.sectors[sector + n // SECTOR] = data[n : n + SECTOR].ljust(
                SECTOR, b"\x00"
            )

    def write(self, path_or_size: int) -> bytes:
        size = path_or_size * SECTOR
        image = bytearray(size)
        for sector, data in self.sectors.items():
            image[sector * SECTOR : (sector + 1) * SECTOR] = data
        return bytes(image)


def build_image(
    files: dict[str, bytes | int],
    *,
    udf: str | None = "1.02",
    iso9660: bool = True,
    label: str = "DISC",
    modified: datetime = EPOCH,
    max_extent: int = (1 << 30) - SECTOR,
    damaged: tuple[str, ...] = (),
) -> bytes:
    """A disc image holding *files* ({"VIDEO_TS/VTS_01_0.IFO": data}).

    *damaged* names files (by file name) whose UDF file entry is left as a
    blank sector, as on a scratched disc or a corrupt image.

    *udf*: "1.02" (DVD: file entries in the partition), "2.50" (Blu-ray:
    extended file entries in a metadata partition), or None. *iso9660*:
    also record an ISO 9660 file system (a bridge disc when *udf* is set).
    UDF splits files into adjacent extents of at most *max_extent* bytes
    (an extent's length must stay below 1 GiB; real discs split big files).
    """
    root = _tree(files, modified)
    nodes = _walk(root)
    w = _Writer()

    # File data first, each file in its own run of sectors.
    sector = PARTITION_START + 64
    for node in nodes:
        if not node.is_dir:
            node.data_sector = sector
            if node.data:
                w.put(sector, node.data)
            sector += max(1, -(-node.size // SECTOR))
    end = sector

    if iso9660:
        end = _write_iso9660(w, root, nodes, end, label)
    if udf is not None:
        # The UDF recognition sequence follows the ISO 9660 descriptors
        # (sectors 16-17) on a bridge disc, else starts at sector 16.
        end = _write_udf(
            w,
            root,
            nodes,
            end,
            udf,
            label,
            vrs_at=18 if iso9660 else 16,
            max_extent=max_extent,
            damaged=damaged,
        )
    w.put(end, b"")  # the image ends after the last structure
    return w.write(end + 1)


# --- ISO 9660 ------------------------------------------------------------------------


def _iso_record(
    name: bytes, extent: int, size: int, is_dir: bool, when: datetime
) -> bytes:
    """A directory record (ECMA-119 9.1)."""
    stamp = when.astimezone(timezone.utc)
    body = (
        bytes([0])
        + _both32(extent)
        + _both32(size)
        + bytes(
            [
                stamp.year - 1900,
                stamp.month,
                stamp.day,
                stamp.hour,
                stamp.minute,
                stamp.second,
                0,
            ]
        )
        + bytes([0x02 if is_dir else 0x00, 0, 0])
        + _both16(1)
        + bytes([len(name)])
        + name
    )
    body += b"\x00" if len(body) % 2 == 0 else b""  # pad to an even length
    return bytes([len(body) + 1]) + body  # the length includes the padding


def _write_iso9660(
    w: _Writer, root: _Node, nodes: list[_Node], at: int, label: str
) -> int:
    directories = [n for n in nodes if n.is_dir]
    for directory in directories:
        directory.iso_sector = at
        at += 1
    parents = {id(child): d for d in directories for child in d.children.values()}
    for directory in directories:
        parent = parents.get(id(directory), root)
        records = _iso_record(
            b"\x00", directory.iso_sector, SECTOR, True, directory.modified
        )
        records += _iso_record(
            b"\x01", parent.iso_sector, SECTOR, True, parent.modified
        )
        for child in sorted(directory.children.values(), key=lambda n: n.name):
            if child.is_dir:
                records += _iso_record(
                    child.name.encode(), child.iso_sector, SECTOR, True, child.modified
                )
            else:
                records += _iso_record(
                    child.name.encode() + b";1",
                    child.data_sector,
                    child.size,
                    False,
                    child.modified,
                )
        if len(records) > SECTOR:
            raise ValueError("directory too large for one sector")
        w.put(directory.iso_sector, records)

    pvd = bytearray(SECTOR)
    pvd[0:7] = b"\x01CD001\x01"
    pvd[40:72] = label.encode("ascii").ljust(32)
    pvd[80:88] = _both32(at + 1)
    pvd[120:124] = _both16(1)  # volume set size
    pvd[124:128] = _both16(1)  # volume sequence number
    pvd[128:132] = _both16(SECTOR)
    pvd[156:190] = _iso_record(b"\x00", root.iso_sector, SECTOR, True, root.modified)
    pvd[881] = 1  # file structure version
    w.put(16, bytes(pvd))
    w.put(17, b"\xffCD001\x01")  # volume descriptor set terminator
    return at


# --- UDF ------------------------------------------------------------------------------


def _fid(name: str, icb_block: int, partition: int, characteristics: int) -> bytes:
    """A file identifier descriptor (ECMA-167 4/14.4), padded to 4 bytes."""
    ident = _dchars(name) if name else b""
    body = (
        struct.pack("<HBB", 1, characteristics, len(ident))
        + _long_ad(SECTOR, icb_block, partition)
        + struct.pack("<H", 0)
        + ident
    )
    fid = _tag(TAG_FID, body, 0)
    return fid + bytes(-len(fid) % 4)


def _file_entry(
    node_type: int,
    size: int,
    ads: bytes,
    long_ads: bool,
    extended: bool,
    when: datetime,
    location: int,
) -> bytes:
    """A file entry (ECMA-167 4/14.9) or extended file entry (4/14.17)."""
    icb = struct.pack("<IHHHBB", 0, 4, 0, 1, 0, node_type) + bytes(6)  # + parent
    icb += struct.pack("<H", 1 if long_ads else 0)  # allocation type
    stamp = _timestamp(when)
    blocks = -(-size // SECTOR)
    common = struct.pack("<IIIHBBI", 0, 0, 0x7FFF, 1, 0, 0, 0) + struct.pack(
        "<QQ", size, size if extended else blocks
    )
    if extended:
        body = (
            icb
            + common
            + struct.pack("<Q", blocks)
            + stamp * 4  # access, modification, creation, attribute
            + bytes(4)  # checkpoint
            + bytes(4)  # reserved
            + bytes(16) * 2  # extended attribute ICB, stream directory ICB
            + _regid(b"*synth")
            + bytes(8)  # unique id
            + struct.pack("<II", 0, len(ads))
            + ads
        )
        return _tag(TAG_EFE, body, location, version=3)
    body = (
        icb
        + common
        + stamp * 3  # access, modification, attribute
        + bytes(4)  # checkpoint
        + bytes(16)  # extended attribute ICB
        + _regid(b"*synth")
        + bytes(8)
        + struct.pack("<II", 0, len(ads))
        + ads
    )
    return _tag(TAG_FE, body, location)


def _write_udf(
    w: _Writer,
    root: _Node,
    nodes: list[_Node],
    at: int,
    revision: str,
    label: str,
    *,
    vrs_at: int,
    max_extent: int,
    damaged: tuple[str, ...] = (),
) -> int:
    metadata = revision == "2.50"
    nsr = b"NSR03" if metadata else b"NSR02"
    for n, ident in enumerate((b"BEA01", nsr, b"TEA01")):
        w.put(vrs_at + n, bytes([0]) + ident + bytes([1]))

    # Layout of the structures, in logical blocks of the structure
    # partition (the metadata partition on 2.50, else the physical one).
    structure_ref = 1 if metadata else 0
    block = 0
    fsd_block = block
    block += 1
    parents: dict[int, _Node] = {}
    for node in nodes:
        node.fe_block = block
        block += 1
        if node.is_dir:
            for child in node.children.values():
                parents[id(child)] = node
    for node in nodes:
        if node.is_dir:
            node.dir_block = block
            block += 1  # one block of FIDs per directory is plenty here
    structure_blocks = block

    # Where the structure blocks physically go. On 2.50 the metadata
    # partition starts there (block n is metadata block n); on 1.02 blocks
    # are counted from the start of the physical partition.
    struct_sector = at
    at += structure_blocks
    base = 0 if metadata else struct_sector - PARTITION_START

    def ref(block_number: int) -> int:
        return base + block_number

    def fid_data(directory: _Node) -> bytes:
        parent = parents.get(id(directory), directory)
        data = _fid("", ref(parent.fe_block), structure_ref, 0x0A)  # parent, dir
        for child in sorted(directory.children.values(), key=lambda n: n.name):
            data += _fid(
                child.name,
                ref(child.fe_block),
                structure_ref,
                0x02 if child.is_dir else 0,
            )
        return data

    meta_fe_sector = at
    if metadata:
        at += 1
    partition_length = at - PARTITION_START

    def phys(block_number: int) -> int:
        return struct_sector + block_number

    for node in nodes:
        location = ref(node.fe_block)
        if node.is_dir:
            data = fid_data(node)
            ads = (
                _long_ad(len(data), ref(node.dir_block), structure_ref)
                if metadata
                else _short_ad(len(data), ref(node.dir_block))
            )
            w.put(phys(node.dir_block), data)
            w.put(
                phys(node.fe_block),
                _file_entry(
                    FILE_TYPE_DIRECTORY,
                    len(data),
                    ads,
                    metadata,
                    metadata,
                    node.modified,
                    location,
                ),
            )
        elif node.name in damaged:
            w.put(phys(node.fe_block), bytes(SECTOR))
        else:
            lb = node.data_sector - PARTITION_START
            ads = b""
            for offset in range(0, node.size, max_extent):
                length = min(max_extent, node.size - offset)
                extent_lb = lb + offset // SECTOR
                ads += (
                    _long_ad(length, extent_lb, 0)
                    if metadata
                    else _short_ad(length, extent_lb)
                )
            w.put(
                phys(node.fe_block),
                _file_entry(
                    FILE_TYPE_FILE,
                    node.size,
                    ads,
                    metadata,
                    metadata,
                    node.modified,
                    location,
                ),
            )

    fsd = (
        _timestamp(EPOCH)
        + struct.pack("<HHIIII", 3, 3, 1, 1, 0, 0)
        + bytes(64)  # LV identifier character set
        + _dstring(label, 128)
        + bytes(64)  # file set character set
        + _dstring(label, 32)
        + bytes(32) * 2  # copyright, abstract file identifiers
        + _long_ad(SECTOR, ref(root.fe_block), structure_ref)
        + _regid(b"*OSTA UDF Compliant")
    ).ljust(512 - 16, b"\x00")
    w.put(phys(fsd_block), _tag(TAG_FSD, fsd, ref(fsd_block)))

    if metadata:
        # The metadata file (UDF 2.50 2.2.13): one extent over the structure
        # blocks, so metadata block n is physical partition block start + n.
        start_lb = struct_sector - PARTITION_START
        meta_fe = _file_entry(
            FILE_TYPE_METADATA,
            structure_blocks * SECTOR,
            _short_ad(structure_blocks * SECTOR, start_lb),
            False,
            True,
            EPOCH,
            meta_fe_sector - PARTITION_START,
        )
        w.put(meta_fe_sector, meta_fe)

    # Volume descriptor sequence at sectors 32-36; anchor at 256.
    pd = (
        struct.pack("<IHH", 1, 1, 0)
        + _regid(b"+NSR03" if metadata else b"+NSR02")
        + bytes(128)
        + struct.pack("<III", 1, PARTITION_START, partition_length)
        + _regid(b"*synth")
    ).ljust(512 - 16, b"\x00")
    maps = struct.pack("<BBHH", 1, 6, 1, 0)
    if metadata:
        maps += (
            struct.pack("<BBH", 2, 64, 0)
            + _regid(b"*UDF Metadata Partition", b"\x50\x02")
            + struct.pack(
                "<HHIIIIHB",
                1,
                0,
                meta_fe_sector - PARTITION_START,
                0xFFFFFFFF,
                0xFFFFFFFF,
                32,
                1,
                0,
            )
            + bytes(5)
        )
    lvd = (
        struct.pack("<I", 2)
        + bytes(64)  # descriptor character set
        + _dstring(label, 128)
        + struct.pack("<I", SECTOR)
        + _regid(b"*OSTA UDF Compliant", b"\x50\x02" if metadata else b"\x02\x01")
        + _long_ad(SECTOR, ref(fsd_block), structure_ref)
        + struct.pack("<II", len(maps), 2 if metadata else 1)
        + _regid(b"*synth")
        + bytes(128)
        + _short_ad(0, 0)  # integrity sequence extent
        + maps
    )
    w.put(32, _tag(TAG_PD, pd, 32))
    w.put(33, _tag(TAG_LVD, lvd, 33))
    w.put(34, _tag(TAG_TD, bytes(496), 34))
    avdp = (_short_ad(16 * SECTOR, 32) + _short_ad(16 * SECTOR, 32)).ljust(
        512 - 16, b"\x00"
    )
    w.put(256, _tag(TAG_AVDP, avdp, 256))
    return at
