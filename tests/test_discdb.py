"""Tests for optional TheDiscDB lookup and contribution support."""

from __future__ import annotations

import hashlib
import email.message
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

import pytest
import cli
import session
import scan
import disc_reader

import discdb
from discdb import DiscDbClient, DiscDbOptions
from models import DiscMetadata, Stream, StreamType, Title
from models import RuntimeState
from settings import Settings
from scan import pick_main_feature


def make_title(
    index: int,
    source: Path,
    duration: float,
    *,
    playlist: str | None = None,
    dvd_title_id: int | None = None,
) -> Title:
    title = Title(
        index=index,
        source_file=source,
        name=f"Title {index}",
        duration_seconds=duration,
    )
    title.playlist_name = playlist
    title.dvd_title_id = dvd_title_id
    title.streams = [
        Stream(index=0, stream_type=StreamType.VIDEO, codec="h264", pid=4113)
    ]
    return title


def remote_item(
    *,
    global_id: str | None = None,
    fingerprint: str | None = None,
    content_hash: str = "HASH",
    titles: list[dict[str, Any]] | None = None,
    upc: str | None = None,
) -> discdb.DiscDbMediaItem:
    remote_titles = [discdb.DiscDbRemoteTitle(**title) for title in titles or []]
    release = discdb.DiscDbRemoteRelease(
        slug="release",
        title="Release",
        year=2024,
        locale="en-us",
        regionCode="A",
        upc=upc,
        discs=[
            discdb.DiscDbRemoteDisc(
                index=0,
                name="Disc 1",
                format="Blu-Ray",
                slug="disc-1",
                contentHash=content_hash,
                globalDiscId=global_id,
                fingerprint=fingerprint,
                titles=remote_titles,
            )
        ],
    )
    return discdb.DiscDbMediaItem(
        title="Remote Movie",
        year=2024,
        slug="remote-movie-2024",
        type="Movie",
        externalids=discdb.DiscDbExternalIds(tmdb="42", imdb="tt0042"),
        releases=[release],
    )


def options(**values: Any) -> DiscDbOptions:
    return DiscDbOptions(**values)


def test_graphql_urls_use_separate_public_and_contribution_schemas():
    assert discdb.graphql_url("https://thediscdb.com/") == (
        "https://thediscdb.com/graphql/"
    )
    assert discdb.contribution_graphql_url("https://thediscdb.com") == (
        "https://thediscdb.com/graphql/contributions/"
    )


def test_lookup_sends_all_strong_identifiers(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def post(
        _self: DiscDbClient, _query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        captured.update(variables)
        return {"mediaItems": {"nodes": []}}

    monkeypatch.setattr(DiscDbClient, "_post_json", post)
    metadata = DiscMetadata(
        disc_hash="D" * 32,
        matrix256_fingerprint="f" * 64,
        aacs_disc_id="A" * 40,
        libdvdread_disc_id="D" * 32,
        upc_ean="123",
    )

    result = DiscDbClient(options()).lookup(metadata)

    assert result.items == ()
    assert len(captured["where"]["or"]) == 4


def test_disc_hash_uses_the_reference_size_algorithm():
    expected = hashlib.md5((1).to_bytes(8, "little") + (2).to_bytes(8, "little"))

    assert discdb.calculate_disc_hash([1, 2]) == expected.hexdigest().upper()


def test_disc_hash_from_paths_selects_sorted_video_files_only():
    disc_hash = discdb.calculate_disc_hash_from_paths(
        [
            ("BDMV/STREAM/01002.m2ts", 3),
            ("BDMV/CLIPINF/01002.clpi", 99),
            ("BDMV/STREAM/01001.m2ts", 2),
            ("VIDEO_TS/VIDEO_TS.IFO", 7),
        ]
    )

    expected = hashlib.md5(
        (2).to_bytes(8, "little")
        + (3).to_bytes(8, "little")
        + (7).to_bytes(8, "little")
    )
    assert disc_hash == expected.hexdigest().upper()


class _FakeResponse:
    def __init__(self, body: str, url: str | None = None) -> None:
        self._body = body.encode("utf-8")
        self._url = url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url or "https://thediscdb.com/graphql/"


def test_graphql_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch):
    body = json.dumps({"errors": [{"message": "field unavailable"}]})

    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(body)

    monkeypatch.setattr(discdb.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(discdb.DiscDbError, match="field unavailable"):
        DiscDbClient(options())._post_json("query {}", {})


def test_http_and_network_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch):
    error = urllib.error.HTTPError(
        "https://thediscdb.com/graphql/",
        503,
        "Unavailable",
        email.message.Message(),
        io.BytesIO(b"try later"),
    )

    def raise_http_error(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    monkeypatch.setattr(discdb.urllib.request, "urlopen", raise_http_error)

    with pytest.raises(discdb.DiscDbError, match="HTTP 503"):
        DiscDbClient(options())._post_json("query {}", {})

    def raise_timeout(*_args: object, **_kwargs: object) -> NoReturn:
        raise TimeoutError("timed out")

    monkeypatch.setattr(discdb.urllib.request, "urlopen", raise_timeout)
    with pytest.raises(discdb.DiscDbError, match="timed out"):
        DiscDbClient(options(timeout_seconds=0.01))._post_json("query {}", {})


def test_authentication_redirect_is_reported_clearly(monkeypatch: pytest.MonkeyPatch):
    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            "<html>login</html>",
            "https://thediscdb.com/Account/Login?ReturnUrl=/graphql/contributions",
        )

    monkeypatch.setattr(discdb.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(discdb.DiscDbError, match="check or refresh your cookie"):
        DiscDbClient(options())._post_json(
            "mutation {}",
            {},
            url=discdb.contribution_graphql_url("https://thediscdb.com"),
        )


def test_strong_match_accepts_identical_recopies(monkeypatch: pytest.MonkeyPatch):
    item = remote_item(global_id="A" * 40, titles=[])

    def fake_post_json(*_args: object) -> dict[str, Any]:
        return {"mediaItems": {"nodes": [item]}}

    monkeypatch.setattr(DiscDbClient, "_post_json", fake_post_json)
    result = DiscDbClient(options()).lookup(DiscMetadata(aacs_disc_id="a" * 40))

    assert result.match is not None
    assert result.match.disc is item["releases"][0]["discs"][0]
    assert result.ambiguous is False


def test_disc_hash_is_a_strong_match(monkeypatch: pytest.MonkeyPatch):
    item = remote_item(content_hash="C" * 32, titles=[])

    def fake_post_json(*_args: object) -> dict[str, Any]:
        return {"mediaItems": {"nodes": [item]}}

    monkeypatch.setattr(DiscDbClient, "_post_json", fake_post_json)

    result = DiscDbClient(options()).lookup(DiscMetadata(disc_hash="c" * 32))

    assert result.match is not None
    assert result.match.disc is item["releases"][0]["discs"][0]


def test_upc_ambiguity_is_not_auto_applied(monkeypatch: pytest.MonkeyPatch):
    first = remote_item(
        upc="123",
        titles=[{"index": 0, "duration": "1:00:00", "sourceFile": "00800.mpls"}],
    )
    second = remote_item(
        upc="123",
        titles=[{"index": 0, "duration": "1:30:00", "sourceFile": "00801.mpls"}],
    )

    def fake_post_json(*_args: object) -> dict[str, Any]:
        return {"mediaItems": {"nodes": [first, second]}}

    monkeypatch.setattr(DiscDbClient, "_post_json", fake_post_json)

    result = DiscDbClient(options()).lookup(DiscMetadata(upc_ean="123"))

    assert result.match is None


def test_apply_bluray_match_and_remote_main_priority(tmp_path: Path):
    main = make_title(
        0,
        tmp_path / "00822.mpls",
        2 * 3600 + 16 * 60 + 20,
        playlist="00822",
    )
    decoy = make_title(
        1,
        tmp_path / "00999.mpls",
        2 * 3600 + 16 * 60 + 30,
        playlist="00999",
    )
    decoy.streams.extend(
        [
            Stream(index=1, stream_type=StreamType.AUDIO, codec="dts"),
            Stream(index=2, stream_type=StreamType.SUBTITLE, codec="hdmv_pgs_subtitle"),
        ]
    )
    remote_titles = [
        {
            "index": 0,
            "duration": "2:16:20",
            "sourceFile": "00822.mpls",
            "filename": "Remote Movie (2024) [1080p].mkv",
            "item": {
                "title": "Remote Movie",
                "season": None,
                "episode": 4,
                "type": "MainMovie",
            },
        }
    ]
    lookup = discdb.DiscDbLookupResult(
        items=(remote_item(global_id="A" * 40, titles=remote_titles),),
        match=discdb.DiscDbMatch(
            remote_item(global_id="A" * 40, titles=remote_titles),
            remote_item(global_id="A" * 40, titles=remote_titles)["releases"][0],
            remote_item(global_id="A" * 40, titles=remote_titles)["releases"][0][
                "discs"
            ][0],
        ),
    )

    applied = discdb.apply_discdb_match([main, decoy], DiscMetadata(), lookup)

    assert applied is not None
    assert applied.title_indexes == (0,)
    assert main.name == "Remote Movie (2024) [1080p]"
    assert main.discdb_episode_number == 4
    assert main.discdb_is_main_movie is True
    assert pick_main_feature([decoy, main]) == main.index


def test_apply_dvd_match_uses_logical_title_id(tmp_path: Path):
    title = make_title(
        0,
        tmp_path / "VTS_01_1.VOB",
        3600,
        dvd_title_id=7,
    )
    item = remote_item(
        global_id="D" * 32,
        titles=[
            {
                "index": 0,
                "duration": "1:00:00",
                "sourceFile": "07",
                "filename": "Episode 7.mkv",
                "item": {
                    "title": "Episode 7",
                    "season": 2,
                    "episode": 7,
                    "type": "Episode",
                },
            }
        ],
    )
    result = discdb.DiscDbLookupResult(
        items=(item,),
        match=discdb.DiscDbMatch(
            item, item["releases"][0], item["releases"][0]["discs"][0]
        ),
    )

    applied = discdb.apply_discdb_match([title], DiscMetadata(), result)

    assert applied is not None
    assert title.name == "Episode 7"
    assert title.discdb_season_number == 2
    assert title.discdb_episode_number == 7


def test_duration_mismatch_prevents_title_application(tmp_path: Path):
    title = make_title(0, tmp_path / "00800.mpls", 3600, playlist="00800")
    item = remote_item(
        fingerprint="f" * 64,
        titles=[
            {
                "index": 0,
                "duration": "1:30:00",
                "sourceFile": "00800.mpls",
                "filename": "Wrong Cut.mkv",
            }
        ],
    )
    result = discdb.DiscDbLookupResult(
        items=(item,),
        match=discdb.DiscDbMatch(
            item, item["releases"][0], item["releases"][0]["discs"][0]
        ),
    )

    assert discdb.apply_discdb_match([title], DiscMetadata(), result) is None
    assert title.name == "Title 0"


def test_duplicate_local_editions_do_not_share_one_remote_title(tmp_path: Path):
    default = make_title(0, tmp_path / "01.VOB", 3600, dvd_title_id=1)
    alternate = make_title(1, tmp_path / "01.VOB", 3600, dvd_title_id=1)
    alternate.dvd_pgc_number = 2
    item = remote_item(
        global_id="D" * 32,
        titles=[
            {
                "index": 0,
                "duration": "1:00:00",
                "sourceFile": "01",
                "filename": "Main Movie.mkv",
                "item": {
                    "title": "Main Movie",
                    "season": None,
                    "episode": None,
                    "type": "MainMovie",
                },
            }
        ],
    )
    result = discdb.DiscDbLookupResult(
        items=(item,),
        match=discdb.DiscDbMatch(
            item, item["releases"][0], item["releases"][0]["discs"][0]
        ),
    )

    assert (
        discdb.apply_discdb_match([default, alternate], DiscMetadata(), result) is None
    )
    assert default.name == "Title 0"
    assert alternate.name == "Title 1"
    assert default.discdb_is_main_movie is False


def test_synthetic_scan_log_contains_parser_line_families():
    title = make_title(0, Path("BDMV/STREAM/01000.m2ts"), 3661, playlist="00800")
    title.disc_name = "Remote Movie"
    title.estimated_size_bytes = 1_500_000_000
    title.chapters = [0.0, 600.0]
    title.iso_internal_paths = ["BDMV/STREAM/01000.m2ts", "BDMV/STREAM/01001.m2ts"]
    title.streams.append(Stream(index=1, stream_type=StreamType.AUDIO, codec="dts"))

    log = discdb.synthetic_scan_log([title], is_bluray=True)

    assert "TCOUNT:1" in log
    assert 'CINFO:2,0,"Remote Movie"' in log
    assert 'TINFO:0,9,0,"1:01:01"' in log
    assert 'TINFO:0,16,0,"00800.mpls"' in log
    assert 'TINFO:0,26,0,"01000,01001"' in log
    assert 'SINFO:0,0,1,0,"Video"' in log
    assert 'SINFO:0,0,7,0,"H.264"' in log


def test_bundle_contains_hash_files_and_logs(tmp_path: Path):
    source = tmp_path / "disc"
    video_ts = source / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    (video_ts / "VIDEO_TS.IFO").write_bytes(b"vmg")
    (video_ts / "VTS_01_1.VOB").write_bytes(b"vob")
    title = make_title(0, video_ts / "VTS_01_1.VOB", 3600, dvd_title_id=1)
    metadata = DiscMetadata(
        upc_ean="123",
        libdvdread_disc_id="D" * 32,
        disc_hash=discdb.calculate_disc_hash([4, 8]),
    )

    bundle = discdb.build_contribution_bundle(
        source,
        [title],
        metadata,
        options(bundle_dir=tmp_path / "bundle"),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["identifiers"]["libdvdread_disc_id"] == "D" * 32
    assert manifest["identifiers"]["disc_hash"] == discdb.calculate_disc_hash([4, 8])
    assert [entry["name"] for entry in manifest["hash_files"]] == [
        "VIDEO_TS.IFO",
        "VTS_01_1.VOB",
    ]
    assert manifest["fingerprint_files"] == [
        {"path": "VIDEO_TS/VIDEO_TS.IFO", "size": 3},
        {"path": "VIDEO_TS/VTS_01_1.VOB", "size": 3},
    ]
    assert manifest["titles"][0]["segment_map"] == ""
    log = (bundle / "scan_log.txt").read_text()
    assert 'TINFO:0,24,0,"01"' in log
    assert "TINFO:0,26," not in log


def test_contribution_bundle_rejects_plain_video_source(tmp_path: Path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"video")
    title = make_title(0, source, 3600)

    with pytest.raises(discdb.DiscDbError, match="DVD or Blu-ray source"):
        discdb.build_contribution_bundle(
            source,
            [title],
            DiscMetadata(),
            options(bundle_dir=tmp_path / "bundle"),
        )


def test_contribution_format_distinguishes_uhd_bluray():
    hd_title = make_title(0, Path("00800.mpls"), 3600, playlist="00800")
    uhd_title = make_title(1, Path("00801.mpls"), 3600, playlist="00801")
    uhd_title.video_streams[0].width = 3840
    uhd_title.video_streams[0].height = 2160

    assert discdb._contribution_format([hd_title], True) == "Blu-ray"
    assert discdb._contribution_format([uhd_title], True) == "4K"
    assert discdb._contribution_format([hd_title], False) == "DVD"


def test_iso_hash_files_use_internal_timestamps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    source = tmp_path / "movie.iso"
    source.write_bytes(b"iso")

    def fake_list_metadata(_source: Path) -> list[disc_reader._IsoFileMetadata]:
        return [
            disc_reader._IsoFileMetadata(
                "BDMV/STREAM/01000.m2ts",
                123,
                # UDF timestamps carry their own zone; +01:00 -> 02:04:05Z.
                datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=1))),
            ),
            disc_reader._IsoFileMetadata("VIDEO_TS/VIDEO_TS.IFO", 456, None),
        ]

    monkeypatch.setattr("disc_reader._list_iso_file_metadata", fake_list_metadata)

    files = discdb.collect_hash_files(source)

    assert files == [
        {
            "index": 0,
            "name": "01000.m2ts",
            "creationTime": "2024-01-02T02:04:05Z",
            "size": 123,
        },
        {
            "index": 1,
            "name": "VIDEO_TS.IFO",
            "creationTime": discdb._iso_datetime(source.stat().st_mtime),
            "size": 456,
        },
    ]


def test_iso_fingerprint_files_include_every_iso_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    source = tmp_path / "movie.iso"
    source.write_bytes(b"iso")

    def fake_list_files(_source: Path) -> tuple[list[str], dict[str, int]]:
        return (
            ["BDMV/META/dl/bdmt_eng.xml", "BDMV/STREAM/01000.m2ts"],
            {"BDMV/META/dl/bdmt_eng.xml": 10, "BDMV/STREAM/01000.m2ts": 20},
        )

    monkeypatch.setattr("disc_reader._list_iso_files", fake_list_files)

    assert discdb.collect_fingerprint_files(source) == [
        {"path": "BDMV/META/dl/bdmt_eng.xml", "size": 10},
        {"path": "BDMV/STREAM/01000.m2ts", "size": 20},
    ]


def test_direct_submission_uses_hash_create_upload_and_status(
    monkeypatch: pytest.MonkeyPatch,
):
    manifest = {
        "source": {"format": "Blu-ray"},
        "identifiers": {
            "aacs_disc_id": "A" * 40,
            "matrix256_fingerprint": "F" * 64,
        },
        "fingerprint_files": [{"path": "BDMV/STREAM/01000.m2ts", "size": 123}],
        "hash_files": [
            {
                "index": 0,
                "name": "01000.m2ts",
                "creationTime": "2024-01-01T00:00:00Z",
                "size": 123,
            }
        ],
    }
    # Mixed call records; the create call's payload dict is indexed below.
    calls: list[tuple[Any, ...]] = []
    client_class = discdb.DiscDbContributionClient

    def fake_hash_disc(
        self: discdb.DiscDbContributionClient,
        contribution_id: str,
        files: list[discdb.FileHashInfo],
        fingerprint_files: list[discdb.DiscFingerprintFileInfo],
    ) -> tuple[str, str | None]:
        calls.append(("hash", contribution_id))
        return "CONTENT_HASH", "f" * 64

    def fake_create_disc(
        self: discdb.DiscDbContributionClient, *args: object, **kwargs: object
    ) -> str:
        calls.append(("create", args, kwargs))
        return "DISC_ID"

    def fake_upload_logs(
        self: discdb.DiscDbContributionClient,
        contribution_id: str,
        disc_id: str,
        logs: str,
    ) -> None:
        calls.append(("upload", contribution_id, disc_id))

    def fake_upload_status(
        self: discdb.DiscDbContributionClient, disc_id: str
    ) -> tuple[bool, str | None]:
        return True, None

    def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_class, "hash_disc", fake_hash_disc)
    monkeypatch.setattr(client_class, "create_disc", fake_create_disc)
    monkeypatch.setattr(client_class, "upload_logs", fake_upload_logs)
    monkeypatch.setattr(client_class, "upload_status", fake_upload_status)
    monkeypatch.setattr(discdb.time, "sleep", fake_sleep)

    disc_id, url = discdb.submit_contribution_bundle(
        manifest,
        "TCOUNT:1\n",
        options(cookie="session=1", contribution_id="contribution"),
    )

    assert disc_id == "DISC_ID"
    assert url.endswith("/contribution/contribution/discs/DISC_ID/identify")
    assert calls == [
        ("hash", "contribution"),
        (
            "create",
            ("contribution", "CONTENT_HASH"),
            {
                "format_name": "Blu-ray",
                "name": "Disc 1",
                "slug": "disc-1",
                "global_disc_id": "A" * 40,
                "fingerprint": "f" * 64,
            },
        ),
        ("upload", "contribution", "DISC_ID"),
    ]

    default_create = calls[1]
    calls.clear()
    monkeypatch.setattr(client_class, "create_disc", fake_create_disc)

    discdb.submit_contribution_bundle(
        manifest,
        "TCOUNT:1\n",
        options(
            cookie="session=1",
            contribution_id="contribution",
            disc_name="Bonus Disc",
        ),
    )

    assert default_create != calls[1]
    assert calls[1][2]["name"] == "Bonus Disc"
    assert calls[1][2]["slug"] == "bonus-disc"


def test_contribution_mutations_use_authenticated_contribution_schema(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    def post(
        _self: DiscDbClient,
        _query: str,
        _variables: dict[str, Any],
        *,
        cookie: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        captured.update(cookie=cookie, url=url, variables=_variables)
        return {
            "hashDisc": {
                "discHash": {"hash": "HASH", "fingerprint": "F" * 64},
                "errors": None,
            }
        }

    monkeypatch.setattr(DiscDbClient, "_post_json", post)
    client = discdb.DiscDbContributionClient(
        options(cookie="session=1", contribution_id="contribution")
    )

    assert client.hash_disc("contribution", [], []) == ("HASH", "F" * 64)
    assert captured == {
        "cookie": "session=1",
        "url": "https://thediscdb.com/graphql/contributions/",
        "variables": {
            "input": {
                "contributionId": "contribution",
                "files": [],
                "fingerprintFiles": [],
            }
        },
    }


def test_aacs_hash_reference_vector(tmp_path: Path):
    root = tmp_path
    (root / "AACS").mkdir()
    unit_key = root / "AACS" / "Unit_Key_RO.inf"
    unit_key.write_bytes(b"unit key")

    from scan import _compute_aacs_disc_id

    assert _compute_aacs_disc_id(root) == hashlib.sha1(b"unit key").hexdigest().upper()


def test_discdb_cli_options_default_to_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = RuntimeState()
    args = cli._build_arg_parser().parse_args([str(tmp_path)])

    cli._apply_parsed_args(args, state)

    assert state.discdb_options.enabled is False
    assert state.discdb_options.contribute is False
    assert state.discdb_options.base_url == "https://thediscdb.com"


def test_discdb_cli_flags_override_settings_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = RuntimeState(settings=Settings(discdb_enabled=False, discdb_timeout=9.0))
    monkeypatch.setenv("THEDISCDB_BASE_URL", "https://example.test")
    monkeypatch.setenv("THEDISCDB_COOKIE", "environment-cookie")
    args = cli._build_arg_parser().parse_args(
        [
            str(tmp_path),
            "--discdb",
            "--discdb-url",
            "https://discdb.test",
            "--discdb-timeout",
            "3.5",
            "--discdb-contribute=direct",
            "--discdb-bundle-dir",
            str(tmp_path / "bundle"),
            "--no-discdb-open",
            "--discdb-contribution-id",
            "contribution",
            "--discdb-disc-name",
            "Bonus Disc",
            "--discdb-cookie",
            " flag-cookie ",
        ]
    )

    cli._apply_parsed_args(args, state)

    assert state.discdb_options.enabled is True
    assert state.discdb_options.base_url == "https://discdb.test"
    assert state.discdb_options.timeout_seconds == 3.5
    assert state.discdb_options.contribute is True
    assert state.discdb_options.contribute_mode == "direct"
    assert state.discdb_options.bundle_dir == tmp_path / "bundle"
    assert state.discdb_options.open_browser is False
    assert state.discdb_options.contribution_id == "contribution"
    assert state.discdb_options.disc_name == "Bonus Disc"
    assert state.discdb_options.cookie == "flag-cookie"


def test_discdb_cli_reads_persistent_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = RuntimeState(
        settings=Settings(
            discdb_enabled=True,
            discdb_base_url="https://settings.test",
            discdb_timeout=4,
            discdb_open_browser=False,
            discdb_contribute="manual",
        )
    )
    args = cli._build_arg_parser().parse_args([str(tmp_path)])

    cli._apply_parsed_args(args, state)

    assert state.discdb_options.enabled is True
    assert state.discdb_options.base_url == "https://settings.test"
    assert state.discdb_options.timeout_seconds == 4
    assert state.discdb_options.open_browser is False
    assert state.discdb_options.contribution_id is None  # flag only
    assert state.discdb_options.contribute is True
    assert state.discdb_options.contribute_mode == "manual"


def test_cli_lookup_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    title = make_title(0, tmp_path / "00800.mpls", 3600, playlist="00800")
    state = RuntimeState(discdb_options=DiscDbOptions(enabled=True))

    def failing_lookup(*_args: object) -> NoReturn:
        raise discdb.DiscDbError("service unavailable")

    monkeypatch.setattr(DiscDbClient, "lookup", failing_lookup)

    session.apply_discdb_lookup([title], DiscMetadata(), state)

    assert title.name == "Title 0"
    captured = capsys.readouterr()
    assert "TheDiscDB lookup failed" in captured.out + captured.err


def test_cli_episode_actions_include_remote_episodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    local_episode = make_title(0, tmp_path / "01.VOB", 3600, dvd_title_id=1)
    local_episode.episode_number = 1
    remote_episode = make_title(1, tmp_path / "02.VOB", 3601, dvd_title_id=2)
    remote_episode.discdb_episode_number = 2
    state = RuntimeState()
    ripped: list[list[Title]] = []

    def fake_rip_title_batch(
        titles: list[Title], _state: RuntimeState | None = None
    ) -> None:
        ripped.append(titles)

    monkeypatch.setattr(cli, "_rip_title_batch", fake_rip_title_batch)

    cli._rip_episode_batch([remote_episode, local_episode], state)

    assert ripped == [[remote_episode, local_episode]]


def test_manual_contribution_preparation_does_not_open_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    title = make_title(0, tmp_path / "00800.mpls", 3600, playlist="00800")
    state = RuntimeState(
        discdb_options=DiscDbOptions(
            contribute=True,
            contribute_mode="manual",
            bundle_dir=tmp_path / "bundle",
            open_browser=True,
        )
    )
    built: list[Path] = []

    def fake_build_bundle(*_args: object) -> Path:
        built.append(tmp_path / "bundle")
        return tmp_path / "bundle"

    def fail_open_url(*_args: object) -> NoReturn:
        raise AssertionError

    monkeypatch.setattr(discdb, "build_contribution_bundle", fake_build_bundle)
    monkeypatch.setattr(discdb, "open_contribution_url", fail_open_url)

    cli._prepare_discdb_contribution(tmp_path, [title], DiscMetadata(), state)

    assert built == [tmp_path / "bundle"]


def test_main_exits_after_contribution_without_rip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = RuntimeState(
        discdb_options=DiscDbOptions(
            contribute=True,
            contribute_mode="manual",
            bundle_dir=tmp_path / "bundle",
        )
    )
    prepared: list[Path] = []
    monkeypatch.setattr(cli, "RUNTIME_STATE", state)

    def fake_initialize_cli(
        _state: RuntimeState | None = None,
    ) -> tuple[Path | None, str, int | None, list[str] | None, list[int] | None]:
        return tmp_path, "interactive", None, None, None

    def fake_configure_runtime(_state: RuntimeState | None = None) -> None:
        return None

    def fake_scan_source(
        _source: Path, _state: RuntimeState | None = None
    ) -> tuple[list[Title], DiscMetadata | None]:
        return [], DiscMetadata()

    def fake_prepare_contribution(
        source: Path,
        _titles: list[Title],
        _metadata: DiscMetadata | None,
        _runtime_state: RuntimeState,
    ) -> None:
        prepared.append(source)

    def fail_run_action(*_args: object) -> NoReturn:
        raise AssertionError

    monkeypatch.setattr(cli, "_initialize_cli", fake_initialize_cli)
    monkeypatch.setattr(cli, "_configure_runtime", fake_configure_runtime)
    monkeypatch.setattr(cli, "_scan_source", fake_scan_source)
    monkeypatch.setattr(cli, "_prepare_discdb_contribution", fake_prepare_contribution)
    monkeypatch.setattr(cli, "_run_action", fail_run_action)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert prepared == [tmp_path]


def test_iso_aacs_identifier_uses_bounded_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    scanner = scan.Scanner(tmp_path / "movie.iso", scan.Config(), RuntimeState())
    unit_key = tmp_path / "Unit_Key_RO.inf"
    unit_key.write_bytes(b"iso unit key")
    extracted: list[tuple[Path, str, dict[str, Any]]] = []

    def extract(iso_path: Path, internal_path: str, **kwargs: object) -> Path:
        extracted.append((iso_path, internal_path, kwargs))
        return unit_key

    monkeypatch.setattr("disc_reader._extract_iso_prefix", extract)

    scanner._add_iso_aacs_disc_id(["AACS/Unit_Key_RO.inf"])

    assert extracted == [
        (
            tmp_path / "movie.iso",
            "AACS/Unit_Key_RO.inf",
            {
                "size_mb": 16,
                "temp_files": scanner.cleanup.temp_files,
            },
        )
    ]
    assert scanner.disc_metadata.aacs_disc_id == (
        hashlib.sha1(b"iso unit key").hexdigest().upper()
    )


def test_iso_disc_hash_uses_existing_iso_listing_sizes(tmp_path: Path):
    scanner = scan.Scanner(tmp_path / "movie.iso", scan.Config(), RuntimeState())

    scanner._add_iso_discdb_disc_hash(
        [
            "BDMV/STREAM/01002.m2ts",
            "BDMV/STREAM/01001.m2ts",
            "BDMV/CLIPINF/01001.clpi",
        ],
        {
            "BDMV/STREAM/01002.m2ts": 3,
            "BDMV/STREAM/01001.m2ts": 2,
            "BDMV/CLIPINF/01001.clpi": 99,
        },
    )

    expected = (
        hashlib.md5((2).to_bytes(8, "little") + (3).to_bytes(8, "little"))
        .hexdigest()
        .upper()
    )
    assert scanner.disc_metadata.disc_hash == expected
