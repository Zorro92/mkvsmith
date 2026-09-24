"""TheDiscDB lookup and contribution support.

TheDiscDB is used strictly as an optional enhancement: every lookup and
contribution error is reported and ignored/returned rather than invalidating a
successful local disc scan. HTTP access uses only the standard library so no
new runtime dependency is added.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast
from collections.abc import Iterable

from i18n import tr
from models import DiscDbOptions, DiscMetadata, Stream, StreamType, Title


DISCDB_LOOKUP_QUERY = """
query DiscDbLookup($where: MediaItemFilterInput!) {
  mediaItems(where: $where, first: 20) {
    nodes {
      title
      year
      slug
      type
      externalids { tmdb imdb }
      releases {
        slug
        title
        year
        locale
        regionCode
        upc
        discs {
          index
          name
          format
          slug
          contentHash
          globalDiscId
          fingerprint
          titles {
            index
            duration
            displaySize
            sourceFile
            size
            segmentMap
            filename
            item {
              ... on DiscItemReference {
                title
                season
                episode
                type
              }
            }
          }
        }
      }
    }
  }
}
"""
STATUS_POLL_ATTEMPTS = 60


class GraphQLError(TypedDict):
    message: str


class DiscHashData(TypedDict):
    hash: str
    fingerprint: str


class DiscDbExternalIds(TypedDict):
    tmdb: str | None
    imdb: str | None


class DiscDbItemReference(TypedDict):
    title: str | None
    season: int | None
    episode: int | None
    type: str | None


class DiscDbRemoteTitle(TypedDict, total=False):
    index: int
    duration: str | None
    displaySize: str | None
    sourceFile: str | None
    size: int | None
    segmentMap: str | None
    filename: str | None
    item: DiscDbItemReference | None


class DiscDbRemoteDisc(TypedDict):
    index: int
    name: str | None
    format: str | None
    slug: str | None
    contentHash: str | None
    globalDiscId: str | None
    fingerprint: str | None
    titles: list[DiscDbRemoteTitle]


class DiscDbRemoteRelease(TypedDict):
    slug: str
    title: str | None
    year: int | None
    locale: str | None
    regionCode: str | None
    upc: str | None
    discs: list[DiscDbRemoteDisc]


class DiscDbMediaItem(TypedDict):
    title: str
    year: int
    slug: str
    type: str | None
    externalids: DiscDbExternalIds | None
    releases: list[DiscDbRemoteRelease]


class DiscDbMediaItemConnection(TypedDict):
    nodes: list[DiscDbMediaItem]


class DiscDbLookupData(TypedDict):
    mediaItems: DiscDbMediaItemConnection


class HashDiscPayload(TypedDict):
    discHash: DiscHashData | None
    errors: list[GraphQLError] | None


class CreateDiscPayload(TypedDict):
    userContributionDisc: dict[str, Any] | None
    errors: list[GraphQLError] | None


class DiscUploadStatusPayload(TypedDict):
    discUploadStatus: dict[str, Any] | None
    errors: list[GraphQLError] | None


class DiscUploadStatus(TypedDict):
    logsUploaded: bool
    logUploadError: str | None


class FileHashInfo(TypedDict):
    index: int
    name: str
    creationTime: str
    size: int


class DiscFingerprintFileInfo(TypedDict):
    path: str
    size: int


@dataclass(frozen=True)
class DiscDbMatch:
    media_item: DiscDbMediaItem
    release: DiscDbRemoteRelease
    disc: DiscDbRemoteDisc
    title_indexes: tuple[int, ...] = ()


@dataclass(frozen=True)
class DiscDbLookupResult:
    items: tuple[DiscDbMediaItem, ...]
    match: DiscDbMatch | None
    ambiguous: bool = False


class DiscDbError(RuntimeError):
    """A recoverable TheDiscDB client or protocol error."""


def graphql_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/graphql/"


def calculate_disc_hash(sizes: Iterable[int]) -> str:
    """Calculate TheDiscDB's legacy Disc Hash from file sizes.

    The reference implementation feeds each selected file size to MD5 as a
    little-endian int64, in the submitted file-list order, and ignores names
    and timestamps. Browser submissions normally enumerate the relevant files
    in path order, so mkvsmith uses the same sorted file selection.
    """
    digest = hashlib.md5()  # noqa: S324 - legacy database identifier
    for size in sizes:
        digest.update(size.to_bytes(8, "little", signed=True))
    return digest.hexdigest().upper()


def _is_disc_hash_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    upper = normalized.upper()
    return upper.startswith("VIDEO_TS/") or (
        upper.startswith("BDMV/STREAM/") and upper.endswith(".M2TS")
    )


def calculate_disc_hash_from_paths(
    path_sizes: Iterable[tuple[str, int]],
) -> str | None:
    selected = sorted(
        (path, size) for path, size in path_sizes if _is_disc_hash_path(path)
    )
    if not selected:
        return None
    return calculate_disc_hash(size for _path, size in selected)


def contribution_graphql_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/graphql/contributions/"


class DiscDbClient:
    def __init__(self, options: DiscDbOptions) -> None:
        self.options = options

    def _post_json(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        cookie: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "mkvsmith/0.3.0",
        }
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(
            url or graphql_url(self.options.base_url),
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.options.timeout_seconds
            ) as response:
                response_url = str(response.geturl())
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise DiscDbError(f"TheDiscDB HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiscDbError(f"TheDiscDB request failed: {exc}") from exc

        if "Account/Login" in response_url:
            raise DiscDbError(
                "TheDiscDB authentication required (check or refresh your cookie)"
            )

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DiscDbError("TheDiscDB returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise DiscDbError("TheDiscDB returned an unexpected JSON response")
        errors = result.get("errors")
        if errors:
            messages = "; ".join(
                str(error.get("message", ""))
                for error in errors
                if isinstance(error, dict)
            )
            raise DiscDbError(f"TheDiscDB query failed: {messages}")
        data_value = result.get("data")
        if data_value is None:
            raise DiscDbError("TheDiscDB query returned no data")
        if not isinstance(data_value, dict):
            raise DiscDbError("TheDiscDB query returned unexpected data")
        return data_value

    def lookup(self, metadata: DiscMetadata) -> DiscDbLookupResult:
        disc_hash = metadata.disc_hash.upper() if metadata.disc_hash else None
        global_ids = {
            value.upper()
            for value in (metadata.aacs_disc_id, metadata.libdvdread_disc_id)
            if value
        }
        filters: list[dict[str, Any]] = []
        if disc_hash:
            filters.append(
                {
                    "releases": {
                        "some": {"discs": {"some": {"contentHash": {"eq": disc_hash}}}}
                    }
                }
            )
        if metadata.matrix256_fingerprint:
            filters.append(
                {
                    "releases": {
                        "some": {
                            "discs": {
                                "some": {
                                    "fingerprint": {
                                        "eq": metadata.matrix256_fingerprint
                                    }
                                }
                            }
                        }
                    }
                }
            )
        for global_id in sorted(global_ids):
            filters.append(
                {
                    "releases": {
                        "some": {"discs": {"some": {"globalDiscId": {"eq": global_id}}}}
                    }
                }
            )
        if metadata.upc_ean and not filters:
            filters.append({"releases": {"some": {"upc": {"eq": metadata.upc_ean}}}})
        if not filters:
            return DiscDbLookupResult(items=(), match=None)

        try:
            data = self._post_json(DISCDB_LOOKUP_QUERY, {"where": {"or": filters}})
            lookup_data = cast(DiscDbLookupData, data)
            items = tuple(lookup_data["mediaItems"]["nodes"])
            return DiscDbLookupResult(
                items=items,
                match=_select_disc_match(items, metadata),
                ambiguous=_has_ambiguous_strong_match(items, metadata),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DiscDbError(
                f"TheDiscDB returned an invalid lookup response: {exc}"
            ) from exc


def _remote_discs(
    items: tuple[DiscDbMediaItem, ...],
) -> tuple[tuple[DiscDbMediaItem, DiscDbRemoteRelease, DiscDbRemoteDisc], ...]:
    return tuple(
        (item, release, disc)
        for item in items
        for release in item["releases"]
        for disc in release["discs"]
    )


def _disc_signature(disc: DiscDbRemoteDisc) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            title.get("sourceFile"),
            title.get("duration"),
            title.get("filename"),
            (title.get("item") or {}).get("type"),
            (title.get("item") or {}).get("season"),
            (title.get("item") or {}).get("episode"),
        )
        for title in disc["titles"]
    )


def _select_disc_match(
    items: tuple[DiscDbMediaItem, ...], metadata: DiscMetadata
) -> DiscDbMatch | None:
    remote = _remote_discs(items)
    if not remote:
        return None

    global_ids = {
        value.upper()
        for value in (metadata.aacs_disc_id, metadata.libdvdread_disc_id)
        if value
    }
    disc_hash = metadata.disc_hash.upper() if metadata.disc_hash else None
    strong: list[tuple[DiscDbMediaItem, DiscDbRemoteRelease, DiscDbRemoteDisc]] = []
    for item, release, disc in remote:
        if (
            (disc_hash and (disc.get("contentHash") or "").upper() == disc_hash)
            or (
                metadata.matrix256_fingerprint
                and disc.get("fingerprint") == metadata.matrix256_fingerprint
            )
            or (disc.get("globalDiscId") or "").upper() in global_ids
        ):
            strong.append((item, release, disc))

    if strong:
        signatures = {_disc_signature(entry[2]) for entry in strong}
        if len(signatures) == 1:
            item, release, disc = strong[0]
            return DiscDbMatch(item, release, disc)
        return None

    if not metadata.upc_ean:
        return None
    upc_matches = [
        (item, release, disc)
        for item, release, disc in remote
        if release.get("upc") == metadata.upc_ean
    ]
    if len(upc_matches) != 1:
        return None
    item, release, disc = upc_matches[0]
    return DiscDbMatch(item, release, disc)


def _has_ambiguous_strong_match(
    items: tuple[DiscDbMediaItem, ...], metadata: DiscMetadata
) -> bool:
    global_ids = {
        value.upper()
        for value in (metadata.aacs_disc_id, metadata.libdvdread_disc_id)
        if value
    }
    disc_hash = metadata.disc_hash.upper() if metadata.disc_hash else None
    strong = [
        disc
        for _item, _release, disc in _remote_discs(items)
        if (disc_hash and (disc.get("contentHash") or "").upper() == disc_hash)
        or (
            metadata.matrix256_fingerprint
            and disc.get("fingerprint") == metadata.matrix256_fingerprint
        )
        or (disc.get("globalDiscId") or "").upper() in global_ids
    ]
    return bool(strong and len({_disc_signature(disc) for disc in strong}) > 1)


def _parse_duration(value: str | None) -> float | None:
    if not value:
        return None
    parts = value.split(":")
    if not all(part.strip() for part in parts):
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if not 1 <= len(numbers) <= 3:
        return None
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    hours, minutes, seconds = numbers
    return hours * 3600 + minutes * 60 + seconds


def _local_source_keys(title: Title) -> set[str]:
    keys: set[str] = set()
    if title.playlist_name:
        keys.update(
            {
                title.playlist_name.lower(),
                f"{title.playlist_name.lower()}.mpls",
            }
        )
    if title.dvd_title_id is not None:
        keys.update({str(title.dvd_title_id), f"{title.dvd_title_id:02d}"})
    if title.source_file.suffix.lower() == ".m2ts":
        keys.add(title.source_file.name.lower())
    for internal_path in title.iso_internal_paths:
        path = Path(internal_path)
        if path.suffix.lower() in {".m2ts", ".mpls"}:
            keys.add(path.name.lower())
    return keys


def _remote_source_key(title: DiscDbRemoteTitle) -> str | None:
    source = title.get("sourceFile")
    return source.lower() if source else None


def apply_discdb_match(
    titles: list[Title], metadata: DiscMetadata, result: DiscDbLookupResult
) -> DiscDbMatch | None:
    del metadata
    match = result.match
    if match is None:
        return None

    correlated: list[tuple[Title, DiscDbRemoteTitle]] = []
    for title in titles:
        candidates = [
            remote
            for remote in match.disc["titles"]
            if _remote_source_key(remote) in _local_source_keys(title)
        ]
        candidates = [
            remote
            for remote in candidates
            if (duration := _parse_duration(remote.get("duration"))) is not None
            and abs(duration - title.duration_seconds) <= 3.0
        ]
        if len(candidates) != 1:
            continue
        correlated.append((title, candidates[0]))

    if not correlated:
        return None
    remote_targets = {
        (remote.get("sourceFile"), remote.get("duration"), remote.get("index"))
        for _title, remote in correlated
    }
    if len(remote_targets) != len(correlated):
        return None
    matched_indexes: list[int] = []
    for title, remote in correlated:
        if remote_filename := remote.get("filename"):
            title.name = Path(remote_filename).stem
        item = remote.get("item")
        if item:
            title.discdb_item_type = item.get("type")
            title.discdb_season_number = item.get("season")
            title.discdb_episode_number = item.get("episode")
            title.discdb_is_main_movie = item.get("type") == "MainMovie"
        matched_indexes.append(title.index)
    return DiscDbMatch(
        media_item=match.media_item,
        release=match.release,
        disc=match.disc,
        title_indexes=tuple(matched_indexes),
    )


def _iso_datetime(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _iso_datetime_from_7z(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    normalized = value.split(".", 1)[0]
    try:
        local_time = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        return _iso_datetime(local_time.astimezone().timestamp())
    except ValueError:
        return fallback


def _folder_hash_files(source: Path) -> list[FileHashInfo]:
    video_ts = source / "VIDEO_TS"
    stream_dir = source / "BDMV" / "STREAM"
    paths: list[Path] = []
    if video_ts.is_dir():
        paths.extend(sorted(path for path in video_ts.iterdir() if path.is_file()))
    if stream_dir.is_dir():
        paths.extend(sorted(stream_dir.glob("*.m2ts")))
    return [
        FileHashInfo(
            index=index,
            name=path.name,
            creationTime=_iso_datetime(path.stat().st_mtime),
            size=path.stat().st_size,
        )
        for index, path in enumerate(paths)
    ]


def _iso_hash_files(source: Path) -> list[FileHashInfo]:
    from disc_reader import _list_iso_file_metadata_7z

    entries = _list_iso_file_metadata_7z(source)
    selected: list[tuple[str, int, str | None]] = []
    for entry in entries:
        normalized = entry.path.replace("\\", "/")
        if _is_disc_hash_path(normalized):
            selected.append((normalized, entry.size, entry.modified))
    timestamp = _iso_datetime(source.stat().st_mtime)
    selected.sort(key=lambda item: item[0])
    return [
        FileHashInfo(
            index=index,
            name=Path(path).name,
            creationTime=_iso_datetime_from_7z(modified, timestamp),
            size=size,
        )
        for index, (path, size, modified) in enumerate(selected)
    ]


def collect_hash_files(source: Path) -> list[FileHashInfo]:
    try:
        if source.is_file():
            return _iso_hash_files(source)
        return _folder_hash_files(source)
    except OSError as exc:
        raise DiscDbError(f"Could not collect TheDiscDB hash files: {exc}") from exc


def _folder_fingerprint_files(source: Path) -> list[DiscFingerprintFileInfo]:
    entries: list[DiscFingerprintFileInfo] = []
    for path in source.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        entries.append(
            DiscFingerprintFileInfo(
                path=path.relative_to(source).as_posix(),
                size=path.stat().st_size,
            )
        )
    return sorted(entries, key=lambda entry: entry["path"])


def _iso_fingerprint_files(source: Path) -> list[DiscFingerprintFileInfo]:
    from disc_reader import _list_iso_files_7z

    paths, sizes = _list_iso_files_7z(source)
    return [
        DiscFingerprintFileInfo(path=path, size=sizes.get(path, 0))
        for path in sorted(paths)
    ]


def collect_fingerprint_files(source: Path) -> list[DiscFingerprintFileInfo]:
    try:
        if source.is_file():
            return _iso_fingerprint_files(source)
        return _folder_fingerprint_files(source)
    except OSError as exc:
        raise DiscDbError(
            f"Could not collect TheDiscDB fingerprint files: {exc}"
        ) from exc


def _stream_type_name(stream_type: StreamType) -> str:
    return {
        StreamType.VIDEO: "Video",
        StreamType.AUDIO: "Audio",
        StreamType.SUBTITLE: "Subtitle",
    }[stream_type]


def _duration_text(seconds: float) -> str:
    whole = max(int(round(seconds)), 0)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _clip_names(title: Title) -> list[str]:
    if title.iso_internal_paths:
        return [Path(path).stem for path in title.iso_internal_paths]
    names = [title.source_file.stem]
    names.extend(path.stem for path in title.append_clips)
    return names


def _source_file(title: Title) -> str:
    if title.playlist_name:
        return f"{title.playlist_name}.mpls"
    if title.dvd_title_id is not None:
        return f"{title.dvd_title_id:02d}"
    return title.source_file.name


def _segment_map(title: Title) -> str:
    # DVD segment maps are cell ranges (for example "1-14,15-31"), while
    # mkvsmith currently exposes chapter times rather than cell IDs. Leave the
    # map blank instead of publishing misleading VOB names.
    if title.dvd_title_id is not None and title.playlist_name is None:
        return ""
    return ",".join(_clip_names(title))


def _quoted(value: object) -> str:
    # TheDiscDB's log parser takes TINFO values directly between the first and
    # final quotes, so comma-separated segment maps remain valid. Embedded
    # quotes are not unescaped by that parser; replace them in descriptive
    # metadata instead of producing an unreadable value.
    text = str(value).replace('"', "'").replace("\r", " ").replace("\n", " ")
    return f'"{text}"'


def _stream_lines(title_index: int, stream: Stream) -> list[str]:
    lines = [
        f"SINFO:{title_index},{stream.type_index},1,0,"
        f"{_quoted(_stream_type_name(stream.stream_type))}"
    ]
    if stream.codec:
        lines.append(
            f"SINFO:{title_index},{stream.type_index},7,0,"
            f"{_quoted(stream.codec_display)}"
        )
    if stream.stream_type == StreamType.AUDIO:
        if stream.codec:
            lines.append(
                f"SINFO:{title_index},{stream.type_index},2,0,"
                f"{_quoted(stream.codec_display)}"
            )
        lines.extend(
            (
                f"SINFO:{title_index},{stream.type_index},3,0,"
                f"{_quoted(stream.language)}",
                f"SINFO:{title_index},{stream.type_index},4,0,"
                f"{_quoted(stream.language)}",
            )
        )
    elif stream.stream_type == StreamType.SUBTITLE:
        lines.extend(
            (
                f"SINFO:{title_index},{stream.type_index},3,0,"
                f"{_quoted(stream.language)}",
                f"SINFO:{title_index},{stream.type_index},4,0,"
                f"{_quoted(stream.language)}",
            )
        )
    elif stream.width and stream.height:
        lines.append(
            f"SINFO:{title_index},{stream.type_index},19,0,"
            f"{_quoted(f'{stream.width}x{stream.height}')}"
        )
    return lines


def synthetic_makemkv_log(titles: list[Title], is_bluray: bool) -> str:
    """Render mkvsmith's parsed disc structures as MakeMKV robot output.

    This is not a MakeMKV invocation. It emits only the line families consumed
    by TheDiscDB's open-source log parser (CINFO/TCOUNT/TINFO/SINFO), avoiding
    a second external media dependency.
    """
    lines = [
        "# Generated by mkvsmith from directly parsed disc metadata",
        f"CINFO:1,0,{_quoted('Blu-ray disc' if is_bluray else 'DVD disc')}",
    ]
    disc_name = next((title.disc_name for title in titles if title.disc_name), None)
    if disc_name:
        lines.append(f"CINFO:2,0,{_quoted(disc_name)}")
    log_titles = [title for title in titles if title.streams]
    if titles and not log_titles:
        raise DiscDbError("Cannot build TheDiscDB scan log: no title has streams")
    lines.append(f"TCOUNT:{len(log_titles)}")

    for title in log_titles:
        index = title.index
        fields: list[tuple[int, object]] = [
            (8, max(len(title.chapters), 0)),
            (9, _duration_text(title.duration_seconds)),
            (10, f"{title.estimated_size_bytes / 1_000_000_000:.1f} GB"),
            (11, max(title.estimated_size_bytes, 0)),
        ]
        if title.playlist_name:
            fields.append((16, _source_file(title)))
            fields.append((26, _segment_map(title)))
        elif title.dvd_title_id is not None:
            fields.append((24, _source_file(title)))
        elif title.source_file.suffix.lower() in {".m2ts", ".vob"}:
            fields.append((7, title.source_file.name))
            fields.append((26, _segment_map(title)))
        lines.extend(
            f"TINFO:{index},{code},0,{_quoted(value)}" for code, value in fields
        )
        for stream in title.streams:
            lines.extend(_stream_lines(index, stream))
    return "\n".join(lines) + "\n"


def _manifest_stream(stream: Stream) -> dict[str, Any]:
    return {
        "index": stream.index,
        "type": stream.stream_type.value,
        "codec": stream.codec,
        "language": stream.language,
        "title": stream.title,
    }


def _manifest_title(title: Title) -> dict[str, Any]:
    return {
        "index": title.index,
        "name": title.name,
        "duration_seconds": round(title.duration_seconds, 3),
        "size_bytes": title.estimated_size_bytes,
        "chapters": len(title.chapters),
        "source_file": _source_file(title),
        "segment_map": _segment_map(title),
        "dvd_title_id": title.dvd_title_id,
        "streams": [_manifest_stream(stream) for stream in title.streams],
    }


def _is_disc_contribution_source(
    source: Path, titles: list[Title], metadata: DiscMetadata, is_bluray: bool
) -> bool:
    if is_bluray:
        return True
    return bool(
        metadata.libdvdread_disc_id
        or metadata.dvd_disc_id
        or metadata.upc_ean
        or metadata.provider_id
        or any(title.dvd_title_id is not None for title in titles)
        or (source / "VIDEO_TS").is_dir()
    )


def _contribution_format(titles: list[Title], is_bluray: bool) -> str:
    if not is_bluray:
        return "DVD"
    is_uhd = any(
        stream.width is not None and stream.width >= 3000
        for title in titles
        for stream in title.video_streams
    )
    return "4K" if is_uhd else "Blu-ray"


def build_contribution_bundle(
    source: Path,
    titles: list[Title],
    metadata: DiscMetadata,
    options: DiscDbOptions,
) -> Path:
    bundle_dir = options.bundle_dir
    if bundle_dir is None:
        bundle_dir = Path.cwd() / "thediscdb-contribution"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    is_bluray = bool(metadata.aacs_disc_id) or any(
        title.playlist_name
        or title.source_file.suffix.lower() == ".m2ts"
        or any(path.lower().endswith(".m2ts") for path in title.iso_internal_paths)
        for title in titles
    )
    if not _is_disc_contribution_source(source, titles, metadata, is_bluray):
        raise DiscDbError("TheDiscDB contributions require a DVD or Blu-ray source")
    hash_files = collect_hash_files(source)
    if not hash_files:
        raise DiscDbError("No DVD/Blu-ray files were available for TheDiscDB hashing")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": "mkvsmith/0.3.0",
        "source": {
            "name": source.name,
            "format": _contribution_format(titles, is_bluray),
        },
        "identifiers": {
            "upc_ean": metadata.upc_ean,
            "matrix256_fingerprint": metadata.matrix256_fingerprint,
            "aacs_disc_id": metadata.aacs_disc_id,
            "libdvdread_disc_id": metadata.libdvdread_disc_id,
            "disc_hash": metadata.disc_hash,
            "windows_dvd_disc_id": metadata.dvd_disc_id,
        },
        "hash_files": hash_files,
        "fingerprint_files": collect_fingerprint_files(source),
        "titles": [_manifest_title(title) for title in titles],
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (bundle_dir / "makemkv_compat.txt").write_text(
        synthetic_makemkv_log(titles, is_bluray), encoding="utf-8"
    )
    return bundle_dir


def contribution_url(options: DiscDbOptions) -> str:
    base = options.base_url.rstrip("/")
    contribution_id = options.contribution_id
    if contribution_id:
        return f"{base}/contribution/{contribution_id}/discs"
    return f"{base}/contribute"


def open_contribution_url(options: DiscDbOptions) -> bool:
    if not options.open_browser:
        return False
    return webbrowser.open(contribution_url(options))


class DiscDbContributionClient:
    def __init__(self, options: DiscDbOptions) -> None:
        if not options.cookie:
            raise DiscDbError(
                tr("Direct TheDiscDB contribution requires an authenticated cookie")
            )
        if not options.contribution_id:
            raise DiscDbError(
                tr("Direct TheDiscDB contribution requires a contribution ID")
            )
        self.options = options
        self.lookup_client = DiscDbClient(options)

    def _mutation(
        self, query: str, variables: dict[str, Any], field: str
    ) -> dict[str, Any]:
        try:
            data = self.lookup_client._post_json(
                query,
                variables,
                cookie=self.options.cookie,
                url=contribution_graphql_url(self.options.base_url),
            )
            payload = cast(dict[str, Any], data.get(field))
        except (KeyError, TypeError, ValueError) as exc:
            raise DiscDbError(
                f"TheDiscDB returned an invalid mutation response: {exc}"
            ) from exc
        if payload is None:
            raise DiscDbError(f"TheDiscDB mutation {field} returned no payload")
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(error.get("message", "")) for error in errors)
            raise DiscDbError(f"TheDiscDB mutation failed: {messages}")
        return payload

    def hash_disc(
        self,
        contribution_id: str,
        files: list[FileHashInfo],
        fingerprint_files: list[DiscFingerprintFileInfo],
    ) -> tuple[str, str | None]:
        payload = self._mutation(
            """
            mutation HashDisc($input: HashDiscInput!) {
              hashDisc(input: $input) {
                discHash { hash }
                errors { message }
              }
            }
            """,
            {
                "input": {
                    "contributionId": contribution_id,
                    "files": files,
                    "fingerprintFiles": fingerprint_files,
                }
            },
            "hashDisc",
        )
        typed = cast(HashDiscPayload, payload)
        disc_hash = typed.get("discHash")
        if not disc_hash or not disc_hash.get("hash"):
            raise DiscDbError("TheDiscDB did not return a disc hash")
        return disc_hash["hash"], disc_hash.get("fingerprint")

    def create_disc(
        self,
        contribution_id: str,
        content_hash: str,
        *,
        format_name: str,
        name: str,
        slug: str,
        global_disc_id: str | None,
        fingerprint: str | None = None,
    ) -> str:
        input_value: dict[str, Any] = {
            "contributionId": contribution_id,
            "contentHash": content_hash,
            "format": format_name,
            "name": name,
            "slug": slug,
        }
        if global_disc_id:
            input_value["globalDiscId"] = global_disc_id
        if fingerprint:
            input_value["fingerprint"] = fingerprint.lower()
        payload = self._mutation(
            """
            mutation CreateDisc($input: CreateDiscInput!) {
              createDisc(input: $input) {
                userContributionDisc { encodedId }
                errors { message }
              }
            }
            """,
            {"input": input_value},
            "createDisc",
        )
        typed = cast(CreateDiscPayload, payload)
        disc = typed.get("userContributionDisc")
        if not disc or not disc.get("encodedId"):
            raise DiscDbError("TheDiscDB did not return a disc ID")
        return str(disc["encodedId"])

    def upload_logs(self, contribution_id: str, disc_id: str, logs: str) -> None:
        request = urllib.request.Request(
            f"{self.options.base_url.rstrip('/')}/api/contribute/"
            f"{contribution_id}/discs/{disc_id}/logs",
            data=logs.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Cookie": self.options.cookie or "",
                "User-Agent": "mkvsmith/0.3.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.options.timeout_seconds
            ) as response:
                response_url = str(response.geturl())
                _ = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise DiscDbError(
                f"TheDiscDB log upload failed: HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DiscDbError(f"TheDiscDB log upload failed: {exc}") from exc
        if "Account/Login" in response_url:
            raise DiscDbError(
                "TheDiscDB log upload requires authentication (check your cookie)"
            )

    def upload_status(self, disc_id: str) -> tuple[bool, str | None]:
        payload = self._mutation(
            """
            mutation DiscUploadStatus($input: DiscUploadStatusInput!) {
              discUploadStatus(input: $input) {
                discUploadStatus { logsUploaded logUploadError }
                errors { message }
              }
            }
            """,
            {"input": {"discId": disc_id}},
            "discUploadStatus",
        )
        typed = cast(DiscUploadStatusPayload, payload)
        raw_status = typed.get("discUploadStatus")
        if raw_status is None:
            raise DiscDbError("TheDiscDB did not return an upload status")
        status = cast(DiscUploadStatus, raw_status)
        return bool(status.get("logsUploaded")), status.get("logUploadError")


def _slugify(value: str) -> str:
    slug = re.sub(r"&", "and", value.lower())
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"[^a-z0-9-]", "", slug).strip("-")


def submit_contribution_bundle(
    manifest: dict[str, Any], logs: str, options: DiscDbOptions
) -> tuple[str, str]:
    client = DiscDbContributionClient(options)
    raw_files = cast(list[dict[str, Any]], manifest.get("hash_files", []))
    files = [
        FileHashInfo(
            index=int(file["index"]),
            name=str(file.get("name", "")),
            creationTime=str(file["creationTime"]),
            size=int(file["size"]),
        )
        for file in raw_files
    ]
    raw_fingerprint_files = cast(
        list[dict[str, Any]], manifest.get("fingerprint_files", [])
    )
    fingerprint_files = [
        DiscFingerprintFileInfo(
            path=str(file["path"]),
            size=int(file["size"]),
        )
        for file in raw_fingerprint_files
    ]
    content_hash, server_fingerprint = client.hash_disc(
        options.contribution_id or "",
        files,
        fingerprint_files,
    )
    source_format = str(
        cast(dict[str, Any], manifest.get("source", {})).get("format", "DVD")
    )
    normalized_format = source_format.lower()
    if normalized_format in {"4k", "uhd"}:
        format_name = "4K"
    elif normalized_format in {"blu-ray", "bluray"}:
        format_name = "Blu-ray"
    else:
        format_name = "DVD"
    identifiers = cast(dict[str, Any], manifest.get("identifiers", {}))
    global_disc_id = cast(
        str | None,
        identifiers.get("aacs_disc_id") or identifiers.get("libdvdread_disc_id"),
    )
    fingerprint = cast(
        str | None,
        identifiers.get("matrix256_fingerprint") or server_fingerprint,
    )
    if fingerprint:
        fingerprint = fingerprint.lower()
    disc_id = client.create_disc(
        options.contribution_id or "",
        content_hash,
        format_name=format_name,
        name=options.disc_name or "Disc 1",
        slug=_slugify(options.disc_name or "Disc 1"),
        global_disc_id=global_disc_id,
        fingerprint=fingerprint,
    )
    client.upload_logs(options.contribution_id or "", disc_id, logs)

    uploaded = False
    error: str | None = None
    for _ in range(STATUS_POLL_ATTEMPTS):
        uploaded, error = client.upload_status(disc_id)
        if uploaded or error:
            break
        time.sleep(0.5)
    if not uploaded:
        detail = error or "upload did not finish"
        raise DiscDbError(f"TheDiscDB did not accept scan logs: {detail}")
    review_url = (
        f"{options.base_url.rstrip('/')}/contribution/"
        f"{options.contribution_id}/discs/{disc_id}/identify"
    )
    return disc_id, review_url
