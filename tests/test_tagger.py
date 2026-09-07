"""Tests for TMDB metadata response parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import models
import pytest
import tagger
from tagger import (
    MovieMetadata,
    TagOptions,
    TmdbClient,
    _region_content_rating,
)


def test_get_metadata_applies_all_response_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmdbClient("test-key")
    responses: dict[str, dict[str, Any]] = {
        "movie/42": {
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "imdb_id": "tt0042",
            "title": "Test Movie",
            "overview": "Test overview",
            "genres": [{"name": "Documentary"}],
            "release_date": "2024-04-05",
            "runtime": 117,
            "original_language": "en",
            "production_companies": [{"name": "Test Studio"}],
            "vote_average": 8.25,
            "budget": 1200,
            "revenue": 2500,
            "status": "Released",
        },
        "movie/42/credits": {
            "cast": [{"name": f"Cast {index}"} for index in range(12)],
            "crew": [
                {"name": "Writer", "department": "Writing", "job": "Screenplay"},
                {
                    "name": "Director",
                    "department": "Directing",
                    "job": "Director",
                },
                {
                    "name": "Not Director",
                    "department": "Directing",
                    "job": "Second Unit",
                },
            ],
        },
        "movie/42/release_dates": {
            "results": [
                {"iso_3166_1": "GB", "release_dates": [{"certification": "12"}]},
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"certification": ""},
                        {"certification": "PG"},
                    ],
                },
            ]
        },
        "movie/42/keywords": {"keywords": [{"name": "independent film"}]},
    }
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(endpoint: str, params: dict[str, Any] | None = None):
        calls.append((endpoint, params))
        return responses[endpoint]

    monkeypatch.setattr(client, "_get", get)
    properties = [
        "TMDbID",
        "IMDbID",
        "Title",
        "Overview",
        "Genres",
        "ReleaseDate",
        "Runtime",
        "OriginalLanguage",
        "ProductionCompanies",
        "UserRating",
        "Cast",
        "Writers",
        "Directors",
        "ContentRating",
        "Keywords",
        "Budget",
        "Revenue",
        "Status",
    ]

    metadata = client.get_metadata(42, properties, region="US", language="fr")

    assert calls == [
        ("movie/42", {"language": "fr"}),
        ("movie/42/credits", None),
        ("movie/42/release_dates", None),
        ("movie/42/keywords", None),
    ]
    assert metadata.tmdb_id == "movie/42"
    assert metadata.imdb_id == "tt0042"
    assert metadata.title == "Test Movie"
    assert metadata.overview == "Test overview"
    assert metadata.genres == ["Documentary"]
    assert metadata.release_date == "2024-04-05"
    assert metadata.runtime == 117
    assert metadata.original_language == "en"
    assert metadata.production_companies == ["Test Studio"]
    assert metadata.user_rating == 8.25
    assert metadata.cast == [f"Cast {index}" for index in range(10)]
    assert metadata.writers == ["Writer (Screenplay)"]
    assert metadata.directors == ["Director"]
    assert metadata.content_rating == "PG"
    assert metadata.keywords == ["independent film"]
    assert metadata.custom_properties == {
        "Budget": "$1,200.00",
        "Revenue": "$2,500.00",
        "Status": "Released",
    }


def test_get_metadata_fetches_only_requested_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmdbClient("test-key")
    calls: list[str] = []

    def get(endpoint: str, _params: dict[str, Any] | None = None):
        calls.append(endpoint)
        return {"title": "Only Title"}

    monkeypatch.setattr(client, "_get", get)

    metadata = client.get_metadata(42, ["Title"])

    assert metadata.title == "Only Title"
    assert calls == ["movie/42"]


def test_region_content_rating_requires_non_empty_certification() -> None:
    release_info = {
        "results": [
            {"iso_3166_1": "GB", "release_dates": [{"certification": "12"}]},
            {"iso_3166_1": "US", "release_dates": [{"certification": ""}]},
        ]
    }

    assert _region_content_rating(release_info, "GB") == "12"
    assert _region_content_rating(release_info, "US") is None
    assert _region_content_rating(release_info, "CA") is None


def test_metadata_preview_rows_order_and_truncation() -> None:
    metadata = MovieMetadata(
        title="Preview Movie",
        tmdb_id="movie/42",
        imdb_id="tt0042",
        release_date="2024-04-05",
        runtime=117,
        genres=["Documentary", "Music"],
        user_rating=8.25,
        content_rating="PG",
        original_language="en",
        directors=["Director One", "Director Two"],
        cast=[f"Cast {index}" for index in range(6)],
        overview="A" * 121,
    )
    metadata.custom_properties["Budget"] = "$1,200.00"
    metadata.custom_properties["Status"] = "Released"

    rows = tagger._metadata_preview_rows(metadata)

    assert rows == [
        ("Title", "Preview Movie"),
        ("TMDb ID", "movie/42"),
        ("IMDb ID", "tt0042"),
        ("Release Date", "2024-04-05"),
        ("Runtime", "117 min"),
        ("Genres", "Documentary, Music"),
        ("User Rating", "8.25"),
        ("Content Rating", "PG"),
        ("Language", "en"),
        ("Directors", "Director One, Director Two"),
        ("Cast", ", ".join(f"Cast {index}" for index in range(5))),
        ("Overview", "A" * 117 + "..."),
        ("Budget", "$1,200.00"),
        ("Status", "Released"),
    ]
    assert tagger._truncate_preview_overview("A" * 120) == "A" * 120


def test_display_preview_prints_plain_rows_when_rich_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "HAS_RICH", False)
    client = TmdbClient("test-key")

    client.display_preview(MovieMetadata(title="Plain Preview"))

    assert capsys.readouterr().out == "  Title: Plain Preview\n"


def test_display_preview_uses_rich_renderer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "HAS_RICH", True)
    rendered: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(
        tagger,
        "_print_rich_metadata_rows",
        lambda rows: rendered.append(rows),
    )
    client = TmdbClient("test-key")

    client.display_preview(MovieMetadata(title="Rich Preview"))

    assert rendered == [[("Title", "Rich Preview")]]
    assert capsys.readouterr().out == ""


def test_tagging_search_title_respects_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = TagOptions(
        title_override="Override Title",
        year_override=2020,
    )

    assert tagger._tagging_search_title("ignored.mkv", override) == (
        "Override Title",
        2020,
    )

    monkeypatch.setattr(tagger, "sanitize_title", lambda _name: ("Clean Title", 2000))
    year_override = TagOptions(year_override=1999)

    assert tagger._tagging_search_title("movie.2000.mkv", year_override) == (
        "Clean Title",
        1999,
    )


def test_fetch_and_confirm_metadata_uses_search_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmdbClient("test-key")
    metadata = MovieMetadata(title="Fetched")
    opts = TagOptions(region="CA", language="fr", confirm=False)
    calls: list[Any] = []

    monkeypatch.setattr(
        client,
        "get_movie_id",
        lambda title, year=None: (
            calls.append(("movie-id", title, year, "", None)) or 42
        ),
    )
    monkeypatch.setattr(
        client,
        "get_metadata",
        lambda movie_id, props, region="US", language=None: (
            calls.append(("metadata", movie_id, None, region, language)) or metadata
        ),
    )
    monkeypatch.setattr(
        client,
        "display_preview",
        lambda preview: calls.append(("preview", preview.title, None, "", None)),
    )

    result = tagger._fetch_and_confirm_metadata(client, "Search Title", 1984, opts)

    assert result is metadata
    assert calls == [
        ("movie-id", "Search Title", 1984, "", None),
        ("metadata", 42, None, "CA", "fr"),
        ("preview", "Fetched", None, "", None),
    ]


def test_fetch_and_confirm_metadata_honours_user_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmdbClient("test-key")
    metadata = MovieMetadata(title="Cancelled")
    opts = TagOptions(confirm=True)
    previews: list[MovieMetadata] = []
    monkeypatch.setattr(client, "get_movie_id", lambda _title, _year=None: 42)
    monkeypatch.setattr(client, "get_metadata", lambda *_args, **_kwargs: metadata)
    monkeypatch.setattr(client, "display_preview", previews.append)
    monkeypatch.setattr(tagger, "_tag_confirm", lambda _prompt: False)

    assert tagger._fetch_and_confirm_metadata(client, "Title", None, opts) is None
    assert previews == [metadata]


def test_prepare_art_attachments_selects_and_downloads_requested_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    client = TmdbClient("test-key")
    metadata = MovieMetadata(
        poster_path="/poster.png",
        backdrop_path="/backdrop.jpg",
    )
    opts = TagOptions(art="both")
    downloads: list[str] = []

    def download_image(image_path: str, size: str = "original"):
        downloads.append(image_path)
        return b"png" if image_path.endswith(".png") else b"jpeg"

    monkeypatch.setattr(client, "download_image", download_image)
    temp_files: list[Path] = []

    attachments = tagger._prepare_art_attachments(client, metadata, opts, temp_files)

    assert downloads == ["/poster.png", "/backdrop.jpg"]
    assert len(attachments) == 2
    assert [attachment["mime"] for attachment in attachments] == [
        "image/png",
        "image/jpeg",
    ]
    assert [attachment["filename"] for attachment in attachments] == [
        "cover.jpg",
        "fanart.jpg",
    ]
    assert [attachment["label"] for attachment in attachments] == [
        "Poster",
        "Backdrop",
    ]
    assert [attachment["path"] for attachment in attachments] == temp_files
    assert temp_files[0].read_bytes() == b"png"
    assert temp_files[1].read_bytes() == b"jpeg"


def test_prepare_tagging_skips_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _opts: None)
    monkeypatch.setattr(
        tagger,
        "TmdbClient",
        lambda _key: (_ for _ in ()).throw(AssertionError),
    )

    assert tagger._prepare_tagging("movie.mkv", TagOptions(), []) == (None, [])


def test_prepare_tagging_fetches_metadata_and_artwork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    metadata = MovieMetadata(
        title="Full Result",
        poster_path="/poster.jpg",
    )
    opts = TagOptions(
        title_override="Override",
        year_override=2020,
        art="poster",
        confirm=False,
    )
    created_clients: list[FakeTaggingClient] = []
    temp_files: list[Path] = []

    class FakeTaggingClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            created_clients.append(self)

        def get_movie_id(self, title: str, year: int | None = None) -> int:
            assert (title, year) == ("Override", 2020)
            return 42

        def get_metadata(
            self,
            movie_id: int,
            properties: list[str],
            region: str = "US",
            language: str | None = None,
        ) -> MovieMetadata:
            assert movie_id == 42
            return metadata

        def display_preview(self, preview: MovieMetadata) -> None:
            assert preview is metadata

        def download_image(
            self, image_path: str, size: str = "original"
        ) -> bytes | None:
            assert image_path == "/poster.jpg"
            return b"image"

    monkeypatch.setattr(tagger, "_resolve_tmdb_key", lambda _opts: "test-key")
    monkeypatch.setattr(tagger, "TmdbClient", FakeTaggingClient)

    result_metadata, attachments = tagger._prepare_tagging(
        "ignored.mkv", opts, temp_files
    )

    assert result_metadata is metadata
    assert len(created_clients) == 1
    assert created_clients[0].api_key == "test-key"
    assert len(attachments) == 1
    assert attachments[0]["path"] == temp_files[0]
    assert temp_files[0].read_bytes() == b"image"


def test_metadata_preview_row_helpers_split_optional_groups() -> None:
    metadata = MovieMetadata(
        title="Core Only",
        release_date="2024-04-05",
        runtime=90,
        directors=["Director"],
        cast=["Cast One", "Cast Two"],
        overview="Overview",
    )

    assert tagger._core_metadata_preview_rows(metadata) == [
        ("Title", "Core Only"),
        ("Release Date", "2024-04-05"),
        ("Runtime", "90 min"),
    ]
    assert tagger._people_metadata_preview_rows(metadata) == [
        ("Directors", "Director"),
        ("Cast", "Cast One, Cast Two"),
    ]
    assert tagger._overview_metadata_preview_row(metadata) == [("Overview", "Overview")]
    assert tagger._metadata_preview_rows(MovieMetadata()) == []


def test_apply_core_metadata_respects_property_selection() -> None:
    metadata = MovieMetadata()
    info = {
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "imdb_id": "",
        "title": "Selected Title",
        "overview": "Unselected overview",
        "release_date": "2024-04-05",
        "runtime": 117,
        "vote_average": 8.25,
        "genres": [{"name": "Documentary"}],
    }

    tagger._apply_core_metadata(metadata, info, ["TMDbID", "Title"], 42)

    assert metadata.poster_path == "/poster.jpg"
    assert metadata.backdrop_path == "/backdrop.jpg"
    assert metadata.tmdb_id == "movie/42"
    assert metadata.imdb_id is None
    assert metadata.title == "Selected Title"
    assert metadata.overview is None
    assert metadata.release_date is None
    assert metadata.runtime is None
    assert metadata.user_rating is None
    assert metadata.genres is None
