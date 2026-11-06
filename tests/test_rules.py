from datetime import datetime, timedelta, timezone

from gatekeeper.rules import (
    Comment,
    Issue,
    has_plan,
    is_assignment_request,
    propose_assignment,
)

T0 = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
PLAN = "I'd like to work on this. My plan: add a Grafana connector class, register it in the registry, and write tests for the auth flow."


def c(i, who, body, minutes=0):
    return Comment(i, who, body, T0 + timedelta(minutes=minutes))


ISSUE = Issue(826, "Add the Grafana connector")


def test_detects_requests():
    assert is_assignment_request("Can I work on this?")
    assert is_assignment_request("hey, please assign me")
    assert is_assignment_request("I'd love to take this one")
    assert not is_assignment_request("This is broken on Windows too")


def test_plan_needs_length_and_a_how():
    assert has_plan(PLAN)
    assert not has_plan("Can I work on this?")
    assert not has_plan("assign me " * 3)


def test_first_eligible_asker_wins():
    comments = [c(1, "alice", PLAN, 0), c(2, "bob", PLAN, 5)]
    p = propose_assignment(ISSUE, comments, {"alice": 1, "bob": 0})
    assert p.winner == "alice"
    assert p.reason.startswith("first to ask")
    assert [o.login for o in p.others] == ["bob"]
    assert p.others[0].reason == "eligible, but asked after alice"


def test_skips_people_at_pr_limit_and_without_plan():
    comments = [
        c(1, "busy", PLAN, 0),
        c(2, "vague", "can I work on this?", 1),
        c(3, "carol", PLAN, 2),
    ]
    p = propose_assignment(ISSUE, comments, {"busy": 5, "vague": 0, "carol": 2})
    assert p.winner == "carol"
    reasons = {o.login: o.reason for o in p.others}
    assert "5 open PRs" in reasons["busy"]
    assert "did not say how" in reasons["vague"]


def test_plan_in_a_later_comment_counts():
    comments = [c(1, "dan", "can I take this?", 0), c(2, "dan", PLAN.replace("I'd like to work on this. ", ""), 3)]
    assert propose_assignment(ISSUE, comments, {}).winner == "dan"


def test_one_person_per_issue():
    assigned = Issue(826, "x", assignees=("alice",))
    assert propose_assignment(assigned, [c(1, "bob", PLAN)], {}) is None
    assert propose_assignment(ISSUE, [c(1, "bob", PLAN)], {}, already_assigned_to="alice") is None


def test_nobody_eligible_still_explains():
    p = propose_assignment(ISSUE, [c(1, "vague", "assign me")], {})
    assert p.winner is None and p.candidates[0].reason


def test_ignores_maintainers_bots_and_non_requests():
    comments = [c(1, "petr", PLAN), c(2, "dependabot[bot]", PLAN), c(3, "eve", "nice idea")]
    assert propose_assignment(ISSUE, comments, {}, ignore={"petr"}) is None
