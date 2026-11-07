"""Parsing chat messages and writing the Gatekeeper's posts."""
from __future__ import annotations

import re

from .rules import Proposal, PullRequest, normalize_verdict

_MENTION = re.compile(r"@[\w.\-]+")
_COMMAND = re.compile(r"^\s*(approve|reject|undo)\b(?:\s+#?(\d+))?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
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
        f"Check it out with `git clone https://github.com/{repo} pr-{pr.number} && cd pr-{pr.number} && "
        f"git fetch origin pull/{pr.number}/head:pr-{pr.number} && git checkout pr-{pr.number}`, "
        "run the tests, and read the diff.\n"
        f"End your reply with exactly one line: `VERDICT: real|low-effort|broken - PR #{pr.number} - <one-line reason>`"
    )


_PICK = re.compile(r"PICK\s*:\s*#?(\d+)\s+@?`?([\w.\-]+)`?\s*[-–—:,]?\s*(.*)", re.IGNORECASE)


def parse_pick(body: str) -> tuple[int, str, str] | None:
    """'PICK: #11 alice - names the files and adds tests' -> (11, 'alice', 'names the files...')."""
    m = _PICK.search(body or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3).strip().rstrip("`").strip() or "best plan"


def judge_request(judge: str, number: int, title: str, candidates: list[dict]) -> str:
    rows = []
    for i, c in enumerate(candidates, 1):
        plan = " ".join(strip_mentions(c["plan"]).split())[:600]
        rows.append(f"{i}. `{c['login']}` (asked {c['asked_at'][:16].replace('T', ' ')}): {plan}")
    return (
        f"@{judge} issue #{number} *{title}* has {len(candidates)} eligible requests. "
        "Pick the plan most likely to become a good pull request: specific, correct, and tested. "
        "Ignore who asked first.\n" + "\n".join(rows) +
        f"\nReply with exactly one line: `PICK: #{number} <login> - <one-line reason>`"
    )


def is_clear_cut(p: Proposal) -> bool:
    """Exactly one eligible person asked: no judgement call for a human to make."""
    return bool(p.winner) and not any(c.eligible for c in p.others)


def auto_text(pid: int, what: str, why: str, undoable: bool = True) -> str:
    tail = f" Reply `undo {pid}` to revert." if undoable else ""
    return f"🤖 **Auto {pid}** · {what}. Why: {why}.{tail}"


HELP = (
    "I'm the Gatekeeper. Maintainers: reply `approve <id>`, `reject <id> [reason]` or `undo <id>`. "
    "Anyone: ask me `free issues` to see what is unassigned."
)
