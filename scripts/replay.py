"""Copy real public issues + comments from a repo onto your demo repo.

    python scripts/replay.py --target you/demo-repo --limit 5
    python scripts/replay.py --target you/demo-repo --issues 826 803

Every copied comment keeps its original author and time in a hidden marker
that the Gatekeeper reads in REPLAY_MODE. @mentions are defused with a
zero-width space so nobody on the real repo gets pinged.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
API = "https://api.github.com"
ZWSP = "​"


def defuse(text: str) -> str:
    """Break @mentions and cross-repo references so the copy notifies no one."""
    text = re.sub(r"@([A-Za-z0-9-])", f"@{ZWSP}\\1", text or "")
    return re.sub(r"\b([\w.-]+/[\w.-]+)#(\d+)", f"\\1#{ZWSP}\\2", text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="sandbox-quantum/switch")
    ap.add_argument("--target", default=os.getenv("GITHUB_REPO"), required=not os.getenv("GITHUB_REPO"))
    ap.add_argument("--label", default="hackathon")
    ap.add_argument("--limit", type=int, default=5, help="how many issues (most-discussed first)")
    ap.add_argument("--issues", type=int, nargs="*", help="specific source issue numbers")
    ap.add_argument("--max-comments", type=int, default=12)
    ap.add_argument("--skip", type=int, nargs="*", default=[], help="source issue numbers already replayed")
    args = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set (.env).")
    if args.target.lower() == args.source.lower():
        sys.exit("Refusing to replay a repo onto itself.")

    gh = httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )

    if args.issues:
        src = [gh.get(f"/repos/{args.source}/issues/{n}").raise_for_status().json() for n in args.issues]
    else:
        items = gh.get(
            f"/repos/{args.source}/issues", params={"labels": args.label, "state": "open", "per_page": 100}
        ).raise_for_status().json()
        pool = (i for i in items if "pull_request" not in i and i["number"] not in args.skip)
        src = sorted(pool, key=lambda i: -i["comments"])[: args.limit]

    gh.post(f"/repos/{args.target}/labels", json={"name": args.label, "color": "f2a541"})  # 422 if it exists: fine

    for issue in src:
        body = (
            f"<!-- replay source={issue['html_url']} -->\n"
            f"> Replayed from {args.source} issue {issue['number']} for the Maintainer Room demo.\n\n"
            + defuse(issue.get("body") or "")[:6000]
        )
        new = gh.post(
            f"/repos/{args.target}/issues",
            json={"title": issue["title"], "body": body, "labels": [args.label]},
        ).raise_for_status().json()
        print(f"#{issue['number']} -> {new['html_url']}")

        comments = gh.get(issue["comments_url"], params={"per_page": 100}).raise_for_status().json()
        for c in comments[: args.max_comments]:
            text = (
                f"<!-- replay author={c['user']['login']} at={c['created_at']} -->\n"
                f"**`{c['user']['login']}`** on {c['created_at'][:16].replace('T', ' ')} UTC:\n\n"
                + defuse(c["body"])[:4000]
            )
            gh.post(f"/repos/{args.target}/issues/{new['number']}/comments", json={"body": text}).raise_for_status()
            time.sleep(1.1)  # stay under GitHub's secondary rate limit
        print(f"   {min(len(comments), args.max_comments)} comments copied")


if __name__ == "__main__":
    main()
