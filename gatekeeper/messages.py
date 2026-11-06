"""Parsing chat messages and writing the Gatekeeper's posts."""
from __future__ import annotations

import re

from .rules import Proposal, PullRequest, normalize_verdict

_MENTION = re.compile(r"@[\w.\-]+")
_COMMAND = re.compile(r"^\s*(approve|reject)\b(?:\s+#?(\d+))?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_VERDICT = re.compile(r"VERDICT\s*:\s*(real|low[- _]?effort|broken)", re.IGNORECASE)
_PR_REF = re.compile(r"\bPR\s*#?(\d+)\b", re.IGNORECASE)


def strip_mentions(body: str) -> str:
    return _MENTION.sub("", body or "").strip()


def parse_command(body: str) -> tuple[str, int | None, str] | None:
    """'approve 3', '@gatekeeper reject #2 too vague' -> (action, id, note)."""
    m = _COMMAND.match(strip_mentions(body))
    if not m:
        return None
    action, pid, note = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    return action, int(pid) if pid else None, note


def parse_verdict(body: str) -> tuple[int, str] | None:
    """'VERDICT: low-effort, PR #12, only reformats' -> (12, 'low-effort')."""
    v = _VERDICT.search(body or "")
    pr = _PR_REF.search(body or "")
    if not v or not pr:
        return None
    verdict = normalize_verdict(v.group(1))
    return (int(pr.group(1)), verdict) if verdict else None


def who(login: str, replay: bool) -> str:
    """In replay mode never @mention: the logins are real people on GitHub."""
    return f"`{login}`" if replay else f"@{login}"


def proposal_text(pid: int, p: Proposal) -> str:
    lines = [f"**Proposal {pid}** · issue #{p.issue.number} *{p.issue.title}* · {len(p.candidates)} request(s)"]
    if p.winner:
        lines.append(f"Assign **{p.winner}**: {p.reason}.")
    else:
        lines.append(f"No assignment: {p.reason}.")
    for c in p.others:
        lines.append(f"- {c.login}: {c.reason}")
    if p.winner:
        lines.append(f"Reply `approve {pid}` or `reject {pid}`. Nothing is written to GitHub until you do.")
    return "\n".join(lines)


def winner_comment(p: Proposal, replay: bool) -> str:
    return (
        f"Assigned to {who(p.winner, replay)}. Reason: {p.reason}.\n\n"
        "Please open a pull request with `Closes #%d` in the description. "
        "If there is no activity for 48 hours the issue may be offered to the next person." % p.issue.number
    )


def others_comment(p: Proposal, replay: bool, free: list[int]) -> str:
    rows = "\n".join(f"- {who(c.login, replay)}: {c.reason}" for c in p.others)
    tail = ""
    if free:
        tail = "\n\nIssues that are still free: " + ", ".join(f"#{n}" for n in free[:5])
    return f"Thanks for offering. This issue went to {who(p.winner, replay)} ({p.reason}).\n{rows}{tail}"


def review_request(pr: PullRequest, reviewers: list[str], repo: str) -> str:
    mentions = " ".join(f"@{r}" for r in reviewers)
    return (
        f"{mentions} please review **PR #{pr.number}** *{pr.title}* by {pr.author}: {pr.url}\n"
        f"Check it out with `git fetch https://github.com/{repo} pull/{pr.number}/head:pr-{pr.number} && git checkout pr-{pr.number}`, "
        "run the tests, and read the diff.\n"
        f"End your reply with exactly one line: `VERDICT: real|low-effort|broken - PR #{pr.number} - <one-line reason>`"
    )


HELP = (
    "I'm the Gatekeeper. Maintainers: reply `approve <id>` or `reject <id> [reason]` to a proposal. "
    "Anyone: ask me `free issues` to see what is unassigned."
)
