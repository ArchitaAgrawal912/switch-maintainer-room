from scripts.replay import defuse
from gatekeeper.github_client import replay_identity
from gatekeeper.switch_client import extract_message


def test_replay_marker_restores_original_author():
    body = "<!-- replay author=kittu at=2026-10-28T10:14:00Z -->\n**`kittu`** wrote:\n\ncan I take this?"
    author, at, text = replay_identity(body, "demo-bot", "2026-11-06T00:00:00Z")
    assert author == "kittu" and at.hour == 10 and "replay" not in text


def test_no_marker_keeps_github_author():
    author, _, text = replay_identity("hello", "alice", "2026-11-06T00:00:00Z")
    assert (author, text) == ("alice", "hello")


def test_defuse_breaks_mentions_and_cross_refs():
    out = defuse("thanks @petr, see sandbox-quantum/switch#12")
    assert "@petr" not in out and "switch#12" not in out


def test_extract_message_reads_real_frame_shape():
    # shape seen on a live Switch 0.31 server: {"event": ..., "data": {...}}
    frame = {"event": "message", "id": 9, "data": {"room_id": "r2", "addressed": False,
             "sender": "u1", "sender_name": "user", "body": "approve 2"}}
    m = extract_message(frame)
    assert (m.room_id, m.sender_name, m.body, m.addressed) == ("r2", "user", "approve 2", False)


def test_extract_message_reads_payload():
    frame = {"type": "message", "id": 7, "room_id": "r1",
             "payload": {"addressed": True, "sender": "u1", "sender_name": "petr", "body": "approve 1"}}
    m = extract_message(frame)
    assert (m.room_id, m.sender_name, m.body, m.addressed) == ("r1", "petr", "approve 1", True)
