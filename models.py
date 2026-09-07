"""
Shared data model, configuration, and logging for mkvsmith.

Extracted from main.py. Contains:
- Stream / Title / Config / TagOptions data classes
- Global mutable state (temp dirs, temp files)
- Logging functions
- Language name lookup
- RipError exception
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, final

try:
    from rich.console import Console

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from dvdifo import _IFOAudioAttrs, _IFOSubpictureAttrs, _IFOVideoAttrs
from i18n import tr


# =============================================================================
# Console wrapper
# =============================================================================


class _Console:
    """Console wrapper.

    When Rich is available, delegates to ``rich.console.Console`` for
    styled output. Otherwise acts as a null writer — ``print()`` is a
    no-op so that code importing ``_console`` never crashes, even when
    Rich is not installed.
    """

    def __init__(self) -> None:
        if HAS_RICH:
            self._inner = Console()
        else:
            self._inner = None

    def print(self, *args: Any, **kwargs: Any) -> None:
        if self._inner is not None:
            self._inner.print(*args, **kwargs)


@dataclass
class RuntimeLogger:
    """Owns log verbosity and console rendering for one runtime state."""

    console: _Console = field(default_factory=_Console)
    debug_enabled: bool = False

    def configure(self, config: Config) -> None:
        # Config is declared later; runtime annotations defer resolution.
        self.debug_enabled = config.debug

    def set_debug(self, enabled: bool) -> None:
        self.debug_enabled = enabled

    def info(self, message: str) -> None:
        if HAS_RICH:
            self.console.print(f"[green][INFO][/green] {message}")
        else:
            print(f"[INFO] {message}")

    def warn(self, message: str) -> None:
        if HAS_RICH:
            self.console.print(f"[yellow][WARN][/yellow] {message}")
        else:
            print(f"[WARN] {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        if HAS_RICH:
            self.console.print(f"[red][ERROR][/red] {message}")
        else:
            print(f"[ERROR] {message}", file=sys.stderr)

    def debug(self, message: str) -> None:
        if not self.debug_enabled:
            return
        if HAS_RICH:
            self.console.print(f"[blue][DEBUG][/blue] {message}")
        else:
            print(f"[DEBUG] {message}")


# Check at module level whether mkvmerge is on PATH.
_HAS_MKVMERGE: bool = shutil.which("mkvmerge") is not None


# =============================================================================
# VTS IFO sector-pointer offsets
# =============================================================================


class _VTSSectorOffset:
    """VTS IFO header sector-pointer offsets (from the start of the IFO).

    Based on pyparsedvd's SectorOffset enum and the DVD-Video specification.
    """

    # Header fields (first sector, offsets in bytes from sector start)
    VTS_ID = 0x000  # 12 bytes: "DVDVIDEO-VTS"
    VTS_LAST_TITLE_SET_SECTOR = 0x00C  # 4 bytes
    VTS_LAST_IFO_SECTOR = 0x01C  # 4 bytes
    VTS_VERSION = 0x020  # 2 bytes
    VTS_CATEGORY = 0x022  # 4 bytes
    VTS_MAT_END = 0x080  # 4 bytes: end address of VTSI_MAT

    # Sector pointers (4 bytes each)
    SECTOR_PTR_VTS_PTT_SRPT = 0x0C8  # Title/Chapter table
    SECTOR_PTR_VTS_PGCI = 0x0CC  # Program Chain Information table
    SECTOR_PTR_VTSM_PGCI_UT = 0x0D0  # Menu PGC table
    SECTOR_PTR_VTS_TMAPTI = 0x0D4  # Time Map table
    SECTOR_PTR_VTSM_C_ADT = 0x0D8  # Menu Cell Address table
    SECTOR_PTR_VTSM_VOBU_ADMAP = 0x0DC
    SECTOR_PTR_VTS_C_ADT = 0x0E0  # Title Cell Address table
    SECTOR_PTR_VTS_VOBU_ADMAP = 0x0E4

    # Video attributes (menu + title VOBs)
    VTSM_VOBS_VIDEO_ATTR = 0x100  # 2 bytes
    VTSM_VOBS_NUM_AUDIO = 0x102  # 2 bytes
    VTSM_VOBS_AUDIO_ATTR = 0x104  # 8 bytes x 2 entries
    VTSM_VOBS_NUM_SUBPIC = 0x154  # 2 bytes
    VTSM_VOBS_SUBPIC_ATTR = 0x156  # 6 bytes x 2 entries

    VTS_VOBS_VIDEO_ATTR = 0x200  # 2 bytes
    VTS_VOBS_NUM_AUDIO = 0x202  # 2 bytes
    VTS_VOBS_AUDIO_ATTR = 0x204  # 8 bytes each, 8 entries (0..7)
    VTS_VOBS_NUM_SUBPIC = 0x254  # 2 bytes
    VTS_VOBS_SUBPIC_ATTR = 0x256  # 6 bytes each, 32 entries

    # Entry sizes
    AUDIO_ENTRY_LEN = 8
    SUBPIC_ENTRY_LEN = 6


# =============================================================================
# Cleanup
# =============================================================================


def _remove_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _unmount_direct_mount(mountpoint: Path, *, interrupt: bool) -> None:
    command = ["sudo", "umount", str(mountpoint)]
    if interrupt:
        # Fail immediately rather than prompting for a password while the user
        # waits for Ctrl+C shutdown cleanup.
        command.insert(1, "-n")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL if interrupt else None,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    try:
        mountpoint.rmdir()
    except OSError:
        pass


def cleanup_temp_dirs(*, interrupt: bool = False) -> None:
    """Delete tracked resources using the process runtime state."""
    RUNTIME_STATE.cleanup.cleanup(interrupt=interrupt)


def register_active_muxer(pgid: int) -> None:
    """Track a running muxer process group so SIGINT/SIGTERM can kill it."""
    RUNTIME_STATE.active_processes.register_muxer(pgid)


def unregister_active_muxer(pgid: int) -> None:
    """Stop tracking *pgid* (mux finished, failed, or was already killed)."""
    RUNTIME_STATE.active_processes.unregister_muxer(pgid)


def register_active_output(out_file: Path) -> None:
    """Track an in-progress output file so Ctrl+C deletes the partial mux."""
    RUNTIME_STATE.active_processes.register_output(out_file)


def unregister_active_output(out_file: Path) -> None:
    """Stop tracking *out_file* (mux completed)."""
    RUNTIME_STATE.active_processes.unregister_output(out_file)


def _kill_active_muxers() -> None:
    """SIGKILL in-flight muxers and delete their partial output files."""
    RUNTIME_STATE.active_processes.kill_muxers()


def set_progress_active(active: bool) -> None:
    """Mark whether a carriage-return progress line is currently on screen."""
    RUNTIME_STATE.active_processes.set_progress_active(active)


def finish_progress_line() -> None:
    """Terminate an on-screen progress line with a newline, if any."""
    RUNTIME_STATE.active_processes.finish_progress_line()


_ = atexit.register(cleanup_temp_dirs)


def _signal_cleanup(signum: int, _frame: object) -> None:
    """Run temp file cleanup on SIGINT/SIGTERM, then restore default handler.

    The active muxer is killed first — it runs in its own session, so the
    terminal's Ctrl+C never reaches it — and its partial output file is
    removed before the tracked temp files are cleaned up.
    """
    finish_progress_line()
    _kill_active_muxers()
    cleanup_temp_dirs(interrupt=True)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# Run cleanup on Ctrl+C and termination signals to prevent orphaned temp dirs.
signal.signal(signal.SIGINT, _signal_cleanup)
signal.signal(signal.SIGTERM, _signal_cleanup)


# =============================================================================
# Language names
# =============================================================================

LANG_NAMES = {
    "eng": "English",
    "en": "English",
    "und": "Undetermined",
    "fre": "French",
    "fr": "French",
    "fra": "French",
    "spa": "Spanish",
    "es": "Spanish",
    "deu": "German",
    "de": "German",
    "ita": "Italian",
    "it": "Italian",
    "por": "Portuguese",
    "pt": "Portuguese",
    "jpn": "Japanese",
    "ja": "Japanese",
    "kor": "Korean",
    "ko": "Korean",
    "chi": "Chinese",
    "zh": "Chinese",
    "zho": "Chinese",
    "rus": "Russian",
    "ru": "Russian",
    "ara": "Arabic",
    "ar": "Arabic",
    "hin": "Hindi",
    "hi": "Hindi",
    "tha": "Thai",
    "th": "Thai",
    "pol": "Polish",
    "pl": "Polish",
    "nld": "Dutch",
    "nl": "Dutch",
    "swe": "Swedish",
    "sv": "Swedish",
    "nor": "Norwegian",
    "no": "Norwegian",
    "dan": "Danish",
    "da": "Danish",
    "fin": "Finnish",
    "fi": "Finnish",
    "cze": "Czech",
    "cs": "Czech",
    "hun": "Hungarian",
    "hu": "Hungarian",
    "tur": "Turkish",
    "tr": "Turkish",
    "ell": "Greek",
    "el": "Greek",
    "heb": "Hebrew",
    "he": "Hebrew",
    "ron": "Romanian",
    "rum": "Romanian",
    "ro": "Romanian",
    "hrv": "Croatian",
    "hr": "Croatian",
    "srp": "Serbian",
    "sr": "Serbian",
    "slk": "Slovak",
    "sk": "Slovak",
    "slv": "Slovenian",
    "sl": "Slovenian",
    "bul": "Bulgarian",
    "bg": "Bulgarian",
    "ukr": "Ukrainian",
    "uk": "Ukrainian",
    "cat": "Catalan",
    "ca": "Catalan",
    "ind": "Indonesian",
    "id": "Indonesian",
    "vie": "Vietnamese",
    "vi": "Vietnamese",
    "fas": "Persian",
    "fa": "Persian",
}


def get_language_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


# =============================================================================
# Exceptions
# =============================================================================


@final
class RipError(Exception):
    def __init__(
        self,
        message: str,
        command: list[str] | None = None,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        title: Title | None = None,
        streams: list[Stream] | None = None,
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.message: str = message
        self.command: list[str] | None = command
        self.returncode: int | None = returncode
        self.stdout: str | None = stdout
        self.stderr: str | None = stderr
        self.title: Title | None = title
        self.streams: list[Stream] | None = streams
        self.cause: BaseException | None = cause

    def format_verbose(self) -> str:
        lines = [
            "\n  "
            + tr("ERROR: {msg}", msg=self.message)
            + (f" (rc={self.returncode})" if self.returncode is not None else "")
        ]
        if self.cause:
            lines.append(
                "  "
                + tr(
                    "CAUSE: {cause}", cause=f"{type(self.cause).__name__}: {self.cause}"
                )
            )
        if self.title:
            lines.extend(
                [
                    "",
                    "  " + tr("Title: {name}", name=self.title.name),
                    "  " + tr("Source: {src}", src=self.title.source_file),
                ]
            )
        if self.command:
            lines.append("\n  CMD: " + " ".join(self.command))
        out = self.stderr or self.stdout
        if out:
            kind = "STDERR" if self.stderr else "STDOUT"
            lines.extend(
                ["\n  " + kind + ":", "  " + "\n  ".join(out.strip().split("\n"))]
            )
        return "\n".join(lines) + "\n"


# =============================================================================
# Stream data model
# =============================================================================


class StreamType(Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


@dataclass
class Stream:
    index: int
    stream_type: StreamType = StreamType.VIDEO
    codec: str = "unknown"
    language: str = "und"
    title: str = ""
    is_default: bool = False
    is_forced: bool = False
    is_hearing_impaired: bool = False
    is_commentary: bool = False
    sample_rate: str | None = None
    channels: int | None = None
    width: int | None = None
    height: int | None = None
    type_index: int = 0
    # MPEG sub-stream ID for DVD/PS sources (audio 0x80-0x8F, subpicture
    # 0x20-0x3F). some media tools enumerate PS streams by first packet
    # appearance, not by ID, so the per-type index above does NOT correspond to the DVD stream
    # number - we keep the ID to look up the authoritative IFO language.
    sub_id: int | None = None
    # Blu-ray stream PID (e.g. 0x1011 for video, 0x1100+ for audio, 0x1200+ for
    # subtitles). This is the stream's identifier in the original source medium,
    # matching MakeMKV's "ID in the original source medium" output.
    pid: int | None = None
    # Video-only metadata that must be explicitly forwarded so the muxer
    # does not substitute wrong defaults (e.g. MPEG-2 SAR, or missing colour
    # signalling on DVD/BD sources).
    sample_aspect_ratio: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None
    field_order: str | None = None
    # Audio quantization bit depth from DVD IFO audio attributes.
    # On DVD, byte 1 bits 4-5 of each audio attribute entry encode
    # the resolution: 0=16bps, 1=20bps, 2=24bps, 3=DRC.
    bits_per_sample: int | None = None

    @property
    def language_display(self) -> str:
        return f"{get_language_name(self.language)} ({self.language})"

    @property
    def display_id(self) -> str:
        prefix_map = {"video": "v", "audio": "a", "subtitle": "s"}
        prefix = prefix_map[self.stream_type.value]
        return f"{prefix}:{self.type_index}"

    @property
    def codec_display(self) -> str:
        return {
            "h264": "H.264",
            "hevc": "H.265",
            "mpeg2video": "MPEG-2",
            "vc1": "VC-1",
            "dvd_subtitle": "DVD Sub",
            "hdmv_pgs_subtitle": "PGS",
            "dts": "DTS",
            "ac3": "AC3",
            "eac3": "E-AC3",
            "truehd": "TrueHD",
            "flac": "FLAC",
            "dts_hd_hr": "DTS-HD HR",
            "dts_hd_ma": "DTS-HD MA",
            "lpcm": "LPCM",
        }.get(self.codec, self.codec.upper())


# =============================================================================
# Multi-edition (seamless branching) data model
# =============================================================================


@dataclass
class EditionAtom:
    """One ordered-chapter atom of a multi-edition MKV.

    ``start``/``end`` are seconds on the *combined* timeline (the union of all
    unique clips muxed back-to-back). A visible atom carries a chapter name;
    hidden atoms are segment-boundary continuations of a chapter that spans a
    branch point (players still play them, but don't list them).
    """

    start: float
    end: float
    hidden: bool = False
    name: str | None = None


@dataclass
class EditionSpec:
    """One edition (playlist cut) of a multi-edition MKV.

    ``atoms`` partition the edition's virtual timeline into ordered-chapter
    ranges over the combined file, following the xin1generator algorithm
    (https://github.com/RollingStar/xin1generator): each clip of the playlist
    contributes one atom, split further at real chapter marks. Atoms that
    start at a branch-point boundary mid-chapter are hidden.
    """

    uid: int
    name: str
    is_default: bool
    atoms: list[EditionAtom] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(a.end - a.start for a in self.atoms)


# =============================================================================
# Title data model
# =============================================================================


@dataclass
class Title:
    index: int
    source_file: Path
    name: str
    duration_seconds: float
    streams: list[Stream] = field(default_factory=list)
    iso_internal_paths: Sequence[str] = field(default_factory=list)
    append_clips: list[Path] = field(default_factory=list)
    # Estimated on-disc size of the title's raw source streams (sum of the
    # M2TS/VOB byte sizes), used to decide whether extraction should stay on a
    # RAM-backed temp dir or spill to disk (see disc_reader.init_ram_budget).
    estimated_size_bytes: int = 0
    chapters: list[float] = field(default_factory=list)
    # Disc-level name parsed from BDMV metadata (bdmt.xml / ID.bdmv) or DVD VMG IFO.
    # Used as the container-level title when TMDB tagging is not available.
    disc_name: str | None = None
    # Disc-level barcode / EAN / catalog number, extracted from VMG TXTDT for DVD
    # or from bdmt_eng.xml (<catalogNumber>) for Blu-ray.
    disc_barcode: str | None = None
    # DVD VTS .IFO stream-ID -> language maps. Set during DVD scanning so the
    # muxer can label streams correctly: some media tools enumerate PS streams by
    # first packet appearance (not by ID), so per-type positional order is
    # wrong and we must look languages up by the MPEG sub-stream ID.
    dvd_audio_lang: dict[int, str] = field(default_factory=dict)
    dvd_sub_lang: dict[int, str] = field(default_factory=dict)
    # DVD VTS .IFO stream attributes (codec, channels, Dolby Surround)
    # keyed by sub-stream ID (0x80+). Set during ``_apply_dvd_ifo_languages``.
    dvd_audio_attrs: dict[int, _IFOAudioAttrs] = field(default_factory=dict)
    # Raw VTS IFO bytes, set during ``_apply_dvd_ifo_languages``. Used by
    # ``_lookup_main_feature_range`` for IFO-based cell trimming.
    dvd_ifo_data: bytes | None = None
    # VTS subpicture attributes parsed from VTSI_MAT SPST_ATRT (6-byte entries
    # at offset 0x0256). Contains coding_mode and code_extension per sub-stream.
    # The code_extension tells us if a subtitle is "forced" (value 9) — we use
    # this to auto-set the forced flag on subtitle streams.
    dvd_subp_attrs: dict[int, _IFOSubpictureAttrs] = field(default_factory=dict)
    # VTS video attributes parsed from VTS_V_ATR (2 bytes at VTSI_MAT+0x200).
    # Contains MPEG version, aspect ratio, standard (NTSC/PAL), resolution.
    # Set during ``_apply_dvd_ifo_languages``.
    dvd_video_attrs: _IFOVideoAttrs | None = None
    # 1-indexed VTS_PGCIT PGC number this title should be ripped from (see
    # ``_enumerate_vts_pgcs``/``_find_main_pgc``). None means "use the
    # disc's own default title designation" (VTS_TTN 1). Set when a VTS has
    # multiple substantial PGCs (seamless-branching editions) so each can be
    # exposed and ripped as its own separate title.
    dvd_pgc_number: int | None = None
    # Human-readable label for an alternate seamless-branching edition (e.g.
    # "Edition 2"), set alongside ``dvd_pgc_number``. ``_apply_disc_name``
    # appends this to the generic "<disc> - Title N" label instead of
    # discarding it, so alternate editions stay visually distinguishable in
    # the title listing.
    dvd_edition_label: str | None = None
    # 1-indexed episode number for TV-series discs detected by
    # ``_detect_episode_pgcs``. When set, ``_apply_disc_name`` labels the
    # title "<disc> - Episode N" instead of the generic title suffix.
    dvd_episode_number: int | None = None
    # True when this title is the "play all" chain on a TV-series disc (a
    # PGC whose duration ≈ the sum of all episodes). Demoted in the sort
    # order so episodes and extras appear before it.
    dvd_play_all: bool = False
    # Per-clip play durations (seconds), aligned with the title's clip
    # sequence: [source_file] + append_clips (folder/device sources) or
    # iso_internal_paths (ISO sources). Populated during Blu-ray scanning
    # from MPLS play items ((out_time - in_time) / 45000) so multi-edition
    # chapter atoms can be computed without re-parsing the playlist.
    clip_durations: list[float] = field(default_factory=list)
    # Per-clip on-disc byte sizes, aligned with clip_durations. Used to size
    # the union of unique clips when combining editions (a sum of the member
    # titles' estimated_size_bytes would double-count shared clips).
    clip_sizes: list[int] = field(default_factory=list)
    # MPLS playlist stem (e.g. "00800") this title was built from, for
    # edition labelling on seamless-branching discs. None for non-BD titles.
    playlist_name: str | None = None
    # Multi-edition chapter specs (one per playlist cut). When non-empty the
    # muxer writes an ordered-chapters XML with one edition per spec plus
    # edition TITLE tags instead of the flat single-edition chapter table.
    # Set by build_multi_edition_title() on the synthetic combined title.
    editions: list[EditionSpec] = field(default_factory=list)

    @property
    def video_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.stream_type == StreamType.VIDEO]

    @property
    def audio_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.stream_type == StreamType.AUDIO]

    @property
    def subtitle_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.stream_type == StreamType.SUBTITLE]

    @property
    def duration_display(self) -> str:
        h, rem = divmod(int(self.duration_seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @property
    def streams_summary(self) -> str:
        v = len(self.video_streams)
        a = len(self.audio_streams)
        s = len(self.subtitle_streams)
        log_debug(f"streams_summary: {v}v {a}a {s}s (total {len(self.streams)})")
        return f"V:{v} A:{a} S:{s}"


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class Config:
    output_dir: Path = Path(".")
    temp_dir: Path | None = None
    preferred_languages: list[str] = field(default_factory=lambda: ["eng", "en", "und"])
    keep_all_audio: bool = True
    keep_all_subtitles: bool = True
    include_forced: bool = True
    min_duration: float = 60.0
    debug: bool = False
    no_sudo: bool = False
    show_all: bool = False
    ui_lang: str | None = None  # --ui-lang override; None = use settings/env
    # Fraction of installed RAM that extraction may consume on a RAM-backed
    # (tmpfs) temp dir before spilling to disk. 0 disables the check.
    ram_limit: float = 0.8
    # Computed at startup (disc_reader.init_ram_budget): the byte budget for a
    # RAM-backed temp dir (= ram_limit * total RAM), or None when the temp dir
    # is disk-backed / RAM could not be detected (no limit enforced).
    ram_budget_bytes: int | None = None


# Default TMDB metadata fetched when --tag is used (matches tagger.py defaults).
DEFAULT_TAG_METADATA = [
    "TMDbID",
    "IMDbID",
    "Cast",
    "Writers",
    "Directors",
    "Title",
    "Overview",
    "Genres",
    "ReleaseDate",
    "Runtime",
]


@dataclass
class TagOptions:
    """Options for fetching/applying TMDB metadata to a finished rip.

    Tagging is an optional post-rip step (driven by tagger.py / TMDB) and is
    deliberately decoupled from the muxer: a tagging failure never discards an
    already-successful rip.
    """

    enabled: bool = False
    no_tag: bool = False
    api_key: str | None = None
    metadata: list[str] = field(default_factory=lambda: list(DEFAULT_TAG_METADATA))
    region: str = "US"
    language: str | None = None
    art: str | None = None  # None | "poster" | "backdrop" | "both"
    save_xml: bool = False
    confirm: bool = True
    title_override: str | None = None
    year_override: int | None = None


# =============================================================================
# Runtime state
# =============================================================================


@dataclass
class RuntimeCleanup:
    """Owns temporary filesystem resources and their shutdown cleanup."""

    temp_dirs: list[Path] = field(default_factory=list)
    temp_files: list[Path] = field(default_factory=list)
    direct_mounts: list[Path] = field(default_factory=list)
    symlinks: list[Path] = field(default_factory=list)

    def register_temp_dir(self, path: Path) -> Path:
        self.temp_dirs.append(path)
        return path

    def register_temp_file(self, path: Path) -> Path:
        self.temp_files.append(path)
        return path

    def register_direct_mount(self, path: Path) -> Path:
        self.direct_mounts.append(path)
        return path

    def unregister_direct_mount(self, path: Path) -> None:
        try:
            self.direct_mounts.remove(path)
        except ValueError:
            pass

    def register_symlink(self, path: Path) -> Path:
        self.symlinks.append(path)
        return path

    def cleanup(self, *, interrupt: bool = False) -> None:
        """Delete tracked resources; interrupt mode never prompts for sudo."""
        for directory in dict.fromkeys(self.temp_dirs):
            shutil.rmtree(directory, ignore_errors=True)
        for file_path in dict.fromkeys(self.temp_files):
            _remove_temp_file(file_path)
        for mountpoint in dict.fromkeys(self.direct_mounts):
            _unmount_direct_mount(mountpoint, interrupt=interrupt)
        for symlink in dict.fromkeys(self.symlinks):
            _remove_temp_file(symlink)


@dataclass
class ActiveProcesses:
    """Owns in-flight muxers, partial outputs, and progress-line state."""

    muxer_pgids: list[int] = field(default_factory=list)
    output_files: list[Path] = field(default_factory=list)
    progress_active: bool = False

    def register_muxer(self, pgid: int) -> None:
        self.muxer_pgids.append(pgid)

    def unregister_muxer(self, pgid: int) -> None:
        try:
            self.muxer_pgids.remove(pgid)
        except ValueError:
            pass

    def register_output(self, out_file: Path) -> None:
        self.output_files.append(out_file)

    def unregister_output(self, out_file: Path) -> None:
        try:
            self.output_files.remove(out_file)
        except ValueError:
            pass

    def kill_muxers(self) -> None:
        """SIGKILL in-flight muxers and delete partial output files."""
        for pgid in self.muxer_pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pgid, signal.SIGKILL)
                except OSError:
                    pass
        self.muxer_pgids.clear()
        for out_file in self.output_files:
            try:
                out_file.unlink(missing_ok=True)
            except OSError:
                pass
        self.output_files.clear()

    def set_progress_active(self, active: bool) -> None:
        self.progress_active = active

    def finish_progress_line(self) -> None:
        if not self.progress_active:
            return
        try:
            sys.stderr.write("\n")
            sys.stderr.flush()
        except OSError:
            pass
        self.progress_active = False


@dataclass
class RuntimeState:
    """Mutable process-wide state, grouped for staged dependency injection."""

    config: Config = field(default_factory=Config)
    tag_options: TagOptions = field(default_factory=TagOptions)
    logger: RuntimeLogger = field(default_factory=RuntimeLogger)
    cleanup: RuntimeCleanup = field(default_factory=RuntimeCleanup)
    active_processes: ActiveProcesses = field(default_factory=ActiveProcesses)

    def __post_init__(self) -> None:
        self.logger.configure(self.config)


RUNTIME_STATE = RuntimeState()


def log_info(message: str) -> None:
    RUNTIME_STATE.logger.info(message)


def log_warn(message: str) -> None:
    RUNTIME_STATE.logger.warn(message)


def log_error(message: str) -> None:
    RUNTIME_STATE.logger.error(message)


def log_debug(message: str) -> None:
    RUNTIME_STATE.logger.debug(message)
