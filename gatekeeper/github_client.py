"""Thin async GitHub REST client.

Replay mode: comments copied from another repo by scripts/replay.py carry
a hidden marker `<!-- replay author=<login> at=<iso> -->`. We read the
original author and time from it, so the rules see the real requester.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from .rules import Comment, Issue, PullRequest

log = logging.getLogger("github")
API = "https://api.github.com"
_MARKER = re.compile(r"<!--\s*replay author=(\S+) at=(\S+)\s*-->")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def replay_identity(body: str, author: str, created: str) -> tuple[str, datetime, str]:
    m = _MARKER.search(body or "")
    if not m:
        return author, _ts(created), body or ""
    return m.group(1), _ts(m.group(2)), _MARKER.sub("", body).strip()


class GitHub:
    def __init__(self, token: str, repo: str, *, dry_run: bool = False):
        self.repo = repo
        self.dry_run = dry_run
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(base_url=API, headers=headers, timeout=30)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        url: str | None = path
        params = {"per_page": 100, **(params or {})}
        while url:
            r = await self.http.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None  # the next link already carries them
        return out

    # ---- reads
    async def list_issues(self, label: str) -> list[Issue]:
        items = await self._get_all(f"/repos/{self.repo}/issues", {"state": "open", "labels": label})
        return [
            Issue(
                number=i["number"],
                title=i["title"],
                assignees=tuple(a["login"] for a in i.get("assignees") or []),
                labels=tuple(l["name"] for l in i.get("labels") or []),
                url=i["html_url"],
            )
            for i in items
            if "pull_request" not in i
        ]

    async def list_comments(self, number: int) -> list[Comment]:
        out = []
        for c in await self._get_all(f"/repos/{self.repo}/issues/{number}/comments"):
            author, at, body = replay_identity(c["body"], c["user"]["login"], c["created_at"])
            out.append(Comment(c["id"], author, body, at))
        return out

    async def list_open_prs(self) -> list[PullRequest]:
        out = []
        for p in await self._get_all(f"/repos/{self.repo}/pulls", {"state": "open"}):
            author, _, body = replay_identity(p.get("body") or "", p["user"]["login"], p["created_at"])
            out.append(PullRequest(p["number"], p["title"], author, body, p["html_url"]))
        return out

    async def count_open_prs(self, login: str, repo: str) -> int:
        q = f"repo:{repo} is:pr is:open author:{login}"
        r = await self.http.get("/search/issues", params={"q": q, "per_page": 1})
        if r.status_code != 200:
            log.warning("PR count for %s failed (%s); assuming 0", login, r.status_code)
            return 0
        return int(r.json().get("total_count", 0))

    # ---- writes (all skipped in dry-run)
    async def _write(self, method: str, path: str, json: dict) -> None:
        if self.dry_run:
            log.info("[dry-run] %s %s %s", method, path, json)
            return
        r = await self.http.request(method, path, json=json)
        if r.status_code >= 300:
            log.error("GitHub %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        r.raise_for_status()

    async def comment(self, number: int, body: str) -> None:
        await self._write("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    async def assign(self, number: int, login: str) -> None:
        await self._write("POST", f"/repos/{self.repo}/issues/{number}/assignees", {"assignees": [login]})

    async def add_label(self, number: int, label: str) -> None:
        await self._write("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": [label]})
