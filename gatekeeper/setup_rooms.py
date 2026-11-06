"""`python -m gatekeeper setup`: build the Maintainer Room on a Switch server.

Creates three rooms (maintainers, reviews, contributors) bridged to the
server's default chat app, adds the Gatekeeper, the reviewers and the
maintainers, defines the roles, links the rooms, and writes the room ids
into .env.
"""
from __future__ import annotations

import logging
import re

from .config import ROOT, Config
from .switch_client import SwitchClient

log = logging.getLogger("setup")

RULES = """Contribution rules for {repo}:
- One person per issue. The first person to ask gets it, if eligible.
- Eligible = fewer than {max_prs} open pull requests and a stated plan.
- A pull request must say `Closes #<issue>` and its author must hold that issue.
- No activity for 48 hours: the issue may be offered to the next person.
- Low-effort pull requests (typos, formatting, unreviewed AI output) are not merged.
Nothing is written to GitHub until a maintainer replies `approve <id>`."""

REVIEWER_ROLE = (
    "Review the pull request the Gatekeeper sends. Check it out, run the tests, read the diff. "
    "Never push, comment on GitHub or merge. End with exactly one line: "
    "`VERDICT: real|low-effort|broken - PR #<number> - <one-line reason>`."
)


def _set_env(values: dict[str, str]) -> None:
    path = ROOT / ".env"
    text = path.read_text("utf-8") if path.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        text, n = re.subn(rf"(?m)^{key}=.*$", line, text)
        if not n:
            text += f"\n{line}"
    path.write_text(text, "utf-8")


async def setup(cfg: Config, sw: SwitchClient, prefix: str = "mroom") -> dict[str, str]:
    me = cfg.agent_name
    users = cfg.maintainer_usernames or None
    rules = RULES.format(repo=cfg.repo, max_prs=cfg.max_open_prs)
    existing = {r["name"]: r["room_id"] for r in await sw.op("list_rooms")}

    plan = [
        ("MAINTAINERS_ROOM_ID", f"{prefix}-maintainers", "Gatekeeper proposals. Maintainers reply approve <id> or reject <id>.", [me]),
        ("REVIEWS_ROOM_ID", f"{prefix}-reviews", "Reviewer agents work the pull requests the Gatekeeper sends.", [me, *cfg.reviewers]),
        ("CONTRIBUTORS_ROOM_ID", f"{prefix}-contributors", "Ask the Gatekeeper which issues are free.", [me]),
    ]
    known = {a["name"] for a in await sw.op("list_agents")}
    missing = [r for r in cfg.reviewers if r not in known]
    if missing:
        log.warning("reviewer agents not found yet: %s. Create them in Switch Console, then run setup again.", missing)

    ids: dict[str, str] = {}
    for env_key, name, desc, wanted in plan:
        agents = [a for a in wanted if a in known]
        if name in existing:
            ids[env_key] = existing[name]
            log.info("room %s exists (%s)", name, existing[name])
            for a in agents:
                if a != me:
                    try:
                        await sw.op("invite_agent_to_room", room_id=existing[name], agent_name=a)
                    except RuntimeError as e:
                        log.info("invite %s: %s", a, str(e)[:120])
            continue
        res = await sw.op("create_room", name=name, description=desc, agent_names=agents, user_names=users, instructions=rules)
        ids[env_key] = res["id"]
        log.info("created room %s (%s)", name, res["id"])
        if res.get("failed_attachments"):
            log.warning("  not attached: %s", res["failed_attachments"])

    for room, role, text, exclusive in (
        (ids["MAINTAINERS_ROOM_ID"], "gatekeeper", "Apply the contribution rules, post proposals, act on GitHub only after approval.", True),
        (ids["REVIEWS_ROOM_ID"], "reviewer", REVIEWER_ROLE, False),
    ):
        try:
            await sw.room_op(room, "define_role", name=role, instructions=text, exclusive=exclusive)
            log.info("defined role %s", role)
        except RuntimeError as e:
            log.info("role %s: %s", role, str(e)[:160])

    for src, dst, label in (
        ("MAINTAINERS_ROOM_ID", "REVIEWS_ROOM_ID", "pull request reviews"),
        ("MAINTAINERS_ROOM_ID", "CONTRIBUTORS_ROOM_ID", "contributor questions"),
        ("CONTRIBUTORS_ROOM_ID", "MAINTAINERS_ROOM_ID", "escalate to maintainers"),
    ):
        try:
            await sw.op("link_rooms", source_room_id=ids[src], target_room_id=ids[dst], label=label)
        except RuntimeError as e:
            log.info("link %s -> %s: %s", src, dst, str(e)[:160])

    _set_env(ids)
    return ids
