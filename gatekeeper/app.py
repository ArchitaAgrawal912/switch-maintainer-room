"""The Gatekeeper: polls GitHub, posts proposals into Switch, acts on approval."""
from __future__ import annotations

import asyncio
import logging

from . import messages as M
from .config import Config
from .github_client import GitHub
from .rules import PullRequest, check_pr_eligible, combine_verdicts, propose_assignment, Issue, Comment, Proposal, Candidate
from .state import State
from .switch_client import Message, SwitchClient

log = logging.getLogger("gatekeeper")


class Gatekeeper:
    def __init__(self, cfg: Config, switch: SwitchClient | None, gh: GitHub, state: State):
        self.cfg = cfg
        self.switch = switch
        self.gh = gh
        self.state = state
        self.reviewers = [r.lower() for r in cfg.reviewers]
        self._doc_ops_ok = True

    # ------------------------------------------------------------ helpers
    async def say(self, room: str, text: str) -> None:
        log.info("[%s] %s", room or "console", text.replace("\n", " | "))
        if self.switch and room:
            await self.switch.post_message(room, text)

    def _is_me(self, msg: Message) -> bool:
        return msg.sender == self.switch.agent_id or msg.sender_name.lower() == self.cfg.agent_name.lower()

    def _is_maintainer(self, msg: Message) -> bool:
        if self.cfg.maintainers:
            return msg.sender_name.lower() in self.cfg.maintainers or msg.sender.lower() in self.cfg.maintainers
        return msg.room_id == self.cfg.maintainers_room

    def _is_reviewer(self, msg: Message) -> bool:
        return not self.reviewers or msg.sender_name.lower() in self.reviewers

    async def free_issues(self) -> list[Issue]:
        issues = await self.gh.list_issues(self.cfg.issue_label)
        return [i for i in issues if not i.assignees and not self.state.holder(i.number)]

    # --------------------------------------------------------------- poll
    async def poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def poll_once(self) -> None:
        counts: dict[str, int] = {}
        for issue in await self.gh.list_issues(self.cfg.issue_label):
            comments = await self.gh.list_comments(issue.number)
            new = [c for c in comments if c.id not in self.state.seen_comments]
            self.state.seen_comments.update(c.id for c in new)
            if not new or self.state.pending_for_issue(issue.number):
                continue
            for login in {c.author for c in comments}:
                if login not in counts:
                    counts[login] = await self.gh.count_open_prs(login, self.cfg.count_prs_repo)
            proposal = propose_assignment(
                issue,
                comments,
                counts,
                max_open_prs=self.cfg.max_open_prs,
                ignore=set(self.cfg.ignore_logins),
                already_assigned_to=self.state.holder(issue.number),
            )
            if proposal:
                await self._propose_assignment(proposal)

        for pr in await self.gh.list_open_prs():
            if pr.number in self.state.seen_prs:
                continue
            self.state.seen_prs.add(pr.number)
            await self._on_new_pr(pr)
        self.state.save()

    # -------------------------------------------------------- assignment
    async def _propose_assignment(self, p: Proposal) -> None:
        if not p.winner:
            await self.say(self.cfg.maintainers_room, M.proposal_text(0, p).replace("**Proposal 0** · ", "FYI · "))
            return
        pid = self.state.add_proposal(
            "assign",
            {
                "issue": p.issue.number,
                "title": p.issue.title,
                "winner": p.winner,
                "reason": p.reason,
                "candidates": [{"login": c.login, "reason": c.reason, "eligible": c.eligible} for c in p.candidates],
            },
        )
        self.state.save()
        await self.say(self.cfg.maintainers_room, M.proposal_text(pid, p))

    async def _do_assign(self, prop: dict, by: str) -> str:
        n, winner = prop["issue"], prop["winner"]
        replay = self.cfg.replay_mode
        p = Proposal(
            Issue(n, prop["title"]),
            winner,
            prop["reason"],
            [Candidate(c["login"], None, 0, False, c["eligible"], c["reason"]) for c in prop["candidates"]],
        )
        if replay:  # replayed logins aren't collaborators on the fork, so label instead of assign
            await self.gh.add_label(n, f"assigned: {winner}")
        else:
            await self.gh.assign(n, winner)
        await self.gh.comment(n, M.winner_comment(p, replay))
        if p.others:
            free = [i.number for i in await self.free_issues() if i.number != n]
            await self.gh.comment(n, M.others_comment(p, replay, free))
        self.state.assign(n, winner)
        self.state.log("assigned", f"#{n}", winner, prop["reason"], by)
        return f"Assigned #{n} to {winner}, replied on the issue and logged it in the ledger." + (
            f" The {len(p.others)} other(s) were told why." if p.others else ""
        )

    # ------------------------------------------------------------ PR flow
    async def _on_new_pr(self, pr: PullRequest) -> None:
        check = check_pr_eligible(pr, self.state.assignment_map())
        if not check.ok:
            body = (
                f"Thanks for the pull request. It can't be reviewed yet: {check.reason}. "
                "Ask for the issue first, and add `Closes #<issue>` to the description once it is assigned to you."
            )
            pid = self.state.add_proposal("pr_comment", {"pr": pr.number, "body": body, "reason": check.reason})
            await self.say(
                self.cfg.maintainers_room,
                f"**Proposal {pid}** · PR #{pr.number} by {pr.author} is not eligible: {check.reason}. "
                f"I won't send it to reviewers. `approve {pid}` posts this note on the PR.",
            )
            return

        room = await self._review_room(pr)
        self.state.verdicts[str(pr.number)] = {}
        await self.say(
            self.cfg.maintainers_room,
            f"PR #{pr.number} {check.reason}. Sent to {', '.join(self.cfg.reviewers) or 'the reviewers'} for review.",
        )
        await self.say(room, M.review_request(pr, self.cfg.reviewers, self.cfg.repo))

    async def _review_room(self, pr: PullRequest) -> str:
        """A side room per PR when SIDE_ROOMS=true and the op works; else the shared review room."""
        if self.cfg.side_rooms and self.switch:
            try:
                res = await self.switch.op(
                    "create_room",
                    name=f"pr-{pr.number}-review",
                    description=f"Review of PR #{pr.number} {pr.title} by {pr.author}",
                    agent_names=[self.cfg.agent_name, *self.cfg.reviewers],
                    user_names=self.cfg.maintainer_usernames or None,
                )
                return str(res["id"])
            except Exception as e:
                log.warning("side room failed (%s); using the review room", e)
        return self.cfg.reviews_room or self.cfg.maintainers_room

    async def _on_verdict(self, msg: Message, pr_number: int, verdict: str) -> None:
        votes = self.state.verdicts.setdefault(str(pr_number), {})
        votes[msg.sender_name or msg.sender] = verdict
        self.state.save()
        expected = max(len(self.cfg.reviewers), 1)
        if len(votes) < expected:
            return
        agree, final = combine_verdicts(votes)
        summary = ", ".join(f"{k}: {v}" for k, v in votes.items())
        if agree:
            body = f"Automated review by {expected} agent(s): **{final}**. A maintainer will take it from here."
            pid = self.state.add_proposal("pr_comment", {"pr": pr_number, "body": body, "reason": f"reviewers agree: {final}"})
            await self.say(
                self.cfg.maintainers_room,
                f"**Proposal {pid}** · PR #{pr_number}: reviewers agree, **{final}** ({summary}). "
                f"`approve {pid}` posts the result on the PR.",
            )
        else:
            self.state.log("review split", f"PR #{pr_number}", "", summary, "reviewers")
            await self.say(
                self.cfg.maintainers_room,
                f"Reviewers **disagree** on PR #{pr_number} ({summary}). Both arguments are in the review room. "
                "I won't post anything on GitHub until you decide.",
            )
        self.state.save()

    # ---------------------------------------------------------- approvals
    async def _on_command(self, msg: Message, action: str, pid: int | None, note: str) -> None:
        pending = self.state.pending()
        if pid is None:
            if len(pending) != 1:
                ids = ", ".join(str(p["id"]) for p in pending) or "none"
                await self.say(msg.room_id, f"Which one? Say `{action} <id>`. Pending: {ids}.")
                return
            pid = pending[0]["id"]
        prop = self.state.proposals.get(str(pid))
        if not prop or prop["status"] != "pending":
            await self.say(msg.room_id, f"Proposal {pid} isn't pending.")
            return
        by = msg.sender_name or msg.sender
        if action == "reject":
            prop["status"] = "rejected"
            self.state.log("rejected", f"#{prop.get('issue') or prop.get('pr')}", prop.get("winner", ""), note or prop.get("reason", ""), by)
            reply = f"Rejected proposal {pid}. Nothing was written to GitHub."
        else:
            try:
                if prop["kind"] == "assign":
                    reply = await self._do_assign(prop, by)
                else:
                    await self.gh.comment(prop["pr"], prop["body"])
                    self.state.log("commented", f"PR #{prop['pr']}", "", prop["reason"], by)
                    reply = f"Posted the note on PR #{prop['pr']} and logged it."
                prop["status"] = "approved"
            except Exception as e:
                log.exception("approve failed")
                reply = f"GitHub refused proposal {pid}: {e}. It is still pending."
        self.state.save()
        await self.say(msg.room_id, reply)
        await self.sync_ledger()

    async def sync_ledger(self) -> None:
        md = self.state.ledger_markdown()
        (self.cfg.state_file.parent / "ledger.md").write_text(md, "utf-8")
        if not (self.switch and self._doc_ops_ok and self.cfg.maintainers_room):
            return
        room = self.cfg.maintainers_room
        try:
            if self.state.ledger_doc_id:
                await self.switch.room_op(room, "update_room_document", document_id=self.state.ledger_doc_id, content=md)
            else:
                res = await self.switch.room_op(
                    room,
                    "create_room_document",
                    name="assignment-ledger",
                    description="Every assignment and review decision, who approved it and why",
                    instructions="Written only by the Gatekeeper. Read it to answer 'why was I unassigned?'.",
                    content=md,
                )
                self.state.ledger_doc_id = res["document_id"]
                self.state.save()
        except Exception as e:
            self._doc_ops_ok = False
            log.warning("ledger document sync disabled (%s); ledger.md is still written locally", e)

    # ----------------------------------------------------------- messages
    async def on_message(self, msg: Message) -> None:
        if self._is_me(msg):
            return
        verdict = M.parse_verdict(msg.body)
        if verdict and self._is_reviewer(msg):
            await self._on_verdict(msg, *verdict)
            return
        cmd = M.parse_command(msg.body)
        if cmd and (msg.addressed or msg.room_id == self.cfg.maintainers_room):
            if self._is_maintainer(msg):
                await self._on_command(msg, *cmd)
            else:
                await self.say(msg.room_id, "Only maintainers can approve or reject proposals.")
            return
        if not msg.addressed:
            return
        text = M.strip_mentions(msg.body).lower()
        if "free" in text or "available" in text:
            free = await self.free_issues()
            lines = [f"- #{i.number} {i.title} {i.url}" for i in free[:10]]
            await self.say(msg.room_id, f"{len(free)} issue(s) are free:\n" + "\n".join(lines) if free else "No free issues right now.")
        else:
            await self.say(msg.room_id, M.HELP)
