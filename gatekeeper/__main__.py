"""python -m gatekeeper <register|ops|rooms|scan|run>"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import config
from .app import Gatekeeper
from .github_client import GitHub
from .state import State
from .switch_client import SwitchClient, register


def _need(value: str, name: str) -> str:
    if not value:
        sys.exit(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


async def cmd_ops(cfg: config.Config) -> None:
    sw = SwitchClient.from_file(cfg.switch_url, cfg.agent_file)
    ops = await sw.list_ops()
    for name, spec in sorted(ops.items()):
        print(f"\n## {name}\n{spec.get('description', '')}\n{json.dumps(spec.get('input_schema', {}), indent=2)}")
    await sw.close()


async def cmd_rooms(cfg: config.Config) -> None:
    sw, task = await _connected(cfg)
    print(json.dumps(await sw.op("list_rooms"), indent=2))
    print("agents:", [a["name"] for a in await sw.op("list_agents")])
    task.cancel()
    await sw.close()


async def _connected(cfg: config.Config) -> tuple[SwitchClient, asyncio.Task]:
    """Ops need a live WebSocket session, so open one first. A one-off command gets
    its own connection id so it never takes over the running Gatekeeper's session."""
    import uuid

    sw = SwitchClient.from_file(cfg.switch_url, cfg.agent_file)
    sw.connection_id = str(uuid.uuid4())

    async def ignore(_msg) -> None:
        return None

    task = asyncio.create_task(sw.listen(ignore))
    await asyncio.wait_for(sw.connected.wait(), timeout=20)
    return sw, task


async def cmd_setup(cfg: config.Config) -> None:
    from .setup_rooms import setup

    _need(cfg.repo, "GITHUB_REPO")
    sw, task = await _connected(cfg)
    try:
        ids = await setup(cfg, sw)
        print("\nRooms ready (ids written to .env):")
        for k, v in ids.items():
            print(f"  {k}={v}")
    finally:
        task.cancel()
        await sw.close()


async def cmd_scan(cfg: config.Config) -> None:
    """One poll with no Switch and no GitHub writes: prints what it would propose."""
    cfg.dry_run = True
    gh = GitHub(cfg.github_token, _need(cfg.repo, "GITHUB_REPO"), dry_run=True)
    state = State(cfg.state_file.with_name("scan-state.json"))
    state.path.unlink(missing_ok=True)
    await Gatekeeper(cfg, None, gh, state).poll_once()
    state.path.unlink(missing_ok=True)
    await gh.close()


def _quiet_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows logs a traceback when a server closes an idle keep-alive socket. Harmless."""
    if isinstance(context.get("exception"), ConnectionError):
        return
    loop.default_exception_handler(context)


async def cmd_run(cfg: config.Config) -> None:
    asyncio.get_running_loop().set_exception_handler(_quiet_resets)
    _need(cfg.maintainers_room, "MAINTAINERS_ROOM_ID")
    sw = SwitchClient.from_file(cfg.switch_url, cfg.agent_file)
    gh = GitHub(cfg.github_token, _need(cfg.repo, "GITHUB_REPO"), dry_run=cfg.dry_run)
    gk = Gatekeeper(cfg, sw, gh, State(cfg.state_file))
    rooms = [r for r in (cfg.maintainers_room, cfg.reviews_room, cfg.contributors_room, cfg.desk_room) if r]

    async def take_role() -> None:
        """The exclusive role is a lease: a dropped socket releases it, so take it on every connect."""
        try:
            await sw.room_op(cfg.maintainers_room, "assume_role", role="gatekeeper")
            logging.info("holding the exclusive 'gatekeeper' role")
        except Exception as e:
            logging.info("assume_role skipped: %s", str(e)[:200])

    sw.on_connect = take_role
    listener = asyncio.create_task(sw.listen(gk.on_message, rooms))
    try:
        await asyncio.wait_for(sw.connected.wait(), timeout=20)
    except asyncio.TimeoutError:
        logging.warning("no connection_state after 20s; continuing (check SWITCH_URL and room ids)")
    await gk.say(cfg.maintainers_room, f"Gatekeeper online. Watching `{cfg.repo}` issues labelled `{cfg.issue_label}` and its pull requests.")
    await gk.sync_ledger()
    poller = asyncio.create_task(gk.poll_forever())
    try:
        await listener
    finally:
        poller.cancel()
        await sw.close()
        await gh.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="gatekeeper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="register the agent once and save .agent.json")
    r.add_argument("--overwrite", action="store_true")
    sub.add_parser("ops", help="list the operations this agent may call, with argument schemas")
    sub.add_parser("rooms", help="list rooms and agents the Gatekeeper can see")
    sub.add_parser("setup", help="create the maintainers/reviews/contributors rooms and write their ids to .env")
    sub.add_parser("scan", help="dry run: poll GitHub once and print proposals")
    sub.add_parser("run", help="connect to Switch and run")
    args = ap.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)-10s %(message)s", datefmt="%H:%M:%S")
    cfg = config.load()

    if args.cmd == "register":
        creds = register(cfg.switch_url, _need(cfg.registration_token, "SWITCH_REGISTRATION_TOKEN"), cfg.agent_name, cfg.agent_file, args.overwrite)
        print(f"Registered '{cfg.agent_name}' as {creds['agent_id']}. Key saved to {cfg.agent_file.name} (keep it secret).")
        return
    asyncio.run({"ops": cmd_ops, "rooms": cmd_rooms, "setup": cmd_setup, "scan": cmd_scan, "run": cmd_run}[args.cmd](cfg))


if __name__ == "__main__":
    main()
