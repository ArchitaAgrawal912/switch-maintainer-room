"""What the Gatekeeper remembers between polls, plus the assignment ledger."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class State:
    def __init__(self, path: Path):
        self.path = path
        data: dict[str, Any] = json.loads(path.read_text("utf-8")) if path.exists() else {}
        self.seen_comments: set[int] = set(data.get("seen_comments", []))
        self.seen_prs: set[int] = set(data.get("seen_prs", []))
        self.next_id: int = data.get("next_id", 1)
        self.proposals: dict[str, dict] = data.get("proposals", {})
        self.assignments: dict[str, dict] = data.get("assignments", {})
        self.verdicts: dict[str, dict[str, str]] = data.get("verdicts", {})
        self.ledger: list[dict] = data.get("ledger", [])
        self.ledger_doc_id: str | None = data.get("ledger_doc_id")

    def save(self) -> None:
        data = {
            "seen_comments": sorted(self.seen_comments),
            "seen_prs": sorted(self.seen_prs),
            "next_id": self.next_id,
            "proposals": self.proposals,
            "assignments": self.assignments,
            "verdicts": self.verdicts,
            "ledger": self.ledger,
            "ledger_doc_id": self.ledger_doc_id,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), "utf-8")
        os.replace(tmp, self.path)

    # ---- proposals
    def add_proposal(self, kind: str, data: dict) -> int:
        pid = self.next_id
        self.next_id += 1
        self.proposals[str(pid)] = {"id": pid, "kind": kind, "status": "pending", "created": now_iso(), **data}
        return pid

    def pending(self) -> list[dict]:
        return [p for p in self.proposals.values() if p["status"] == "pending"]

    def pending_for_issue(self, number: int) -> dict | None:
        return next((p for p in self.pending() if p.get("issue") == number and p["kind"] == "assign"), None)

    # ---- assignments
    def holder(self, number: int) -> str | None:
        a = self.assignments.get(str(number))
        return a["login"] if a else None

    def assignment_map(self) -> dict[int, str]:
        return {int(k): v["login"] for k, v in self.assignments.items()}

    def assign(self, number: int, login: str) -> None:
        self.assignments[str(number)] = {"login": login, "at": now_iso()}

    # ---- ledger
    def log(self, action: str, subject: str, who: str, reason: str, by: str) -> None:
        self.ledger.append({"time": now_iso(), "action": action, "subject": subject, "who": who, "reason": reason, "by": by})

    def ledger_markdown(self) -> str:
        rows = [
            f"| {e['time']} | {e['action']} | {e['subject']} | {e['who']} | {e['reason']} | {e['by']} |"
            for e in reversed(self.ledger)
        ]
        return (
            "# Assignment ledger\n\nEvery decision the Gatekeeper carried out, newest first.\n\n"
            "| Time | Action | Issue / PR | Contributor | Reason | Approved by |\n"
            "| --- | --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"
        )
