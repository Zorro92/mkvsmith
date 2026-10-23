"""Native read-only disc image filesystem reader (UDF 1.02-2.60, ISO9660).

Reads the file tree of a DVD / Blu-ray / HD DVD ``.iso`` directly so that
listing and extracting members needs no external tool (previously 7z).

UDF is preferred because it is the authoritative filesystem on every video
disc format: DVD-Video carries a UDF 1.02 bridge, while Blu-ray and HD DVD
images are UDF 2.50 and often omit ISO9660 entirely. ISO9660 (with Joliet
names when present) is the fallback for images built without UDF.

References: ECMA-167 3rd edition, OSTA UDF 2.50 (sections 2.2.10 / 2.2.13
for the metadata partition), ECMA-119 (ISO9660), Joliet specification.
libudfread (code.videolan.org/videolan/libudfread) is the closest reference
implementation.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Protocol

SECTOR = 2048
_COPY_CHUNK = 8 * 1024 * 1024

# Guard against corrupt images whose directory structure loops or explodes.
_MAX_ENTRIES = 1_000_000

# ECMA-167 descriptor tag identifiers.
_TAG_AVDP = 2
_TAG_VDP = 3
_TAG_PD = 5
_TAG_LVD = 6
_TAG_TD = 8
_TAG_FSD = 256
_TAG_FID = 257
_TAG_AED = 258
_TAG_IE = 259
_TAG_FE = 261
_TAG_EFE = 266

_UDF_METADATA_PARTITION = b"*UDF Metadata Partition"
_UDF_SPARABLE_PARTITION = b"*UDF Sparable Partition"


class _Readable(Protocol):
    """The subset of a binary file the parsers need."""

    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def read(self, size: int = -1, /) -> bytes: ...


class IsoImageError(Exception):
    """The image has no filesystem this module can read, or it is corrupt."""


@dataclass(frozen=True)
class IsoRun:
    """One contiguous span of a file's data.

    *offset* is the absolute byte offset in the image, or ``None`` for an
    unrecorded (sparse) span that reads as zeros.
    """

    offset: int | None
    length: int


@dataclass(frozen=True)
class IsoEntry:
    """A regular file inside the image."""

    path: str
    size: int
    modified: datetime | None
    runs: tuple[IsoRun, ...]


# =============================================================================
# Public API
# =============================================================================


class IsoImage:
    """An open disc image. Use as a context manager."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO = path.open("rb")
        try:
            self.filesystem, self.label, entries, self.skipped = _read_filesystem(
                self._file
            )
        except BaseException:
            self._file.close()
            raise
        self.entries: dict[str, IsoEntry] = {entry.path: entry for entry in entries}

    def __enter__(self) -> IsoImage:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._file.close()

    def files(self) -> list[IsoEntry]:
        return list(self.entries.values())

    def read(self, entry: IsoEntry, offset: int = 0, size: int | None = None) -> bytes:
        """Read *size* bytes of *entry* starting at *offset* (default: to EOF)."""
        end = entry.size if size is None else min(entry.size, offset + size)
        out = bytearray()
        pos = 0
        for run in entry.runs:
            if offset >= end:
                break
            run_end = pos + run.length
            if offset < run_end:
                take = min(run_end, end) - offset
                if run.offset is None:
                    out += bytes(take)
                else:
                    self._file.seek(run.offset + offset - pos)
                    chunk = self._file.read(take)
                    if len(chunk) != take:
                        raise IsoImageError(f"{entry.path}: image truncated")
                    out += chunk
                offset += take
            pos = run_end
        return bytes(out)

    def copy_to(
        self,
        entry: IsoEntry,
        dest: Path,
        limit: int | None = None,
        before_chunk: Callable[[], None] | None = None,
        copied: Callable[[int], None] | None = None,
    ) -> None:
        """Write *entry* (or its first *limit* bytes) to *dest*.

        *before_chunk* runs before each chunk is copied; raising from it
        stops the copy (and the exception propagates). *copied* gets the
        bytes written so far after each chunk.
        """
        total = entry.size if limit is None else min(limit, entry.size)
        with dest.open("wb") as out:
            for offset in range(0, total, _COPY_CHUNK):
                if before_chunk is not None:
                    before_chunk()
                size = min(_COPY_CHUNK, total - offset)
                out.write(self.read(entry, offset, size))
                if copied is not None:
                    copied(offset + size)


def _read_filesystem(f: _Readable) -> tuple[str, str, list[IsoEntry], list[str]]:
    """The image's filesystem, label, files, and the UDF entries it skipped.

    UDF wins even with entries skipped: on a DVD the ISO9660 side is the one
    copy protection falsifies, and Blu-ray images often have none
    (docs/formats/filesystems.md). ISO9660 is read only when UDF can't be,
    or lists no file at all.
    """
    errors: list[str] = []
    udf_result: tuple[str, str, list[IsoEntry], list[str]] | None = None
    try:
        udf = _UdfReader(f)
        udf_result = "udf", udf.label, udf.files(), udf.skipped
        if udf_result[2]:
            return udf_result
        errors.append("UDF: no files")
    except IsoImageError as exc:
        errors.append(f"UDF: {exc}")
    try:
        iso = _Iso9660Reader(f)
        return "iso9660", iso.label, iso.files(), []
    except IsoImageError as exc:
        errors.append(f"ISO9660: {exc}")
    if udf_result is not None:
        return udf_result
    raise IsoImageError("; ".join(errors))


# =============================================================================
# Shared helpers
# =============================================================================


def _read_sectors(f: _Readable, sector: int, count: int = 1) -> bytes:
    f.seek(sector * SECTOR)
    data = f.read(count * SECTOR)
    if len(data) != count * SECTOR:
        raise IsoImageError(f"short read at sector {sector}")
    return data


def _merge_runs(runs: list[IsoRun]) -> tuple[IsoRun, ...]:
    """Coalesce physically adjacent runs (large files span many extents)."""
    merged: list[IsoRun] = []
    for run in runs:
        if run.length == 0:
            continue
        if merged:
            last = merged[-1]
            if (
                last.offset is not None
                and run.offset is not None
                and last.offset + last.length == run.offset
            ) or (last.offset is None and run.offset is None):
                merged[-1] = IsoRun(last.offset, last.length + run.length)
                continue
        merged.append(run)
    return tuple(merged)


def _trim_runs(runs: list[IsoRun], size: int, path: str) -> tuple[IsoRun, ...]:
    """*runs* cut to *size* bytes (the last extent may be padded to a block
    boundary), merged where adjacent."""
    trimmed: list[IsoRun] = []
    remaining = size
    for run in runs:
        if remaining <= 0:
            break
        trimmed.append(IsoRun(run.offset, min(run.length, remaining)))
        remaining -= run.length
    if remaining > 0:
        raise IsoImageError(f"{path}: allocation shorter than file size")
    return _merge_runs(trimmed)


def _check_entry_count(count: int) -> None:
    if count > _MAX_ENTRIES:
        raise IsoImageError("directory tree too large (corrupt image?)")


# =============================================================================
# UDF
# =============================================================================


@dataclass(frozen=True)
class _UdfFile:
    is_dir: bool
    size: int
    modified: datetime | None
    runs: tuple[IsoRun, ...]
    # Data stored inside the file entry itself (allocation type 3).
    embedded: bytes | None
    embedded_offset: int | None
    # ICB file type: 4 directory, 5 file, 12 symbolic link, 13 stream
    # directory, ... (ECMA-167 4/14.6.6, UDF 2.50 2.3.5.2).
    file_type: int = 5
    # The same data read through the metadata mirror, when it lies in a
    # metadata partition that has one (directories do).
    mirror_runs: tuple[IsoRun, ...] | None = None


def _u16(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 2], "little")


def _u32(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 4], "little")


def _udf_tag(buf: bytes, offset: int = 0) -> int:
    return _u16(buf, offset)


def _udf_dchars(raw: bytes) -> str:
    """Decode an OSTA CS0 compressed unicode string."""
    if not raw:
        return ""
    if raw[0] == 8:
        return raw[1:].decode("latin-1")
    if raw[0] == 16:
        return raw[1 : 1 + (len(raw) - 1) // 2 * 2].decode("utf-16-be")
    raise IsoImageError(f"bad CS0 compression id {raw[0]}")


def _udf_dstring(field: bytes) -> str:
    length = field[-1] if field else 0
    return _udf_dchars(field[:length]).rstrip("\0") if length else ""


def _udf_timestamp(buf: bytes, offset: int) -> datetime | None:
    """Decode an ECMA-167 timestamp (1/7.3) into an aware datetime."""
    type_tz, year, month, day, hour, minute, second, centi, hundreds, micro = (
        struct.unpack_from("<HhBBBBBBBB", buf, offset)
    )
    tz_minutes = type_tz & 0x0FFF
    if tz_minutes & 0x0800:
        tz_minutes -= 0x1000
    # -2047 means "no timezone specified"; treat it as UTC.
    tz = (
        timezone.utc if tz_minutes == -2047 else timezone(timedelta(minutes=tz_minutes))
    )
    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            centi * 10000 + hundreds * 100 + micro,
            tzinfo=tz,
        )
    except ValueError:
        return None


def _crc_itu(data: bytes) -> int:
    """CRC-ITU-T (x^16 + x^12 + x^5 + 1, initial 0), as UDF descriptor tags use."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
        crc &= 0xFFFF
    return crc


def _udf_tag_ok(
    buf: bytes, ident: int, location: int | None = None, *, crc: bool = False
) -> bool:
    """Whether *buf* starts with a sound *ident* descriptor tag (ECMA-167 3/7.2).

    The checksum covers the tag's other 15 bytes; *location* (when given) is
    the block the tag says it is in; *crc* also checks the descriptor CRC,
    kept for the volume structures since it costs a byte loop. These pick
    between copies (anchor, sequence, metadata file); a reader still accepts
    an unchecked descriptor when no copy passes, as libdvdread does.
    """
    if len(buf) < 16 or _udf_tag(buf) != ident:
        return False
    if (sum(buf[0:4]) + sum(buf[5:16])) & 0xFF != buf[4]:
        return False
    if location is not None and _u32(buf, 12) != location:
        return False
    if crc:
        length = _u16(buf, 10)
        if length and 16 + length <= len(buf):
            if _crc_itu(buf[16 : 16 + length]) != _u16(buf, 8):
                return False
    return True


@dataclass
class _Partition:
    start: int
    length: int


# A logical-volume partition map: what kind, and its partition number.
_MAP_PHYSICAL, _MAP_METADATA, _MAP_UNSUPPORTED = "physical", "metadata", "unsupported"
# Volume descriptor pointers followed at most this many times (loops).
_MAX_VDS_EXTENTS = 16
# Indirect entries followed at most this many times per file (loops).
_MAX_INDIRECT = 16
# Symbolic links and stream directories, which aren't files to list.
_SPECIAL_FILE_TYPES = frozenset({12, 13})


class _UdfReader:
    def __init__(self, f: _Readable) -> None:
        self._f = f
        self._sectors = f.seek(0, 2) // SECTOR
        self._partitions: dict[int, _Partition] = {}
        # One entry per logical-volume partition map: (kind, partition number).
        self._maps: list[tuple[str, int]] = []
        # Runs of the metadata file and of its mirror, as (sector in the
        # partition, or None for an unallocated span; bytes).
        self._meta_runs: list[tuple[int | None, int]] = []
        self._mirror_runs: list[tuple[int | None, int]] = []
        self.label = ""
        # Entries files() couldn't read, each with its reason.
        self.skipped: list[str] = []
        self._root = self._read_volume()

    # -- addressing ---------------------------------------------------------

    def _sector_of(self, part: int, lb: int, *, mirror: bool = False) -> int:
        """Absolute image sector of logical block *lb* in partition ref *part*.

        *mirror* reads a metadata-partition block from the metadata mirror
        file instead of the metadata file (UDF 2.50 2.2.13).
        """
        if part >= len(self._maps):
            raise IsoImageError(f"partition reference {part} out of range")
        kind, pnum = self._maps[part]
        if kind == _MAP_UNSUPPORTED:
            raise IsoImageError(f"partition reference {part} has an unsupported map")
        partition = self._partitions.get(pnum)
        if partition is None:
            raise IsoImageError(f"partition {pnum} has no descriptor")
        if kind == _MAP_PHYSICAL:
            if partition.length and lb >= partition.length:
                raise IsoImageError(f"block {lb} outside partition {pnum}")
            return partition.start + lb
        # Metadata-partition blocks are offsets into the metadata file.
        remaining = lb * SECTOR
        for sector, length in self._mirror_runs if mirror else self._meta_runs:
            if remaining < length:
                if sector is None:
                    raise IsoImageError(f"metadata block {lb} is unallocated")
                return partition.start + sector + remaining // SECTOR
            remaining -= length
        raise IsoImageError(f"metadata block {lb} outside the metadata file")

    def _read_block(self, part: int, lb: int, count: int = 1) -> bytes:
        if part < len(self._maps) and self._maps[part][0] == _MAP_METADATA:
            # Consecutive metadata blocks needn't be adjacent on disc.
            return b"".join(
                _read_sectors(self._f, self._sector_of(part, lb + n))
                for n in range(count)
            )
        return _read_sectors(self._f, self._sector_of(part, lb), count)

    # -- volume structure -------------------------------------------------

    def _find_anchor(self) -> bytes:
        """The anchor volume descriptor pointer: at sector 256, N - 256 or N
        (N the last sector; UDF 2.50 2.2.3), the first sound one."""
        last = self._sectors - 1
        candidates = [256, *(sector for sector in (last - 256, last) if sector > 256)]
        found: list[bytes] = []
        for sector in candidates:
            try:
                buf = _read_sectors(self._f, sector)
            except IsoImageError:
                continue
            if _udf_tag_ok(buf, _TAG_AVDP, sector, crc=True):
                return buf
            if _udf_tag(buf) == _TAG_AVDP:
                found.append(buf)
        if found:
            return found[0]
        raise IsoImageError("no anchor volume descriptor at sector 256, N-256 or N")

    def _read_vds(
        self, length: int, location: int, *, strict: bool
    ) -> tuple[dict[int, _Partition], bytes] | None:
        """Partitions and the logical volume descriptor from one volume
        descriptor sequence, following its pointers (tag 3) and keeping, of
        several copies, the one with the highest sequence number (ECMA-167
        3/8.4.3). *strict* skips descriptors whose tag doesn't check out.
        None when the sequence has no partition or logical volume."""
        partitions: dict[int, tuple[int, _Partition]] = {}
        lvd: tuple[int, bytes] | None = None
        extents = 0
        while length and extents < _MAX_VDS_EXTENTS:
            extents += 1
            next_extent: tuple[int, int] | None = None
            for sector in range(location, location + length // SECTOR):
                try:
                    desc = _read_sectors(self._f, sector)
                except IsoImageError:
                    break
                tag = _udf_tag(desc)
                if strict and not _udf_tag_ok(desc, tag, sector, crc=True):
                    continue
                sequence = _u32(desc, 16)
                if tag == _TAG_PD:
                    pnum = _u16(desc, 22)
                    if pnum not in partitions or partitions[pnum][0] <= sequence:
                        partition = _Partition(_u32(desc, 188), _u32(desc, 192))
                        partitions[pnum] = (sequence, partition)
                elif tag == _TAG_LVD:
                    if lvd is None or lvd[0] <= sequence:
                        lvd = (sequence, desc)
                elif tag == _TAG_VDP:
                    next_extent = (_u32(desc, 20), _u32(desc, 24))
                    break
                elif tag == _TAG_TD:
                    break
            if next_extent is None:
                break
            length, location = next_extent
        if lvd is None or not partitions:
            return None
        return {pnum: entry[1] for pnum, entry in partitions.items()}, lvd[1]

    def _read_volume(self) -> tuple[int, int]:
        self._check_vrs()
        avdp = self._find_anchor()
        main = (_u32(avdp, 16), _u32(avdp, 20))
        reserve = (_u32(avdp, 24), _u32(avdp, 28))
        # The main sequence, else the reserve copy (UDF 2.50 6.9.2.3); then
        # either without the tag checks, for images that skip them.
        volume = (
            self._read_vds(*main, strict=True)
            or self._read_vds(*reserve, strict=True)
            or self._read_vds(*main, strict=False)
            or self._read_vds(*reserve, strict=False)
        )
        if volume is None:
            raise IsoImageError("no logical volume descriptor")
        self._partitions, lvd = volume
        block_size = _u32(lvd, 212)
        if block_size != SECTOR:
            raise IsoImageError(f"unsupported logical block size {block_size}")
        self.label = _udf_dstring(lvd[84:212])

        metadata_map: tuple[int, int, int, int] | None = None
        nmaps = _u32(lvd, 268)
        offset = 440
        for _ in range(nmaps):
            if offset + 2 > len(lvd):
                raise IsoImageError("truncated partition map")
            map_type, map_len = lvd[offset], lvd[offset + 1]
            if map_len == 0 or offset + map_len > len(lvd):
                raise IsoImageError("invalid partition map length")
            if map_type == 1 and map_len >= 6:
                self._maps.append((_MAP_PHYSICAL, _u16(lvd, offset + 4)))
            elif map_type == 2 and map_len >= 40:
                ident = lvd[offset + 5 : offset + 28].rstrip(b"\0")
                pnum = _u16(lvd, offset + 38)
                if ident == _UDF_METADATA_PARTITION and map_len >= 59:
                    metadata_map = (
                        pnum,
                        _u32(lvd, offset + 40),
                        _u32(lvd, offset + 44),
                        lvd[offset + 58],
                    )
                    self._maps.append((_MAP_METADATA, pnum))
                elif ident == _UDF_SPARABLE_PARTITION:
                    # Sparing only relocates defective packets: read in place.
                    self._maps.append((_MAP_PHYSICAL, pnum))
                else:
                    # A virtual (VAT) or unknown map: entries addressed
                    # through it fail; the rest of the volume still reads.
                    self._maps.append((_MAP_UNSUPPORTED, pnum))
            else:
                self._maps.append((_MAP_UNSUPPORTED, -1))
            offset += map_len

        if metadata_map is not None:
            self._map_metadata(*metadata_map)

        fsd_lb, fsd_part = _u32(lvd, 252), _u16(lvd, 256)
        fsd = self._read_block(fsd_part, fsd_lb)
        if _udf_tag(fsd) != _TAG_FSD and self._mirror_runs:
            fsd = _read_sectors(self._f, self._sector_of(fsd_part, fsd_lb, mirror=True))
        if _udf_tag(fsd) != _TAG_FSD:
            raise IsoImageError("bad file set descriptor")
        root_lb, root_part = _u32(fsd, 404), _u16(fsd, 408)
        return root_part, root_lb

    def _map_metadata(
        self, pnum: int, file_lb: int, mirror_lb: int, flags: int
    ) -> None:
        """Map the metadata partition onto its metadata file, or onto the
        mirror when the metadata file's entry is unreadable (UDF 2.50
        2.2.13; libudfread does the same). Unallocated spans are kept as
        gaps, which the spec allows."""
        partition = self._partitions.get(pnum)
        if partition is None:
            raise IsoImageError("UDF partition start not found")
        phys_ref = next(
            (
                i
                for i, (kind, p) in enumerate(self._maps)
                if kind == _MAP_PHYSICAL and p == pnum
            ),
            None,
        )
        if phys_ref is None:
            raise IsoImageError("metadata partition has no physical partition")
        start = partition.start

        def runs(lb: int) -> list[tuple[int | None, int]] | None:
            sector = start + lb
            try:
                fe = _read_sectors(self._f, sector)
                if _udf_tag(fe) not in (_TAG_FE, _TAG_EFE):
                    return None
                meta = self._parse_file_entry(
                    fe, phys_ref, "<metadata>", sector * SECTOR
                )
            except IsoImageError:
                return None
            return [
                (
                    None if run.offset is None else run.offset // SECTOR - start,
                    run.length,
                )
                for run in meta.runs
            ]

        main, mirror = runs(file_lb), runs(mirror_lb)
        if main is None and mirror is None:
            raise IsoImageError("metadata file and its mirror are unreadable")
        if main is None:
            self.skipped.append("<metadata>: metadata file unreadable; used its mirror")
        self._meta_runs = main if main is not None else mirror or []
        self._mirror_runs = mirror if main is not None and mirror else []
        del flags  # either copy can be read whether or not it duplicates

    def _check_vrs(self) -> None:
        """An NSR descriptor within the extended area of the volume
        recognition sequence, which runs from sector 16 to the first blank
        sector; the extended area opens with BEA01 (ECMA-167 2/8.3, 2/9.2).
        ISO 9660's CD001 descriptors may come first on a bridge disc."""
        extended = False
        for sector in range(16, 256):
            try:
                ident = _read_sectors(self._f, sector)[1:6]
            except IsoImageError:
                break
            if ident == b"BEA01":
                extended = True
            elif ident == b"TEA01":
                extended = False
            elif ident in (b"NSR02", b"NSR03") and extended:
                return
            elif ident == b"\0\0\0\0\0":
                break
        raise IsoImageError("no UDF volume recognition sequence")

    # -- files ----------------------------------------------------------------

    def _parse_file_entry(
        self, buf: bytes, part: int, path: str, fe_offset: int
    ) -> _UdfFile:
        """Parse a (extended) file entry read from image byte *fe_offset*."""
        tag = _udf_tag(buf)
        if tag == _TAG_FE:
            ad_base, l_ea_off, mtime_off = 176, 168, 84
        elif tag == _TAG_EFE:
            ad_base, l_ea_off, mtime_off = 216, 208, 92
        else:
            raise IsoImageError(f"{path}: expected a file entry, got tag {tag}")
        file_type = buf[16 + 11]
        alloc_type = _u16(buf, 16 + 18) & 7
        size = int.from_bytes(buf[56:64], "little")
        l_ea, l_ad = _u32(buf, l_ea_off), _u32(buf, l_ea_off + 4)
        start = ad_base + l_ea
        ads = buf[start : start + l_ad]
        modified = _udf_timestamp(buf, mtime_off)
        is_dir = file_type == 4
        if alloc_type == 3:
            return _UdfFile(
                is_dir, size, modified, (), ads[:size], fe_offset + start, file_type
            )
        runs: list[IsoRun] = []
        mirror: list[IsoRun] | None = [] if self._mirror_runs else None
        for ext_part, lb, length, recorded in self._allocation_descriptors(
            ads, alloc_type, part, path
        ):
            offset = self._sector_of(ext_part, lb) * SECTOR if recorded else None
            runs.append(IsoRun(offset, length))
            if mirror is not None:
                in_metadata = (
                    ext_part < len(self._maps)
                    and self._maps[ext_part][0] == _MAP_METADATA
                )
                if in_metadata and recorded:
                    try:
                        sector = self._sector_of(ext_part, lb, mirror=True)
                    except IsoImageError:
                        mirror = None
                        continue
                    mirror.append(IsoRun(sector * SECTOR, length))
                else:
                    mirror.append(IsoRun(offset, length))
        trimmed = _trim_runs(runs, size, path)
        mirror_runs = _trim_runs(mirror, size, path) if mirror is not None else None
        return _UdfFile(
            is_dir, size, modified, trimmed, None, None, file_type, mirror_runs
        )

    def _allocation_descriptors(
        self, ads: bytes, alloc_type: int, part: int, path: str
    ) -> Iterator[tuple[int, int, int, bool]]:
        # short_ad, long_ad, ext_ad (ECMA-167 4/14.14): length (top 2 bits
        # the extent type), then the block, and the partition but in a
        # short_ad; an ext_ad has recorded and information lengths between.
        if alloc_type == 0:
            step, lb_at, part_at = 8, 4, None
        elif alloc_type == 1:
            step, lb_at, part_at = 16, 4, 8
        elif alloc_type == 2:
            step, lb_at, part_at = 20, 12, 16
        else:
            raise IsoImageError(f"{path}: unsupported allocation type {alloc_type}")
        first = True
        for _ in range(_MAX_ENTRIES):
            i = 0
            next_ads: bytes | None = None
            while i + step <= len(ads):
                raw_len, lb = _u32(ads, i), _u32(ads, i + lb_at)
                ext_part = part if part_at is None else _u16(ads, i + part_at)
                length, ext_type = raw_len & 0x3FFFFFFF, raw_len >> 30
                i += step
                if length == 0:
                    if first:
                        # Copy protections add an empty first descriptor so
                        # that readers find the file truncated; libdvdread
                        # skips it (merge request !6).
                        first = False
                        continue
                    break
                first = False
                if ext_type == 3:
                    # Continuation: the rest of the descriptors live in an AED.
                    aed = self._read_block(ext_part, lb, -(-length // SECTOR))
                    if _udf_tag(aed) != _TAG_AED:
                        raise IsoImageError(f"{path}: bad allocation extent")
                    l_ad = _u32(aed, 20)
                    next_ads = aed[24 : 24 + l_ad]
                    break
                yield ext_part, lb, length, ext_type == 0
            if next_ads is None:
                return
            ads = next_ads
        raise IsoImageError(f"{path}: allocation extent chain too long")

    def _file_at(self, part: int, lb: int, path: str) -> _UdfFile:
        """The file entry at block *lb*, following indirect entries (tag
        259, ICB strategy 4096) and, in a metadata partition, reading the
        metadata mirror when the metadata file's block holds no entry."""
        for _ in range(_MAX_INDIRECT):
            sector = self._sector_of(part, lb)
            buf = _read_sectors(self._f, sector)
            tag = _udf_tag(buf)
            if tag not in (_TAG_FE, _TAG_EFE, _TAG_IE) and self._mirror_runs:
                kind = self._maps[part][0] if part < len(self._maps) else ""
                if kind == _MAP_METADATA:
                    sector = self._sector_of(part, lb, mirror=True)
                    buf = _read_sectors(self._f, sector)
                    tag = _udf_tag(buf)
            if tag != _TAG_IE:
                return self._parse_file_entry(buf, part, path, sector * SECTOR)
            # Indirect entry: the next ICB is the long_ad at byte 36.
            lb, part = _u32(buf, 40), _u16(buf, 44)
        raise IsoImageError(f"{path}: indirect entry chain too long")

    def _file_bytes(self, node: _UdfFile, path: str, *, mirror: bool = False) -> bytes:
        if node.embedded is not None:
            return node.embedded
        out = bytearray()
        for run in node.mirror_runs or () if mirror else node.runs:
            if run.offset is None:
                out += bytes(run.length)
                continue
            self._f.seek(run.offset)
            chunk = self._f.read(run.length)
            if len(chunk) != run.length:
                raise IsoImageError(f"{path}: image truncated")
            out += chunk
        return bytes(out)

    def _children(
        self, node: _UdfFile, path: str
    ) -> Iterator[tuple[str | IsoImageError, int, int]]:
        """A directory's entries: (name, partition, block). A name that won't
        decode comes as the error instead, so the rest can still be listed;
        a bad identifier descriptor ends the listing (the next one can't be
        found)."""
        data = self._file_bytes(node, path)
        if node.mirror_runs and data and _udf_tag(data) != _TAG_FID:
            # The metadata file's copy is damaged; the mirror's may not be.
            data = self._file_bytes(node, path, mirror=True)
        i = 0
        while i + 38 <= len(data):
            if _udf_tag(data, i) != _TAG_FID:
                raise IsoImageError(f"{path or '/'}: bad file identifier at {i}")
            characteristics, l_fi = data[i + 18], data[i + 19]
            icb_lb, icb_part = _u32(data, i + 24), _u16(data, i + 28)
            l_iu = _u16(data, i + 36)
            name_start = i + 38 + l_iu
            name_raw = data[name_start : name_start + l_fi]
            i += (38 + l_iu + l_fi + 3) & ~3
            # Skip the parent entry (0x08) and deleted entries (0x04).
            if characteristics & 0x0C:
                continue
            try:
                name: str | IsoImageError = _udf_dchars(name_raw)
            except IsoImageError as exc:
                name = exc
            yield name, icb_part, icb_lb

    def files(self) -> list[IsoEntry]:
        """Every regular file. An entry that can't be read is skipped and
        noted in ``skipped`` (with the reason), as libudfread does, rather
        than losing the whole tree; only an unreadable root is fatal."""
        entries: list[IsoEntry] = []
        # Directories already walked, so a directory linked twice can't loop;
        # a file linked under two names is listed under both.
        seen_dirs: set[tuple[int, int]] = {self._root}
        stack: list[tuple[str, _UdfFile]] = [("", self._file_at(*self._root, ""))]
        count = 0
        while stack:
            dir_path, node = stack.pop()
            for name, part, lb, child in self._readable_children(node, dir_path):
                count += 1
                _check_entry_count(count)
                path = f"{dir_path}/{name}" if dir_path else name
                if child is None:
                    try:
                        child = self._file_at(part, lb, path)
                    except IsoImageError as exc:
                        self.skipped.append(str(exc))
                        continue
                if child.is_dir:
                    if (part, lb) not in seen_dirs:
                        seen_dirs.add((part, lb))
                        stack.append((path, child))
                elif child.file_type in _SPECIAL_FILE_TYPES:
                    continue
                else:
                    runs = child.runs
                    if child.embedded_offset is not None and child.size:
                        # Tiny files live inside their own file entry.
                        runs = (IsoRun(child.embedded_offset, child.size),)
                    entries.append(IsoEntry(path, child.size, child.modified, runs))
        entries.sort(key=lambda entry: entry.path)
        return entries

    def _readable_children(
        self, node: _UdfFile, dir_path: str
    ) -> Iterator[tuple[str, int, int, _UdfFile | None]]:
        """``_children`` with its failures noted in ``skipped``."""
        try:
            for name, part, lb in self._children(node, dir_path):
                if isinstance(name, IsoImageError):
                    self.skipped.append(f"{dir_path or '/'}: {name}")
                    continue
                yield name, part, lb, None
        except IsoImageError as exc:
            self.skipped.append(str(exc))


# =============================================================================
# ISO9660 / Joliet
# =============================================================================


def _iso_timestamp(record: bytes) -> datetime | None:
    """Decode the 7-byte directory-record timestamp (ECMA-119 9.1.5)."""
    year, month, day, hour, minute, second, tz = struct.unpack_from(
        "<BBBBBBb", record, 18
    )
    try:
        return datetime(
            1900 + year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone(timedelta(minutes=15 * tz)),
        )
    except ValueError:
        return None


def _iso_name(raw: bytes, joliet: bool) -> str:
    name = raw.decode("utf-16-be") if joliet else raw.decode("latin-1")
    name = name.split(";", 1)[0]
    return name[:-1] if name.endswith(".") else name


class _Iso9660Reader:
    def __init__(self, f: _Readable) -> None:
        self._f = f
        pvd: bytes | None = None
        joliet: bytes | None = None
        for sector in range(16, 64):
            desc = _read_sectors(f, sector)
            if desc[1:6] != b"CD001":
                break
            if desc[0] == 1 and pvd is None:
                pvd = desc
            elif desc[0] == 2 and desc[88:91] in (b"%/@", b"%/C", b"%/E"):
                joliet = desc
            elif desc[0] == 255:
                break
        if pvd is None:
            raise IsoImageError("no primary volume descriptor")
        self._joliet = joliet is not None
        volume = joliet or pvd
        if _u16(volume, 128) != SECTOR:
            raise IsoImageError("unsupported ISO9660 block size")
        label_raw = volume[40:72]
        self.label = (
            label_raw.decode("utf-16-be", "replace")
            if self._joliet
            else label_raw.decode("latin-1")
        ).strip()
        self._root = volume[156:190]
        self._primary_root = pvd[156:190]

    def _records(self, extent: int, size: int) -> Iterator[bytes]:
        data = _read_sectors(self._f, extent, -(-size // SECTOR))[:size]
        i = 0
        while i < len(data):
            length = data[i]
            if length == 0:
                # Records never straddle sectors; skip the padding.
                i = (i // SECTOR + 1) * SECTOR
                continue
            yield data[i : i + length]
            i += length

    def files(self) -> list[IsoEntry]:
        """The Joliet tree's files, or the primary tree's when the Joliet one
        won't read (they describe the same files; Joliet has long names)."""
        if self._joliet:
            try:
                return self._walk(self._root, joliet=True)
            except IsoImageError:
                pass
        return self._walk(self._primary_root, joliet=False)

    def _walk(self, root: bytes, *, joliet: bool) -> list[IsoEntry]:
        entries: dict[str, IsoEntry] = {}
        visited: set[int] = set()
        # A directory's records start after its extended attribute record.
        root_extent = _u32(root, 2) + root[1]
        stack: list[tuple[str, int, int]] = [("", root_extent, _u32(root, 10))]
        while stack:
            dir_path, extent, size = stack.pop()
            if extent in visited:
                continue
            visited.add(extent)
            _check_entry_count(len(visited) + len(entries))
            pending: IsoEntry | None = None
            for record in self._records(extent, size):
                if len(record) < 33:
                    raise IsoImageError("truncated ISO9660 directory record")
                name_len = record[32]
                name_raw = record[33 : 33 + name_len]
                if name_raw in (b"\0", b"\1"):
                    continue
                flags = record[25]
                if flags & 0x04:
                    # An associated file (ECMA-119 9.1.6) shares its main
                    # file's name; it isn't the file.
                    continue
                # The data follows the extended attribute record, if any.
                rec_extent = _u32(record, 2) + record[1]
                rec_size = _u32(record, 10)
                path_name = _iso_name(name_raw, joliet)
                path = f"{dir_path}/{path_name}" if dir_path else path_name
                if flags & 0x02:
                    stack.append((path, rec_extent, rec_size))
                    continue
                runs = _iso_runs(rec_extent, rec_size, record[26], record[27])
                if pending is not None and pending.path == path:
                    # Multi-extent file (> 4 GiB): later records continue it.
                    pending = IsoEntry(
                        path,
                        pending.size + rec_size,
                        pending.modified,
                        _merge_runs([*pending.runs, *runs]),
                    )
                else:
                    pending = IsoEntry(path, rec_size, _iso_timestamp(record), runs)
                if not flags & 0x80:
                    entries[path] = pending
                    pending = None
        return sorted(entries.values(), key=lambda entry: entry.path)


def _iso_runs(extent: int, size: int, unit: int, gap: int) -> tuple[IsoRun, ...]:
    """A file section's data: contiguous, or for an interleaved file (ECMA-119
    9.1.7-9.1.8) *unit* sectors at a time with *gap* sectors between."""
    if not unit:
        return (IsoRun(extent * SECTOR, size),)
    runs: list[IsoRun] = []
    sector, remaining = extent, size
    while remaining > 0:
        length = min(unit * SECTOR, remaining)
        runs.append(IsoRun(sector * SECTOR, length))
        remaining -= length
        sector += unit + gap
    return tuple(runs)
