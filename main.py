"""
YT Downloader — desktop app for YouTube videos, MP3 audio, and playlists.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import customtkinter as ctk
from PIL import Image, ImageChops, ImageDraw, ImageFont
from tkinter import filedialog, messagebox

try:
    import yt_dlp
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing yt-dlp. Install with: py -m pip install -r requirements.txt"
    ) from exc


APP_NAME = "YT Downloader"
APP_VERSION = "1.4.10"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = app_root()
LEGACY_CONFIG_DIR = Path.home() / ".yt-downloader"


def init_portable_data_dir() -> Path:
    """All app data lives next to the app (portable USB / folder copy)."""
    data_dir = APP_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if LEGACY_CONFIG_DIR.exists():
        for name in ("config.json", "queue.json", "app.log", "launch.log"):
            src = LEGACY_CONFIG_DIR / name
            dst = data_dir / name
            if src.exists() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
    return data_dir


CONFIG_DIR = init_portable_data_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
QUEUE_FILE = CONFIG_DIR / "queue.json"
LOG_FILE = CONFIG_DIR / "app.log"
LAUNCH_LOG = CONFIG_DIR / "launch.log"
ENTRIES_PAGE_SIZE = 60
# Segment cesty: písmena (včetně diakritiky), tečka, pomlčka, nebo %XX
# (např. @Nemluvsd%C4%9Btma = @Nemluvsdětma).
_YT_SEG = r"(?:[\w.\-]|%[0-9A-Fa-f]{2})+"
_YT_QUERY = r"(?:\?[^\s\"'<>]+)?"
# video, playlist, shorts, live, embed, @handle / channel / c / user (+ záložky)
YOUTUBE_RE = re.compile(
    r"(https?://)?((?:www|m|music)\.)?"
    r"(?:"
    r"youtube\.com/"
    r"(?:"
    r"watch\?[^\s\"'<>]+"
    r"|playlist\?list=[\w\-]+"
    rf"|shorts/{_YT_SEG}"
    rf"|live/{_YT_SEG}"
    rf"|embed/{_YT_SEG}"
    rf"|@{_YT_SEG}(?:/(?:videos|streams|shorts|releases|playlists|featured))?{_YT_QUERY}"
    rf"|channel/{_YT_SEG}(?:/(?:videos|streams|shorts|playlists))?{_YT_QUERY}"
    rf"|c/{_YT_SEG}(?:/(?:videos|streams|shorts|playlists))?{_YT_QUERY}"
    rf"|user/{_YT_SEG}(?:/(?:videos|streams|shorts|playlists))?{_YT_QUERY}"
    r")"
    rf"|youtu\.be/{_YT_SEG}(?:\?[^\s\"'<>]*)?"
    r")",
    re.IGNORECASE,
)

log = logging.getLogger("yt_downloader")


def setup_logging() -> Path:
    """Write app errors (and warnings) to a rotating log file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("yt_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

    def _excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.error(
            "Neodchycená výjimka\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):

        def _thread_excepthook(args):  # type: ignore[no-untyped-def]
            if args.exc_type is SystemExit:
                return
            logger.error(
                "Neodchycená výjimka ve vlákně %s\n%s",
                getattr(args.thread, "name", "?"),
                "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                ),
            )

        threading.excepthook = _thread_excepthook  # type: ignore[assignment]

    logger.info("%s %s spuštěn", APP_NAME, APP_VERSION)
    return LOG_FILE


def log_exception(context: str, exc: BaseException) -> None:
    log.error("%s: %s", context, exc, exc_info=exc)


def log_error(message: str, **extra: Any) -> None:
    if extra:
        detail = " | ".join(f"{k}={v}" for k, v in extra.items())
        log.error("%s | %s", message, detail)
    else:
        log.error("%s", message)


class MediaMode(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


class JobStatus(str, Enum):
    QUEUED = "Ve frontě"
    RESOLVING = "Načítání playlistu…"
    READY = "Připraveno"
    DOWNLOADING = "Stahování…"
    CONVERTING = "Konverze…"
    PAUSED = "Pozastaveno"
    DONE = "Hotovo"
    ERROR = "Chyba"
    CANCELLED = "Zrušeno"
    SKIPPED = "Přeskočeno"


QUALITY_OPTIONS = [
    ("Nejlepší dostupné", "best"),
    ("4K (2160p)", "2160"),
    ("1440p", "1440"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
]

# Rozpracované soubory (*.part, spojování, převod na MP3) žijí tady.
# Po dokončení je yt-dlp přesune do cílové složky a prázdný adresář se smaže.
INCOMPLETE_SUBDIR = "_nedokoncene"


def remove_incomplete_dir_if_empty(out_dir: Path) -> None:
    """Smaž _nedokoncene, když v ní nezůstal žádný soubor."""
    folder = out_dir / INCOMPLETE_SUBDIR
    try:
        if not folder.is_dir():
            return
        if any(path.is_file() for path in folder.rglob("*")):
            return
        shutil.rmtree(folder)
        log.info("Smazána prázdná složka %s", folder)
    except OSError as exc:
        log.warning("Podsložku %s nejde smazat: %s", folder, exc)


_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv"}
_AUDIO_SUFFIXES = {".mp3"}
_CONFIG_LOCK = threading.Lock()


def tally_downloaded_files(video_dir: str, audio_dir: str) -> tuple[int, int, int]:
    """Počty a velikost hotových souborů ve složkách videa a MP3."""
    videos = mp3s = total = 0
    seen: set[str] = set()

    def walk(root: str, suffixes: set[str], kind: str) -> None:
        nonlocal videos, mp3s, total
        base = Path(root)
        if not base.is_dir():
            return
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if INCOMPLETE_SUBDIR in path.parts:
                continue
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                total += path.stat().st_size
            except OSError:
                pass
            if kind == "audio":
                mp3s += 1
            else:
                videos += 1

    walk(video_dir, _VIDEO_SUFFIXES, "video")
    walk(audio_dir, _AUDIO_SUFFIXES, "audio")
    return videos, mp3s, total


def format_total_gb(nbytes: int) -> str:
    gb = max(0, int(nbytes)) / (1024 ** 3)
    if gb >= 10:
        return f"{gb:.0f} GB"
    text = f"{gb:.2f}".rstrip("0").rstrip(".")
    return f"{text} GB"


def download_totals_line(videos: int, mp3s: int, nbytes: int) -> str:
    return (
        f"Staženo: Videa {max(0, int(videos))}, "
        f"MP3 {max(0, int(mp3s))}, {format_total_gb(nbytes)}"
    )


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RESOLVING,
    JobStatus.READY,
    JobStatus.DOWNLOADING,
    JobStatus.CONVERTING,
    JobStatus.PAUSED,
}


@dataclass
class PlaylistEntry:
    video_id: str
    url: str
    title: str
    index: int
    selected: bool = True
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    error: str = ""
    output_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
            "index": self.index,
            "selected": self.selected,
            "status": self.status.name,
            "progress": self.progress,
            "error": self.error,
            "output_path": self.output_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaylistEntry":
        status_name = data.get("status", JobStatus.QUEUED.name)
        try:
            status = JobStatus[status_name]
        except KeyError:
            status = JobStatus.QUEUED
        if status in (JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.RESOLVING):
            status = JobStatus.QUEUED
        return cls(
            video_id=data.get("video_id", ""),
            url=data.get("url", ""),
            title=data.get("title", "Neznámé video"),
            index=int(data.get("index") or 0),
            selected=bool(data.get("selected", True)),
            status=status,
            progress=float(data.get("progress") or 0),
            error=data.get("error", ""),
            output_path=data.get("output_path", ""),
        )


@dataclass
class DownloadJob:
    id: str
    url: str
    mode: MediaMode
    quality: str
    title: str = "Načítání…"
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    speed: str = ""
    speed_bps: float = 0.0
    eta: str = ""
    error: str = ""
    output_path: str = ""
    is_playlist: bool = False
    playlist_index: int = 0
    playlist_count: int = 0
    resolve_loaded: int = 0
    resolve_total: int = 0
    entries: list[PlaylistEntry] = field(default_factory=list)
    expanded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode.value,
            "quality": self.quality,
            "title": self.title,
            "status": self.status.name,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "output_path": self.output_path,
            "is_playlist": self.is_playlist,
            "playlist_index": self.playlist_index,
            "playlist_count": self.playlist_count,
            "resolve_loaded": self.resolve_loaded,
            "resolve_total": self.resolve_total,
            "entries": [e.to_dict() for e in self.entries],
            "expanded": self.expanded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadJob":
        status_name = data.get("status", JobStatus.QUEUED.name)
        try:
            status = JobStatus[status_name]
        except KeyError:
            status = JobStatus.QUEUED
        if status in (
            JobStatus.DOWNLOADING,
            JobStatus.CONVERTING,
            JobStatus.RESOLVING,
        ):
            status = JobStatus.QUEUED
        mode_raw = data.get("mode", MediaMode.VIDEO.value)
        try:
            mode = MediaMode(mode_raw)
        except ValueError:
            mode = MediaMode.VIDEO
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            url=data.get("url", ""),
            mode=mode,
            quality=data.get("quality", "best"),
            title=data.get("title", "Načítání…"),
            status=status,
            progress=float(data.get("progress") or 0),
            speed="",
            eta="",
            error=data.get("error", ""),
            output_path=data.get("output_path", ""),
            is_playlist=bool(data.get("is_playlist")),
            playlist_index=int(data.get("playlist_index") or 0),
            playlist_count=int(data.get("playlist_count") or 0),
            resolve_loaded=int(data.get("resolve_loaded") or 0),
            resolve_total=int(data.get("resolve_total") or 0),
            entries=[PlaylistEntry.from_dict(e) for e in data.get("entries") or []],
            # Never restore expanded UI — large playlists freeze startup
            expanded=False,
        )


@dataclass
class AppConfig:
    video_dir: str = field(
        default_factory=lambda: str(
            Path.home() / "Downloads" / "YT Downloader" / "Video"
        )
    )
    audio_dir: str = field(
        default_factory=lambda: str(
            Path.home() / "Downloads" / "YT Downloader" / "MP3"
        )
    )
    # legacy — migrated into video_dir on load
    download_dir: str = ""
    clipboard_monitor: bool = False
    # legacy bool — při načtení se převede na playlist_mode
    download_playlists: bool = False
    # ask | single | all
    playlist_mode: str = "ask"
    auto_download: bool = True
    auto_remove_finished: bool = False
    default_mode: str = MediaMode.VIDEO.value
    default_quality: str = "best"
    audio_bitrate: str = "192"
    # off | browser | file — for age-restricted / "Please sign in" videos
    cookies_mode: str = "off"
    cookies_browser: str = "firefox"
    cookies_file: str = ""
    # dark | light | system
    appearance: str = "dark"
    stats_videos: int = 0
    stats_mp3: int = 0
    stats_bytes: int = 0

    @classmethod
    def load(cls) -> "AppConfig":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # migrate old single download_dir
                if data.get("download_dir") and not data.get("video_dir"):
                    base = data["download_dir"]
                    data["video_dir"] = base
                    data.setdefault(
                        "audio_dir",
                        str(Path(base).parent / "YT Downloader" / "MP3")
                        if Path(base).name != "YT Downloader"
                        else str(Path(base) / "MP3"),
                    )
                    if Path(base).name == "YT Downloader":
                        data["audio_dir"] = str(Path(base) / "MP3")
                        data["video_dir"] = str(Path(base) / "Video")
                cfg = cls(
                    **{k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                )
                if data.get("playlist_mode") not in ("ask", "single", "all"):
                    # starý přepínač: zapnuto = celý playlist, vypnuto = zeptat se
                    cfg.playlist_mode = (
                        "all" if data.get("download_playlists") else "ask"
                    )
                cfg.download_playlists = cfg.playlist_mode == "all"
                if cfg.appearance not in ("dark", "light", "system"):
                    cfg.appearance = "dark"
                cfg._normalize_stats()
                if not cfg.video_dir:
                    cfg.video_dir = str(
                        Path.home() / "Downloads" / "YT Downloader" / "Video"
                    )
                if not cfg.audio_dir:
                    cfg.audio_dir = str(
                        Path.home() / "Downloads" / "YT Downloader" / "MP3"
                    )
                cfg.download_dir = cfg.video_dir
                if not all(
                    key in data
                    for key in ("stats_videos", "stats_mp3", "stats_bytes")
                ):
                    cfg.seed_download_stats()
                return cfg
            except Exception:
                pass
        cfg = cls()
        cfg.download_dir = cfg.video_dir
        cfg.seed_download_stats()
        return cfg

    def _normalize_stats(self) -> None:
        for name in ("stats_videos", "stats_mp3", "stats_bytes"):
            try:
                value = int(getattr(self, name) or 0)
            except (TypeError, ValueError):
                value = 0
            setattr(self, name, max(0, value))

    def seed_download_stats(self) -> None:
        """Jednorázově dopočítá soubory, které už ve složkách jsou."""
        videos, mp3s, total = tally_downloaded_files(self.video_dir, self.audio_dir)
        self.stats_videos = videos
        self.stats_mp3 = mp3s
        self.stats_bytes = total
        self.save()

    def add_completed_download(self, mode: MediaMode, nbytes: int) -> None:
        with _CONFIG_LOCK:
            self._normalize_stats()
            if mode == MediaMode.AUDIO:
                self.stats_mp3 += 1
            else:
                self.stats_videos += 1
            self.stats_bytes += max(0, int(nbytes or 0))
            self._write()

    def save(self) -> None:
        with _CONFIG_LOCK:
            self._write()

    def _write(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.download_dir = self.video_dir
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def dir_for_mode(self, mode: MediaMode) -> str:
        return self.audio_dir if mode == MediaMode.AUDIO else self.video_dir


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_RE.search(text.strip()))


def decode_youtube_url(url: str) -> str:
    """Doplň schéma a dekóduj %XX v cestě (diakritika v @handle)."""
    raw = url.strip().rstrip(").,;]")
    if not raw.startswith("http"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    return urlunparse(parsed._replace(path=unquote(parsed.path or "")))


def extract_youtube_urls(text: str) -> list[str]:
    urls = [m.group(0) for m in YOUTUBE_RE.finditer(text)]
    normalized: list[str] = []
    for u in urls:
        u = decode_youtube_url(u)
        if is_channel_url(u):
            u = normalize_channel_url(u)
        if u not in normalized:
            normalized.append(u)
    return normalized


def has_list_param(url: str) -> bool:
    return bool(re.search(r"[?&]list=", url, re.IGNORECASE))


def is_dedicated_playlist_url(url: str) -> bool:
    lower = url.lower()
    return "playlist?list=" in lower or "/playlist/" in lower


def is_channel_url(url: str) -> bool:
    """YouTube channel / handle pages (@name, /channel/, /c/, /user/)."""
    try:
        raw = url.strip()
        if not raw.startswith("http"):
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return False
        if "youtube.com" not in host:
            return False
        parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
        if not parts:
            return False
        first = parts[0]
        if first.startswith("@"):
            return True
        if first in ("channel", "c", "user") and len(parts) >= 2:
            return True
    except Exception:
        pass
    return False


def normalize_channel_url(url: str) -> str:
    """Point bare channel links at the /videos tab for yt-dlp."""
    raw = url.strip()
    if not raw.startswith("http"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if not parts:
        return raw
    tabs = {
        "videos",
        "streams",
        "shorts",
        "releases",
        "playlists",
        "featured",
        "about",
        "community",
    }
    path = ""
    if parts[0].startswith("@"):
        if len(parts) == 1 or parts[1].lower() not in tabs:
            path = f"/{parts[0]}/videos"
        else:
            path = f"/{parts[0]}/{parts[1]}"
    elif parts[0] in ("channel", "c", "user") and len(parts) >= 2:
        if len(parts) == 2 or parts[2].lower() not in tabs:
            path = f"/{parts[0]}/{parts[1]}/videos"
        else:
            path = f"/{parts[0]}/{parts[1]}/{parts[2]}"
    else:
        return raw
    return urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc or "www.youtube.com",
            path,
            "",
            parsed.query or "",
            "",
        )
    )


# ask = okno s výběrem, single = jen video z odkazu, all = celý playlist
PLAYLIST_MODE_OPTIONS = [
    ("Vždy se zeptat", "ask"),
    ("Jednotlivě", "single"),
    ("Celý playlist", "all"),
]


def playlist_action(url: str, mode: str) -> str:
    """Jak naložit s odkazem: 'single', 'ask' nebo 'all'."""
    if mode not in ("ask", "single", "all"):
        mode = "single"
    collection = (
        is_channel_url(url)
        or is_dedicated_playlist_url(url)
        or has_list_param(url)
    )
    if not collection:
        return "single"
    if mode == "ask":
        return "ask"
    if mode == "all":
        return "all"
    # jednotlivě: watch?v=&list= je jedno video; čistý playlist a kanál nemají „jedno video“
    if is_channel_url(url) or is_dedicated_playlist_url(url):
        return "all"
    return "single"


def video_id_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if "youtu.be" in (parsed.netloc or ""):
            return (parsed.path or "").strip("/").split("/")[0]
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        parts = (parsed.path or "").strip("/").split("/")
        if parts and parts[0] == "shorts" and len(parts) > 1:
            return parts[1]
    except Exception:
        pass
    return ""


def is_valid_video_id(video_id: str) -> bool:
    """YouTube video IDs are 11 chars: A-Za-z0-9_-* """
    return bool(re.fullmatch(r"[\w-]{11}", video_id or ""))


def normalize_watch_url(url: str, video_id: str = "") -> str:
    vid = video_id or video_id_from_url(url)
    if is_valid_video_id(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return url.strip()


def share_url(url: str, video_id: str = "") -> str:
    """Odkaz ke zkopírování: watch URL videa, jinak původní odkaz playlistu/kanálu."""
    cleaned = (url or "").strip()
    vid = video_id or video_id_from_url(cleaned)
    if is_valid_video_id(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return cleaned


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


MIN_FREE_DISK_BYTES = 5 * 1024 ** 3
DISK_CHECK_INTERVAL = 10.0


def disk_free_bytes(path: str) -> Optional[int]:
    """Volné místo na svazku, kam patří cílová složka."""
    probe = Path(path)
    try:
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                return None
            probe = parent
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def disk_warning_text(folders: list[tuple[str, str]]) -> str:
    """Text varování, když na disku stahování zbývá méně než 5 GB."""
    grouped: dict[str, dict[str, Any]] = {}
    for label, path in folders:
        if not path:
            continue
        free = disk_free_bytes(path)
        if free is None or free >= MIN_FREE_DISK_BYTES:
            continue
        drive = os.path.splitdrive(os.path.abspath(path))[0] or os.path.abspath(path)
        slot = grouped.setdefault(
            drive.lower(),
            {"drive": drive, "free": free, "labels": []},
        )
        slot["free"] = min(int(slot["free"]), free)
        if label not in slot["labels"]:
            slot["labels"].append(label)
    if not grouped:
        return ""
    lines: list[str] = []
    for slot in grouped.values():
        names = " a ".join(slot["labels"])
        gb = slot["free"] / (1024 ** 3)
        shown = f"{gb:.2f} GB" if gb < 10 else f"{gb:.0f} GB"
        lines.append(f"{slot['drive']} ({names}) — zbývá {shown}")
    return (
        "Na disku pro stahování zbývá méně než 5 GB.\n\n"
        + "\n".join(lines)
        + "\n\nUvolni místo, jinak se stahování nemusí dokončit."
    )


def clear_job_speed(job: "DownloadJob") -> None:
    job.speed = ""
    job.speed_bps = 0.0
    job.eta = ""


def queue_status_line(jobs: list["DownloadJob"], paused: bool) -> str:
    """Souhrn celé fronty pro spodní lištu."""
    done = errors = total = 0
    speed_bps = 0.0
    active = False
    for job in jobs:
        if job.status == JobStatus.CANCELLED:
            continue
        if job.status in (
            JobStatus.DOWNLOADING,
            JobStatus.CONVERTING,
            JobStatus.RESOLVING,
        ):
            active = True
        speed_bps += max(0.0, float(job.speed_bps or 0))
        if job.is_playlist and job.entries:
            for entry in job.entries:
                if entry.status == JobStatus.SKIPPED:
                    continue
                if not entry.selected and entry.status not in (
                    JobStatus.DONE,
                    JobStatus.ERROR,
                    JobStatus.DOWNLOADING,
                    JobStatus.CONVERTING,
                ):
                    continue
                total += 1
                if entry.status == JobStatus.DONE:
                    done += 1
                elif entry.status == JobStatus.ERROR:
                    errors += 1
        elif job.is_playlist:
            if job.status == JobStatus.ERROR:
                total += 1
                errors += 1
            else:
                total += max(1, job.resolve_total or job.playlist_count or 1)
        else:
            total += 1
            if job.status == JobStatus.DONE:
                done += 1
            elif job.status == JobStatus.ERROR:
                errors += 1
    if total <= 0:
        return "Status: nic ve frontě"
    remaining = total - done - errors
    if active and not paused:
        verb = "stahuji"
    elif paused and remaining > 0:
        verb = "pozastaveno"
    elif remaining > 0:
        verb = "čeká"
    else:
        verb = "hotovo"
    if speed_bps > 0 and active and not paused:
        pretty = format_bytes(speed_bps).replace(".0 ", " ")
        speed = pretty + "/s"
    else:
        speed = "—"
    return (
        f"Status: {verb} {done} / {total}, chyb {errors}, "
        f"rychlost stahování {speed}"
    )


def format_seconds(s: Optional[float]) -> str:
    if s is None or s < 0:
        return ""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


BROWSER_OPTIONS = [
    ("Firefox (doporučeno)", "firefox"),
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Brave", "brave"),
    ("Opera", "opera"),
]

# Chromium-based: cookie DB is often locked by background processes
CHROMIUM_BROWSERS = {"chrome", "edge", "brave", "opera", "chromium", "vivaldi"}
BROWSER_PROCESS_NAMES = {
    "brave": ("brave.exe", "brave"),
    "chrome": ("chrome.exe", "chrome"),
    "edge": ("msedge.exe", "msedge"),
    "opera": ("opera.exe", "opera"),
    "firefox": ("firefox.exe", "firefox"),
}


def list_browser_pids(browser: str) -> list[int]:
    names = BROWSER_PROCESS_NAMES.get(browser.lower(), ())
    if not names or sys.platform != "win32":
        return []
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue
        proc_name = parts[0].lower()
        if any(proc_name == n.lower() or proc_name.startswith(n.lower()) for n in names):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_browser_processes(browser: str) -> int:
    """Force-quit browser processes (needed to unlock Chromium cookie DB)."""
    names = BROWSER_PROCESS_NAMES.get(browser.lower(), ())
    killed = 0
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for name in names:
        exe = name if name.endswith(".exe") else f"{name}.exe"
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/IM", exe, "/T"],
                capture_output=True,
                text=True,
                creationflags=creation,
            )
            if r.returncode == 0:
                killed += 1
                log.info("Ukončeno: %s", exe)
        except Exception as exc:
            log.warning("taskkill %s selhal: %s", exe, exc)
    # Wait until processes actually disappear
    for _ in range(15):
        if not list_browser_pids(browser):
            break
        time.sleep(0.4)
    else:
        log.warning("Některé procesy %s stále běží po taskkill", browser)
    time.sleep(0.5)
    return killed


def windows_force_copy(src: Path, dst: Path) -> None:
    """Copy a file even when another process has it open (Chromium Cookies DB)."""
    if sys.platform != "win32":
        shutil.copy2(src, dst)
        return

    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000007  # READ|WRITE|DELETE
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32
    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    ReadFile = kernel32.ReadFile
    ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    ReadFile.restype = wintypes.BOOL
    CloseHandle = kernel32.CloseHandle
    SetLastError = kernel32.SetLastError
    GetLastError = kernel32.GetLastError

    SetLastError(0)
    handle = CreateFileW(
        str(src),
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    last_err = int(GetLastError())
    # INVALID_HANDLE_VALUE is all bits set; ctypes returns it as a large unsigned int.
    invalid = {None, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}
    try:
        handle_value = int(handle) if handle is not None else None
    except (TypeError, ValueError):
        handle_value = None
    if handle_value in invalid or handle_value == INVALID:
        raise PermissionError(
            f"Cannot open locked file: {src} (WinError {last_err})"
        )

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as out:
            buf = ctypes.create_string_buffer(1024 * 1024)
            read = wintypes.DWORD(0)
            while True:
                ok = ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
                if not ok:
                    raise OSError(f"ReadFile failed for {src} (WinError {GetLastError()})")
                if read.value == 0:
                    break
                out.write(buf.raw[: read.value])
    finally:
        CloseHandle(handle)


def _patched_shutil_copy(src, dst, *args, **kwargs):
    """shutil.copy used by yt-dlp for cookie DB — fall back to shared-mode copy."""
    try:
        return _ORIG_SHUTIL_COPY(src, dst, *args, **kwargs)
    except (PermissionError, OSError):
        windows_force_copy(Path(src), Path(dst))
        return dst


_ORIG_SHUTIL_COPY = shutil.copy
# Patch once so yt-dlp can copy Brave/Chrome Cookies while browser holds a lock
shutil.copy = _patched_shutil_copy  # type: ignore[assignment]


def export_browser_cookies(browser: str, force_quit: bool = False) -> Path:
    """
    Export browser cookies to data/cookies-<browser>.txt (Netscape).
    For Chromium browsers, optionally kill leftover processes first.
    """
    browser = browser.lower().strip()
    out = CONFIG_DIR / f"cookies-{browser}.txt"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Prohlížeč se zavírá jen když to uživatel potvrdil (force_quit).
    # Samotné selhání čtení ho nikdy neukončuje.
    if force_quit and browser in CHROMIUM_BROWSERS and list_browser_pids(browser):
        log.info("Ukončuji %s kvůli cookies (potvrzeno uživatelem)…", browser)
        kill_browser_processes(browser)
    elif browser in CHROMIUM_BROWSERS and list_browser_pids(browser):
        log.warning(
            "%s běží — cookies se zkusí načíst bez ukončení prohlížeče.",
            browser,
        )

    from yt_dlp.cookies import YDLLogger, extract_cookies_from_browser

    class _Log(YDLLogger):
        def __init__(self) -> None:
            pass

        def debug(self, message):
            log.debug("%s", message)

        def info(self, message):
            log.info("%s", message)

        def warning(self, message, only_once=False):
            log.warning("%s", message)

        def error(self, message, only_once=False):
            log.error("%s", message)

        def progress_bar(self):
            return None

    def _do_export() -> Path:
        jar = extract_cookies_from_browser(browser, None, _Log())
        jar.save(str(out))
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError("Export cookies vrátil prázdný soubor")
        log.info("Cookies z %s uloženy do %s (%s položek)", browser, out, len(jar))
        return out

    attempts = 4 if force_quit else 1
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return _do_export()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            log.info("Cookies po ukončení %s ještě nejsou volné, čekám…", browser)
            time.sleep(0.5)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Export cookies selhal")


def is_content_unavailable(msg: str) -> bool:
    lower = strip_ansi(msg).lower()
    return any(
        s in lower
        for s in (
            "video unavailable",
            "claimed content",
            "blocked due to",
            "who has blocked it in your country",
            "private video",
            "this video is private",
            "removed by the uploader",
            "has been removed",
            "no longer available",
            "account associated with this video has been terminated",
            "unsupported url",
            "copyright",
        )
    )


def needs_cookies(msg: str) -> bool:
    lower = strip_ansi(msg).lower()
    return "please sign in" in lower or "sign in to confirm" in lower


def is_expected_download_failure(msg: str) -> bool:
    return is_content_unavailable(msg) or needs_cookies(msg)


def friendly_error(exc: BaseException) -> str:
    msg = strip_ansi(str(exc))
    lower = msg.lower()
    if needs_cookies(msg):
        return (
            "YouTube vyžaduje přihlášení. Zapni Cookies (Firefox nebo cookies.txt) "
            "v nastavení aplikace a zkus znovu."
        )
    if "unsupported url" in lower:
        return "Neplatný nebo nepodporovaný odkaz."
    if "claimed content" in lower or "blocked due to" in lower:
        m = re.search(
            r"blocked due to the claimed content by (.+?)(?:\.|$)",
            msg,
            re.IGNORECASE,
        )
        who = m.group(1).strip() if m else "vlastníka práv"
        return f"Nedostupné — zablokováno kvůli autorským právům ({who})."
    if "terminated" in lower or "no longer available" in lower:
        return "Video už není dostupné (účet zrušen / smazáno)."
    if "video unavailable" in lower:
        return "Video není dostupné (smazané, soukromé nebo zablokované)."
    if "private video" in lower:
        return "Video je soukromé."
    if "cannot open locked file" in lower or (
        "cookie" in lower and ("could not copy" in lower or "winerror 32" in lower)
    ):
        return "Cookies jsou zamčené, protože prohlížeč ještě běží."
    if "failed to load cookies" in lower or "cookie" in lower and "permission" in lower:
        return (
            "Cookies z prohlížeče nejde načíst (soubor je zamčený). "
            "Potvrď ukončení prohlížeče a zkus to znovu."
        )
    if "page needs to be reloaded" in lower:
        return (
            "YouTube dočasně odmítl požadavek. Zkus to znovu za chvíli, "
            "nebo aktualizuj yt-dlp přes start.bat."
        )
    if "ffmpeg" in lower and ("not found" in lower or "is not installed" in lower):
        return "Chybí ffmpeg. Nainstaluj: winget install Gyan.FFmpeg"
    if "http error 403" in lower or "forbidden" in lower:
        return "YouTube vrátil 403. Zkus znovu nebo aktualizuj yt-dlp."
    parts = [p.strip() for p in msg.replace("..", ".").split(".") if p.strip()]
    uniq: list[str] = []
    for p in parts:
        # drop long FAQ URLs from yt-dlp messages
        if "http://" in p.lower() or "https://" in p.lower():
            continue
        if p not in uniq:
            uniq.append(p)
    return ". ".join(uniq)[:240] or msg[:240]


def log_download_issue(context: str, exc: BaseException) -> None:
    """Log YouTube expected failures without flooding the file with tracebacks."""
    msg = strip_ansi(str(exc))
    if is_expected_download_failure(msg):
        log.warning("%s: %s", context, friendly_error(exc))
    else:
        log_exception(context, exc)


def base_ydl_opts(config: Optional[AppConfig] = None) -> dict[str, Any]:
    # With cookies prefer web clients (avoid tv_* session mismatch)
    if config and config.cookies_mode in ("browser", "file"):
        clients = ["web", "web_safari", "web_embedded", "-tv", "-tv_downgraded"]
    else:
        clients = ["default", "web_safari", "web_embedded", "-tv_downgraded"]

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {
                "player_client": clients,
            }
        },
    }

    if config:
        mode = (config.cookies_mode or "off").lower()
        if mode == "browser":
            browser = (config.cookies_browser or "firefox").lower()
            # Chromium: export to file first (handles locked DB + background Brave)
            if browser in CHROMIUM_BROWSERS:
                cached = CONFIG_DIR / f"cookies-{browser}.txt"
                try:
                    need = True
                    if cached.is_file() and cached.stat().st_size > 0:
                        age = time.time() - cached.stat().st_mtime
                        need = age > 2 * 3600
                    if need:
                        try:
                            export_browser_cookies(browser, force_quit=False)
                        except Exception as exc:
                            log.warning(
                                "Export cookies (%s) selhal, prohlížeč se neukončuje: %s",
                                browser,
                                exc,
                            )
                    if cached.is_file() and cached.stat().st_size > 0:
                        opts["cookiefile"] = str(cached)
                except Exception as exc:
                    log.warning("Export cookies (%s) selhal: %s", browser, exc)
                    if cached.is_file() and cached.stat().st_size > 0:
                        opts["cookiefile"] = str(cached)
            else:
                opts["cookiesfrombrowser"] = (browser, None, None, None)
        elif mode == "file" and config.cookies_file:
            path = Path(config.cookies_file)
            if path.is_file():
                opts["cookiefile"] = str(path)
            else:
                log.warning("Cookies soubor neexistuje: %s", path)

    return opts


class _ControlFlow(Exception):
    """Stažení se přeruší schválně (pauza nebo přednost informací o frontě)."""


class _YieldForInfo(_ControlFlow):
    """Nejdřív se mají načíst seznamy playlistů."""


class _JobPaused(_ControlFlow):
    """Tahle úloha je pozastavená."""


class DownloadEngine:
    def __init__(
        self,
        config: AppConfig,
        on_update: Callable[[DownloadJob], None],
        on_queue_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.on_update = on_update
        self.on_queue_changed = on_queue_changed or (lambda: None)
        self._queue: list[DownloadJob] = []
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._cancel_ids: set[str] = set()
        self._active_id: Optional[str] = None
        self._paused = threading.Event()
        self._paused.clear()  # not paused
        self._paused_job_ids: set[str] = set()
        self._wake = threading.Event()
        # (url, callback(title, entries, error)) — načtení seznamu má přednost
        self._previews: list[
            tuple[str, Callable[[str, list[PlaylistEntry], Optional[str]], None]]
        ] = []
        self._persist_lock = threading.Lock()
        self._persist_timer: Optional[threading.Timer] = None

    # --- persistence -----------------------------------------------------

    def load_queue(self, start_worker: bool = True) -> None:
        if not QUEUE_FILE.exists():
            return
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            jobs = [DownloadJob.from_dict(j) for j in data.get("jobs") or []]
        except Exception as exc:
            log_exception("Načítání fronty", exc)
            return
        with self._lock:
            if self.config.auto_download:
                for job in jobs:
                    if job.status == JobStatus.READY:
                        job.status = JobStatus.QUEUED
            for job in jobs:
                job.expanded = False
                if job.status == JobStatus.PAUSED:
                    self._paused_job_ids.add(job.id)
            self._queue = jobs
        log.info(
            "Načtena fronta: %s úloh, %s položek playlistů",
            len(jobs),
            sum(len(j.entries) for j in jobs),
        )
        for job in jobs:
            self.on_update(job)
        if start_worker:
            self._ensure_worker()

    def start_worker(self) -> None:
        self._ensure_worker()

    def save_queue_now(self) -> None:
        with self._lock:
            payload = {"jobs": [j.to_dict() for j in self._queue]}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(QUEUE_FILE)

    def schedule_save(self) -> None:
        with self._persist_lock:
            if self._persist_timer:
                self._persist_timer.cancel()
            self._persist_timer = threading.Timer(0.4, self._safe_save)
            self._persist_timer.daemon = True
            self._persist_timer.start()

    def _safe_save(self) -> None:
        try:
            self.save_queue_now()
        except Exception as exc:
            log_exception("Ukládání fronty", exc)

    def _notify(self, job: DownloadJob) -> None:
        self.on_update(job)
        self.schedule_save()

    # --- queue ops -------------------------------------------------------

    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            return list(self._queue)

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        with self._lock:
            for job in self._queue:
                if job.id == job_id:
                    return job
        return None

    def enqueue(
        self,
        url: str,
        mode: MediaMode,
        quality: str,
        start: Optional[bool] = None,
    ) -> Optional[DownloadJob]:
        url = decode_youtube_url(url)
        if not is_youtube_url(url):
            return None

        if is_channel_url(url):
            url = normalize_channel_url(url)

        action = playlist_action(url, self.config.playlist_mode)
        if action == "ask":
            log.warning("enqueue v režimu dotazu — URL má jít přes dialog: %s", url)
            return None
        is_pl = action == "all"
        if not is_pl:
            vid = video_id_from_url(url)
            if vid and not is_valid_video_id(vid):
                log.warning("Odmítnut neplatný video ID: %s (%s)", vid, url)
                return None
            if is_valid_video_id(vid):
                url = normalize_watch_url(url, vid)

        auto = self.config.auto_download if start is None else start

        with self._lock:
            for job in self._queue:
                if job.url == url and job.status in ACTIVE_STATUSES:
                    return None
            job = DownloadJob(
                id=str(uuid.uuid4()),
                url=url,
                mode=mode,
                quality=quality,
                is_playlist=is_pl,
                status=JobStatus.QUEUED if (auto or is_pl) else JobStatus.READY,
            )
            # playlist se nejdřív jen načte (má přednost před soubory)
            if is_pl:
                job.status = JobStatus.QUEUED
            self._queue.append(job)

        self._notify(job)
        self.on_queue_changed()
        self._ensure_worker()
        return job

    def enqueue_prepared(
        self,
        url: str,
        mode: MediaMode,
        quality: str,
        title: str,
        entries: list[PlaylistEntry],
    ) -> Optional[DownloadJob]:
        """Playlist už s vybranými položkami — bez dalšího načítání."""
        url = decode_youtube_url(url)
        if is_channel_url(url):
            url = normalize_channel_url(url)
        chosen = [e for e in entries if e.selected]
        if not chosen:
            return None
        auto = self.config.auto_download
        with self._lock:
            for job in self._queue:
                if job.url == url and job.status in ACTIVE_STATUSES:
                    return None
            for entry in entries:
                if entry.selected:
                    entry.status = JobStatus.QUEUED
                    entry.error = ""
                    entry.progress = 0
                elif entry.status not in (JobStatus.DONE,):
                    entry.status = JobStatus.SKIPPED
            job = DownloadJob(
                id=str(uuid.uuid4()),
                url=url,
                mode=mode,
                quality=quality,
                title=title or "Playlist",
                is_playlist=True,
                status=JobStatus.QUEUED if auto else JobStatus.READY,
                playlist_count=len(entries),
                entries=entries,
                expanded=False,
            )
            self._queue.append(job)
        self._notify(job)
        self.on_queue_changed()
        self._ensure_worker()
        return job

    def request_preview(
        self,
        url: str,
        callback: Callable[[str, list[PlaylistEntry], Optional[str]], None],
    ) -> None:
        """Načti seznam videí přednostně (stahování souborů počká)."""
        url = decode_youtube_url(url)
        if is_channel_url(url):
            url = normalize_channel_url(url)
        with self._lock:
            self._previews.append((url, callback))
        self._ensure_worker()

    def enqueue_many(
        self, urls: list[str], mode: MediaMode, quality: str
    ) -> tuple[int, int]:
        added = 0
        skipped = 0
        for url in urls:
            job = self.enqueue(url, mode, quality)
            if job:
                added += 1
            else:
                skipped += 1
        return added, skipped

    def cancel(self, job_id: str) -> None:
        self._cancel_ids.add(job_id)
        with self._lock:
            for job in self._queue:
                if job.id != job_id:
                    continue
                if job.status in (
                    JobStatus.QUEUED,
                    JobStatus.READY,
                    JobStatus.PAUSED,
                    JobStatus.RESOLVING,
                ):
                    job.status = JobStatus.CANCELLED
                    self._notify(job)
                break

    def remove_job(self, job_id: str) -> None:
        """Cancel if active and delete from queue permanently."""
        self._cancel_ids.add(job_id)
        with self._lock:
            self._queue = [j for j in self._queue if j.id != job_id]
        self.schedule_save()
        self.on_queue_changed()

    def maybe_auto_remove(self, job: DownloadJob) -> None:
        if not self.config.auto_remove_finished:
            return
        if job.status == JobStatus.DONE:
            self.remove_job(job.id)

    def clear_finished(self) -> None:
        with self._lock:
            self._queue = [
                j
                for j in self._queue
                if j.status not in (JobStatus.DONE, JobStatus.CANCELLED)
            ]
        self.schedule_save()
        self.on_queue_changed()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
            with self._lock:
                for job in self._queue:
                    if job.id in self._paused_job_ids:
                        continue
                    if job.status in (
                        JobStatus.DOWNLOADING,
                        JobStatus.CONVERTING,
                        JobStatus.RESOLVING,
                    ):
                        job.status = JobStatus.PAUSED
                        self._notify(job)
        else:
            was = self._paused.is_set()
            self._paused.clear()
            if was:
                with self._lock:
                    for job in self._queue:
                        if (
                            job.status == JobStatus.PAUSED
                            and job.id not in self._paused_job_ids
                        ):
                            job.status = JobStatus.QUEUED
                            self._notify(job)
                self._ensure_worker()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def start_job(self, job_id: str) -> None:
        with self._lock:
            self._paused_job_ids.discard(job_id)
            for job in self._queue:
                if job.id == job_id and job.status in (
                    JobStatus.READY,
                    JobStatus.PAUSED,
                    JobStatus.ERROR,
                ):
                    if job.status == JobStatus.ERROR:
                        job.error = ""
                        job.progress = 0
                        for entry in job.entries:
                            if entry.selected and entry.status == JobStatus.ERROR:
                                entry.status = JobStatus.QUEUED
                                entry.progress = 0
                                entry.error = ""
                    job.status = JobStatus.QUEUED
                    self._notify(job)
                    break
        self._ensure_worker()

    def is_job_paused(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._paused_job_ids

    def toggle_job_pause(self, job_id: str) -> None:
        """Pozastav / pokračuj jen u jednoho playlistu. Ostatní běží dál."""
        resume = False
        with self._lock:
            job = next((j for j in self._queue if j.id == job_id), None)
            if not job or not job.is_playlist:
                return
            if job.status in (JobStatus.DONE, JobStatus.CANCELLED):
                return
            if job_id in self._paused_job_ids:
                self._paused_job_ids.discard(job_id)
                if job.status == JobStatus.PAUSED:
                    job.status = JobStatus.QUEUED
                self._notify(job)
                resume = True
            else:
                self._paused_job_ids.add(job_id)
                if job.status in (
                    JobStatus.DOWNLOADING,
                    JobStatus.CONVERTING,
                    JobStatus.QUEUED,
                    JobStatus.READY,
                    JobStatus.RESOLVING,
                ):
                    job.status = JobStatus.PAUSED
                self._notify(job)
        if resume:
            self._ensure_worker()

    def retry_entry(self, job_id: str, video_id: str) -> None:
        """Znovu zařaď jedno chybné video. Ve frontě zůstává."""
        with self._lock:
            self._paused_job_ids.discard(job_id)
            for job in self._queue:
                if job.id != job_id:
                    continue
                for entry in job.entries:
                    if entry.video_id != video_id:
                        continue
                    if entry.status != JobStatus.ERROR:
                        return
                    entry.selected = True
                    entry.status = JobStatus.QUEUED
                    entry.progress = 0
                    entry.error = ""
                    if job.status != JobStatus.DOWNLOADING:
                        job.status = JobStatus.QUEUED
                        job.error = ""
                    self._notify(job)
                    break
                break
        self._ensure_worker()

    def set_entry_selected(self, job_id: str, video_id: str, selected: bool) -> None:
        with self._lock:
            for job in self._queue:
                if job.id != job_id:
                    continue
                for entry in job.entries:
                    if entry.video_id == video_id:
                        entry.selected = selected
                        if not selected and entry.status == JobStatus.QUEUED:
                            entry.status = JobStatus.SKIPPED
                        elif selected and entry.status == JobStatus.SKIPPED:
                            entry.status = JobStatus.QUEUED
                        self._notify(job)
                        return

    def set_all_entries_selected(self, job_id: str, selected: bool) -> None:
        with self._lock:
            for job in self._queue:
                if job.id != job_id:
                    continue
                for entry in job.entries:
                    if entry.status in (
                        JobStatus.DONE,
                        JobStatus.DOWNLOADING,
                        JobStatus.CONVERTING,
                        JobStatus.ERROR,
                    ):
                        continue
                    entry.selected = selected
                    entry.status = (
                        JobStatus.QUEUED if selected else JobStatus.SKIPPED
                    )
                self._notify(job)
                return

    def set_expanded(self, job_id: str, expanded: bool) -> None:
        with self._lock:
            for job in self._queue:
                if job.id == job_id:
                    job.expanded = expanded
                    self.schedule_save()
                    return

    # --- worker ----------------------------------------------------------

    def _ensure_worker(self) -> None:
        self._wake.set()
        with self._lock:
            has_preview = bool(self._previews)
        if self._paused.is_set() and not has_preview:
            return
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run_loop, daemon=True, name="download-worker"
        )
        self._worker.start()

    def _has_priority_info_work(self, exclude_job_id: Optional[str] = None) -> bool:
        """Seznamy playlistů se mají načíst dřív, než se stáhne další soubor."""
        with self._lock:
            if self._previews:
                return True
            for other in self._queue:
                if exclude_job_id and other.id == exclude_job_id:
                    continue
                if other.id in self._cancel_ids:
                    continue
                if (
                    other.is_playlist
                    and not other.entries
                    and other.status
                    in (
                        JobStatus.QUEUED,
                        JobStatus.RESOLVING,
                        JobStatus.READY,
                        JobStatus.PAUSED,
                    )
                ):
                    return True
        return False

    def _interrupt_reason(self, job: DownloadJob) -> Optional[str]:
        if job.id in self._cancel_ids:
            return "cancel"
        if job.id in self._paused_job_ids or self._paused.is_set():
            return "pause"
        if self._has_priority_info_work(job.id):
            return "yield"
        return None

    def _next_resolve_job(self) -> Optional[DownloadJob]:
        if self._paused.is_set():
            return None
        with self._lock:
            for job in self._queue:
                if job.id in self._cancel_ids:
                    continue
                if (
                    job.is_playlist
                    and not job.entries
                    and job.status
                    in (
                        JobStatus.QUEUED,
                        JobStatus.RESOLVING,
                        JobStatus.READY,
                        JobStatus.PAUSED,
                    )
                ):
                    return job
        return None

    def _next_download_job(self) -> Optional[DownloadJob]:
        if self._paused.is_set():
            return None
        with self._lock:
            for job in self._queue:
                if job.id in self._cancel_ids or job.id in self._paused_job_ids:
                    continue
                if job.status != JobStatus.QUEUED:
                    continue
                if job.is_playlist and not job.entries:
                    continue
                return job
        return None

    def _run_loop(self) -> None:
        while True:
            if self._previews:
                self._run_preview()
                continue
            if not self._paused.is_set():
                job = self._next_resolve_job()
                if job:
                    self._run_one(job, self._resolve_only)
                    continue
                job = self._next_download_job()
                if job:
                    if job.is_playlist:
                        self._run_one(job, self._download_playlist_entries)
                    else:
                        self._run_one(job, self._download_single)
                    continue
            self._wake.clear()
            if self._worker_has_work() or self._wake.is_set():
                continue
            self._wake.wait(0.3)

    def _worker_has_work(self) -> bool:
        if self._previews:
            return True
        if self._paused.is_set():
            return False
        return bool(self._next_resolve_job() or self._next_download_job())

    def _run_one(self, job: DownloadJob, fn: Callable[[DownloadJob], None]) -> None:
        self._active_id = job.id
        try:
            fn(job)
        except _ControlFlow:
            if job.status not in (
                JobStatus.CANCELLED,
                JobStatus.PAUSED,
                JobStatus.ERROR,
                JobStatus.DONE,
            ):
                job.status = JobStatus.QUEUED
                self._notify(job)
        except Exception as exc:
            job.status = JobStatus.ERROR
            job.error = friendly_error(exc)
            log_download_issue(f"Job {job.id} ({job.url})", exc)
            self._notify(job)
        finally:
            self._active_id = None
            self._cancel_ids.discard(job.id)

    def _run_preview(self) -> None:
        with self._lock:
            if not self._previews:
                return
            url, callback = self._previews.pop(0)
        try:
            title, entries = self._extract_flat(url)
            err: Optional[str] = None
            if not entries:
                err = "Playlist je prázdný nebo nejde načíst."
                entries = []
        except Exception as exc:
            log_download_issue(f"Náhled playlistu {url}", exc)
            title, entries, err = "", [], friendly_error(exc)
        try:
            callback(title, entries, err)
        except Exception as exc:
            log_exception("Zobrazení výběru playlistu", exc)

    def _resolve_only(self, job: DownloadJob) -> None:
        if job.entries:
            return
        ok = self._resolve_playlist(job)
        if not ok:
            return
        if job.id in self._paused_job_ids or self._paused.is_set():
            job.status = JobStatus.PAUSED
            self._notify(job)
            return
        if not self.config.auto_download:
            job.status = JobStatus.READY
            job.progress = 0
            self._notify(job)
            return
        job.status = JobStatus.QUEUED
        self._notify(job)

    def _extract_flat(self, url: str) -> tuple[str, list[PlaylistEntry]]:
        url = decode_youtube_url(url)
        if is_channel_url(url):
            url = normalize_channel_url(url)
        opts = {
            **base_ydl_opts(self.config),
            "extract_flat": "in_playlist",
            "skip_download": True,
            "noplaylist": False,
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Nepodařilo se načíst playlist.")

        raw_entries = [e for e in (info.get("entries") or []) if e]
        total = int(info.get("playlist_count") or len(raw_entries) or 0)
        title = info.get("title") or f"Playlist ({total or len(raw_entries)})"
        entries: list[PlaylistEntry] = []
        for i, entry in enumerate(raw_entries, start=1):
            vid = entry.get("id") or video_id_from_url(entry.get("url") or "")
            if not is_valid_video_id(str(vid)):
                continue
            item_title = entry.get("title") or vid or f"Video {i}"
            item_url = entry.get("url") or entry.get("webpage_url") or ""
            item_url = normalize_watch_url(item_url, str(vid))
            entries.append(
                PlaylistEntry(
                    video_id=str(vid),
                    url=item_url,
                    title=str(item_title),
                    index=int(entry.get("playlist_index") or i),
                    selected=True,
                    status=JobStatus.QUEUED,
                )
            )
        return str(title), entries

    def _resolve_playlist(self, job: DownloadJob) -> bool:
        job.status = JobStatus.RESOLVING
        job.progress = 0
        job.resolve_loaded = 0
        job.resolve_total = 0
        job.error = ""
        self._notify(job)

        if job.id in self._cancel_ids:
            job.status = JobStatus.CANCELLED
            self._notify(job)
            return False

        try:
            title, entries = self._extract_flat(job.url)
        except yt_dlp.utils.DownloadCancelled:
            job.status = (
                JobStatus.CANCELLED
                if job.id in self._cancel_ids
                else JobStatus.PAUSED
            )
            self._notify(job)
            return False
        except Exception as exc:
            job.status = JobStatus.ERROR
            job.error = friendly_error(exc)
            log_download_issue(f"Načítání playlistu {job.url}", exc)
            self._notify(job)
            return False

        if job.id in self._cancel_ids:
            job.status = JobStatus.CANCELLED
            self._notify(job)
            return False

        if not entries:
            job.status = JobStatus.ERROR
            job.error = "Playlist je prázdný nebo nejde načíst."
            log_error("Playlist bez položek", url=job.url, title=title)
            self._notify(job)
            return False

        job.title = title
        job.entries = entries
        job.playlist_count = len(entries)
        job.resolve_loaded = len(entries)
        job.resolve_total = len(entries)
        job.playlist_index = len(entries)
        job.progress = 100
        job.expanded = False
        self._notify(job)
        return True

    def _stop_for(self, job: DownloadJob, reason: str) -> None:
        if reason == "cancel":
            job.status = JobStatus.CANCELLED
        elif reason == "pause":
            job.status = JobStatus.PAUSED
        else:
            job.status = JobStatus.QUEUED
            if reason == "yield":
                log.info("Stahování ustupuje načtení fronty: %s", job.url)
        clear_job_speed(job)
        self._notify(job)

    def _download_playlist_entries(self, job: DownloadJob) -> None:
        for entry in job.entries:
            if not entry.selected and entry.status not in (
                JobStatus.DONE,
                JobStatus.DOWNLOADING,
            ):
                entry.status = JobStatus.SKIPPED

        while True:
            reason = self._interrupt_reason(job)
            if reason:
                self._stop_for(job, reason)
                return

            entry = next(
                (
                    e
                    for e in job.entries
                    if e.selected and e.status == JobStatus.QUEUED
                ),
                None,
            )
            if entry is None:
                break

            job.status = JobStatus.DOWNLOADING
            job.playlist_index = entry.index
            self._notify(job)

            result = self._download_url_into(
                job,
                entry.url,
                entry_ref=entry,
                playlist_title=job.title,
                playlist_index=entry.index,
            )
            if result in ("yield", "pause", "cancel"):
                self._stop_for(job, result)
                return

            selected = [e for e in job.entries if e.selected]
            finished = sum(
                1
                for e in selected
                if e.status
                in (JobStatus.DONE, JobStatus.ERROR, JobStatus.SKIPPED)
            )
            job.progress = (finished / len(selected)) * 100 if selected else 100
            self._notify(job)

        selected = [e for e in job.entries if e.selected]
        oks = sum(1 for e in selected if e.status == JobStatus.DONE)
        skipped = sum(1 for e in selected if e.status == JobStatus.SKIPPED)
        errs = sum(1 for e in selected if e.status == JobStatus.ERROR)
        job.progress = 100
        clear_job_speed(job)
        if not selected:
            job.status = JobStatus.DONE
            job.error = "Žádné položky k stažení."
        elif errs:
            job.status = JobStatus.ERROR
            first = next(
                (e.error for e in selected if e.status == JobStatus.ERROR and e.error),
                "",
            )
            if errs == 1 and first:
                job.error = first
            elif oks:
                job.error = (
                    f"Staženo {oks}, chyba u {errs}. "
                    "Chybná videa zůstávají ve frontě — můžeš je opakovat."
                )
            else:
                job.error = (
                    f"Nepodařilo se stáhnout {errs} videí. "
                    "Zůstávají ve frontě — můžeš opakovat."
                )
            log_error(
                "Playlist s chybami",
                url=job.url,
                ok=oks,
                skipped=skipped,
                errors=errs,
            )
        else:
            job.status = JobStatus.DONE
            if skipped and oks:
                job.error = f"Hotovo: {oks} staženo, {skipped} přeskočeno."
            elif skipped and not oks:
                job.error = f"Nic se nestáhlo ({skipped} přeskočeno)."
            else:
                job.error = ""
        self._notify(job)
        self.maybe_auto_remove(job)

    def _download_single(self, job: DownloadJob) -> None:
        if not self.config.auto_download and job.status == JobStatus.READY:
            return
        reason = self._interrupt_reason(job)
        if reason:
            self._stop_for(job, reason)
            return
        result = self._download_url_into(job, job.url, entry_ref=None)
        if result in ("yield", "pause", "cancel"):
            self._stop_for(job, result)
            return
        if result == "ok":
            job.status = JobStatus.DONE
            job.progress = 100
            clear_job_speed(job)
            job.error = ""
            self._notify(job)
            self.maybe_auto_remove(job)
            return
        if job.status != JobStatus.ERROR:
            job.status = JobStatus.ERROR
        if not job.error:
            job.error = "Stažení selhalo"
        clear_job_speed(job)
        self._notify(job)

    def _download_url_into(
        self,
        job: DownloadJob,
        url: str,
        entry_ref: Optional[PlaylistEntry],
        playlist_title: str = "",
        playlist_index: int = 0,
    ) -> str:
        """Vrátí ok, fail, yield, pause nebo cancel."""
        vid = video_id_from_url(url)
        if vid and not is_valid_video_id(vid):
            err = "Neplatné video ID — přeskočeno."
            log.warning("Neplatné video ID u %s", url)
            if entry_ref:
                entry_ref.status = JobStatus.SKIPPED
                entry_ref.error = err
            else:
                job.status = JobStatus.ERROR
                job.error = err
            self._notify(job)
            return "fail"
        if is_valid_video_id(vid):
            url = normalize_watch_url(url, vid)

        out_dir = Path(self.config.dir_for_mode(job.mode))
        out_dir.mkdir(parents=True, exist_ok=True)
        # Relativní šablona — jinak yt-dlp ignoruje paths a píše rovnou do cíle.
        outtmpl = self._output_template(job, playlist_title, playlist_index)

        if job.mode == MediaMode.AUDIO:
            fmt = "bestaudio/best"
            postprocessors = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self.config.audio_bitrate,
                }
            ]
        else:
            q = job.quality
            if q == "best":
                fmt = "bestvideo*+bestaudio/best"
            else:
                fmt = f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
            postprocessors = [{"key": "FFmpegMetadata"}]

        target = entry_ref  # shorthand

        def progress_hook(d: dict[str, Any]) -> None:
            if job.id in self._cancel_ids:
                raise yt_dlp.utils.DownloadCancelled("Zrušeno uživatelem")
            if job.id in self._paused_job_ids:
                raise _JobPaused()
            if self._has_priority_info_work(job.id):
                raise _YieldForInfo()
            if self._paused.is_set():
                raise yt_dlp.utils.DownloadCancelled("Pozastaveno")

            status = d.get("status")
            if status == "downloading":
                job.status = JobStatus.DOWNLOADING
                if target:
                    target.status = JobStatus.DOWNLOADING
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total * 100) if total else 0
                if target:
                    target.progress = pct
                    # overall approx by finished + current
                    selected = [e for e in job.entries if e.selected]
                    finished = sum(1 for e in selected if e.status == JobStatus.DONE)
                    job.progress = (
                        ((finished + pct / 100) / len(selected)) * 100
                        if selected
                        else pct
                    )
                else:
                    job.progress = pct
                raw_speed = d.get("speed") or 0
                try:
                    job.speed_bps = float(raw_speed or 0)
                except (TypeError, ValueError):
                    job.speed_bps = 0.0
                job.speed = (
                    format_bytes(job.speed_bps) + "/s" if job.speed_bps else ""
                )
                job.eta = format_seconds(d.get("eta"))
                info = d.get("info_dict") or {}
                if info.get("title"):
                    if target:
                        target.title = info["title"]
                    else:
                        job.title = info["title"]
                self._notify(job)
            elif status == "finished":
                clear_job_speed(job)
                if job.mode == MediaMode.AUDIO:
                    job.status = JobStatus.CONVERTING
                    if target:
                        target.status = JobStatus.CONVERTING
                        target.progress = 100
                filename = d.get("filename") or ""
                if filename:
                    job.output_path = filename
                    if target:
                        target.output_path = filename
                self._notify(job)

        opts: dict[str, Any] = {
            **base_ydl_opts(self.config),
            "format": fmt,
            "outtmpl": outtmpl,
            "paths": {
                "home": str(out_dir),
                "temp": INCOMPLETE_SUBDIR,
            },
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": 4,
            "postprocessors": postprocessors,
            "writethumbnail": False,
            "noplaylist": True,
            "ignoreerrors": False,
        }
        if job.mode == MediaMode.VIDEO:
            opts["merge_output_format"] = "mp4"

        try:
            pending = self._interrupt_reason(job)
            if pending:
                return pending
            with yt_dlp.YoutubeDL(opts) as ydl:
                # fetch title for single if needed
                if not target and (not job.title or job.title == "Načítání…"):
                    try:
                        meta = ydl.extract_info(url, download=False)
                        if meta and meta.get("title"):
                            job.title = meta["title"]
                            self._notify(job)
                    except Exception:
                        pass
                pending = self._interrupt_reason(job)
                if pending:
                    return pending
                info = ydl.extract_info(url, download=True)
            final = ""
            if isinstance(info, dict):
                final = str(info.get("filepath") or "")
                if not final:
                    requested = info.get("requested_downloads") or []
                    if requested and isinstance(requested[0], dict):
                        final = str(requested[0].get("filepath") or "")
            if final:
                job.output_path = final
                if target:
                    target.output_path = final
            if target:
                target.status = JobStatus.DONE
                target.progress = 100
                target.error = ""
            self._remember_download(job, final)
            return "ok"
        except _YieldForInfo:
            if target and target.status != JobStatus.DONE:
                target.status = JobStatus.QUEUED
                target.progress = 0
            return "yield"
        except _JobPaused:
            if target and target.status != JobStatus.DONE:
                target.status = JobStatus.QUEUED
                target.progress = 0
            return "pause"
        except yt_dlp.utils.DownloadCancelled:
            if job.id in self._cancel_ids:
                if target and target.status != JobStatus.DONE:
                    target.status = JobStatus.CANCELLED
                return "cancel"
            if target and target.status != JobStatus.DONE:
                target.status = JobStatus.QUEUED
                target.progress = 0
            return "pause"
        except Exception as exc:
            err = friendly_error(exc)
            log_download_issue(f"Stahování {url}", exc)
            if target:
                target.status = JobStatus.ERROR
                target.error = err
                target.progress = 0
                job.error = err
            else:
                job.status = JobStatus.ERROR
                job.error = err
            self._notify(job)
            return "fail"
        finally:
            remove_incomplete_dir_if_empty(out_dir)

    def _remember_download(self, job: DownloadJob, path: str) -> None:
        size = 0
        if path:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        try:
            self.config.add_completed_download(job.mode, size)
        except Exception as exc:
            log.warning("Statistiku stahování nejde uložit: %s", exc)

    def _output_template(
        self,
        job: DownloadJob,
        playlist_title: str,
        playlist_index: int,
    ) -> str:
        """Relativní cesta v cílové složce. Rozpracovaný soubor je v _nedokoncene."""
        if job.mode == MediaMode.AUDIO:
            leaf = "%(title)s.%(ext)s"
            indexed = "%(playlist_index)03d - %(title)s.%(ext)s"
        else:
            leaf = "%(title)s [%(height)sp].%(ext)s"
            indexed = "%(playlist_index)03d - %(title)s [%(height)sp].%(ext)s"
        if job.is_playlist and playlist_index:
            folder = self._safe_name(playlist_title or "Playlist")
            if job.mode == MediaMode.AUDIO:
                leaf = f"{playlist_index:03d} - %(title)s.%(ext)s"
            else:
                leaf = f"{playlist_index:03d} - %(title)s [%(height)sp].%(ext)s"
            return f"{folder}/{leaf}"
        if job.is_playlist and playlist_title:
            return f"%(playlist_title)s/{indexed}"
        return leaf

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
        return cleaned[:80] or "Playlist"


class ClipboardMonitor:
    def __init__(
        self,
        root: ctk.CTk,
        on_url: Callable[[str], None],
        interval_ms: int = 800,
    ) -> None:
        self.root = root
        self.on_url = on_url
        self.interval_ms = interval_ms
        self.enabled = False
        self._last = ""
        self._after_id: Optional[str] = None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled:
            try:
                self._last = self.root.clipboard_get()
            except Exception:
                self._last = ""
            self._tick()
        elif self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None

    def _tick(self) -> None:
        if not self.enabled:
            return
        try:
            text = self.root.clipboard_get()
        except Exception:
            text = ""
        if text and text != self._last:
            self._last = text
            for url in extract_youtube_urls(text):
                self.on_url(url)
        self._after_id = self.root.after(self.interval_ms, self._tick)

    def note(self, text: str) -> None:
        """Vlastní zápis do schránky — monitor ho nesmí znovu přidat do fronty."""
        self._last = text


def _pointer_inside(widget: Any) -> bool:
    try:
        if not widget.winfo_exists():
            return False
        x, y = widget.winfo_pointerxy()
        target = widget.winfo_containing(x, y)
    except Exception:
        return False
    while target is not None:
        if target is widget:
            return True
        target = getattr(target, "master", None)
    return False


class _HoverReveal:
    """Tlačítko je ve layoutu jen když je ukazatel myši nad řádkem."""

    def __init__(self, row: Any, button: ctk.CTkButton, grid_kwargs: dict[str, Any]) -> None:
        self.row = row
        self.button = button
        self.grid_kwargs = grid_kwargs
        self.label = "Kopírovat odkaz"
        self._token = 0
        self._bound: set[int] = set()
        self._reset_job: Optional[str] = None
        button.grid(**grid_kwargs)
        button.grid_remove()
        self.rebind()

    def rebind(self, root: Any = None) -> None:
        self._bind(self.row if root is None else root)

    def _bind(self, widget: Any) -> None:
        key = id(widget)
        if key not in self._bound:
            self._bound.add(key)
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._bind(child)

    def _enter(self, _event: Any = None) -> None:
        self._token += 1
        self.button.grid(**self.grid_kwargs)

    def _leave(self, _event: Any = None) -> None:
        self._token += 1
        token = self._token
        self.row.after(50, lambda: self._maybe_hide(token))

    def _maybe_hide(self, token: int) -> None:
        if token != self._token:
            return
        try:
            if not self.row.winfo_exists():
                return
        except Exception:
            return
        if _pointer_inside(self.row):
            return
        self.button.grid_remove()
        self.button.configure(text=self.label)

    def flash(self, text: str = "Zkopírováno") -> None:
        self.button.configure(text=text)
        if self._reset_job:
            try:
                self.row.after_cancel(self._reset_job)
            except Exception:
                pass
        self._reset_job = self.row.after(1200, self._restore_label)

    def _restore_label(self) -> None:
        try:
            if self.button.winfo_exists():
                self.button.configure(text=self.label)
        except Exception:
            pass


class JobRow(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        job: DownloadJob,
        on_remove: Callable[[str], None],
        on_start: Callable[[str], None],
        on_toggle_expand: Callable[[str], None],
        on_entry_toggle: Callable[[str, str, bool], None],
        on_select_all: Callable[[str, bool], None],
        on_toggle_pause: Callable[[str], None],
        on_retry_entry: Callable[[str, str], None],
        is_job_paused: Callable[[str], bool],
        is_globally_paused: Callable[[], bool],
        on_copy_url: Callable[[str], bool],
    ) -> None:
        super().__init__(master, fg_color=("#eceff1", "#1e2430"), corner_radius=12)
        self.job = job
        self.on_remove = on_remove
        self.on_start = on_start
        self.on_toggle_expand = on_toggle_expand
        self.on_entry_toggle = on_entry_toggle
        self.on_select_all = on_select_all
        self.on_toggle_pause = on_toggle_pause
        self.on_retry_entry = on_retry_entry
        self.is_job_paused = is_job_paused
        self.is_globally_paused = is_globally_paused
        self.on_copy_url = on_copy_url
        self._entry_vars: dict[str, ctk.BooleanVar] = {}
        self._building_entries = False
        self._entries_page = 0
        self._entries_sig: tuple[Any, ...] = ()
        self._page_lbl: Optional[ctk.CTkLabel] = None

        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        top.grid_columnconfigure(1, weight=1)

        self.expand_btn = ctk.CTkButton(
            top,
            text="Rozbalit",
            width=100,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=("#90a4ae", "#455a64"),
            hover_color=("#cfd8dc", "#2a3344"),
            text_color=("#455a64", "#cfd8dc"),
            command=lambda: on_toggle_expand(job.id),
        )
        self.expand_btn.grid(row=0, column=0, padx=(0, 8))
        self.expand_btn.grid_remove()

        self.title_lbl = ctk.CTkLabel(
            top,
            text=job.title,
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.title_lbl.grid(row=0, column=1, sticky="ew")

        self.copy_btn = ctk.CTkButton(
            top,
            text="Kopírovat odkaz",
            width=132,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=("#90a4ae", "#455a64"),
            hover_color=("#cfd8dc", "#2a3344"),
            text_color=("#455a64", "#cfd8dc"),
            command=self._copy_job_link,
        )
        self._copy_hover = _HoverReveal(
            self, self.copy_btn, {"row": 0, "column": 2, "padx": (8, 0)}
        )

        self.pause_btn = ctk.CTkButton(
            top,
            text="Pauza",
            width=110,
            height=28,
            fg_color=("#fff3e0", "#3e2f1a"),
            text_color=("#ef6c00", "#ffb74d"),
            hover_color=("#ffe0b2", "#5a4324"),
            command=lambda: on_toggle_pause(job.id),
        )
        self.pause_btn.grid(row=0, column=3, padx=(4, 0))

        self.start_btn = ctk.CTkButton(
            top,
            text="▶",
            width=32,
            height=28,
            fg_color="transparent",
            hover_color=("#c8e6c9", "#1b3d2f"),
            text_color=("#2e7d32", "#81c784"),
            command=lambda: on_start(job.id),
        )
        self.start_btn.grid(row=0, column=4, padx=(4, 0))

        self.remove_btn = ctk.CTkButton(
            top,
            text="✕",
            width=32,
            height=28,
            fg_color="transparent",
            hover_color=("#ffcdd2", "#5c2b2b"),
            text_color=("#c62828", "#ef9a9a"),
            command=lambda: on_remove(job.id),
        )
        self.remove_btn.grid(row=0, column=5, padx=(4, 0))

        self.meta_lbl = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            text_color=("#607d8b", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        )
        self.meta_lbl.grid(row=1, column=0, sticky="ew", padx=14)

        self.progress = ctk.CTkProgressBar(self, height=8)
        self.progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 4))
        self.progress.set(0)

        self.status_lbl = ctk.CTkLabel(
            self,
            text=job.status.value,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("#455a64", "#b0bec5"),
        )
        self.status_lbl.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 2))

        self.error_lbl = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            wraplength=760,
            font=ctk.CTkFont(size=12),
            text_color=("#c62828", "#ef9a9a"),
        )
        self.error_lbl.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.error_lbl.grid_remove()

        self.entries_wrap = ctk.CTkFrame(self, fg_color="transparent")
        self.entries_wrap.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.entries_wrap.grid_columnconfigure(0, weight=1)
        self.entries_wrap.grid_remove()

        self.refresh(job)

    def refresh(self, job: DownloadJob) -> None:
        self.job = job
        title = job.title[:80] + ("…" if len(job.title) > 80 else "")
        self.title_lbl.configure(text=title)
        self.progress.set(max(0.0, min(1.0, job.progress / 100.0)))

        meta = ["MP3" if job.mode == MediaMode.AUDIO else "Video"]
        if job.mode == MediaMode.VIDEO:
            qlabel = next(
                (l for l, v in QUALITY_OPTIONS if v == job.quality), job.quality
            )
            meta.append(qlabel)
        if job.is_playlist:
            kind = "Kanál" if is_channel_url(job.url) else "Playlist"
            meta.append(
                f"{kind} ({len(job.entries) or job.playlist_count or '?'})"
            )
        self.meta_lbl.configure(text=" · ".join(meta))

        parts = [job.status.value]
        if job.status == JobStatus.RESOLVING:
            if job.resolve_total:
                parts.append(f"{job.resolve_loaded}/{job.resolve_total}")
            parts.append(f"{job.progress:.0f}%")
        elif job.is_playlist and job.entries and job.status == JobStatus.DOWNLOADING:
            cur = next(
                (e for e in job.entries if e.status == JobStatus.DOWNLOADING), None
            )
            done = sum(1 for e in job.entries if e.status == JobStatus.DONE)
            sel = sum(1 for e in job.entries if e.selected)
            parts.append(f"{done}/{sel}")
            if cur:
                parts.append(cur.title[:40])
            err_n = sum(1 for e in job.entries if e.status == JobStatus.ERROR)
            if err_n:
                parts.append(f"{err_n} chyb")
        if job.progress and job.status in (
            JobStatus.DOWNLOADING,
            JobStatus.RESOLVING,
        ):
            if f"{job.progress:.0f}%" not in parts:
                parts.append(f"{job.progress:.0f}%")
        if job.speed:
            parts.append(job.speed)
        if job.eta:
            parts.append(f"ETA {job.eta}")
        self.status_lbl.configure(
            text=" · ".join(parts),
            text_color=(
                ("#c62828", "#ef9a9a")
                if job.status == JobStatus.ERROR
                else ("#455a64", "#b0bec5")
            ),
        )
        if job.error:
            failed = job.status == JobStatus.ERROR or any(
                entry.status == JobStatus.ERROR for entry in job.entries
            )
            self.error_lbl.configure(
                text=job.error,
                text_color=(
                    ("#c62828", "#ef9a9a")
                    if failed
                    else ("#78909c", "#90a4ae")
                ),
            )
            self.error_lbl.grid()
        else:
            self.error_lbl.grid_remove()

        can_expand = job.is_playlist and bool(job.entries)
        if job.is_playlist:
            self.expand_btn.configure(
                state="normal" if can_expand else "disabled",
                text="Sbalit" if job.expanded and can_expand else "Rozbalit",
            )
            self.expand_btn.grid()
        else:
            self.expand_btn.grid_remove()

        show_pause = job.is_playlist and job.status in (
            JobStatus.QUEUED,
            JobStatus.RESOLVING,
            JobStatus.READY,
            JobStatus.DOWNLOADING,
            JobStatus.CONVERTING,
            JobStatus.PAUSED,
        )
        if show_pause:
            if job.status == JobStatus.READY:
                self.pause_btn.configure(
                    text="Pokračovat",
                    state="normal",
                    command=lambda jid=job.id: self.on_start(jid),
                )
            elif self.is_globally_paused():
                self.pause_btn.configure(
                    text="Pauza",
                    state="disabled",
                    command=lambda jid=job.id: self.on_toggle_pause(jid),
                )
            elif self.is_job_paused(job.id):
                self.pause_btn.configure(
                    text="Pokračovat",
                    state="normal",
                    command=lambda jid=job.id: self.on_toggle_pause(jid),
                )
            elif job.status == JobStatus.PAUSED:
                self.pause_btn.configure(
                    text="Pokračovat",
                    state="normal",
                    command=lambda jid=job.id: self.on_start(jid),
                )
            else:
                self.pause_btn.configure(
                    text="Pauza",
                    state="normal",
                    command=lambda jid=job.id: self.on_toggle_pause(jid),
                )
            self.pause_btn.grid()
        else:
            self.pause_btn.grid_remove()

        if job.is_playlist:
            if job.status == JobStatus.ERROR:
                self.start_btn.configure(text="Opakovat", width=88, state="normal")
                self.start_btn.grid()
            else:
                self.start_btn.grid_remove()
        elif job.status == JobStatus.ERROR:
            self.start_btn.configure(text="Opakovat", width=88, state="normal")
            self.start_btn.grid()
        else:
            show_start = job.status in (JobStatus.READY, JobStatus.PAUSED)
            self.start_btn.configure(
                text="▶",
                width=32,
                state="normal" if show_start else "disabled",
            )
            self.start_btn.grid()
        self.remove_btn.configure(state="normal")

        if job.expanded and can_expand:
            self._render_entries(job)
            self.entries_wrap.grid()
        else:
            self.entries_wrap.grid_remove()
        self._copy_hover.rebind()

    def _copy_job_link(self) -> None:
        url = (self.job.url or "").strip()
        if url and self.on_copy_url(url):
            self._copy_hover.flash()

    def _copy_entry_link(self, url: str, hover: _HoverReveal) -> None:
        if url and self.on_copy_url(url):
            hover.flash()

    def _entries_signature(self, job: DownloadJob) -> tuple[Any, ...]:
        return (job.id, len(job.entries), self._entries_page)

    def _render_entries(self, job: DownloadJob) -> None:
        """Render playlist rows into the job card — no nested scroll (queue scrolls)."""
        total = len(job.entries)
        pages = max(1, (total + ENTRIES_PAGE_SIZE - 1) // ENTRIES_PAGE_SIZE)
        if self._entries_page >= pages:
            self._entries_page = pages - 1
        if self._entries_page < 0:
            self._entries_page = 0

        start = self._entries_page * ENTRIES_PAGE_SIZE
        end = min(start + ENTRIES_PAGE_SIZE, total)
        page_entries = job.entries[start:end]
        sig = self._entries_signature(job)

        if sig != self._entries_sig:
            for child in self.entries_wrap.winfo_children():
                child.destroy()
            self._entry_vars.clear()
            self._entries_sig = sig

            toolbar = ctk.CTkFrame(self.entries_wrap, fg_color="transparent")
            toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            toolbar.grid_columnconfigure(2, weight=1)

            ctk.CTkButton(
                toolbar,
                text="Vybrat vše",
                width=90,
                height=28,
                fg_color="transparent",
                border_width=1,
                border_color=("#90a4ae", "#455a64"),
                command=lambda: self.on_select_all(job.id, True),
            ).grid(row=0, column=0, padx=(0, 6))
            ctk.CTkButton(
                toolbar,
                text="Zrušit výběr",
                width=90,
                height=28,
                fg_color="transparent",
                border_width=1,
                border_color=("#90a4ae", "#455a64"),
                command=lambda: self.on_select_all(job.id, False),
            ).grid(row=0, column=1, padx=(0, 10))

            nav = ctk.CTkFrame(toolbar, fg_color="transparent")
            nav.grid(row=0, column=3, sticky="e")

            self._page_lbl = ctk.CTkLabel(
                nav,
                text=f"{start + 1}–{end} z {total}",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=("#455a64", "#cfd8dc"),
            )
            self._page_lbl.pack(side="left", padx=(0, 8))

            prev_btn = ctk.CTkButton(
                nav,
                text="◀ Předchozí",
                width=110,
                height=28,
                command=lambda: self._change_page(job, -1),
            )
            prev_btn.pack(side="left", padx=(0, 4))
            if self._entries_page <= 0:
                prev_btn.configure(state="disabled")

            next_btn = ctk.CTkButton(
                nav,
                text="Další ▶",
                width=100,
                height=28,
                command=lambda: self._change_page(job, 1),
            )
            next_btn.pack(side="left")
            if self._entries_page >= pages - 1:
                next_btn.configure(state="disabled")

            # Plain frame — NOT CTkScrollableFrame (one scroll = fronta)
            list_frame = ctk.CTkFrame(
                self.entries_wrap,
                fg_color=("#e8eef3", "#151a24"),
                corner_radius=8,
            )
            list_frame.grid(row=1, column=0, sticky="ew")
            list_frame.grid_columnconfigure(0, weight=1)
            self._list_frame = list_frame

            for i, entry in enumerate(page_entries):
                var = ctk.BooleanVar(value=entry.selected)
                self._entry_vars[entry.video_id] = var
                row = ctk.CTkFrame(list_frame, fg_color="transparent")
                row.grid(row=i, column=0, sticky="ew", padx=8, pady=2)
                row.grid_columnconfigure(1, weight=1)

                cb = ctk.CTkCheckBox(
                    row,
                    text="",
                    width=24,
                    variable=var,
                    command=lambda vid=entry.video_id, v=var: self.on_entry_toggle(
                        job.id, vid, bool(v.get())
                    ),
                )
                cb.grid(row=0, column=0)
                if entry.status in (
                    JobStatus.DONE,
                    JobStatus.DOWNLOADING,
                    JobStatus.CONVERTING,
                ):
                    cb.configure(state="disabled")

                lbl = ctk.CTkLabel(
                    row,
                    text=f"{entry.index:03d}. {entry.title[:70]}",
                    anchor="w",
                    font=ctk.CTkFont(size=12),
                )
                lbl.grid(row=0, column=1, sticky="ew", padx=6)

                st = ctk.CTkLabel(
                    row,
                    text=self._entry_status_text(entry),
                    anchor="e",
                    width=70,
                    font=ctk.CTkFont(size=11),
                    text_color=self._entry_status_color(entry),
                )
                st.grid(row=0, column=2)

                retry = ctk.CTkButton(
                    row,
                    text="Opakovat",
                    width=72,
                    height=24,
                    fg_color=("#ffebee", "#4a2424"),
                    text_color=("#c62828", "#ef9a9a"),
                    hover_color=("#ffcdd2", "#5c2b2b"),
                    command=lambda vid=entry.video_id: self.on_retry_entry(
                        job.id, vid
                    ),
                )
                retry.grid(row=0, column=4, padx=(6, 0))
                if entry.status != JobStatus.ERROR:
                    retry.grid_remove()

                entry_url = share_url(entry.url, entry.video_id)
                entry_hover: Optional[_HoverReveal] = None
                if entry_url:
                    copy_btn = ctk.CTkButton(
                        row,
                        text="Kopírovat odkaz",
                        width=124,
                        height=24,
                        font=ctk.CTkFont(size=11),
                        fg_color="transparent",
                        border_width=1,
                        border_color=("#90a4ae", "#455a64"),
                        hover_color=("#cfd8dc", "#2a3344"),
                        text_color=("#455a64", "#cfd8dc"),
                    )
                    entry_hover = _HoverReveal(
                        row,
                        copy_btn,
                        {"row": 0, "column": 3, "padx": (6, 0)},
                    )
                    copy_btn.configure(
                        command=lambda u=entry_url, h=entry_hover: self._copy_entry_link(
                            u, h
                        )
                    )

                err = ctk.CTkLabel(
                    row,
                    text=entry.error if entry.status == JobStatus.ERROR else "",
                    anchor="w",
                    justify="left",
                    wraplength=640,
                    font=ctk.CTkFont(size=11),
                    text_color=("#c62828", "#ef9a9a"),
                )
                err.grid(row=1, column=1, columnspan=4, sticky="ew", padx=6)
                if entry.status != JobStatus.ERROR or not entry.error:
                    err.grid_remove()
                if entry_hover is not None:
                    entry_hover.rebind()

                setattr(self, f"_st_{entry.video_id}", st)
                setattr(self, f"_cb_{entry.video_id}", cb)
                setattr(self, f"_retry_{entry.video_id}", retry)
                setattr(self, f"_err_{entry.video_id}", err)

            if pages > 1:
                foot = ctk.CTkFrame(self.entries_wrap, fg_color="transparent")
                foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
                ctk.CTkButton(
                    foot,
                    text=f"Dalších {min(ENTRIES_PAGE_SIZE, total - end)} písniček ▶"
                    if end < total
                    else "Konec seznamu",
                    height=32,
                    command=lambda: self._change_page(job, 1),
                    state="normal" if end < total else "disabled",
                ).pack(fill="x")
        else:
            for entry in page_entries:
                var = self._entry_vars.get(entry.video_id)
                if var is not None and bool(var.get()) != entry.selected:
                    var.set(entry.selected)
                st = getattr(self, f"_st_{entry.video_id}", None)
                if st is not None:
                    st.configure(
                        text=self._entry_status_text(entry),
                        text_color=self._entry_status_color(entry),
                    )
                retry = getattr(self, f"_retry_{entry.video_id}", None)
                if retry is not None:
                    if entry.status == JobStatus.ERROR:
                        retry.grid()
                    else:
                        retry.grid_remove()
                err = getattr(self, f"_err_{entry.video_id}", None)
                if err is not None:
                    if entry.status == JobStatus.ERROR and entry.error:
                        err.configure(text=entry.error)
                        err.grid()
                    else:
                        err.grid_remove()
                cb = getattr(self, f"_cb_{entry.video_id}", None)
                if cb is not None:
                    state = (
                        "disabled"
                        if entry.status
                        in (
                            JobStatus.DONE,
                            JobStatus.DOWNLOADING,
                            JobStatus.CONVERTING,
                        )
                        else "normal"
                    )
                    cb.configure(state=state)

    def _change_page(self, job: DownloadJob, delta: int) -> None:
        total = len(job.entries)
        pages = max(1, (total + ENTRIES_PAGE_SIZE - 1) // ENTRIES_PAGE_SIZE)
        self._entries_page = max(0, min(pages - 1, self._entries_page + delta))
        self._entries_sig = ()  # force rebuild
        self._render_entries(job)

    @staticmethod
    def _entry_status_text(entry: PlaylistEntry) -> str:
        if entry.status == JobStatus.DOWNLOADING:
            return f"{entry.progress:.0f}%"
        if entry.status == JobStatus.DONE:
            return "✓"
        if entry.status == JobStatus.ERROR:
            return "Chyba"
        if entry.status == JobStatus.SKIPPED:
            return "—"
        if not entry.selected:
            return "—"
        if entry.status == JobStatus.CONVERTING:
            return "MP3…"
        return ""

    @staticmethod
    def _entry_status_color(entry: PlaylistEntry) -> tuple[str, str]:
        if entry.status == JobStatus.ERROR:
            return ("#c62828", "#ef9a9a")
        if entry.status == JobStatus.DONE:
            return ("#2e7d32", "#81c784")
        return ("#78909c", "#90a4ae")


def _icon_font(size: int) -> ImageFont.ImageFont:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        path = Path(windir) / "Fonts" / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _moon_icon(rgb: tuple[int, int, int]) -> Image.Image:
    size = 48
    disk = Image.new("L", (size, size), 0)
    ImageDraw.Draw(disk).ellipse((4, 6, 40, 42), fill=255)
    hole = Image.new("L", (size, size), 0)
    ImageDraw.Draw(hole).ellipse((16, 2, 54, 40), fill=255)
    alpha = ImageChops.subtract(disk, hole)
    img = Image.new("RGBA", (size, size), (*rgb, 255))
    img.putalpha(alpha)
    return img


def _sun_icon(rgb: tuple[int, int, int], letter: str = "") -> Image.Image:
    size = 48
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = 24.0
    for i in range(8):
        ang = math.radians(i * 45 - 90)
        x1 = cx + math.cos(ang) * 13
        y1 = cy + math.sin(ang) * 13
        x2 = cx + math.cos(ang) * 22
        y2 = cy + math.sin(ang) * 22
        draw.line((x1, y1, x2, y2), fill=(*rgb, 255), width=4)
    draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(*rgb, 255))
    if letter:
        draw.text(
            (cx, cy + 1),
            letter,
            font=_icon_font(17),
            fill=(62, 36, 8, 255),
            anchor="mm",
        )
    return img


def theme_images() -> dict[str, ctk.CTkImage]:
    """Měsíc = tmavý, slunce = světlý, slunce s A = podle systému."""
    sun = _sun_icon((249, 168, 37))
    auto = _sun_icon((249, 168, 37), "A")
    size = (18, 18)
    return {
        "dark": ctk.CTkImage(
            light_image=_moon_icon((55, 71, 79)),
            dark_image=_moon_icon((236, 239, 241)),
            size=size,
        ),
        "light": ctk.CTkImage(light_image=sun, dark_image=sun, size=size),
        "system": ctk.CTkImage(light_image=auto, dark_image=auto, size=size),
    }


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1360x700")
        self.minsize(1180, 520)

        self.config_data = AppConfig.load()
        if self.config_data.appearance not in ("dark", "light", "system"):
            self.config_data.appearance = "dark"
        ctk.set_appearance_mode(self.config_data.appearance)
        ctk.set_default_color_theme("blue")
        Path(self.config_data.video_dir).mkdir(parents=True, exist_ok=True)
        Path(self.config_data.audio_dir).mkdir(parents=True, exist_ok=True)

        self.engine = DownloadEngine(
            self.config_data, self._on_job_update, self._on_queue_structure_change
        )
        self.rows: dict[str, JobRow] = {}
        self._ui_pending: dict[str, DownloadJob] = {}
        self._rebuild_queue = False
        self._ask_queue: list[str] = []
        self._ask_busy = False
        self._preview_gen = 0
        self._preview_lock = threading.Lock()
        self._preview_results: list[tuple[Any, ...]] = []
        self._disk_warned = False
        self._disk_check_at = 0.0

        self._build_ui()
        self.clipboard = ClipboardMonitor(self, self._on_clipboard_url)
        self._apply_switch_states()

        self.engine.load_queue(start_worker=False)
        self._rebuild_all_rows()
        self._refresh_status_bar()
        # Start downloads after UI is ready (avoids freeze on large playlists)
        self.after(300, self.engine.start_worker)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._flush_ui)

    def _apply_switch_states(self) -> None:
        if self.config_data.clipboard_monitor:
            self.clip_switch.select()
            self.clipboard.set_enabled(True)
        if self.config_data.auto_download:
            self.auto_switch.select()
        if self.config_data.auto_remove_finished:
            self.auto_remove_switch.select()

    def _build_ui(self) -> None:
        self.configure(fg_color=("#f5f7fa", "#12151c"))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text=APP_NAME,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=("#0d47a1", "#90caf9"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text="YouTube videa · MP3 · playlisty",
            font=ctk.CTkFont(size=12),
            text_color=("#607d8b", "#78909c"),
        ).grid(row=1, column=0, sticky="w")

        hdr_btns = ctk.CTkFrame(header, fg_color="transparent")
        hdr_btns.grid(row=0, column=1, rowspan=2, sticky="e")

        self._theme_images = theme_images()
        theme_bar = ctk.CTkFrame(
            hdr_btns,
            fg_color=("#e2e8ee", "#2a3140"),
            corner_radius=8,
            height=30,
        )
        theme_bar.pack(side="left", padx=(0, 8))
        self.theme_buttons: dict[str, ctk.CTkButton] = {}
        for mode in ("dark", "light", "system"):
            btn = ctk.CTkButton(
                theme_bar,
                text="",
                image=self._theme_images[mode],
                width=34,
                height=26,
                corner_radius=6,
                fg_color="transparent",
                hover_color=("#cfd8dc", "#3a4458"),
                command=lambda m=mode: self._set_appearance(m),
            )
            btn.pack(side="left", padx=1, pady=2)
            self.theme_buttons[mode] = btn
        self._apply_theme_buttons()

        ctk.CTkButton(
            hdr_btns,
            text="Log chyb",
            width=84,
            height=28,
            fg_color=("#efebe9", "#2a2220"),
            text_color=("#6d4c41", "#bcaaa4"),
            hover_color=("#d7ccc8", "#3e322f"),
            command=self._open_log,
        ).pack(side="left", padx=(0, 6))

        self.pause_btn = ctk.CTkButton(
            hdr_btns,
            text="⏸ Pauza",
            width=96,
            height=28,
            fg_color=("#fff3e0", "#3e2f1a"),
            text_color=("#ef6c00", "#ffb74d"),
            hover_color=("#ffe0b2", "#5a4324"),
            command=self._toggle_pause,
        )
        self.pause_btn.pack(side="left")

        card = ctk.CTkFrame(self, fg_color=("#ffffff", "#1a1f2b"), corner_radius=12)
        card.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 4))
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=0)

        url_row = ctk.CTkFrame(card, fg_color="transparent")
        url_row.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=(8, 4))
        url_row.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            url_row,
            placeholder_text="Vlož odkaz na video, playlist nebo kanál (@…)",
            height=34,
            font=ctk.CTkFont(size=13),
            border_width=0,
            fg_color=("#f0f4f8", "#12151c"),
        )
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._add_download())

        ctk.CTkButton(
            url_row,
            text="Přidat",
            width=100,
            height=34,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("#1565c0", "#1e88e5"),
            hover_color=("#0d47a1", "#42a5f5"),
            command=self._add_download,
        ).grid(row=0, column=1, padx=(8, 0))

        controls = ctk.CTkFrame(card, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=(12, 8), pady=(0, 4))

        ctk.CTkLabel(
            controls,
            text="Formát",
            text_color=("#78909c", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 6))
        self.mode_seg = ctk.CTkSegmentedButton(
            controls,
            values=["Video", "MP3"],
            command=self._on_mode_change,
            height=28,
            font=ctk.CTkFont(size=12),
        )
        self.mode_seg.set(
            "Video"
            if self.config_data.default_mode == MediaMode.VIDEO.value
            else "MP3"
        )
        self.mode_seg.pack(side="left")

        ctk.CTkLabel(
            controls,
            text="Kvalita",
            text_color=("#78909c", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(12, 6))
        self.quality_menu = ctk.CTkOptionMenu(
            controls,
            values=[l for l, _ in QUALITY_OPTIONS],
            height=28,
            width=150,
            font=ctk.CTkFont(size=12),
        )
        q_labels = {v: l for l, v in QUALITY_OPTIONS}
        self.quality_menu.set(
            q_labels.get(self.config_data.default_quality, "Nejlepší dostupné")
        )
        self.quality_menu.pack(side="left")

        ctk.CTkLabel(
            controls,
            text="Playlist",
            text_color=("#78909c", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(12, 6))
        self.playlist_mode_seg = ctk.CTkSegmentedButton(
            controls,
            values=[label for label, _mode in PLAYLIST_MODE_OPTIONS],
            command=self._on_playlist_mode,
            height=28,
            font=ctk.CTkFont(size=12),
        )
        pl_labels = {value: text for text, value in PLAYLIST_MODE_OPTIONS}
        self.playlist_mode_seg.set(
            pl_labels.get(self.config_data.playlist_mode, "Vždy se zeptat")
        )
        self.playlist_mode_seg.pack(side="left", padx=(0, 14))

        self.clip_switch = ctk.CTkSwitch(
            controls,
            text="Monitorovat schránku",
            command=self._toggle_clipboard,
            font=ctk.CTkFont(size=12),
        )
        self.clip_switch.pack(side="left", padx=(0, 10))

        self.auto_switch = ctk.CTkSwitch(
            controls,
            text="Automaticky stahovat po přidání",
            command=self._toggle_auto,
            font=ctk.CTkFont(size=12),
        )
        self.auto_switch.pack(side="left", padx=(0, 10))

        self.auto_remove_switch = ctk.CTkSwitch(
            controls,
            text="Smazat z fronty po stažení",
            command=self._toggle_auto_remove,
            font=ctk.CTkFont(size=12),
        )
        self.auto_remove_switch.pack(side="left")

        cookies_row = ctk.CTkFrame(card, fg_color="transparent")
        cookies_row.grid(row=2, column=0, sticky="ew", padx=(12, 8), pady=(0, 8))

        ctk.CTkLabel(
            cookies_row,
            text="Cookies",
            text_color=("#78909c", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 8))

        self.cookies_mode_menu = ctk.CTkOptionMenu(
            cookies_row,
            values=["Vypnuto", "Z prohlížeče", "Ze souboru"],
            width=130,
            height=28,
            command=self._on_cookies_mode,
        )
        mode_labels = {"off": "Vypnuto", "browser": "Z prohlížeče", "file": "Ze souboru"}
        self.cookies_mode_menu.set(
            mode_labels.get(self.config_data.cookies_mode, "Vypnuto")
        )
        self.cookies_mode_menu.pack(side="left", padx=(0, 8))

        self.cookies_browser_menu = ctk.CTkOptionMenu(
            cookies_row,
            values=[l for l, _ in BROWSER_OPTIONS],
            width=160,
            height=28,
            command=self._on_cookies_browser,
        )
        b_labels = {v: l for l, v in BROWSER_OPTIONS}
        self.cookies_browser_menu.set(
            b_labels.get(self.config_data.cookies_browser, "Firefox (doporučeno)")
        )
        self.cookies_browser_menu.pack(side="left", padx=(0, 8))

        self.cookies_file_lbl = ctk.CTkLabel(
            cookies_row,
            text=self._short_path(self.config_data.cookies_file or "cookies.txt", 28)
            if self.config_data.cookies_file
            else "žádný soubor",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("#78909c", "#90a4ae"),
        )
        self.cookies_file_lbl.pack(side="left", padx=(0, 6))

        self.cookies_file_btn = ctk.CTkButton(
            cookies_row,
            text="Vybrat…",
            width=80,
            height=28,
            command=self._pick_cookies_file,
        )
        self.cookies_file_btn.pack(side="left")

        self._sync_cookies_widgets()

        side = ctk.CTkFrame(card, fg_color="transparent")
        side.grid(row=0, column=1, rowspan=3, sticky="ne", padx=(0, 12), pady=(8, 8))

        import_btns = ctk.CTkFrame(side, fg_color="transparent")
        import_btns.pack(anchor="e")

        ctk.CTkLabel(
            import_btns,
            text="Vložit:",
            text_color=("#78909c", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            import_btns,
            text="Ze schránky",
            width=100,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=("#90a4ae", "#455a64"),
            text_color=("#455a64", "#cfd8dc"),
            hover_color=("#eceff1", "#263238"),
            command=self._import_clipboard,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            import_btns,
            text="Ze souboru",
            width=96,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=("#90a4ae", "#455a64"),
            text_color=("#455a64", "#cfd8dc"),
            hover_color=("#eceff1", "#263238"),
            command=self._import_file,
        ).pack(side="left")

        def folder_line(
            path: str,
            kind: str,
            open_text: str,
            colors: dict[str, tuple[str, str] | str],
        ) -> ctk.CTkLabel:
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(anchor="e", pady=(6, 0))
            lbl = ctk.CTkLabel(
                row,
                text=self._short_path(path),
                width=210,
                anchor="e",
                font=ctk.CTkFont(size=11),
            )
            lbl.pack(side="left")
            ctk.CTkButton(
                row,
                text="…",
                width=28,
                height=26,
                command=lambda k=kind: self._pick_folder(k),
            ).pack(side="left", padx=(4, 4))
            ctk.CTkButton(
                row,
                text=open_text,
                width=108,
                height=26,
                command=lambda k=kind: self._open_folder(k),
                **colors,
            ).pack(side="left")
            return lbl

        self.audio_folder_lbl = folder_line(
            self.config_data.audio_dir,
            "audio",
            "Složka MP3",
            {
                "fg_color": ("#e8f5e9", "#1b3d2f"),
                "text_color": ("#2e7d32", "#a5d6a7"),
                "hover_color": ("#c8e6c9", "#244a38"),
            },
        )
        self.video_folder_lbl = folder_line(
            self.config_data.video_dir,
            "video",
            "Složka videa",
            {
                "fg_color": ("#e3f2fd", "#1e3a5f"),
                "text_color": ("#1565c0", "#bbdefb"),
                "hover_color": ("#bbdefb", "#274a73"),
            },
        )

        ctk.CTkButton(
            side,
            text="Vyčistit hotové",
            width=120,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color=("#90a4ae", "#455a64"),
            text_color=("#455a64", "#cfd8dc"),
            hover_color=("#eceff1", "#263238"),
            command=self._clear_finished,
        ).pack(anchor="e", pady=(6, 0))

        queue_wrap = ctk.CTkFrame(self, fg_color="transparent")
        queue_wrap.grid(row=2, column=0, sticky="nsew", padx=16, pady=(2, 4))
        queue_wrap.grid_columnconfigure(0, weight=1)
        queue_wrap.grid_rowconfigure(1, weight=1)

        qhead = ctk.CTkFrame(queue_wrap, fg_color="transparent")
        qhead.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        qhead.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            qhead,
            text="Fronta stahování",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.queue_scroll = ctk.CTkScrollableFrame(
            queue_wrap, fg_color="transparent", corner_radius=0
        )
        self.queue_scroll.grid(row=1, column=0, sticky="nsew")
        self.queue_scroll.grid_columnconfigure(0, weight=1)

        self.empty_lbl = ctk.CTkLabel(
            self.queue_scroll,
            text="Zatím nic ve frontě.\nVlož odkaz, importuj soubor nebo schránku.",
            text_color=("#90a4ae", "#607d8b"),
            font=ctk.CTkFont(size=14),
            justify="center",
        )
        self.empty_lbl.grid(row=0, column=0, pady=40)

        status_row = ctk.CTkFrame(self, fg_color="transparent")
        status_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
        status_row.grid_columnconfigure(0, weight=1)
        bar_font = ctk.CTkFont(size=12)
        bar_color = ("#546e7a", "#b0bec5")
        self.totals_bar = ctk.CTkLabel(
            status_row,
            text=download_totals_line(0, 0, 0),
            font=bar_font,
            anchor="w",
            text_color=bar_color,
        )
        self.totals_bar.grid(row=0, column=0, sticky="w")
        self.status_bar = ctk.CTkLabel(
            status_row,
            text="Status: nic ve frontě",
            font=bar_font,
            anchor="e",
            text_color=bar_color,
        )
        self.status_bar.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self._refresh_status_bar()

        self._on_mode_change(self.mode_seg.get())

    def _apply_theme_buttons(self) -> None:
        current = self.config_data.appearance
        for mode, btn in self.theme_buttons.items():
            if mode == current:
                btn.configure(
                    fg_color=("#1565c0", "#1e88e5"),
                    hover_color=("#0d47a1", "#42a5f5"),
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    hover_color=("#cfd8dc", "#3a4458"),
                )

    def _set_appearance(self, mode: str) -> None:
        if mode not in ("dark", "light", "system"):
            mode = "dark"
        self.config_data.appearance = mode
        self.config_data.save()
        ctk.set_appearance_mode(mode)
        self._apply_theme_buttons()

    def _short_path(self, path: str, max_len: int = 32) -> str:
        """Jednotka, tři tečky a konec cesty: C:\\...\\dokumenty\\hudba."""
        text = (path or "").strip().replace("/", "\\")
        if len(text) <= max_len:
            return text
        drive, rest = os.path.splitdrive(text)
        parts = [part for part in rest.split("\\") if part]
        prefix = f"{drive}\\..." if drive else "..."
        if not parts:
            return text[:max_len]
        tail = parts[-1]
        for part in reversed(parts[:-1]):
            candidate = f"{part}\\{tail}"
            if len(f"{prefix}\\{candidate}") > max_len:
                break
            tail = candidate
        shown = f"{prefix}\\{tail}"
        if len(shown) <= max_len:
            return shown
        room = max_len - len(prefix) - 1
        if room <= 0:
            return prefix[:max_len]
        return f"{prefix}\\{tail[-room:]}"

    def _current_mode(self) -> MediaMode:
        return MediaMode.AUDIO if self.mode_seg.get() == "MP3" else MediaMode.VIDEO

    def _current_quality(self) -> str:
        label = self.quality_menu.get()
        for l, v in QUALITY_OPTIONS:
            if l == label:
                return v
        return "best"

    def _on_mode_change(self, value: str) -> None:
        self.quality_menu.configure(state="normal" if value == "Video" else "disabled")

    def _classify_url(self, url: str) -> tuple[str, str]:
        url = decode_youtube_url(url)
        if is_channel_url(url):
            url = normalize_channel_url(url)
        return playlist_action(url, self.config_data.playlist_mode), url

    def _add_urls(self, urls: list[str]) -> None:
        if not urls:
            messagebox.showwarning(APP_NAME, "Nenalezen žádný YouTube odkaz.")
            return
        normal: list[str] = []
        ask: list[str] = []
        for raw in urls:
            action, url = self._classify_url(raw)
            if action == "ask":
                ask.append(url)
            else:
                normal.append(url)
        added = skipped = 0
        if normal:
            added, skipped = self.engine.enqueue_many(
                normal, self._current_mode(), self._current_quality()
            )
        self._persist_prefs()
        if normal and added == 0 and skipped and not ask:
            messagebox.showinfo(APP_NAME, "Všechny odkazy už jsou ve frontě.")
        elif skipped:
            messagebox.showinfo(
                APP_NAME, f"Přidáno {added}, přeskočeno duplicit {skipped}."
            )
        if ask:
            self._ask_queue.extend(ask)
            self._pump_ask_queue()

    def _add_download(self) -> None:
        text = self.url_entry.get().strip()
        urls = extract_youtube_urls(text) if text else []
        if not urls and text and is_youtube_url(text):
            urls = [text]
        if not urls:
            messagebox.showwarning(APP_NAME, "Vlož platný YouTube odkaz.")
            return
        self._add_urls(urls)
        self.url_entry.delete(0, "end")

    def _import_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        urls = extract_youtube_urls(text or "")
        self._add_urls(urls)

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Import odkazů",
            filetypes=[
                ("Text / seznamy", "*.txt *.csv *.m3u *.md"),
                ("Všechny soubory", "*.*"),
            ],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Soubor nejde přečíst:\n{exc}")
            log_exception(f"Import souboru {path}", exc)
            return
        self._add_urls(extract_youtube_urls(text))

    def _on_clipboard_url(self, url: str) -> None:
        self._add_urls([url])

    def _copy_url(self, url: str) -> bool:
        url = (url or "").strip()
        if not url:
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.clipboard.note(url)
        except Exception as exc:
            log.warning("Kopírování odkazu selhalo: %s", exc)
            return False
        return True

    def _toggle_clipboard(self) -> None:
        enabled = bool(self.clip_switch.get())
        self.clipboard.set_enabled(enabled)
        self.config_data.clipboard_monitor = enabled
        self.config_data.save()

    def _on_playlist_mode(self, label: str) -> None:
        mode = next(
            (value for text, value in PLAYLIST_MODE_OPTIONS if text == label),
            "ask",
        )
        self.config_data.playlist_mode = mode
        self.config_data.download_playlists = mode == "all"
        self.config_data.save()

    def _pump_ask_queue(self) -> None:
        if self._ask_busy:
            return
        skipped = 0
        while self._ask_queue:
            url = self._ask_queue.pop(0)
            if any(
                j.url == url and j.status in ACTIVE_STATUSES for j in self.engine.jobs()
            ):
                skipped += 1
                continue
            self._ask_busy = True
            self._begin_playlist_preview(url)
            return
        if skipped:
            messagebox.showinfo(APP_NAME, "Playlist už je ve frontě.")

    def _finish_ask(self) -> None:
        self._ask_busy = False
        self._pump_ask_queue()

    def _place_window(self, win: ctk.CTkToplevel, width: int, height: int) -> None:
        self.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _raise_window(self, win: ctk.CTkToplevel) -> None:
        win.transient(self)
        win.lift()
        win.focus_force()
        try:
            win.attributes("-topmost", True)
            win.after(250, lambda: win.attributes("-topmost", False))
        except Exception:
            pass
        try:
            win.grab_set()
        except Exception:
            pass

    def _begin_playlist_preview(self, url: str) -> None:
        self._preview_gen += 1
        gen = self._preview_gen
        loading = ctk.CTkToplevel(self)
        loading.title(APP_NAME)
        loading.resizable(False, False)
        loading.protocol(
            "WM_DELETE_WINDOW", lambda: self._cancel_preview(gen, loading)
        )
        ctk.CTkLabel(
            loading,
            text="Načítám seznam videí…",
            justify="center",
            font=ctk.CTkFont(size=14),
        ).pack(padx=28, pady=(24, 12))
        ctk.CTkButton(
            loading,
            text="Zrušit",
            width=110,
            command=lambda: self._cancel_preview(gen, loading),
        ).pack(pady=(0, 20))
        self._place_window(loading, 360, 148)
        self._raise_window(loading)

        def done(
            title: str, entries: list[PlaylistEntry], err: Optional[str]
        ) -> None:
            with self._preview_lock:
                self._preview_results.append(
                    (gen, loading, url, title, list(entries), err)
                )

        self.engine.request_preview(url, done)

    def _cancel_preview(self, gen: int, loading: ctk.CTkToplevel) -> None:
        if gen != self._preview_gen:
            return
        self._preview_gen += 1
        try:
            loading.destroy()
        except Exception:
            pass
        self._finish_ask()

    def _on_preview_ready(
        self,
        gen: int,
        loading: ctk.CTkToplevel,
        url: str,
        title: str,
        entries: list[PlaylistEntry],
        err: Optional[str],
    ) -> None:
        if gen != self._preview_gen:
            return
        try:
            loading.destroy()
        except Exception:
            pass
        if err or not entries:
            messagebox.showerror(
                APP_NAME,
                err or "Playlist je prázdný nebo nejde načíst.",
                parent=self,
            )
            self._finish_ask()
            return
        self._open_playlist_picker(url, title, entries)

    def _open_playlist_picker(
        self, url: str, title: str, entries: list[PlaylistEntry]
    ) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Vyber videa z playlistu")
        win.minsize(760, 480)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)
        page = {"n": 0}

        head = ctk.CTkLabel(
            win,
            text=title,
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))
        ctk.CTkLabel(
            win,
            text="Zaškrtnutá videa se po potvrzení přidají do fronty. Výchozí je vše.",
            anchor="w",
            text_color=("#607d8b", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).grid(row=1, column=0, sticky="ew", padx=16)

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(body, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.grid_columnconfigure(3, weight=1)

        count_lbl = ctk.CTkLabel(toolbar, text="", font=ctk.CTkFont(size=12, weight="bold"))
        count_lbl.grid(row=0, column=3, sticky="e", padx=8)

        nav = ctk.CTkFrame(toolbar, fg_color="transparent")
        nav.grid(row=0, column=4, sticky="e")
        page_lbl = ctk.CTkLabel(nav, text="", font=ctk.CTkFont(size=12))
        page_lbl.pack(side="left", padx=(0, 6))

        scroll = ctk.CTkScrollableFrame(body, fg_color=("#e8eef3", "#151a24"))
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        foot.grid_columnconfigure(0, weight=1)

        ok_btn = ctk.CTkButton(foot, text="Přidat do fronty", width=160, height=36)

        def selected_count() -> int:
            return sum(1 for entry in entries if entry.selected)

        def sync_count() -> None:
            n = selected_count()
            count_lbl.configure(text=f"Vybráno {n} z {len(entries)}")
            ok_btn.configure(
                text=f"Přidat do fronty ({n})",
                state="normal" if n else "disabled",
            )

        def render_page() -> None:
            total = len(entries)
            pages = max(1, (total + ENTRIES_PAGE_SIZE - 1) // ENTRIES_PAGE_SIZE)
            page["n"] = max(0, min(pages - 1, page["n"]))
            start = page["n"] * ENTRIES_PAGE_SIZE
            end = min(start + ENTRIES_PAGE_SIZE, total)
            page_lbl.configure(text=f"{start + 1}–{end} z {total}")
            prev_btn.configure(state="normal" if page["n"] > 0 else "disabled")
            next_btn.configure(state="normal" if page["n"] < pages - 1 else "disabled")
            for child in scroll.winfo_children():
                child.destroy()
            for i, entry in enumerate(entries[start:end]):
                var = ctk.BooleanVar(value=entry.selected)

                def toggle(v=var, item=entry) -> None:
                    item.selected = bool(v.get())
                    sync_count()

                row = ctk.CTkFrame(scroll, fg_color="transparent")
                row.grid(row=i, column=0, sticky="ew", padx=4, pady=1)
                row.grid_columnconfigure(1, weight=1)
                ctk.CTkCheckBox(
                    row, text="", width=24, variable=var, command=toggle
                ).grid(row=0, column=0)
                ctk.CTkLabel(
                    row,
                    text=f"{entry.index:03d}. {entry.title[:90]}",
                    anchor="w",
                    font=ctk.CTkFont(size=13),
                ).grid(row=0, column=1, sticky="ew", padx=6)
            sync_count()

        def set_all(selected: bool) -> None:
            for entry in entries:
                entry.selected = selected
            render_page()

        def shift(delta: int) -> None:
            page["n"] += delta
            render_page()

        ctk.CTkButton(
            toolbar,
            text="Vybrat vše",
            width=100,
            height=28,
            command=lambda: set_all(True),
        ).grid(row=0, column=0, padx=(0, 6))
        ctk.CTkButton(
            toolbar,
            text="Zrušit výběr",
            width=100,
            height=28,
            fg_color="transparent",
            border_width=1,
            command=lambda: set_all(False),
        ).grid(row=0, column=1)

        fmt_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        fmt_box.grid(row=0, column=2, sticky="w", padx=(12, 0))
        ctk.CTkLabel(
            fmt_box,
            text="Formát",
            text_color=("#607d8b", "#90a4ae"),
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 6))
        mode_pick = ctk.CTkSegmentedButton(
            fmt_box,
            values=["Video", "MP3"],
            height=28,
            font=ctk.CTkFont(size=12),
        )
        mode_pick.set("MP3" if self._current_mode() == MediaMode.AUDIO else "Video")
        mode_pick.pack(side="left")

        prev_btn = ctk.CTkButton(
            nav, text="◀", width=36, height=28, command=lambda: shift(-1)
        )
        prev_btn.pack(side="left", padx=(0, 4))
        next_btn = ctk.CTkButton(
            nav, text="▶", width=36, height=28, command=lambda: shift(1)
        )
        next_btn.pack(side="left")

        def close() -> None:
            try:
                win.destroy()
            except Exception:
                pass
            self._finish_ask()

        def confirm() -> None:
            if not selected_count():
                return
            picked = (
                MediaMode.AUDIO if mode_pick.get() == "MP3" else MediaMode.VIDEO
            )
            job = self.engine.enqueue_prepared(
                url,
                picked,
                self._current_quality(),
                title,
                entries,
            )
            try:
                win.destroy()
            except Exception:
                pass
            if job is None:
                messagebox.showinfo(
                    APP_NAME,
                    "Playlist už je ve frontě, nebo není nic vybrané.",
                    parent=self,
                )
            self._finish_ask()

        ctk.CTkButton(
            foot,
            text="Zrušit",
            width=100,
            height=36,
            fg_color="transparent",
            border_width=1,
            command=close,
        ).grid(row=0, column=0, sticky="w")
        ok_btn.configure(command=confirm)
        ok_btn.grid(row=0, column=1, sticky="e")

        win.protocol("WM_DELETE_WINDOW", close)
        self._place_window(win, 740, 640)
        self._raise_window(win)
        render_page()

    def _toggle_auto(self) -> None:
        self.config_data.auto_download = bool(self.auto_switch.get())
        self.config_data.save()
        if self.config_data.auto_download:
            for job in self.engine.jobs():
                if job.status == JobStatus.READY:
                    self.engine.start_job(job.id)

    def _toggle_auto_remove(self) -> None:
        self.config_data.auto_remove_finished = bool(self.auto_remove_switch.get())
        self.config_data.save()
        if self.config_data.auto_remove_finished:
            for job in list(self.engine.jobs()):
                if job.status == JobStatus.DONE:
                    self.engine.remove_job(job.id)

    def _sync_cookies_widgets(self) -> None:
        mode = self.config_data.cookies_mode
        if mode == "browser":
            self.cookies_browser_menu.configure(state="normal")
            self.cookies_file_btn.configure(state="disabled")
            self.cookies_file_lbl.configure(text_color=("#b0bec5", "#546e7a"))
        elif mode == "file":
            self.cookies_browser_menu.configure(state="disabled")
            self.cookies_file_btn.configure(state="normal")
            self.cookies_file_lbl.configure(text_color=("#455a64", "#cfd8dc"))
        else:
            self.cookies_browser_menu.configure(state="disabled")
            self.cookies_file_btn.configure(state="disabled")
            self.cookies_file_lbl.configure(text_color=("#b0bec5", "#546e7a"))

    def _on_cookies_mode(self, label: str) -> None:
        mapping = {
            "Vypnuto": "off",
            "Z prohlížeče": "browser",
            "Ze souboru": "file",
        }
        self.config_data.cookies_mode = mapping.get(label, "off")
        self.config_data.save()
        self._sync_cookies_widgets()
        log.info("Cookies režim: %s", self.config_data.cookies_mode)
        if self.config_data.cookies_mode == "browser":
            self._refresh_browser_cookies(ask_quit=True)

    def _on_cookies_browser(self, label: str) -> None:
        for l, v in BROWSER_OPTIONS:
            if l == label:
                self.config_data.cookies_browser = v
                self.config_data.save()
                log.info("Cookies prohlížeč: %s", v)
                if self.config_data.cookies_mode == "browser":
                    self._refresh_browser_cookies(ask_quit=True)
                return

    def _refresh_browser_cookies(self, ask_quit: bool = True) -> None:
        """Export cookies now; for Brave/Chrome ask to kill background processes."""
        browser = (self.config_data.cookies_browser or "firefox").lower()
        if browser not in CHROMIUM_BROWSERS:
            return

        def work(force: bool) -> None:
            try:
                path = export_browser_cookies(browser, force_quit=force)
                self.after(
                    0,
                    lambda p=path, b=browser: messagebox.showinfo(
                        APP_NAME,
                        f"Cookies z {b} načteny.\n{p.name}",
                        parent=self,
                    ),
                )
            except Exception as exc:
                msg = friendly_error(exc)
                pids = list_browser_pids(browser)
                if pids and not force:
                    self.after(
                        0,
                        lambda b=browser, n=len(pids), m=msg: self._ask_quit_and_retry_cookies(
                            b, n, m
                        ),
                    )
                else:
                    tip = (
                        "\n\nTip: použij Firefox, nebo v Brave rozšíření "
                        "„Get cookies.txt LOCALLY“ a režim Cookies → Ze souboru."
                    )
                    self.after(
                        0,
                        lambda m=msg + tip, b=browser: messagebox.showerror(
                            APP_NAME,
                            f"Cookies z {b} nejde načíst:\n{m}",
                            parent=self,
                        ),
                    )

        pids = list_browser_pids(browser)
        if pids and ask_quit:
            if not messagebox.askyesno(
                APP_NAME,
                f"{browser.capitalize()} běží ({len(pids)} procesů) a má zamčené cookies.\n\n"
                f"Ukončit {browser.capitalize()} a načíst cookies?\n"
                f"Prohlížeč se zavře až po kliknutí na Ano. Otevřené karty se zavřou.",
                parent=self,
            ):
                messagebox.showinfo(
                    APP_NAME,
                    f"{browser.capitalize()} zůstává spuštěný, cookies se nenačetly.",
                    parent=self,
                )
                return
            threading.Thread(target=work, args=(True,), daemon=True).start()
            return
        threading.Thread(target=work, args=(False,), daemon=True).start()

    def _ask_quit_and_retry_cookies(self, browser: str, n: int, err: str) -> None:
        if messagebox.askyesno(
            APP_NAME,
            f"{err}\n\n{browser.capitalize()} pořád běží ({n} procesů).\n"
            f"Ukončit ho až teď a zkusit cookies znovu?",
            parent=self,
        ):
            def retry() -> None:
                try:
                    path = export_browser_cookies(browser, force_quit=True)
                    self.after(
                        0,
                        lambda p=path, b=browser: messagebox.showinfo(
                            APP_NAME,
                            f"Cookies z {b} načteny.\n{p.name}",
                            parent=self,
                        ),
                    )
                except Exception as exc:
                    tip = (
                        "\n\nTip: Firefox, nebo Brave → Get cookies.txt LOCALLY → Ze souboru."
                    )
                    self.after(
                        0,
                        lambda e=friendly_error(exc) + tip: messagebox.showerror(
                            APP_NAME,
                            f"Stále nejde:\n{e}",
                            parent=self,
                        ),
                    )

            threading.Thread(target=retry, daemon=True).start()

    def _pick_cookies_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Vyber cookies.txt (Netscape formát)",
            filetypes=[
                ("Cookies", "*.txt"),
                ("Všechny soubory", "*.*"),
            ],
        )
        if path:
            self.config_data.cookies_file = path
            self.config_data.cookies_mode = "file"
            self.cookies_mode_menu.set("Ze souboru")
            self.cookies_file_lbl.configure(text=self._short_path(path, 36))
            self.config_data.save()
            self._sync_cookies_widgets()
            log.info("Cookies soubor: %s", path)

    def _toggle_pause(self) -> None:
        if self.engine.is_paused():
            self.engine.set_paused(False)
            self.pause_btn.configure(text="⏸ Pauza")
        else:
            self.engine.set_paused(True)
            self.pause_btn.configure(text="▶ Pokračovat")
        self._refresh_status_bar()
        for job in self.engine.jobs():
            row = self.rows.get(job.id)
            if row:
                row.refresh(job)

    def _pick_folder(self, kind: str = "video") -> None:
        initial = (
            self.config_data.audio_dir
            if kind == "audio"
            else self.config_data.video_dir
        )
        title = (
            "Vyber složku pro MP3"
            if kind == "audio"
            else "Vyber složku pro videa"
        )
        path = filedialog.askdirectory(initialdir=initial, title=title)
        if path:
            if kind == "audio":
                self.config_data.audio_dir = path
                self.audio_folder_lbl.configure(text=self._short_path(path))
            else:
                self.config_data.video_dir = path
                self.video_folder_lbl.configure(text=self._short_path(path))
            self.config_data.save()
            self._maybe_warn_disk_space()

    def _open_folder(self, kind: str = "video") -> None:
        path = Path(
            self.config_data.audio_dir
            if kind == "audio"
            else self.config_data.video_dir
        )
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def _open_log(self) -> None:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if not LOG_FILE.exists():
                LOG_FILE.write_text("", encoding="utf-8")
            os.startfile(LOG_FILE)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Log nejde otevřít:\n{exc}")
            log_exception("Otevření logu", exc)

    def _clear_finished(self) -> None:
        self.engine.clear_finished()
        self._rebuild_all_rows()

    def _remove_job(self, job_id: str) -> None:
        self.engine.remove_job(job_id)

    def _start_job(self, job_id: str) -> None:
        if self.engine.is_paused():
            self.engine.set_paused(False)
            self.pause_btn.configure(text="⏸ Pauza")
        self.engine.start_job(job_id)

    def _toggle_job_pause(self, job_id: str) -> None:
        if self.engine.is_paused():
            return
        self.engine.toggle_job_pause(job_id)

    def _retry_entry(self, job_id: str, video_id: str) -> None:
        if self.engine.is_paused():
            self.engine.set_paused(False)
            self.pause_btn.configure(text="⏸ Pauza")
        self.engine.retry_entry(job_id, video_id)

    def _toggle_expand(self, job_id: str) -> None:
        job = self.engine.get_job(job_id)
        if not job:
            return
        self.engine.set_expanded(job_id, not job.expanded)
        if job_id in self.rows:
            self.rows[job_id].refresh(job)

    def _entry_toggle(self, job_id: str, video_id: str, selected: bool) -> None:
        self.engine.set_entry_selected(job_id, video_id, selected)

    def _select_all(self, job_id: str, selected: bool) -> None:
        self.engine.set_all_entries_selected(job_id, selected)

    def _on_job_update(self, job: DownloadJob) -> None:
        self._ui_pending[job.id] = job

    def _on_queue_structure_change(self) -> None:
        self._rebuild_queue = True

    def _flush_ui(self) -> None:
        with self._preview_lock:
            ready = list(self._preview_results)
            self._preview_results.clear()
        for item in ready:
            self._on_preview_ready(*item)
        if self._rebuild_queue:
            self._rebuild_queue = False
            self._rebuild_all_rows()
        if self._ui_pending:
            pending = dict(self._ui_pending)
            self._ui_pending.clear()
            for job in pending.values():
                self._render_job(job)
        self._refresh_status_bar()
        now = time.monotonic()
        if (
            not self._disk_warned
            and now - self._disk_check_at >= DISK_CHECK_INTERVAL
        ):
            self._disk_check_at = now
            self._maybe_warn_disk_space()
        self.after(100, self._flush_ui)

    def _maybe_warn_disk_space(self) -> None:
        """Jedno varování za běh programu. Po restartu se může ukázat znovu."""
        if self._disk_warned:
            return
        text = disk_warning_text(
            [
                ("složka videa", self.config_data.video_dir),
                ("složka MP3", self.config_data.audio_dir),
            ]
        )
        if not text:
            return
        self._disk_warned = True
        log.warning("Málo místa na disku pro stahování")
        messagebox.showwarning(APP_NAME, text, parent=self)

    def _rebuild_all_rows(self) -> None:
        for row in self.rows.values():
            row.destroy()
        self.rows.clear()
        jobs = self.engine.jobs()
        if not jobs:
            self.empty_lbl.grid()
            return
        self.empty_lbl.grid_remove()
        for job in jobs:
            self._render_job(job)

    def _render_job(self, job: DownloadJob) -> None:
        alive_ids = {j.id for j in self.engine.jobs()}
        if job.id not in alive_ids:
            if job.id in self.rows:
                self.rows[job.id].destroy()
                del self.rows[job.id]
            if not self.rows:
                self.empty_lbl.grid()
            return
        self.empty_lbl.grid_remove()
        if job.id in self.rows:
            self.rows[job.id].refresh(job)
        else:
            row = JobRow(
                self.queue_scroll,
                job,
                self._remove_job,
                self._start_job,
                self._toggle_expand,
                self._entry_toggle,
                self._select_all,
                self._toggle_job_pause,
                self._retry_entry,
                self.engine.is_job_paused,
                self.engine.is_paused,
                self._copy_url,
            )
            row.grid(sticky="ew", pady=5)
            self.rows[job.id] = row
        # remove rows no longer in queue
        for jid in list(self.rows.keys()):
            if jid not in alive_ids:
                self.rows[jid].destroy()
                del self.rows[jid]
        if not self.rows:
            self.empty_lbl.grid()

    def _refresh_status_bar(self) -> None:
        cfg = self.config_data
        self.totals_bar.configure(
            text=download_totals_line(
                cfg.stats_videos, cfg.stats_mp3, cfg.stats_bytes
            )
        )
        self.status_bar.configure(
            text=queue_status_line(self.engine.jobs(), self.engine.is_paused())
        )

    def _persist_prefs(self) -> None:
        self.config_data.default_mode = self._current_mode().value
        self.config_data.default_quality = self._current_quality()
        self.config_data.save()

    def _on_close(self) -> None:
        self._persist_prefs()
        self.clipboard.set_enabled(False)
        try:
            self.engine.set_paused(True)
            self.engine.save_queue_now()
            log.info("%s ukončen", APP_NAME)
        except Exception as exc:
            log_exception("Ukončení aplikace", exc)
        self.destroy()


def _append_launch_log(text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with LAUNCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    except Exception:
        pass


def main() -> None:
    if sys.version_info < (3, 10):
        msg = (
            f"Potřebuješ Python 3.10+ (máš {sys.version.split()[0]}).\n"
            "Spusť start.bat, nebo: winget install Python.Python.3.12"
        )
        _append_launch_log(msg)
        raise SystemExit(msg)

    setup_logging()
    _append_launch_log(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} | start | Python {sys.version.split()[0]}"
    )
    try:
        app = App()
        _append_launch_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | UI ready")
        app.mainloop()
        _append_launch_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | exit ok")
    except Exception as exc:
        log_exception("Fatální chyba při běhu UI", exc)
        _append_launch_log(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | FATAL\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        try:
            messagebox.showerror(
                APP_NAME,
                f"Aplikace spadla při startu.\n\n{exc}\n\n"
                f"Log: {LOG_FILE}\nSpouštění: {LAUNCH_LOG}",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
