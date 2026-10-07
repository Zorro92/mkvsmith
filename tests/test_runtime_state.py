"""Tests for the grouped runtime-state object and CLI state application."""

from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Callable

import pytest

from mkvsmith import cli
from mkvsmith import disc_reader
from mkvsmith import dvdifo
from mkvsmith import models
from mkvsmith import scan
from mkvsmith.models import Config, DiscMetadata, RuntimeState


def test_config_instances_do_not_share_defaults() -> None:
    first = Config()
    second = Config()

    first.preferred_languages.append("fr")

    assert first.preferred_languages == ["eng", "en", "und", "fr"]
    assert second.preferred_languages == ["eng", "en", "und"]


def test_runtime_state_owns_isolated_controllers() -> None:
    first = RuntimeState()
    second = RuntimeState()

    first.cleanup.register_temp_dir(Path("first-dir"))
    first.cleanup.register_temp_file(Path("first-file"))
    first.active_processes.register_muxer(42)
    first.active_processes.register_output(Path("first-output"))

    assert first.cleanup.temp_dirs == [Path("first-dir")]
    assert first.cleanup.temp_files == [Path("first-file")]
    assert first.active_processes.muxer_pgids == [42]
    assert first.active_processes.output_files == [Path("first-output")]
    assert second.cleanup.temp_dirs == []
    assert second.cleanup.temp_files == []
    assert second.active_processes.muxer_pgids == []
    assert second.active_processes.output_files == []


def test_cleanup_helpers_use_injected_runtime_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = RuntimeState()
    monkeypatch.setattr(models, "RUNTIME_STATE", runtime_state)
    output = tmp_path / "partial.mkv"
    output.write_bytes(b"partial")
    runtime_state.active_processes.register_muxer(42)
    runtime_state.active_processes.register_output(output)
    runtime_state.cleanup.register_temp_file(tmp_path / "temporary.tmp")
    runtime_state.active_processes.progress_active = True

    models._kill_active_muxers()
    models.finish_progress_line()
    models.cleanup_temp_dirs()

    assert runtime_state.active_processes.muxer_pgids == []
    assert runtime_state.active_processes.output_files == []
    assert runtime_state.active_processes.progress_active is False
    assert not output.exists()
    assert not runtime_state.cleanup.temp_files[0].exists()


def test_apply_parsed_args_populates_config_and_tag_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_state = RuntimeState()
    config = runtime_state.config
    tag_options = runtime_state.tag_options
    source = tmp_path / "movie.iso"
    output = tmp_path / "rips"
    temp_dir = tmp_path / "scratch"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mkvsmith",
            str(source),
            str(output),
            "--lang",
            "en,und",
            "--no-all-audio",
            "--no-subs",
            "--no-forced",
            "--min-duration",
            "42",
            "--debug",
            "--temp-dir",
            str(temp_dir),
            "--ram-limit",
            "0.5",
            "--show-all",
            "--ui-lang",
            "es",
            "--tag",
            "--no-tag",
            "--tmdb-key",
            "secret",
            "--tag-metadata",
            "Title,Overview",
            "--tag-region",
            "CA",
            "--tag-language",
            "fr",
            "--tag-art",
            "both",
            "--save-tag-xml",
            "--no-tag-confirm",
            "--tag-title",
            "Override",
            "--tag-year",
            "2020",
        ],
    )

    result = cli._apply_parsed_args(cli._build_arg_parser().parse_args(), runtime_state)

    assert result == (source, None, None, None)
    assert config.output_dir == output
    assert config.preferred_languages == ["en", "und"]
    assert config.keep_all_audio is False
    assert config.keep_all_subtitles is False
    assert config.include_forced is False
    assert config.min_duration == 42
    assert config.debug is True
    assert runtime_state.logger.debug_enabled is True
    assert config.temp_dir == temp_dir
    assert config.ram_limit == 0.5
    assert config.show_all is True
    assert config.ui_lang == "es"
    # --no-tag beats --tag.
    assert tag_options.enabled is False
    assert tag_options.no_tag is True
    assert tag_options.api_key == "secret"
    assert tag_options.metadata == ["Title", "Overview"]
    assert tag_options.region == "CA"
    assert tag_options.language == "fr"
    assert tag_options.art == "both"
    assert tag_options.save_xml is True
    assert tag_options.confirm is False
    assert tag_options.title_override == "Override"
    assert tag_options.year_override == 2020


def test_configure_runtime_uses_injected_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_state = RuntimeState(config=Config(debug=True))
    runtime_state.config.temp_dir = tmp_path / "scratch"
    debug_loggers: list[Callable[[str], None] | None] = []
    budgets: list[models.Config] = []
    monkeypatch.setattr(cli.dvdifo, "set_debug", debug_loggers.append)

    def fake_init_ram_budget(config: models.Config) -> None:
        budgets.append(config)

    monkeypatch.setattr("mkvsmith.disc_reader.init_ram_budget", fake_init_ram_budget)

    def only_tmp_path(_config: models.Config | None = None) -> list[Path]:
        return [tmp_path]

    monkeypatch.setattr("mkvsmith.disc_reader.temp_base_candidates", only_tmp_path)
    old_tempdir = __import__("tempfile").tempdir

    try:
        cli._configure_runtime(runtime_state)
    finally:
        __import__("tempfile").tempdir = old_tempdir

    assert len(debug_loggers) == 1
    logger = debug_loggers[0]
    assert logger is not None
    logger("Injected through controller")
    assert "Injected through controller" in capsys.readouterr().out
    assert budgets == [runtime_state.config]
    assert runtime_state.config.temp_dir.is_dir()


def test_init_ram_budget_uses_injected_config_without_global_mutation() -> None:
    config = Config(ram_limit=0, ram_budget_bytes=123)
    global_config = Config(ram_limit=0, ram_budget_bytes=456)

    disc_reader.init_ram_budget(config)

    assert config.ram_budget_bytes is None
    assert global_config.ram_budget_bytes == 456


def test_scanner_uses_injected_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = Config(min_duration=17)
    source = tmp_path / "movie.iso"
    scan_calls: list[tuple[Path, Config]] = []

    def fake_detect_source_type(_source: Path) -> disc_reader.SourceType:
        return disc_reader.SourceType.DVD

    def fake_scan_dvd_source(
        source: Path, config: Config | None = None
    ) -> tuple[list[models.Title], DiscMetadata]:
        scan_calls.append((source, config or Config()))
        return [], DiscMetadata(name="Injected Disc")

    monkeypatch.setattr(disc_reader, "detect_source_type", fake_detect_source_type)
    monkeypatch.setattr(scan, "_scan_dvd_source", fake_scan_dvd_source)

    scanner = scan.Scanner(source, config)

    assert scanner.config is config
    assert scanner.scan() == []
    assert scan_calls == [(source, config)]
    assert scanner.disc_name == "Injected Disc"


def test_mkvm_creator_selects_streams_with_injected_config(tmp_path: Path) -> None:
    config = Config(
        preferred_languages=["fr"],
        keep_all_audio=False,
        keep_all_subtitles=True,
        include_forced=False,
    )
    title = models.Title(
        index=0,
        source_file=tmp_path / "movie.mkv",
        name="Movie",
        duration_seconds=100.0,
    )
    title.streams = [
        models.Stream(index=0, stream_type=models.StreamType.VIDEO),
        models.Stream(
            index=1,
            stream_type=models.StreamType.AUDIO,
            language="fr",
        ),
        models.Stream(
            index=2,
            stream_type=models.StreamType.AUDIO,
            language="en",
        ),
        models.Stream(
            index=3,
            stream_type=models.StreamType.SUBTITLE,
            language="en",
            is_forced=True,
        ),
        models.Stream(
            index=4,
            stream_type=models.StreamType.SUBTITLE,
            language="fr",
        ),
    ]
    runtime_state = RuntimeState(config=config)
    creator = cli.MKVCreator(tmp_path, runtime_state=runtime_state, config=config)

    selected = creator.select_streams(title)

    assert creator.config is config
    assert creator.cleanup is runtime_state.cleanup
    assert creator.active_processes is runtime_state.active_processes
    assert [stream.index for stream in selected] == [0, 1, 4]


def test_temp_base_for_title_uses_injected_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = Config(
        temp_dir=tmp_path / "disk-temp",
        output_dir=tmp_path / "output",
        ram_budget_bytes=100,
    )
    assert config.temp_dir is not None
    config.temp_dir.mkdir()

    def fake_is_ram_backed_dir(_path: Path) -> bool:
        return False

    monkeypatch.setattr(disc_reader, "_is_ram_backed_dir", fake_is_ram_backed_dir)
    runtime_state = RuntimeState()
    monkeypatch.setattr(disc_reader, "RUNTIME_STATE", runtime_state)

    spill = disc_reader.temp_base_for_title(200, config)
    # Spills land in this run's marked session dir under the disk base.
    assert spill is not None and spill.parent == config.temp_dir
    assert spill.name.startswith(models.SESSION_DIR_PREFIX)
    assert runtime_state.cleanup.temp_dirs == [spill]
    assert disc_reader.temp_base_for_title(200, config) == spill
    assert disc_reader.temp_base_for_title(50, config) is None


def test_title_ranking_uses_injected_config() -> None:
    global_config = Config(show_all=False, min_duration=60)
    injected_config = Config(show_all=True, min_duration=120)
    title = models.Title(
        index=0,
        source_file=Path("short.mkv"),
        name="Short",
        duration_seconds=90.0,
    )
    title.streams = [
        models.Stream(index=0, stream_type=models.StreamType.VIDEO),
        models.Stream(index=1, stream_type=models.StreamType.AUDIO, language="en"),
    ]

    assert scan._get_notable_titles([title], injected_config) == ([title], 0)
    assert scan._get_notable_titles([title], global_config) == ([], 1)
    assert scan.pick_main_feature([title], global_config) == 0
    assert scan._main_feature_score(title, injected_config)[0] == -1
    assert scan._main_feature_score(title, global_config)[0] > -1


def test_runtime_logger_and_dvdifo_use_injected_logger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_state = RuntimeState(config=Config(debug=True))
    monkeypatch.setattr(models, "RUNTIME_STATE", runtime_state)

    models.log_debug("Model debug")
    dvdifo.set_debug(runtime_state.logger.debug)
    dvdifo.log_debug("DVDIFO debug")

    output = capsys.readouterr().out
    assert "Model debug" in output
    assert "DVDIFO debug" in output
