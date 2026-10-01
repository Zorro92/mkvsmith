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
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict, cast, final

try:
    from rich.console import Console as _ImportedConsole

    _rich_console_class: type[_ImportedConsole] | None = _ImportedConsole
except ImportError:
    _rich_console_class = None

from dvdifo import _IFOAudioAttrs, _IFOSubpictureAttrs, _IFOVideoAttrs
from i18n import tr

HAS_RICH = _rich_console_class is not None


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
        if HAS_RICH and _rich_console_class is not None:
            self._inner = _rich_console_class()
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

# Keep in sync with pyproject.toml [project].version. Shared modules such as
# discdb.py cannot import cli.py without creating a dependency cycle.
MKVSMITH_VERSION = "0.8.0"


# =============================================================================
# Cleanup
# =============================================================================


def _remove_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _has_mounted_child(directory: Path) -> bool:
    """True when a direct child of *directory* is still a mount point.

    Loop-mount points are created at the top of the session temp dir, so a
    failed unmount leaves one there; deleting through it would walk the
    mounted image.
    """
    try:
        return any(os.path.ismount(child) for child in directory.iterdir())
    except OSError:
        return False


def _remove_temp_dir(directory: Path) -> None:
    if _has_mounted_child(directory):
        return
    shutil.rmtree(directory, ignore_errors=True)


# =============================================================================
# Per-run session temp dirs and the stale-session sweep
# =============================================================================

# Every run keeps its temp files inside a session dir named
# ``mkvsmith-tmp-<pid>-<random>`` holding an owner marker. A run killed
# without cleanup (SIGKILL, OOM killer, power loss) leaves its session dir
# behind; the next run removes it once the owner is provably gone.
SESSION_DIR_PREFIX = "mkvsmith-tmp-"
_SESSION_MARKER = ".mkvsmith-owner.json"


class SessionOwner(TypedDict):
    pid: int
    host: str
    # Kernel start time of the owner (Linux), so a reused PID is not
    # mistaken for the original owner. ``None`` where unavailable.
    start: str | None


def _process_start_token(pid: int) -> str | None:
    """Start time of *pid* from ``/proc/<pid>/stat``, or ``None``."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces or ')'; fields after the last ')'
    # start at field 3, so starttime (field 22) is index 19.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def create_session_dir(base: Path) -> Path:
    """Create a marked session temp dir under *base* for this process."""
    pid = os.getpid()
    path = Path(tempfile.mkdtemp(prefix=f"{SESSION_DIR_PREFIX}{pid}-", dir=base))
    owner = SessionOwner(
        pid=pid, host=socket.gethostname(), start=_process_start_token(pid)
    )
    (path / _SESSION_MARKER).write_text(json.dumps(owner))
    return path


def _read_session_owner(directory: Path) -> SessionOwner | None:
    try:
        raw: object = json.loads((directory / _SESSION_MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    pid = data.get("pid")
    host = data.get("host")
    start = data.get("start")
    if not isinstance(pid, int) or not isinstance(host, str):
        return None
    if start is not None and not isinstance(start, str):
        return None
    return SessionOwner(pid=pid, host=host, start=start)


def _session_owner_gone(owner: SessionOwner) -> bool:
    """True only when the owning process has certainly exited."""
    if owner["host"] != socket.gethostname():
        # Temp on shared storage: another machine's run may still be live.
        return False
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return True
    except OSError:
        # EPERM: the PID exists under another user.
        return False
    recorded = owner["start"]
    current = _process_start_token(owner["pid"])
    return recorded is not None and current is not None and current != recorded


def _tree_size(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def sweep_stale_session_dirs(bases: Iterable[Path]) -> list[tuple[Path, int]]:
    """Delete session dirs under *bases* whose owning run has exited.

    Only directories carrying a valid owner marker, owned by the current
    user, created on this host, and holding no live mount are touched.
    Returns ``(path, bytes)`` for each directory removed. POSIX only:
    Windows has no signal-0 liveness probe (``os.kill(pid, 0)`` sends
    CTRL_C_EVENT there), so the sweep is a no-op.
    """
    if os.name != "posix":
        return []
    removed: list[tuple[Path, int]] = []
    for base in dict.fromkeys(bases):
        try:
            candidates = sorted(base.glob(f"{SESSION_DIR_PREFIX}*"))
        except OSError:
            continue
        for directory in candidates:
            try:
                info = directory.lstat()
            except OSError:
                continue
            if not directory.is_dir() or directory.is_symlink():
                continue
            if info.st_uid != os.getuid():
                continue
            owner = _read_session_owner(directory)
            if owner is None or not _session_owner_gone(owner):
                continue
            if _has_mounted_child(directory):
                continue
            size = _tree_size(directory)
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append((directory, size))
    return removed


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


# Windows defines neither killpg nor SIGKILL; ``os.kill(pid, 9)`` maps to
# TerminateProcess there, and 9 is SIGKILL's conventional POSIX value.
_SIGKILL: int = getattr(signal, "SIGKILL", 9)


def _kill_process_group(pid: int) -> None:
    """Best-effort SIGKILL of a tracked process and, on POSIX, its group.

    Mux children run in their own session (see ``mkv.py``), so the whole
    process group must be killed on POSIX. Windows has no process groups;
    ``os.kill`` with SIGKILL maps to TerminateProcess there. Missing or
    already-exited PIDs are ignored.
    """
    if os.name != "posix":
        try:
            os.kill(pid, _SIGKILL)
        except OSError:
            pass
        return
    try:
        os.killpg(pid, _SIGKILL)
        return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        os.killpg(os.getpgid(pid), _SIGKILL)
    except OSError:
        try:
            os.kill(pid, _SIGKILL)
        except OSError:
            pass


def set_progress_active(active: bool) -> None:
    """Mark whether a carriage-return progress line is currently on screen."""
    RUNTIME_STATE.active_processes.set_progress_active(active)


def finish_progress_line() -> None:
    """Terminate an on-screen progress line with a newline, if any."""
    RUNTIME_STATE.active_processes.finish_progress_line()


_ = atexit.register(cleanup_temp_dirs)


def _signal_cleanup(signum: int, _frame: object) -> None:
    """Run temp file cleanup on SIGINT/SIGTERM/SIGHUP, then re-raise.

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
# A closed terminal sends SIGHUP. Unhandled, it kills Python without cleanup
# while the muxer (in its own session) keeps running. Leave it alone when
# already ignored, so ``nohup mkvsmith ...`` keeps surviving the hangup.
if hasattr(signal, "SIGHUP") and signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:
    signal.signal(signal.SIGHUP, _signal_cleanup)


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
    # matching the reference "ID in the original source medium" output.
    pid: int | None = None
    # Blu-ray SubPath the stream lives in (MPLS stream_entry types 2-4), or
    # None for streams in the PlayItem's main clip. mkvsmith muxes only
    # main-path clips, so such streams are listed but never muxed.
    subpath_id: int | None = None
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
    def is_muxable(self) -> bool:
        """False for Blu-ray SubPath streams, which mkvsmith cannot mux."""
        return self.subpath_id is None

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
    atoms: list[EditionAtom] = field(default_factory=list[EditionAtom])

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
    streams: list[Stream] = field(default_factory=list[Stream])
    iso_internal_paths: Sequence[str] = field(default_factory=list[str])
    append_clips: list[Path] = field(default_factory=list[Path])
    # Estimated on-disc size of the title's raw source streams (sum of the
    # M2TS/VOB byte sizes), used to decide whether extraction should stay on a
    # RAM-backed temp dir or spill to disk (see disc_reader.init_ram_budget).
    estimated_size_bytes: int = 0
    chapters: list[float] = field(default_factory=list[float])
    # Disc-level name parsed from BDMV metadata (bdmt.xml / ID.bdmv) or DVD VMG IFO.
    # Used as the container-level title when TMDB tagging is not available.
    disc_name: str | None = None
    # DVD VTS .IFO stream-ID -> language maps. Set during DVD scanning so the
    # muxer can label streams correctly: some media tools enumerate PS streams by
    # first packet appearance (not by ID), so per-type positional order is
    # wrong and we must look languages up by the MPEG sub-stream ID.
    dvd_audio_lang: dict[int, str] = field(default_factory=dict[int, str])
    dvd_sub_lang: dict[int, str] = field(default_factory=dict[int, str])
    # DVD VTS .IFO stream attributes (codec, channels, Dolby Surround)
    # keyed by sub-stream ID (0x80+). Set during ``_apply_dvd_ifo_languages``.
    dvd_audio_attrs: dict[int, _IFOAudioAttrs] = field(
        default_factory=dict[int, _IFOAudioAttrs]
    )
    # Raw VTS IFO bytes, set during ``_apply_dvd_ifo_languages``. Used by
    # ``_lookup_main_feature_range`` for IFO-based cell trimming.
    dvd_ifo_data: bytes | None = None
    # VTS subpicture attributes parsed from VTSI_MAT SPST_ATRT (6-byte entries
    # at offset 0x0256). Contains coding_mode and code_extension per sub-stream.
    # The code_extension tells us if a subtitle is "forced" (value 9) — we use
    # this to auto-set the forced flag on subtitle streams.
    dvd_subp_attrs: dict[int, _IFOSubpictureAttrs] = field(
        default_factory=dict[int, _IFOSubpictureAttrs]
    )
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
    # Human-readable label for a substantial PGC exposed as its own title,
    # set alongside ``dvd_pgc_number``: "Edition N" when the PGC re-cuts the
    # default title's footage (seamless branching), or "PGC N" for an
    # unrelated program sharing the VTS (e.g. a bonus feature). See
    # ``_find_alternate_edition_pgcs``. ``_apply_disc_name`` appends this to
    # the generic "<disc> - Title N" label instead of discarding it, so
    # alternate PGCs stay visually distinguishable in the title listing.
    dvd_edition_label: str | None = None
    # 1-indexed episode number for TV-series discs detected by
    # ``_detect_episode_pgcs``. When set, ``_apply_disc_name`` labels the
    # title "<disc> - Episode N" instead of the generic title suffix.
    dvd_episode_number: int | None = None
    # Part suffix for episodes authored as two separately-ripped segments
    # (Superman 1988: each DVD episode = a ~19-minute part "a" plus a
    # ~5-minute short "b", in alternating PGCs). Together with
    # ``dvd_episode_number`` the title is labelled "Episode Na"/"Episode Nb".
    dvd_episode_part: str | None = None
    # True when this title is the "play all" chain on a TV-series disc (a
    # PGC whose duration ≈ the sum of all episodes). Demoted in the sort
    # order so episodes and extras appear before it.
    dvd_play_all: bool = False
    # Logical DVD title number from VMG TT_SRPT. TheDiscDB and the reference ripper use
    # this as a DVD title's source identifier (for example "01").
    dvd_title_id: int | None = None
    # XPL title number for HD DVD titles (e.g. 3 for "Main Movie"). Marks a
    # title as HD DVD-sourced so the muxer runs the EVO subtitle fallback;
    # None for all other sources.
    hddvd_title_number: int | None = None
    # XPL id attribute ("MainMovie", "Trailer3") for HD DVD titles: the
    # authorial main-feature token. None for all other sources.
    hddvd_id: str | None = None
    # Per-clip play durations (seconds), aligned with the title's clip
    # sequence: [source_file] + append_clips (folder/device sources) or
    # iso_internal_paths (ISO sources). Populated during Blu-ray scanning
    # from MPLS play items ((out_time - in_time) / 45000) so multi-edition
    # chapter atoms can be computed without re-parsing the playlist.
    clip_durations: list[float] = field(default_factory=list[float])
    # Per-clip on-disc byte sizes, aligned with clip_durations. Used to size
    # the union of unique clips when combining editions (a sum of the member
    # titles' estimated_size_bytes would double-count shared clips).
    clip_sizes: list[int] = field(default_factory=list[int])
    # True when the MPLS joins any clip to its predecessor seamlessly
    # (connection_condition 5/6). The muxer then trims each clip's trailing
    # audio frames so every track stays locked to the video across joins.
    seamless_connections: bool = False
    # MPLS playlist stem (e.g. "00800") this title was built from, for
    # edition labelling on seamless-branching discs. None for non-BD titles.
    playlist_name: str | None = None
    # Matching metadata applied from TheDiscDB. These fields are deliberately
    # absent when no unique remote title correlation exists.
    discdb_item_type: str | None = None
    discdb_season_number: int | None = None
    discdb_episode_number: int | None = None
    discdb_is_main_movie: bool = False
    # Multi-edition chapter specs (one per playlist cut). When non-empty the
    # muxer writes an ordered-chapters XML with one edition per spec plus
    # edition TITLE tags instead of the flat single-edition chapter table.
    # Set by build_multi_edition_title() on the synthetic combined title.
    editions: list[EditionSpec] = field(default_factory=list[EditionSpec])

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


@dataclass(frozen=True)
class DiscMetadata:
    """Disc-level identity parsed from disc metadata."""

    name: str | None = None
    upc_ean: str | None = None
    provider_id: str | None = None
    dvd_disc_id: str | None = None
    # TheDiscDB's global DVD identifier: libdvdread DVDDiscID() (uppercase MD5
    # over the VMG/VTS IFOs). This is distinct from dvd_disc_id, which retains
    # the Windows IDvdInfo2/pydvdid CRC-64 identifier.
    libdvdread_disc_id: str | None = None
    # TheDiscDB's global Blu-ray/UHD identifier: SHA-1 over AACS Unit_Key_RO.inf.
    aacs_disc_id: str | None = None
    # TheDiscDB's legacy format-specific Disc Hash (MD5 over the selected
    # video-file sizes in listing order).
    disc_hash: str | None = None
    matrix256_fingerprint: str | None = None


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
    # Extract EIA-608 closed captions as a text subtitle track (SRT or ASS
    # sidecar, see cc608_format) — opt-in via --cc-srt, since the captions
    # usually duplicate the VobSub tracks.
    extract_cc608: bool = False
    # CC sidecar format: "srt" (portable plain text) or "ass" (preserves
    # horizontal speaker positioning and italics).
    cc608_format: str = "srt"
    ui_lang: str | None = None  # --ui-lang override; None = use settings/env
    # Fraction of RAM-backed (tmpfs) temp capacity (total RAM vs. tmpfs size,
    # whichever is smaller) that extraction may consume before spilling to
    # disk. 0 disables the check.
    ram_limit: float = 0.8
    # Computed at startup (disc_reader.init_ram_budget): the byte budget for a
    # RAM-backed temp dir (= ram_limit * min(total RAM, tmpfs size)), or None
    # when the temp dir is disk-backed / RAM could not be detected (no limit
    # enforced).
    ram_budget_bytes: int | None = None
    # Overwrite existing output files without asking (--force). By default
    # create_mkv confirms before overwriting an existing .mkv.
    force_overwrite: bool = False


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


@dataclass
class DiscDbOptions:
    """Options for TheDiscDB lookup and community contributions.

    Lookup/contribution failures must never invalidate an otherwise successful
    local scan or rip.
    """

    enabled: bool = False
    base_url: str = "https://thediscdb.com"
    timeout_seconds: float = 10.0
    contribute: bool = False
    contribute_mode: str = "browser"
    bundle_dir: Path | None = None
    open_browser: bool = True
    contribution_id: str | None = None
    disc_name: str | None = None
    cookie: str | None = None


# =============================================================================
# Runtime state
# =============================================================================


@dataclass
class RuntimeCleanup:
    """Owns temporary filesystem resources and their shutdown cleanup."""

    temp_dirs: list[Path] = field(default_factory=list[Path])
    temp_files: list[Path] = field(default_factory=list[Path])
    direct_mounts: list[Path] = field(default_factory=list[Path])
    symlinks: list[Path] = field(default_factory=list[Path])

    session_dirs: dict[Path, Path] = field(default_factory=dict[Path, Path])

    def register_temp_dir(self, path: Path) -> Path:
        self.temp_dirs.append(path)
        return path

    def session_dir(self, base: Path) -> Path:
        """This run's session temp dir under *base*, created on first use."""
        existing = self.session_dirs.get(base)
        if existing is not None and existing.is_dir():
            return existing
        path = self.register_temp_dir(create_session_dir(base))
        self.session_dirs[base] = path
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
        """Delete tracked resources; interrupt mode never prompts for sudo.

        Mounts go first: their mount points live inside the session temp
        dir, which is only deleted once nothing is mounted in it.
        """
        for mountpoint in dict.fromkeys(self.direct_mounts):
            _unmount_direct_mount(mountpoint, interrupt=interrupt)
        for file_path in dict.fromkeys(self.temp_files):
            _remove_temp_file(file_path)
        for directory in dict.fromkeys(self.temp_dirs):
            _remove_temp_dir(directory)
        for symlink in dict.fromkeys(self.symlinks):
            _remove_temp_file(symlink)


@dataclass
class ActiveProcesses:
    """Owns in-flight muxers, partial outputs, and progress-line state."""

    muxer_pgids: list[int] = field(default_factory=list[int])
    output_files: list[Path] = field(default_factory=list[Path])
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
            _kill_process_group(pgid)
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


ConfirmFn = Callable[[str], bool]
"""Yes/no question hook: receives the full (translated) message, returns True."""

TextPromptFn = Callable[[str, str | None], str]
"""Free-text hook: receives (prompt, default), returns the entered text."""


def _stdin_confirm(message: str) -> bool:
    """Default confirm hook: ask on stdin, treating close/interrupt as No."""
    try:
        answer = input(message + " ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _stdin_text(prompt: str, default: str | None = None) -> str:
    """Default text hook: read a line from stdin, falling back to *default*."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default or ""
    return ans or (default or "")


@dataclass
class UserPrompts:
    """Injectable user-interaction hooks (default: stdin).

    Core modules must ask the user through these instead of calling
    ``input()`` directly, so a future GUI can substitute dialog callbacks
    and headless flows never block on stdin. The message strings stay at the
    call site (translated via ``tr()``); only the transport is injected.
    """

    confirm: ConfirmFn = _stdin_confirm
    text: TextPromptFn = _stdin_text


@dataclass
class RuntimeState:
    """Mutable process-wide state, grouped for staged dependency injection."""

    config: Config = field(default_factory=Config)
    tag_options: TagOptions = field(default_factory=TagOptions)
    discdb_options: DiscDbOptions = field(default_factory=DiscDbOptions)
    disc_metadata: DiscMetadata = field(default_factory=DiscMetadata)
    logger: RuntimeLogger = field(default_factory=RuntimeLogger)
    cleanup: RuntimeCleanup = field(default_factory=RuntimeCleanup)
    active_processes: ActiveProcesses = field(default_factory=ActiveProcesses)
    prompts: UserPrompts = field(default_factory=UserPrompts)

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
