# Disc filesystems (UDF and ISO 9660)

How mkvsmith reads a disc image's files without mounting it: `isofs.py`
for UDF and the ISO 9660 fallback, and the parts of `disc_reader.py` and
`drives.py` that touch raw sectors. Every image is a "cooked" dump of
2048-byte sectors, with no headers.

## Sources

| Letter | Source | Where |
|---|---|---|
| E167 | ECMA-167 3rd edition (1997): volume and file structure | `.agents/specs/udf/`; https://ecma-international.org/wp-content/uploads/ECMA-167_3rd_edition_june_1997.pdf |
| E119 | ECMA-119 4th edition (2019): ISO 9660, Joliet in Annex C | `.agents/specs/udf/`; https://ecma-international.org/wp-content/uploads/ECMA-119_4th_edition_june_2019.pdf |
| U102, U250, U260 | OSTA UDF 1.02, 2.50, 2.60 | `.agents/specs/udf/`; http://www.osta.org/specs/ |
| LUR | libudfread (LGPL-2.1+), `udf_volume.c`, `udf_fs.c`, `ecma167.c` @ `b0bc6957` | https://code.videolan.org/videolan/libudfread |
| LDR | libdvdread (GPL-2.0+), `dvd_udf.c` @ `0f30df53`; merge request !6, issue #12 | https://code.videolan.org/videolan/libdvdread |
| LDC | libdvdcss (GPL-2.0+), `libdvdcss.c` @ `811ce97a` | https://code.videolan.org/videolan/libdvdcss |
| LX | Linux `fs/udf`, `fs/isofs` (GPL-2.0-only) | https://github.com/torvalds/linux |

ECMA's terms allow citing and paraphrasing, not copying; OSTA allows
unchanged copies. Linux is GPL-2.0-only, so it is cited, never copied.

## Which filesystem, on which disc

| Disc | Filesystem | Notes |
|---|---|---|
| DVD-Video | UDF 1.02 (ECMA-167 2nd edition, `NSR02`), usually with ISO 9660 alongside (a "bridge" disc) | players read only UDF (U250 6.9) |
| HD DVD | UDF 2.50 | no public profile found |
| BD-ROM | UDF 2.50, read-only partition, metadata partition required | U260 6.16.1, 6.16.4 |
| BD-RE | UDF 2.50, access type 4 | U260 6.16.2 |
| BD-R | UDF 2.60 with pseudo-overwrite (access type 0), else UDF 2.50 | U260 6.16.3 |

DVD-Video files are each one extent under 1 GiB, with strategy-4 file
entries and 8-bit names, directly in `VIDEO_TS` (U250 6.9.1). BDMV forbids
hard links, symbolic links, multisession and the VAT (U260 6.16.4); BD has
no sparing table and BD-ROM no metadata bitmap (U260 6.16, 6.16.1). M2TS
files over 4 GiB are routine.

## UDF

### The chain to the root directory

1. **Volume recognition sequence**, from sector 16, one descriptor per
   sector: `BEA01`, then `NSR02` or `NSR03`, then `TEA01`. ISO 9660's
   `CD001` descriptors may come first on a bridge disc. (E167 2/9) LUR scans
   sectors 16–255 and accepts an NSR only after `BEA01`.
2. **Anchor volume descriptor pointer** in at least two of sector 256,
   N−256 and N, where N is the last sector (U102, U250 2.2.3). DVD-Video
   records 256 and N; a player needs only 256 (U250 6.9.2.2). BD-ROM should
   record all three (U260 6.16.1). It points at the main volume descriptor
   sequence (length at byte 16, location at 20) and the reserve copy (24).
3. **Volume descriptor sequence**: among others, the partition descriptor
   (tag 5) and logical volume descriptor (tag 6). A volume descriptor
   pointer (tag 3) continues the sequence elsewhere. Where several copies of
   a descriptor exist, the one with the highest sequence number (bytes
   16–19) prevails. If the main sequence can't be read, the reserve is used.
   (E167 3/8.4, 3/10.3; U250 6.9.2.3)
4. **File set descriptor** (tag 256), found through the logical volume
   descriptor's contents-use field. LDR scanned the partition for it until
   2026, when it switched to this pointer because some authoring tools put
   the descriptor at the end of the partition.
5. **Root directory**: the file set descriptor's ICB at byte 400.

| Descriptor | Field | Offset |
|---|---|---|
| Partition (tag 5) | partition number (u16) | 22 |
| | access type (u32) | 184 |
| | starting location (u32) | 188 |
| | length (u32) | 192 |
| Logical volume (tag 6) | logical volume identifier (dstring 128) | 84 |
| | logical block size (u32), 2048 on every video disc | 212 |
| | domain identifier (`*OSTA UDF Compliant`, revision in its suffix) | 216 |
| | file set descriptor location (long_ad) | 248 |
| | map table length, number of partition maps | 264, 268 |
| | partition maps | 440 |
| File set (tag 256) | root directory ICB (long_ad) | 400 |

(E167 3/10.5, 3/10.6, 4/14.1.)

### Partition maps

Type 1 is 6 bytes: type, length, volume sequence number (u16 at 2),
partition number (u16 at 4). Type 2 is 64 bytes with an entity identifier
at 4 naming its kind: `*UDF Virtual Partition` (the VAT, for write-once
media), `*UDF Sparable Partition` (defect management), or
`*UDF Metadata Partition`. (E167 3/10.7; U250 2.2.8–2.2.10) LUR skips map
types it doesn't support instead of failing.

### Descriptor tags

Every descriptor starts with a 16-byte tag (E167 3/7.2):

| Offset | Field |
|---|---|
| 0 | tag identifier (u16) |
| 2 | descriptor version: 2 (`NSR02`) or 3 (`NSR03`) |
| 4 | checksum: sum of bytes 0–3 and 5–15, mod 256 |
| 6 | tag serial number |
| 8 | CRC-ITU-T (x¹⁶+x¹²+x⁵+1, initial 0) over the CRC-length bytes after the tag |
| 10 | CRC length (0: no CRC) |
| 12 | tag location: the block that holds the descriptor |

The tag location makes a tag that checks out "extremely likely" to be a real
descriptor (E167 3/7.2, note 5). UDF requires CRCs on every descriptor
except space bitmaps (U250 2.1). Readers differ:

| Reader | Checks |
|---|---|
| LX | location, checksum, version, CRC |
| LUR | checksum, version, CRC; on a bad metadata block, retries from the mirror |
| LDR | nothing ("TODO: check CRC") |
| `isofs.py` | the identifier only |

LDR is the DVD reader under VLC and other software players, so DVD images
with bad CRCs can exist and still play.

### File entries

| Field | File entry (tag 261) | Extended file entry (tag 266) |
|---|---|---|
| ICB tag | 16 | 16 |
| information length (u64) | 56 | 56 |
| modification time | 84 | 92 |
| extended attributes length | 168 | 208 |
| allocation descriptors length | 172 | 212 |
| extended attributes, then allocation descriptors | 176 | 216 |

The ICB tag (E167 4/14.6) gives the strategy (byte +4: 4, or 4096 for a
chain through indirect entries, tag 259), the file type (+11) and the
allocation descriptor type (+18, bits 0–2). File types: 4 directory,
5 file, 12 symbolic link, 13 stream directory; UDF adds 248 VAT, 249
real-time file, 250 metadata file, 251 metadata mirror, 252 metadata bitmap
(U250 2.3.5.2). DVD-Video and BD-R use strategy 4 only.

### Allocation descriptors

| Type | Size | Layout |
|---|---|---|
| 0 short_ad | 8 | length, block (partition implied) |
| 1 long_ad | 16 | length, block, partition reference, 6 bytes implementation use |
| 2 ext_ad | 20 | |
| 3 | | data embedded in the entry |

The length's top two bits give the extent type: 0 recorded, 1 allocated but
not recorded, 2 neither, 3 the next extent of descriptors (an allocation
extent descriptor, tag 258, at most one block, with its descriptors from
byte 24). The low 30 bits are the length in bytes; a zero length ends the
list. An extent is under 1 GiB, so any larger file has several. (E167
4/14.14, 4/14.5; U250 2.3.10)

### File identifier descriptors

A directory's data is a list of these (tag 257, E167 4/14.4):
characteristics at 18 (bit 0 hidden, 1 directory, 2 deleted, 3 parent),
identifier length at 19, the entry's ICB (long_ad) at 20, implementation-use
length at 36, then that and the identifier, padded to a multiple of 4.

Names are OSTA CS0: the first byte is 8 (one byte per character) or 16
(UTF-16 big-endian), with no byte-order mark; 254 and 255 occur only on
deleted entries (U250 2.1.1). DVD-Video uses 8-bit names (U250 6.9.1).

Timestamps (E167 1/7.3): a type-and-timezone word (signed 12-bit minutes,
−2047 when unspecified), then year, month, day, hour, minute, second,
centiseconds, hundreds of microseconds, microseconds.

### The metadata partition

On BD and other UDF 2.50 volumes the file entries, directories and file set
descriptor live in a virtual partition: its block *n* is at byte *n* × 2048
of the *metadata file*, an ordinary extended file entry in the physical
partition. (U250 2.2.13)

| Offset in the 64-byte map | Field |
|---|---|
| 36 | volume sequence number |
| 38 | partition number |
| 40 | metadata file location |
| 44 | metadata mirror file location |
| 48 | metadata bitmap file location |
| 52 | allocation unit size |
| 56 | alignment unit size |
| 58 | flags; bit 0: the mirror is a full copy |

(U250 2.2.10.) The metadata files' extents are short_ads in the same
physical partition, "recorded" or "not allocated" (U250 2.2.13.1). With the
flag clear the mirror points at the same blocks; with it set, either copy
can be read and the main file is primary. LUR reads the mirror when the
main entry or a block fails, but maps only the first extent of the metadata
file.

## ISO 9660 and Joliet

Volume descriptors from sector 16, with `CD001` at byte 1: type 1 primary,
2 supplementary, 255 terminator. The primary's volume identifier is at 40
(32 bytes), its block size at 128 and its root directory record at 156.
Joliet is a supplementary descriptor whose escape sequence at 88 is `%/@`,
`%/C` or `%/E`, with UCS-2 big-endian names. (E119 8.4, Annex C)

Directory records (E119 9.1):

| Offset | Field |
|---|---|
| 0 | record length |
| 1 | extended attribute record length |
| 2 | extent, both-endian |
| 10 | data length, both-endian |
| 18 | date (7 bytes; GMT offset in 15-minute units) |
| 25 | flags: bit 1 directory, bit 2 associated file, bit 7 multi-extent |
| 26, 27 | file unit size, interleave gap |
| 32 | name length; the name follows |

An extent holds at most 4 GiB − 1; a larger file is several records with the
same name, all but the last flagged multi-extent (level 3). File data starts
after the extended attribute record. Records don't cross a sector boundary.

## Which side to trust on a DVD

Copy protection falsifies one filesystem or both:

- **Bogus backup file.** LDR issue #12 describes a retail DVD whose UDF
  side lists a backup (`.BUP`) of 4.2 GB that holds a title set's video,
  while that set's VOB entries are missing.
- **Empty first descriptor.** Some protections put an empty allocation
  descriptor first, so readers truncate the file; LDR keeps the first
  descriptor with a non-zero location (merge request !6).
- **Decoy ISO 9660.** Some discs' ISO 9660 side lists files no player uses
  (a 2002 Linux kernel list thread; secondary).

Players follow the IFO's sector addresses, so the trust order is: the IFO's
sectors, then UDF, then ISO 9660.

`isofs.py` keeps to that order when an entry is damaged: a file entry or
directory it can't read is skipped and listed in `IsoImage.skipped`, and the
rest of the UDF tree is used, as LUR skips bad directory entries. A name
that won't decode skips just that entry; a bad identifier descriptor ends
its directory's listing, since the next one can't be found. Only an
unreadable root, or a UDF tree with no files at all, falls back to
ISO 9660. Opening an image with skipped entries logs a warning.

## The CSS check

`disc_reader.css_scrambled_packets` reads the PES scrambling-control bits
(`0x30`) of the byte after the PES header, as LDC does (`buf[0x14] & 0x30`
for a sector whose pack header has no stuffing). mkvsmith also allows for
pack stuffing (`14 + (data[13] & 7)`), skips system headers, navigation and
padding packets, and counts only `0xBD` and `0xC0`–`0xEF` packets.

## Where `isofs.py` departs from the references on purpose

These go beyond a reference, and shouldn't be "fixed" to match one:

- **Metadata file extents:** all of them are mapped; LUR maps only the
  first.
- **Extended file entries:** they're read; LDR reads only tag 261.
- **File sizes:** taken from the information length; LDR uses the first
  extent's length.
- **Large files:** many allocation descriptors and chained allocation
  extents are followed; LDR uses one descriptor.
- **Fragmented directories:** read; LUR refuses them.

Sources disagree on the volume label: `isofs.py` takes the logical volume
identifier; LUR returns the UDF primary volume descriptor's identifier
(byte 24).

## Open parser mismatches

- **Metadata mirror never read.** A "not allocated" extent in the metadata
  file raises an error, though the spec allows one.
- **No tag validation.** Checksum, version, CRC and tag location aren't
  checked.
- **Anchors and sequences.** Only the anchor at 256 and the main sequence
  are read; volume descriptor pointers aren't followed; the last descriptor
  seen wins, not the highest sequence number.
- **Empty first descriptor.** LDR's workaround is missing.
- **Indirect entries.** Strategy 4096 (tag 259) isn't followed.
- **No bounds checks.** Block numbers aren't checked against the partition
  length or the image size.
- **Hard links.** De-duplication covers files as well as directories, so a
  second path to a file is dropped.
- **Map types.** VAT and sparable maps raise errors instead of being
  skipped.
- **Multi-block reads.** Reads in the metadata partition ignore extent
  boundaries.
- **Recognition.** Only sectors 16–31 are checked, and `BEA01` isn't
  required.
- **File types.** Symbolic links and other special types are listed as
  files, and ext_ad raises an error.
- **ISO 9660.** The extended attribute record, associated files and
  interleaving are ignored, and Joliet always wins over the primary tree.
