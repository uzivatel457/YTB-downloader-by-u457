"""Kontrola a stažení aktualizací z GitHub Releases."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from changelog import GITHUB_REPO

_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_ZIP_NAME = re.compile(r"^YT-Downloader-.+\.zip$", re.IGNORECASE)
_VERSION_PARTS = re.compile(r"\d+")


@dataclass
class ReleaseInfo:
    version: str
    html_url: str
    zip_url: str
    zip_name: str
    zip_size: int


def normalize_version(tag: str) -> str:
    text = (tag or "").strip()
    if text.lower().startswith("v") and len(text) > 1 and text[1].isdigit():
        text = text[1:]
    return text


def version_tuple(version: str) -> tuple[int, ...]:
    parts = [int(p) for p in _VERSION_PARTS.findall(normalize_version(version))]
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    return version_tuple(remote) > version_tuple(local)


def fetch_latest_release() -> ReleaseInfo:
    data = _get_json(_API_LATEST)
    if not isinstance(data, dict):
        raise RuntimeError("GitHub releases")
    message = str(data.get("message") or "")
    if message:
        raise RuntimeError(message)

    version = normalize_version(str(data.get("tag_name") or ""))
    if not version:
        raise RuntimeError("missing tag")

    html_url = str(data.get("html_url") or "")
    zip_url = ""
    zip_name = ""
    zip_size = 0
    assets = data.get("assets") or []
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            if not _ZIP_NAME.match(name):
                continue
            zip_name = name
            zip_url = str(asset.get("browser_download_url") or "")
            try:
                zip_size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                zip_size = 0
            break

    return ReleaseInfo(
        version=version,
        html_url=html_url,
        zip_url=zip_url,
        zip_name=zip_name,
        zip_size=zip_size,
    )


def download_release_zip(
    url: str,
    dest_path: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "YT-Downloader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        done = 0
        with dest_path.open("wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)


def _get_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "YT-Downloader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))
