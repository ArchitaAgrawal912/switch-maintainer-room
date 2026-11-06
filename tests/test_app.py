"""End-to-end flow with fake GitHub and fake Switch: request -> proposal -> approve -> GitHub write."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gatekeeper.app import Gatekeeper
from gatekeeper.config import Config
from gatekeeper.rules import Comment, Issue, PullRequest
from gatekeeper.state import State
from gatekeeper.switch_client import Message

T0 = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
PLAN = "Can I work on this? My plan: add the connector class, register it, and write tests for the auth flow end to end."


class FakeGitHub:
    def __init__(self):
        self.writes = []
        self.issues = [Issue(826, "Add the Grafana connector")]
        self.comments = {826: [Comment(1, "kittu", PLAN, T0), Comment(2, "busy", PLAN, T0 + timedelta(minutes=5))]}
        self.prs = []

    async def list_issues(self, label): return self.issues
    async def list_comments(self, n): return self.comments.get(n, [])
    async def list_open_prs(self): return self.prs
    async def count_open_prs(self, login, repo): return 5 if login == "busy" else 1
    async def comment(self, n, body): self.writes.append(("comment", n, body))
    async def assign(self, n, login): self.writes.append(("assign", n, login))
    async def add_label(self, n, label): self.writes.append(("label", n, label))


class FakeSwitch:
    agent_id = "gk"
    def __init__(self): self.posts = []
    async def post_message(self, room, text): self.posts.append((room, text))
    async def op(self, name, **args): raise RuntimeError("no ops in tests")


def cfg(tmp: Path) -> Config:
    return Config(
        switch_url="http://x", registration_token="", agent_name="gatekeeper",
        maintainers_room="maint", reviews_room="rev", contributors_room="", reviewers=["claude", "codex"],
        maintainers=[], maintainer_usernames=[], side_rooms=False, github_token="", repo="me/demo", issue_label="hackathon",
        count_prs_repo="me/demo", ignore_logins=[], max_open_prs=5, poll_seconds=60, replay_mode=True,
        dry_run=False, state_file=tmp / "state.json", agent_file=tmp / ".agent.json",
    )


def msg(body, room="maint", sender="petr"):
    return Message(room, sender, sender, body, True, None, {})


def test_assignment_needs_approval_then_writes(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gk = Gatekeeper(cfg(tmp_path), sw, gh, State(tmp_path / "state.json"))

    asyncio.run(gk.poll_once())
    assert gh.writes == []  # nothing written before approval
    assert "Assign **kittu**" in sw.posts[-1][1] and "5 open PRs" in sw.posts[-1][1]

    asyncio.run(gk.poll_once())  # no new comments -> no duplicate proposal
    assert sum("Proposal" in p[1] for p in sw.posts) == 1

    asyncio.run(gk.on_message(msg("approve 1")))
    assert ("label", 826, "assigned: kittu") in gh.writes
    assert all("@kittu" not in w[2] for w in gh.writes if w[0] == "comment")  # replay mode: no pings
    assert gk.state.holder(826) == "kittu"
    assert (tmp_path / "ledger.md").read_text().count("kittu") >= 1


def test_reject_writes_nothing(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gk = Gatekeeper(cfg(tmp_path), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    asyncio.run(gk.on_message(msg("@gatekeeper reject 1 wait for the design doc")))
    assert gh.writes == [] and gk.state.proposals["1"]["status"] == "rejected"


def test_pr_review_disagreement_goes_to_human(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gk = Gatekeeper(cfg(tmp_path), sw, gh, State(tmp_path / "state.json"))
    gk.state.assign(826, "kittu")
    gh.prs = [PullRequest(912, "Grafana connector", "kittu", "Closes #826", "http://pr")]
    asyncio.run(gk.poll_once())
    assert any(room == "rev" and "PR #912" in text for room, text in sw.posts)

    asyncio.run(gk.on_message(msg("VERDICT: real - PR #912 - tests pass", room="rev", sender="claude")))
    asyncio.run(gk.on_message(msg("VERDICT: broken - PR #912 - misses nested JSON", room="rev", sender="codex")))
    assert "disagree" in sw.posts[-1][1] and gh.writes == []


def test_ineligible_pr_is_not_reviewed(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gh.comments = {}
    gh.prs = [PullRequest(913, "typo", "mallory", "fix typo")]
    gk = Gatekeeper(cfg(tmp_path), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    assert all(room != "rev" for room, _ in sw.posts)
    assert "not eligible" in sw.posts[-1][1]
