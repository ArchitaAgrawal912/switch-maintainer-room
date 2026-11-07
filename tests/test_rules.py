from datetime import datetime, timedelta, timezone

from gatekeeper.rules import (
    Assignment,
    Comment,
    Issue,
    PullRequest,
    check_pr_eligible,
    combine_verdicts,
    find_stale,
    has_plan,
    is_assignment_request,
    plan_score,
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


def test_equal_plans_tie_break_on_who_asked_first():
    comments = [c(1, "alice", PLAN, 0), c(2, "bob", PLAN, 5)]
    p = propose_assignment(ISSUE, comments, {"alice": 1, "bob": 0})
    assert p.winner == "alice"
    assert p.reason.startswith("most concrete plan of 2")
    assert [o.login for o in p.others] == ["bob"]
    assert p.others[0].reason == "eligible, same plan score but asked after alice"


DETAILED = (
    "Can I take this? Plan:\n1. Add `connectors/grafana.py` with the auth flow\n"
    "2. Register it in `registry.py`\n3. Write pytest tests for token refresh and error handling\n"
    "4. Update the docs page"
)


def test_a_more_concrete_plan_beats_asking_first():
    comments = [c(1, "racer", PLAN, 0), c(2, "careful", DETAILED, 30)]
    p = propose_assignment(ISSUE, comments, {})
    assert p.winner == "careful"
    assert "more concrete" in p.others[0].reason


def test_plan_score_rewards_specifics_not_padding():
    assert plan_score(DETAILED) > plan_score(PLAN)
    assert plan_score("I will do it " * 200) <= 4 + 0  # padding is capped


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


def test_pr_eligibility():
    held = {803: "kittu"}
    ok = check_pr_eligible(PullRequest(912, "t", "kittu", "Adds redaction.\n\nCloses #803"), held)
    assert ok.ok and ok.issue_number == 803
    assert not check_pr_eligible(PullRequest(913, "t", "kittu", "no link"), held).ok
    wrong = check_pr_eligible(PullRequest(914, "t", "mallory", "Fixes #803"), held)
    assert not wrong.ok and "assigned to kittu" in wrong.reason
    assert "not assigned" in check_pr_eligible(PullRequest(915, "t", "x", "resolves #1"), held).reason


def test_find_stale():
    fresh = Assignment(1, "a", T0, T0 + timedelta(hours=40))
    stale = Assignment(2, "b", T0, T0)
    assert find_stale([fresh, stale], T0 + timedelta(hours=49)) == [stale]


def test_combine_verdicts():
    assert combine_verdicts({"claude": "real", "codex": "real"}) == (True, "real")
    assert combine_verdicts({"claude": "real", "codex": "broken"}) == (False, None)
