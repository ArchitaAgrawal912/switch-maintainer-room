"""Switch agent-protocol client.

Follows docs.switchagents.ai/switch-rooms/internals/agent-protocol and
agent-connection:
  - register once:      POST /agents                (registration key)
  - live events:        WS   /agents/{id}/connection/ws?connection_id=...
                        answer every `ping` with {"type":"pong","cursor":N} within 6s
  - post a message:     POST /agents/{id}/message   {"room_id","content"}
  - operations:         GET  /agents/{id}/ops, POST /agents/{id}/ops/{name}
                        (+ X-Switch-Connection-Id when the op needs a room)

The docs don't print full frame JSON, so `extract_message` reads fields
defensively. Run with LOG_LEVEL=DEBUG to see raw frames.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import websockets
from websockets.asyncio.client import connect

log = logging.getLogger("switch")

INTEGRATION_PROFILE = {
    "connection_model": "session_addressable",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
}


@dataclass
class Message:
    room_id: str
    sender: str
    sender_name: str
    body: str
    addressed: bool
    thread_id: str | None
    raw: dict


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def frame_data(frame: dict) -> dict:
    """Frames look like {"event": "...", "id": N, "data": {...}} (older docs say payload)."""
    for key in ("data", "payload"):
        if isinstance(frame.get(key), dict):
            return frame[key]
    return frame


def extract_message(frame: dict) -> Message:
    p = frame_data(frame)
    if isinstance(p.get("payload"), dict):  # message fields may sit one level deeper
        p = {**p, **p["payload"]}
    return Message(
        room_id=_pick(frame, "room_id", "room") or _pick(p, "room_id", "room", default=""),
        sender=str(_pick(p, "sender", "user_id", default="")),
        sender_name=str(_pick(p, "sender_name", "user_name", "sender", default="")),
        body=str(_pick(p, "body", "content", "text", default="")),
        addressed=bool(_pick(p, "addressed", default=False)),
        thread_id=_pick(p, "thread_id"),
        raw=frame,
    )


# ---------------------------------------------------------------- register


def register(base: str, registration_token: str, name: str, out: Path, overwrite: bool = False) -> dict:
    body = {
        "name": name,
        "description": "Maintainer Room gatekeeper: applies the repo's contribution rules and asks a maintainer before acting",
        "connector_type": "custom",
        "integration_profile": INTEGRATION_PROFILE,
    }
    if overwrite:
        body["overwrite"] = True
    r = httpx.post(f"{base}/agents", headers={"Authorization": f"Bearer {registration_token}"}, json=body, timeout=30)
    if r.status_code == 409:
        raise SystemExit(f"An agent named '{name}' already exists. Use --overwrite to issue a new key (breaks the old one).")
    r.raise_for_status()
    data = r.json()
    creds = {"agent_id": data["id"], "api_key": data["api_key"], "connection_id": str(uuid.uuid4())}
    out.write_text(json.dumps(creds, indent=2), "utf-8")
    return creds


# ------------------------------------------------------------------ client


class SwitchClient:
    def __init__(self, base: str, agent_id: str, api_key: str, connection_id: str):
        self.base = base
        self.agent_id = agent_id
        self.connection_id = connection_id
        self.auth = {"Authorization": f"Bearer {api_key}"}
        self.http = httpx.AsyncClient(base_url=base, headers=self.auth, timeout=60)
        self.cursor: int | None = None
        self.generation: Any = None
        self.rooms: list = []
        self.connected = asyncio.Event()
        self._room_lock = asyncio.Lock()
        self._active_room: str | None = None
        self.on_connect: Callable[[], Awaitable[None]] | None = None

    @classmethod
    def from_file(cls, base: str, path: Path) -> "SwitchClient":
        if not path.exists():
            raise SystemExit(f"{path.name} not found. Run `python -m gatekeeper register` first.")
        c = json.loads(path.read_text("utf-8"))
        return cls(base, c["agent_id"], c["api_key"], c["connection_id"])

    async def close(self) -> None:
        await self.http.aclose()

    # ---- HTTP
    async def post_message(self, room_id: str, content: str) -> None:
        r = await self.http.post(f"/agents/{self.agent_id}/message", json={"room_id": room_id, "content": content})
        if r.status_code >= 300:
            log.error("post_message %s -> %s %s", room_id, r.status_code, r.text[:300])
        r.raise_for_status()

    async def list_ops(self) -> dict:
        r = await self.http.get(f"/agents/{self.agent_id}/ops")
        r.raise_for_status()
        return r.json().get("operations", {})

    async def room_op(self, room_id: str, name: str, /, **args: Any) -> Any:
        """Ops like post_message, create_room_document and assume_role act on the
        session's connected room, so switch to `room_id` first (serialised)."""
        async with self._room_lock:
            if self._active_room != room_id:
                await self.op("connect_to_room", room_id=room_id, include_general_instructions=False)
                self._active_room = room_id
            return await self.op(name, **args)

    async def op(self, name: str, /, **args: Any) -> Any:
        r = await self.http.post(
            f"/agents/{self.agent_id}/ops/{name}",
            json=args,
            headers={"X-Switch-Connection-Id": self.connection_id},
        )
        if r.status_code >= 300:
            raise RuntimeError(f"op {name} -> {r.status_code}: {r.text[:300]}")
        return r.json().get("result")

    # ---- WebSocket
    def _ws_url(self, rooms: list[str]) -> str:
        # scope=all: hear every room the agent is in; filter=all: hear unaddressed messages too
        # (the maintainer's "approve 3" needn't @mention the Gatekeeper).
        q: dict[str, Any] = {"connection_id": self.connection_id, "scope": "all", "filter": "all", "client": "maintainer-room"}
        if self.cursor is not None:
            q["start_from"] = self.cursor
        if self.generation is not None:
            q["expected_generation"] = self.generation
        scheme = "wss" if self.base.startswith("https") else "ws"
        host = self.base.split("://", 1)[1]
        return f"{scheme}://{host}/agents/{self.agent_id}/connection/ws?{urlencode(q)}"

    async def listen(self, on_message: Callable[[Message], Awaitable[None]], rooms: list[str] | None = None) -> None:
        """Connect, keep the heartbeat, hand `message` events to a worker. Reconnects."""
        queue: asyncio.Queue[Message] = asyncio.Queue()

        async def worker() -> None:
            while True:
                msg = await queue.get()
                try:
                    await on_message(msg)
                except Exception:  # never let one bad message kill the agent
                    log.exception("handling message failed")

        worker_task = asyncio.create_task(worker())
        delay = 1
        try:
            while True:
                try:
                    stop = await self._session(queue, rooms or [])
                    if stop:
                        return
                    delay = 1
                except (OSError, websockets.ConnectionClosed, websockets.InvalidStatus) as e:
                    log.warning("connection dropped (%s); reconnecting in %ss", e, delay)
                self.connected.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        finally:
            worker_task.cancel()

    async def _session(self, queue: asyncio.Queue, rooms: list[str]) -> bool:
        """One socket's life. Returns True when we must not reconnect."""
        async with connect(self._ws_url(rooms), additional_headers=self.auth, ping_interval=None) as ws:
            async for raw in ws:
                frame = json.loads(raw)
                kind = frame.get("event") or frame.get("type")
                data = frame_data(frame)
                log.debug("frame %s", frame)

                if kind == "ping":  # answer first, before any work
                    await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
                    continue
                if kind == "connection_state":
                    self.generation = data.get("generation", self.generation)
                    self.cursor = data.get("cursor", self.cursor)
                    self.rooms = data.get("rooms", self.rooms)
                    self._active_room = None  # a new socket starts without a connected room
                    self.connected.set()
                    if self.on_connect:  # e.g. re-take the role lease a lapse released
                        asyncio.create_task(self.on_connect())
                    log.info("connected: rooms=%s cursor=%s generation=%s", self.rooms, self.cursor, self.generation)
                    continue
                if kind == "gap":
                    self.cursor = data.get("resumed_at", self.cursor)
                    log.warning("gap in events (%s); resumed at %s", data.get("reason"), self.cursor)
                    continue
                if kind == "evicted":
                    log.warning("evicted: %s %s", data.get("code"), data.get("reason"))
                    return data.get("code") == "taken_over"
                if kind == "refused":
                    log.warning("refused: %s %s", data.get("status"), str(data.get("detail"))[:120])
                    self.generation = None  # a stale generation is the usual cause; retry without it
                    continue
                if kind == "subscription_changed":
                    self.rooms = data.get("rooms", self.rooms)
                    continue

                seq = frame.get("id")
                if isinstance(seq, int):
                    self.cursor = max(self.cursor or 0, seq)
                if kind == "message":
                    queue.put_nowait(extract_message(frame))
        return False
