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
    async def remove_label(self, n, label): self.writes.append(("unlabel", n, label))
    async def unassign(self, n, login): self.writes.append(("unassign", n, login))


class FakeSwitch:
    agent_id = "gk"
    def __init__(self): self.posts = []
    async def post_message(self, room, text): self.posts.append((room, text))
    async def op(self, name, **args): raise RuntimeError("no ops in tests")


def cfg(tmp: Path, auto: bool = False, judge: str = "", timeout: int = 180) -> Config:
    return Config(
        auto_mode=auto, plan_judge=judge, judge_timeout=timeout,
        switch_url="http://x", registration_token="", agent_name="gatekeeper",
        maintainers_room="maint", reviews_room="rev", contributors_room="", desk_room="", desk_bridge="", desk_usernames=[], reviewers=["claude", "codex"],
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


def test_auto_mode_handles_clear_cut_and_undo(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()  # kittu eligible, busy at the PR limit -> clear-cut
    gk = Gatekeeper(cfg(tmp_path, auto=True), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    assert ("label", 826, "assigned: kittu") in gh.writes
    assert "Auto 1" in sw.posts[-1][1] and "undo 1" in sw.posts[-1][1]

    asyncio.run(gk.on_message(msg("undo 1")))
    assert ("unlabel", 826, "assigned: kittu") in gh.writes
    assert gk.state.holder(826) is None and gk.state.proposals["1"]["status"] == "reverted"


def test_auto_mode_still_asks_when_contested(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gh.comments[826].append(Comment(3, "bob", PLAN, T0 + timedelta(minutes=9)))  # second eligible asker
    gk = Gatekeeper(cfg(tmp_path, auto=True), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    assert gh.writes == [] and "Proposal 1" in sw.posts[-1][1]


def test_auto_mode_replies_to_low_effort_but_asks_on_real(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gh.comments = {}
    gk = Gatekeeper(cfg(tmp_path, auto=True), sw, gh, State(tmp_path / "state.json"))
    gk.state.assign(826, "kittu")
    gh.prs = [PullRequest(8, "typo", "kittu", "Closes #826"), PullRequest(7, "feature", "kittu", "Closes #826")]
    asyncio.run(gk.poll_once())
    for pr, verdict in ((8, "low-effort"), (7, "real")):
        for reviewer in ("claude", "codex"):
            asyncio.run(gk.on_message(msg(f"VERDICT: {verdict} - PR #{pr} - x", room="rev", sender=reviewer)))
    assert [w[1] for w in gh.writes if w[0] == "comment"] == [8]  # only the low-effort one was auto-replied
    assert "Proposal" in sw.posts[-1][1] and "real" in sw.posts[-1][1]


def contested(gh):
    gh.comments[826].append(Comment(3, "bob", PLAN, T0 + timedelta(minutes=9)))


def test_judge_picks_between_eligible_plans(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    contested(gh)  # kittu and bob both eligible
    gk = Gatekeeper(cfg(tmp_path, judge="claude"), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    ask = next(text for room, text in sw.posts if room == "rev")
    assert "@claude" in ask and "`kittu`" in ask and "`bob`" in ask and "PICK: #826" in ask
    assert not any("Proposal" in t for _, t in sw.posts)  # waits for the judge

    asyncio.run(gk.poll_once())  # still judging -> no duplicate ask
    assert sum(room == "rev" for room, _ in sw.posts) == 1

    asyncio.run(gk.on_message(msg("PICK: #826 bob - names the files and plans tests", room="rev", sender="claude")))
    post = sw.posts[-1][1]
    assert "Assign **bob**" in post and "names the files" in post
    assert "judge preferred bob" in post
    assert gh.writes == []  # still needs the maintainer


def test_judge_ignored_if_it_picks_someone_ineligible(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    contested(gh)
    gk = Gatekeeper(cfg(tmp_path, judge="claude"), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    asyncio.run(gk.on_message(msg("PICK: #826 busy - lots of PRs", room="rev", sender="claude")))
    assert "826" in gk.state.judging


def test_judge_timeout_falls_back_to_plan_score(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    contested(gh)
    gk = Gatekeeper(cfg(tmp_path, judge="claude", timeout=0), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())  # asks
    asyncio.run(gk.poll_once())  # timeout 0 -> falls back
    assert "Assign **kittu**" in sw.posts[-1][1] and "ranked by plan score" in sw.posts[-1][1]


def test_ineligible_pr_is_not_reviewed(tmp_path):
    gh, sw = FakeGitHub(), FakeSwitch()
    gh.comments = {}
    gh.prs = [PullRequest(913, "typo", "mallory", "fix typo")]
    gk = Gatekeeper(cfg(tmp_path), sw, gh, State(tmp_path / "state.json"))
    asyncio.run(gk.poll_once())
    assert all(room != "rev" for room, _ in sw.posts)
    assert "not eligible" in sw.posts[-1][1]
