from gatekeeper.messages import parse_command, parse_verdict, who


def test_parse_command():
    assert parse_command("approve 3") == ("approve", 3, "")
    assert parse_command("@gatekeeper reject #2 plan too vague") == ("reject", 2, "plan too vague")
    assert parse_command("Approve") == ("approve", None, "")
    assert parse_command("I approve of this idea") is None


def test_parse_verdict():
    assert parse_verdict("Tests pass.\nVERDICT: real - PR #912 - adds redaction") == (912, "real")
    assert parse_verdict("VERDICT: Low effort, PR 12, only formatting") == (12, "low-effort")
    assert parse_verdict("VERDICT: real") is None  # no PR number
    assert parse_verdict("looks fine, PR #3") is None


def test_replay_mode_never_mentions():
    assert who("kittu", replay=True) == "`kittu`"
    assert who("kittu", replay=False) == "@kittu"
