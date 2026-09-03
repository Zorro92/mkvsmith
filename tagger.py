"""
TMDB metadata tagging for mkvsmith.

Metadata is fetched from TMDB and embedded directly during the mkvmerge mux via
--tags global:file.xml (Matroska Tags) and --attachment-* options (cover art).
Fetching uses only the standard library (urllib), so no extra Python
dependencies are required.

Extracted from main.py — the original section header was:
    "Metadata tagging (TMDB) — ported from tagger.py"
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict, final

from models import (
    RipError,
    TagOptions,
    _console,
    _TEMP_FILES,
    log_info,
    log_warn,
)
from settings import load_settings
from i18n import tr

# =============================================================================
# Constants
# =============================================================================

# Kept for backwards-compat with older call sites; settings now live in the
# unified ~/.mkvsmith_config.json (see settings.py), with one-time migration from
# the legacy ~/.mkv_tagger_config.json handled inside load_settings().
TAGGER_CONFIG_PATH = None  # type: ignore[assignment]
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
TMDB_TIMEOUT = 10  # seconds
TMDB_RETRIES = 3

MAX_CAST = 10
MAX_WRITERS = 5
MAX_DIRECTORS = 2
MAX_SEARCH_RESULTS = 5

# =============================================================================
# Data model
# =============================================================================


@dataclass
class MovieMetadata:
    tmdb_id: str | None = None
    imdb_id: str | None = None
    title: str | None = None
    overview: str | None = None
    cast: list[str] | None = None
    writers: list[str] | None = None
    directors: list[str] | None = None
    genres: list[str] | None = None
    release_date: str | None = None
    runtime: int | None = None
    original_language: str | None = None
    production_companies: list[str] | None = None
    user_rating: float | None = None
    content_rating: str | None = None
    keywords: list[str] | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    custom_properties: dict[str, str] = field(default_factory=dict)


# MovieMetadata attribute -> (Matroska tag name, optional formatter). Falsy
# values are skipped. `title` is intentionally absent: it is written to the
# Matroska Segment Title element separately (ffmpeg maps the lowercase "title"
# key to it), and a TITLE tag would collide with it case-insensitively.
_TAG_FIELDS: list[tuple[str, str, Callable[[Any], str] | None]] = [
    ("tmdb_id", "TMDB", None),
    ("imdb_id", "IMDB", None),
    ("overview", "SYNOPSIS", None),
    ("genres", "GENRE", ", ".join),
    ("release_date", "DATE_RELEASED", None),
    ("runtime", "RUNTIME", lambda v: f"{v} min"),
    ("original_language", "ORIGINAL_LANGUAGE", None),
    ("production_companies", "PRODUCTION_STUDIO", ", ".join),
    ("user_rating", "RATING", None),
    ("cast", "ACTOR", ", ".join),
    ("writers", "WRITTEN_BY", ", ".join),
    ("directors", "DIRECTOR", ", ".join),
    ("content_rating", "CONTENT_RATING", None),
    ("keywords", "KEYWORDS", ", ".join),
]

# =============================================================================
# Config helpers
# =============================================================================


def _tagger_load_config() -> dict[str, Any]:
    """Read the tagger config (for the TMDB API key).

    Backwards-compatible: delegates to the unified settings file. Older code
    that stored the key under ~/.mkv_tagger_config.json is migrated by
    load_settings() on first access.
    """
    return load_settings()


def _resolve_tmdb_key(opts: TagOptions) -> str | None:
    """Return the configured TMDB API key, if any (flag > config > env)."""
    key = (
        opts.api_key
        or _tagger_load_config().get("api_key")
        or os.environ.get("TMDB_API_KEY")
    )
    # Settings are free-form JSON: guard against non-string values.
    if not isinstance(key, str) or not key.strip():
        return None
    return key


# =============================================================================
# Title sanitisation for TMDB search
# =============================================================================

_SCENE_TOKENS = re.compile(
    r"\b(?:"
    r"\d{3,4}p|\d{3,4}i|"
    r"4k|8k|uhd|hdr(?:10)?|sdr|"
    r"web-?dl|web-?rip|webcap|webhd|"
    r"blu-?ray|br-?rip|bd-?rip|dvdrip|hdrip|"
    r"hdtv|pdtv|"
    r"x264|x265|h264|h265|hevc|avc|xvid|divx|"
    r"ac3|dts|truehd|atmos|flac|aac|mp3|"
    r"repack|proper|extended|unrated|remastered|internal|readnfo"
    r")\b",
    re.IGNORECASE,
)


def sanitize_title(filename: str) -> tuple[str, int | None]:
    """Extract a clean title and release year, ignoring common scene tags."""
    base = Path(filename).stem
    if re.search(r"\b(?:19|20)\d{2}\b|\d{3,4}[pi]", base, re.IGNORECASE):
        base = re.sub(r"-\s*[A-Za-z0-9]+$", "", base)
    cleaned = re.sub(r"[._]", " ", base)
    cleaned = _SCENE_TOKENS.sub(" ", cleaned)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", cleaned)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        cleaned = cleaned[: year_match.start()] + " " + cleaned[year_match.end() :]
    title = re.sub(r"\s+", " ", cleaned).strip(" -")
    if not title:
        title = re.sub(r"[._]", " ", Path(filename).stem).strip()
    return title, year


# =============================================================================
# TMDB client
# =============================================================================


@final
class TmdbClient:
    """Minimal TMDB v3 client using only the standard library."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        query = {"api_key": self.api_key}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        url = f"{TMDB_BASE_URL}/{endpoint}?{urllib.parse.urlencode(query)}"
        last_err = None
        for attempt in range(TMDB_RETRIES):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "mkvsmith/0.1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=TMDB_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 500, 502, 503, 504) and attempt < TMDB_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RipError(message=f"TMDB request failed: HTTP {e.code} {e.reason}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt < TMDB_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RipError(message=f"TMDB request failed: {e}")
        raise RipError(message=f"TMDB request failed: {last_err}")

    def get_movie_id(self, title: str, year: int | None = None) -> int:
        params: dict[str, object] = {"query": title}
        if year:
            params["year"] = year
        resp = self._get("search/movie", params)
        results = resp.get("results", [])
        if not results:
            raise RipError(message=f"No TMDB results for '{title}'")
        if year:
            year_matches = [
                r
                for r in results
                if str(r.get("release_date", "")).startswith(str(year))
            ]
            if year_matches:
                results = year_matches
        if len(results) > 1:
            log_info(f"Multiple TMDB matches for '{title}':")
            shown = results[:MAX_SEARCH_RESULTS]
            for i, m in enumerate(shown):
                print(
                    f"  {i + 1}. {m.get('title', 'Unknown')} "
                    f"({m.get('release_date', '?')})"
                )
            sel = _tag_prompt("Select the correct movie", default="1")
            try:
                idx = int(sel) - 1
            except ValueError:
                idx = 0
            idx = max(0, min(idx, len(shown) - 1))
            return int(shown[idx]["id"])
        return int(results[0]["id"])

    def get_metadata(
        self,
        movie_id: int,
        props: list[str],
        region: str = "US",
        language: str | None = None,
    ) -> MovieMetadata:
        md = MovieMetadata()
        info = self._get(f"movie/{movie_id}", {"language": language})
        md.poster_path = info.get("poster_path")
        md.backdrop_path = info.get("backdrop_path")
        if "TMDbID" in props:
            md.tmdb_id = f"movie/{movie_id}"
        if "IMDbID" in props and info.get("imdb_id"):
            md.imdb_id = info["imdb_id"]
        if "Title" in props:
            md.title = info.get("title")
        if "Overview" in props:
            md.overview = info.get("overview")
        if "Genres" in props:
            md.genres = [g["name"] for g in info.get("genres", [])]
        if "ReleaseDate" in props:
            md.release_date = info.get("release_date")
        if "Runtime" in props:
            md.runtime = info.get("runtime")
        if "OriginalLanguage" in props:
            md.original_language = info.get("original_language")
        if "ProductionCompanies" in props:
            md.production_companies = [
                c["name"] for c in info.get("production_companies", [])
            ]
        if "UserRating" in props:
            md.user_rating = info.get("vote_average")
        if any(p in props for p in ("Cast", "Writers", "Directors")):
            credits = self._get(f"movie/{movie_id}/credits")
            if "Cast" in props:
                md.cast = [p["name"] for p in credits.get("cast", [])[:MAX_CAST]]
            if "Writers" in props:
                md.writers = [
                    f"{p['name']} ({p['job']})"
                    for p in credits.get("crew", [])
                    if p.get("department") == "Writing"
                ][:MAX_WRITERS]
            if "Directors" in props:
                md.directors = [
                    p["name"]
                    for p in credits.get("crew", [])
                    if p.get("department") == "Directing" and p.get("job") == "Director"
                ][:MAX_DIRECTORS]
        if "ContentRating" in props:
            release_info = self._get(f"movie/{movie_id}/release_dates")
            for country in release_info.get("results", []):
                if country.get("iso_3166_1") == region:
                    for rel in country.get("release_dates", []):
                        if rel.get("certification"):
                            md.content_rating = rel["certification"]
                            break
                    break
        if "Keywords" in props:
            keywords = self._get(f"movie/{movie_id}/keywords")
            md.keywords = [k["name"] for k in keywords.get("keywords", [])]
        for prop in ("Budget", "Revenue", "Status"):
            if prop in props:
                value = info.get(prop.lower())
                if value is None:
                    continue
                if prop == "Budget" and isinstance(value, (int, float)):
                    value = f"${value:,.2f}"
                elif prop == "Revenue" and isinstance(value, (int, float)):
                    value = f"${value:,.2f}"
                elif isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                md.custom_properties[prop] = str(value)
        return md

    def display_preview(self, md: MovieMetadata) -> None:
        from models import HAS_RICH

        rows: list[tuple[str, str]] = []
        if md.title:
            rows.append(("Title", md.title))
        if md.tmdb_id:
            rows.append(("TMDb ID", md.tmdb_id))
        if md.imdb_id:
            rows.append(("IMDb ID", md.imdb_id))
        if md.release_date:
            rows.append(("Release Date", md.release_date))
        if md.runtime:
            rows.append(("Runtime", f"{md.runtime} min"))
        if md.genres:
            rows.append(("Genres", ", ".join(md.genres)))
        if md.user_rating:
            rows.append(("User Rating", str(md.user_rating)))
        if md.content_rating:
            rows.append(("Content Rating", md.content_rating))
        if md.original_language:
            rows.append(("Language", md.original_language))
        if md.directors:
            rows.append(("Directors", ", ".join(md.directors)))
        if md.cast:
            rows.append(("Cast", ", ".join(md.cast[:5])))
        if md.overview:
            overview = md.overview
            if len(overview) > 120:
                overview = overview[:117] + "..."
            rows.append(("Overview", overview))
        for prop, value in md.custom_properties.items():
            rows.append((prop, str(value)))
        if HAS_RICH and _console is not None:
            try:
                from rich.table import Table
            except ImportError:
                pass  # fall through to plain print below
            else:
                table = Table(title=tr("Metadata Preview"))
                table.add_column("Property", style="cyan")
                table.add_column("Value", style="green")
                for k, v in rows:
                    table.add_row(k, v)
                _console.print(table)
                return
        for k, v in rows:
            print(f"  {k}: {v}")

    def download_image(self, image_path: str, size: str = "original") -> bytes | None:
        if not image_path:
            return None
        url = f"{TMDB_IMAGE_BASE}/{size}{image_path}"
        try:
            with urllib.request.urlopen(url, timeout=TMDB_TIMEOUT) as resp:
                data: bytes = resp.read()
                return data
        except (urllib.error.URLError, OSError):
            return None


# =============================================================================
# Interactive prompts
# =============================================================================


def _tag_prompt(prompt: str, default: str | None = None) -> str:
    """Read a line from stdin; return the default on empty input or interrupt."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default or ""
    return ans or (default or "")


def _tag_confirm(prompt: str) -> bool:
    """Simple y/N confirmation via stdin."""
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


_ART_CHOICES = {"1": None, "2": "poster", "3": "backdrop", "4": "both"}


def _prompt_art_choice() -> str | None:
    """Interactively ask which artwork to attach; returns None for 'none'."""
    print(tr("Attach artwork?"))
    print(tr("  1. None"))
    print(tr("  2. Poster"))
    print(tr("  3. Backdrop"))
    print(tr("  4. Both"))
    return _ART_CHOICES.get(_tag_prompt(tr("Choose"), default="1"), None)


# =============================================================================
# Tag XML generation
# =============================================================================


def _metadata_tag_pairs(md: MovieMetadata) -> list[tuple[str, str]]:
    """Build (tag_name, value) pairs for the Matroska Tags, skipping falsy."""
    pairs: list[tuple[str, str]] = []
    for attr, name, fmt in _TAG_FIELDS:
        value = getattr(md, attr)
        if not value:
            continue
        pairs.append((name, fmt(value) if fmt else str(value)))
    for name, value in md.custom_properties.items():
        pairs.append((name.upper(), str(value)))
    return pairs


def _write_tag_xml(md: MovieMetadata, out_path: Path) -> None:
    """Write a standalone Matroska tag XML (reference copy; --save-tag-xml).

    Includes TITLE here, unlike the in-mux tags, since it is a standalone file.
    """
    from xml.dom import minidom

    root = ET.Element("Tags")
    tag = ET.SubElement(root, "Tag")

    def add_simple(name: str, value: str) -> None:
        simple = ET.SubElement(tag, "Simple")
        ET.SubElement(simple, "Name").text = name
        ET.SubElement(simple, "String").text = value

    fields = [
        ("tmdb_id", "TMDB", None),
        ("imdb_id", "IMDb", None),
        ("title", "TITLE", None),
        ("overview", "SYNOPSIS", None),
        ("genres", "GENRE", ", ".join),
        ("release_date", "DATE_RELEASED", None),
        ("runtime", "RUNTIME", lambda v: f"{v} min"),
        ("original_language", "ORIGINAL_LANGUAGE", None),
        ("production_companies", "PRODUCTION_STUDIO", ", ".join),
        ("user_rating", "RATING", None),
        ("cast", "ACTOR", ", ".join),
        ("writers", "WRITTEN_BY", ", ".join),
        ("directors", "DIRECTOR", ", ".join),
        ("content_rating", "CONTENT_RATING", None),
        ("keywords", "KEYWORDS", ", ".join),
    ]
    for attr, name, fmt in fields:
        value = getattr(md, attr)
        if not value:
            continue
        add_simple(name, fmt(value) if fmt else str(value))
    for name, value in md.custom_properties.items():
        add_simple(name.upper(), str(value))

    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    out_path.write_text(xml_str, encoding="utf-8")


# =============================================================================
# High-level orchestration
# =============================================================================


class ArtAttachment(TypedDict):
    """One temp cover-art file to attach at mux time."""

    path: Path
    mime: str
    filename: str
    label: str


def _prepare_tagging(
    search_name: str, opts: TagOptions
) -> tuple[MovieMetadata | None, list[ArtAttachment]]:
    """Fetch TMDB metadata for a rip, prompting for selection/confirmation.

    Returns ``(metadata_or_None, art_attachments)``. ``metadata`` is None when
    tagging is skipped (no key, user cancel). ``art_attachments`` is a list of
    dicts ``{path, mime, filename, label}`` for temp images to attach at mux
    time. Raises RipError on fatal fetch problems; the caller treats a tagging
    failure as non-fatal to the rip itself.
    """
    api_key = _resolve_tmdb_key(opts)
    if not api_key:
        log_warn(
            "Tagging skipped: no TMDB API key (--tmdb-key, --save-key, or TMDB_API_KEY)"
        )
        return None, []

    client = TmdbClient(api_key)
    if opts.title_override:
        s_title, s_year = opts.title_override, opts.year_override
    else:
        s_title, s_year = sanitize_title(search_name)
        if opts.year_override is not None:
            s_year = opts.year_override

    log_info(
        "Looking up TMDB metadata for '"
        + s_title
        + "'"
        + (f" ({s_year})" if s_year else "")
    )
    movie_id = client.get_movie_id(s_title, s_year)
    md = client.get_metadata(
        movie_id, opts.metadata, region=opts.region, language=opts.language
    )
    client.display_preview(md)

    if opts.confirm and not _tag_confirm("Tag this rip with the above metadata?"):
        log_info("Tagging skipped by user")
        return None, []

    art_attachments: list[ArtAttachment] = []
    if opts.art:
        want = {
            "poster": opts.art in ("poster", "both"),
            "backdrop": opts.art in ("backdrop", "both"),
        }
        sources = [
            ("poster", md.poster_path, "Poster", "cover.jpg"),
            ("backdrop", md.backdrop_path, "Backdrop", "fanart.jpg"),
        ]
        for kind, img_path, label, fname in sources:
            if not want[kind] or not img_path:
                continue
            data = client.download_image(img_path)
            if not data:
                log_warn(f"{label} not available")
                continue
            ext = Path(img_path).suffix.lower() or ".jpg"
            tf = Path(tempfile.NamedTemporaryFile(suffix=ext, delete=False).name)
            tf.write_bytes(data)
            _TEMP_FILES.append(tf)
            mime = "image/png" if ext == ".png" else "image/jpeg"
            art_attachments.append(
                {"path": tf, "mime": mime, "filename": fname, "label": label}
            )
            log_info(f"{label} downloaded")

    return md, art_attachments
