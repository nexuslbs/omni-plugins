#!/usr/bin/env python3
"""echo-notify MCP server: a NON-telegram DELIVERY LISTENER for `solve-captcha`.

It is the second listener implementation used to prove the operator's
requirement: several listeners may subscribe to the SAME event, each handles it
independently, and adding/removing/replacing a listener changes NO solver code.

Behaviour: append one JSONL row per received event (correlation id, payload,
received_at) to ECHO_NOTIFY_LOG and echo a one-line summary on stderr. With
ECHO_NOTIFY_MODE=fail it returns an error result instead - the event bus
isolates that failure (the solver and the other listeners keep working).

Tool: echo_notify(event) - the event bus injects the published event.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "echo-notify"
SERVER_VERSION = "0.1.0"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def log_path() -> Path:
    return Path(_env("ECHO_NOTIFY_LOG", "/opt/omni/data/human-intervention/notifications.jsonl")
                or "/opt/omni/data/human-intervention/notifications.jsonl")


def mode() -> str:
    return (_env("ECHO_NOTIFY_MODE", "log") or "log").lower()


def as_event(args: dict) -> dict:
    raw = args.get("event")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def handle_echo_notify(args: dict) -> dict:
    event = as_event(args)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    row = {
        "listener": SERVER_NAME,
        "event": event.get("event"),
        "correlation_id": event.get("correlation_id"),
        "session_id": (payload or {}).get("session_id"),
        "url": (payload or {}).get("url"),
        "reason": (payload or {}).get("reason"),
        "received_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if mode() == "fail":
        return {"ok": False, "status": "error", "listener": SERVER_NAME,
                "reason": "ECHO_NOTIFY_MODE=fail (intentional listener failure)"}
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        return {"ok": False, "status": "error", "listener": SERVER_NAME,
                "reason": f"cannot write {path}: {exc}"}
    sys.stderr.write(f"[{SERVER_NAME}] notify {row['correlation_id']} session={row['session_id']}\n")
    sys.stderr.flush()
    return {"ok": True, "status": "delivered", "listener": SERVER_NAME,
            "log": str(path), "row": row}


TOOLS = [
    {
        "name": "echo_notify",
        "description": (
            "LISTENER action for the `solve-captcha` event: append the hand-off request to a "
            "local JSONL log. Non-telegram subscriber used as the second listener; bind it to "
            "the event through a tasks.yml hook (mode action)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "object", "description": "the published event (injected by the event bus)."},
            },
        },
    },
]

HANDLERS = {"echo_notify": handle_echo_notify}


# ── MCP stdio plumbing (same shape as the other omni-plugins MCP servers) ───


def send_json(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_initialize(req: dict) -> dict:
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def handle_tools_call(msg: dict) -> dict:
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in HANDLERS:
        return make_error(rid, -32601, f"Unknown tool: {name}")
    try:
        result = HANDLERS[name](args)
    except Exception as exc:
        result = {"ok": False, "status": "error", "error": f"{name} error: {exc}"}
    is_error = not result.get("ok", True)
    return make_success(rid, make_tool_result(json.dumps(result, indent=2, sort_keys=True), is_error))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        if method == "initialize":
            send_json(handle_initialize(msg))
        elif method is None or str(method).startswith("notifications/"):
            continue
        elif method == "tools/list":
            send_json(make_success(msg.get("id"), {"tools": TOOLS}))
        elif method == "tools/call":
            send_json(handle_tools_call(msg))
        else:
            send_json(make_error(msg.get("id"), -32601, f"Unknown method: {method}"))


if __name__ == "__main__":
    main()
