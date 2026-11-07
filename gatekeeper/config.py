"""Settings, read from .env (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    switch_url: str
    registration_token: str
    agent_name: str
    maintainers_room: str
    reviews_room: str
    contributors_room: str
    desk_room: str
    desk_bridge: str
    desk_usernames: list[str]
    reviewers: list[str]
    maintainers: list[str]
    maintainer_usernames: list[str]
    side_rooms: bool
    github_token: str
    repo: str
    issue_label: str
    count_prs_repo: str
    ignore_logins: list[str]
    max_open_prs: int
    poll_seconds: int
    replay_mode: bool
    dry_run: bool
    state_file: Path
    agent_file: Path


def load() -> Config:
    repo = os.getenv("GITHUB_REPO", "")
    return Config(
        switch_url=os.getenv("SWITCH_URL", "http://localhost:8000").rstrip("/"),
        registration_token=os.getenv("SWITCH_REGISTRATION_TOKEN", ""),
        agent_name=os.getenv("AGENT_NAME", "gatekeeper"),
        maintainers_room=os.getenv("MAINTAINERS_ROOM_ID", ""),
        reviews_room=os.getenv("REVIEWS_ROOM_ID", ""),
        contributors_room=os.getenv("CONTRIBUTORS_ROOM_ID", ""),
        desk_room=os.getenv("DESK_ROOM_ID", ""),
        desk_bridge=os.getenv("DESK_BRIDGE", "").lower(),
        desk_usernames=_list("DESK_USERNAMES"),
        reviewers=_list("REVIEWERS"),
        maintainers=[m.lower() for m in _list("MAINTAINERS")],
        maintainer_usernames=_list("MAINTAINER_USERNAMES"),
        side_rooms=_bool("SIDE_ROOMS", "true"),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        repo=repo,
        issue_label=os.getenv("ISSUE_LABEL", "hackathon"),
        count_prs_repo=os.getenv("COUNT_PRS_REPO", repo),
        ignore_logins=[x.lower() for x in _list("IGNORE_LOGINS")],
        max_open_prs=int(os.getenv("MAX_OPEN_PRS", "5")),
        poll_seconds=int(os.getenv("POLL_SECONDS", "60")),
        replay_mode=_bool("REPLAY_MODE", "true"),
        dry_run=_bool("DRY_RUN"),
        state_file=ROOT / os.getenv("STATE_FILE", "state.json"),
        agent_file=ROOT / ".agent.json",
    )
