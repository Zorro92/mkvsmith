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
_TAG_PD = 5
_TAG_LVD = 6
_TAG_TD = 8
_TAG_FSD = 256
_TAG_FID = 257
_TAG_AED = 258
_TAG_FE = 261
_TAG_EFE = 266

_UDF_METADATA_PARTITION = b"*UDF Metadata Partition"


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
            self.filesystem, self.label, entries = _read_filesystem(self._file)
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
    ) -> None:
        """Write *entry* (or its first *limit* bytes) to *dest*.

        *before_chunk* runs before each chunk is copied; raising from it
        stops the copy (and the exception propagates).
        """
        total = entry.size if limit is None else min(limit, entry.size)
        with dest.open("wb") as out:
            for offset in range(0, total, _COPY_CHUNK):
                if before_chunk is not None:
                    before_chunk()
                out.write(self.read(entry, offset, min(_COPY_CHUNK, total - offset)))


def _read_filesystem(f: _Readable) -> tuple[str, str, list[IsoEntry]]:
    errors: list[str] = []
    try:
        udf = _UdfReader(f)
        return "udf", udf.label, udf.files()
    except IsoImageError as exc:
        errors.append(f"UDF: {exc}")
    try:
        iso = _Iso9660Reader(f)
        return "iso9660", iso.label, iso.files()
    except IsoImageError as exc:
        errors.append(f"ISO9660: {exc}")
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


class _UdfReader:
    def __init__(self, f: _Readable) -> None:
        self._f = f
        self._part_start: dict[int, int] = {}
        # One entry per logical-volume partition map: (is_metadata, partition number).
        self._maps: list[tuple[bool, int]] = []
        # Physical (sector, length) runs of the metadata file, in order.
        self._meta_runs: list[tuple[int, int]] = []
        self.label = ""
        self._root = self._read_volume()

    # -- addressing ---------------------------------------------------------

    def _sector_of(self, part: int, lb: int) -> int:
        """Absolute image sector of logical block *lb* in partition ref *part*."""
        if part >= len(self._maps):
            raise IsoImageError(f"partition reference {part} out of range")
        is_meta, pnum = self._maps[part]
        start = self._part_start.get(pnum)
        if start is None:
            raise IsoImageError(f"partition {pnum} has no descriptor")
        if not is_meta:
            return start + lb
        # Metadata-partition blocks are offsets into the metadata file.
        remaining = lb * SECTOR
        for sector, length in self._meta_runs:
            if remaining < length:
                return start + sector + remaining // SECTOR
            remaining -= length
        raise IsoImageError(f"metadata block {lb} outside the metadata file")

    def _read_block(self, part: int, lb: int, count: int = 1) -> bytes:
        return _read_sectors(self._f, self._sector_of(part, lb), count)

    # -- volume structure -------------------------------------------------

    def _read_volume(self) -> tuple[int, int]:
        self._check_vrs()
        avdp = _read_sectors(self._f, 256)
        if _udf_tag(avdp) != _TAG_AVDP:
            raise IsoImageError("no anchor volume descriptor at sector 256")
        vds_len, vds_loc = _u32(avdp, 16), _u32(avdp, 20)
        lvd: bytes | None = None
        for i in range(vds_len // SECTOR):
            desc = _read_sectors(self._f, vds_loc + i)
            tag = _udf_tag(desc)
            if tag == _TAG_PD:
                pnum = _u16(desc, 22)
                self._part_start[pnum] = _u32(desc, 188)
            elif tag == _TAG_LVD:
                lvd = desc
            elif tag == _TAG_TD:
                break
        if lvd is None:
            raise IsoImageError("no logical volume descriptor")
        block_size = _u32(lvd, 212)
        if block_size != SECTOR:
            raise IsoImageError(f"unsupported logical block size {block_size}")
        self.label = _udf_dstring(lvd[84:212])

        meta_file_lb: int | None = None
        nmaps = _u32(lvd, 268)
        offset = 440
        for _ in range(nmaps):
            map_type, map_len = lvd[offset], lvd[offset + 1]
            if map_type == 1:
                pnum = _u16(lvd, offset + 4)
                self._maps.append((False, pnum))
            elif map_type == 2:
                ident = lvd[offset + 5 : offset + 28].rstrip(b"\0")
                if ident != _UDF_METADATA_PARTITION:
                    raise IsoImageError(f"unsupported partition map {ident!r}")
                pnum = _u16(lvd, offset + 38)
                meta_file_lb = _u32(lvd, offset + 40)
                self._maps.append((True, pnum))
            else:
                raise IsoImageError(f"unknown partition map type {map_type}")
            if map_len == 0:
                raise IsoImageError("zero-length partition map")
            offset += map_len

        if meta_file_lb is not None:
            pnum = next(p for is_meta, p in self._maps if is_meta)
            fe_sector = self._part_start[pnum] + meta_file_lb
            fe = _read_sectors(self._f, fe_sector)
            phys_ref = next(
                (i for i, (m, p) in enumerate(self._maps) if not m and p == pnum), None
            )
            if phys_ref is None:
                raise IsoImageError("metadata partition has no physical partition")
            meta = self._parse_file_entry(
                fe, phys_ref, "<metadata>", fe_sector * SECTOR
            )
            for run in meta.runs:
                if run.offset is None:
                    raise IsoImageError("sparse metadata file")
                sector = run.offset // SECTOR - self._part_start[pnum]
                self._meta_runs.append((sector, run.length))

        fsd_lb, fsd_part = _u32(lvd, 252), _u16(lvd, 256)
        fsd = self._read_block(fsd_part, fsd_lb)
        if _udf_tag(fsd) != _TAG_FSD:
            raise IsoImageError("bad file set descriptor")
        root_lb, root_part = _u32(fsd, 404), _u16(fsd, 408)
        return root_part, root_lb

    def _check_vrs(self) -> None:
        for sector in range(16, 32):
            ident = _read_sectors(self._f, sector)[1:6]
            if ident in (b"NSR02", b"NSR03"):
                return
            if ident == b"\0\0\0\0\0":
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
            return _UdfFile(is_dir, size, modified, (), ads[:size], fe_offset + start)
        runs: list[IsoRun] = []
        for ext_part, lb, length, recorded in self._allocation_descriptors(
            ads, alloc_type, part, path
        ):
            offset = self._sector_of(ext_part, lb) * SECTOR if recorded else None
            runs.append(IsoRun(offset, length))
        # The last extent may be padded to a block boundary; trim to the size.
        trimmed: list[IsoRun] = []
        remaining = size
        for run in runs:
            if remaining <= 0:
                break
            trimmed.append(IsoRun(run.offset, min(run.length, remaining)))
            remaining -= run.length
        if remaining > 0:
            raise IsoImageError(f"{path}: allocation shorter than file size")
        return _UdfFile(is_dir, size, modified, _merge_runs(trimmed), None, None)

    def _allocation_descriptors(
        self, ads: bytes, alloc_type: int, part: int, path: str
    ) -> Iterator[tuple[int, int, int, bool]]:
        if alloc_type == 0:
            step = 8
        elif alloc_type == 1:
            step = 16
        else:
            raise IsoImageError(f"{path}: unsupported allocation type {alloc_type}")
        for _ in range(_MAX_ENTRIES):
            i = 0
            next_ads: bytes | None = None
            while i + step <= len(ads):
                raw_len, lb = _u32(ads, i), _u32(ads, i + 4)
                ext_part = part if step == 8 else _u16(ads, i + 8)
                length, ext_type = raw_len & 0x3FFFFFFF, raw_len >> 30
                i += step
                if length == 0:
                    break
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
        sector = self._sector_of(part, lb)
        buf = _read_sectors(self._f, sector)
        return self._parse_file_entry(buf, part, path, sector * SECTOR)

    def _file_bytes(self, node: _UdfFile, path: str) -> bytes:
        if node.embedded is not None:
            return node.embedded
        out = bytearray()
        for run in node.runs:
            if run.offset is None:
                out += bytes(run.length)
                continue
            self._f.seek(run.offset)
            chunk = self._f.read(run.length)
            if len(chunk) != run.length:
                raise IsoImageError(f"{path}: image truncated")
            out += chunk
        return bytes(out)

    def _children(self, node: _UdfFile, path: str) -> Iterator[tuple[str, int, int]]:
        data = self._file_bytes(node, path)
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
            yield _udf_dchars(name_raw), icb_part, icb_lb

    def files(self) -> list[IsoEntry]:
        entries: list[IsoEntry] = []
        seen: set[tuple[int, int]] = set()
        stack: list[tuple[str, _UdfFile]] = [("", self._file_at(*self._root, ""))]
        while stack:
            dir_path, node = stack.pop()
            for name, part, lb in self._children(node, dir_path):
                if (part, lb) in seen:
                    continue
                seen.add((part, lb))
                _check_entry_count(len(seen))
                path = f"{dir_path}/{name}" if dir_path else name
                child = self._file_at(part, lb, path)
                if child.is_dir:
                    stack.append((path, child))
                else:
                    runs = child.runs
                    if child.embedded_offset is not None and child.size:
                        # Tiny files live inside their own file entry.
                        runs = (IsoRun(child.embedded_offset, child.size),)
                    entries.append(IsoEntry(path, child.size, child.modified, runs))
        entries.sort(key=lambda entry: entry.path)
        return entries


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
        entries: dict[str, IsoEntry] = {}
        visited: set[int] = set()
        root_extent, root_size = (
            _u32(self._root, 2),
            (_u32(self._root, 10)),
        )
        stack: list[tuple[str, int, int]] = [("", root_extent, root_size)]
        while stack:
            dir_path, extent, size = stack.pop()
            if extent in visited:
                continue
            visited.add(extent)
            _check_entry_count(len(visited) + len(entries))
            pending: IsoEntry | None = None
            for record in self._records(extent, size):
                name_len = record[32]
                name_raw = record[33 : 33 + name_len]
                if name_raw in (b"\0", b"\1"):
                    continue
                flags = record[25]
                rec_extent = _u32(record, 2)
                rec_size = _u32(record, 10)
                path_name = _iso_name(name_raw, self._joliet)
                path = f"{dir_path}/{path_name}" if dir_path else path_name
                if flags & 0x02:
                    stack.append((path, rec_extent, rec_size))
                    continue
                run = IsoRun(rec_extent * SECTOR, rec_size)
                if pending is not None and pending.path == path:
                    # Multi-extent file (> 4 GiB): later records continue it.
                    pending = IsoEntry(
                        path,
                        pending.size + rec_size,
                        pending.modified,
                        _merge_runs([*pending.runs, run]),
                    )
                else:
                    pending = IsoEntry(path, rec_size, _iso_timestamp(record), (run,))
                if not flags & 0x80:
                    entries[path] = pending
                    pending = None
        return sorted(entries.values(), key=lambda entry: entry.path)
