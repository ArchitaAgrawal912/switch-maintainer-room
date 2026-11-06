"""The contribution rules, as pure functions.

Nothing in this module talks to GitHub or Switch. Every decision comes back
with a plain-English reason so a maintainer can see *why* before approving.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ---------------------------------------------------------------- data shapes


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    url: str = ""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    author: str
    body: str
    url: str = ""


@dataclass
class Candidate:
    login: str
    asked_at: datetime
    open_prs: int
    has_plan: bool
    eligible: bool
    reason: str


@dataclass
class Proposal:
    issue: Issue
    winner: str | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def others(self) -> list[Candidate]:
        return [c for c in self.candidates if c.login != self.winner]


@dataclass
class PrCheck:
    ok: bool
    issue_number: int | None
    reason: str


@dataclass
class Assignment:
    issue_number: int
    login: str
    assigned_at: datetime
    last_activity: datetime


# ---------------------------------------------------------- comment parsing

_REQUEST = re.compile(
    r"\b("
    r"assign (?:this|it)?\s*(?:to )?me"
    r"|assign me"
    r"|can i (?:work|take|pick|get|have)"
    r"|could i (?:work|take|pick|get|have)"
    r"|may i (?:work|take|pick)"
    r"|i(?:'d| would) (?:like|love) to (?:work|take|pick|contribute|try)"
    r"|i want to (?:work|take|pick)"
    r"|let me (?:work|take|try)"
    r"|i(?:'ll| will| can) (?:take|work on|pick up) (?:this|it)"
    r"|interested in (?:working|taking)"
    r"|pick this up"
    r")\b",
    re.IGNORECASE,
)

_PLAN_HINT = re.compile(
    r"(\bplan\b|\bapproach\b|\bsteps?\b|\bimplement|\bby (?:adding|changing|updating|using|writing)"
    r"|\bi(?:'ll| will| would| plan to| intend to)\b.{10,}"
    r"|^\s*(?:\d+[.)]|[-*])\s+\w)",
    re.IGNORECASE | re.MULTILINE,
)

_CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE)

PLAN_MIN_WORDS = 15


def is_assignment_request(body: str) -> bool:
    return bool(_REQUEST.search(body or ""))


def has_plan(body: str, min_words: int = PLAN_MIN_WORDS) -> bool:
    """A request 'has a plan' when it is long enough and says how."""
    text = body or ""
    return len(re.findall(r"\w+", text)) >= min_words and bool(_PLAN_HINT.search(text))


def first_requests(comments: list[Comment], ignore: set[str] = frozenset()) -> list[Comment]:
    """Each person's earliest assignment request, oldest first."""
    seen: dict[str, Comment] = {}
    for c in sorted(comments, key=lambda c: c.created_at):
        login = c.author.lower()
        if login in ignore or login.endswith("[bot]"):
            continue
        if login not in seen and is_assignment_request(c.body):
            seen[login] = c
    return list(seen.values())


def best_plan(comments: list[Comment], login: str) -> bool:
    """Did this person describe a plan in any of their comments?"""
    return any(has_plan(c.body) for c in comments if c.author.lower() == login.lower())
