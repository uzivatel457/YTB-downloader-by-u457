"""Známé a opravené chyby podle issues na GitHubu.

Otevřené issues jsou známé chyby, uzavřené issues jsou opravené.
Oprava z verze 1.5.1 je v textu zvlášť — vznikla před issues.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

from i18n import t

GITHUB_REPO = "uzivatel457/YTB-downloader-by-u457"
GITHUB_ISSUES = f"https://github.com/{GITHUB_REPO}/issues"
_API = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class IssueNote:
    number: int
    title: str
    body: str
    url: str


def fetch_changelog() -> tuple[list[IssueNote], list[IssueNote]]:
    """Vrátí (otevřené, uzavřené) issues. Pull requesty vynechá."""
    return _fetch_state("open"), _fetch_state("closed")


def format_changelog(known: list[IssueNote], fixed: list[IssueNote]) -> str:
    """Text okna Změny v jazyce Windows."""
    lines = [
        t("changelog_known"),
        _format_issues(known) or t("changelog_none"),
        "",
        t("changelog_fixed"),
        t("changelog_fixed_151_title"),
        t("changelog_fixed_151_body"),
        "",
        t("changelog_fixed_github"),
        _format_issues(fixed) or t("changelog_none"),
    ]
    return "\n".join(lines).strip() + "\n"


def _fetch_state(state: str) -> list[IssueNote]:
    notes: list[IssueNote] = []
    for page in range(1, 6):
        url = f"{_API}?state={state}&per_page=100&page={page}"
        batch = _get_json(url)
        if not isinstance(batch, list):
            message = ""
            if isinstance(batch, dict):
                message = str(batch.get("message") or "")
            raise RuntimeError(message or "GitHub issues")
        for issue in batch:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            notes.append(
                IssueNote(
                    number=int(issue.get("number") or 0),
                    title=str(issue.get("title") or "").strip(),
                    body=_clean_body(str(issue.get("body") or "")),
                    url=str(issue.get("html_url") or ""),
                )
            )
        if len(batch) < 100:
            break
    notes.sort(key=lambda item: item.number, reverse=True)
    return notes


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


def _clean_body(body: str) -> str:
    text = _COMMENT.sub("", body).replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 1200:
        text = text[:1200].rstrip() + "…"
    return text


def _format_issues(items: list[IssueNote]) -> str:
    blocks: list[str] = []
    for item in items:
        block = f"#{item.number}  {item.title}".rstrip()
        if item.body:
            block += "\n" + item.body
        if item.url:
            block += "\n" + item.url
        blocks.append(block)
    return "\n\n".join(blocks)
