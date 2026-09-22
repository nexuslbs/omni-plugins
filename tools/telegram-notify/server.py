#!/usr/bin/env python3
"""telegram-notify MCP server: a DELIVERY LISTENER for the `solve-captcha` event.

Architecture (operator requirement, telegram thread 2794): the solver PUBLISHES
the canonical event `solve-captcha`; DELIVERY is a separate subscriber. This
plugin is ONE such subscriber. It contains no solver code, no challenge
detection and no page knowledge - it only renders and sends a message.

What it does: for one `solve-captcha` event it makes exactly ONE standalone
Telegram Bot API `sendMessage` call to the configured operator chat id. The
message is NOT associated with a thread, a cause or a kanban task: it is a plain
sendMessage, so the platform stores its own standalone message row.

Message body (the first two lines are EXACT and verbatim; optional context
follows after a blank line):

    Human intervention required
    [Open browser session]

    session: <session id>
    url: <url>
    reason: <reason>
    browser: <access hint (noVNC link)>
    correlation: <correlation id>
    reply: any reply in this chat signals that the human acted (reply "abort" to stop the flow)

No secret ever appears in the message.

Config (plugin config_schema): TELEGRAM_BOT_TOKEN ($secret:TELEGRAM_TOKEN),
TELEGRAM_CHAT_ID, TELEGRAM_API_BASE, TELEGRAM_NOTIFY_TIMEOUT_S.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "telegram-notify"
SERVER_VERSION = "0.1.0"

# The two verbatim lines the operator asked for. Never reformatted, never
# markdown-decorated (the send uses no parse_mode so they stay literal text).
HEADER_LINES = ("Human intervention required", "[Open browser session]")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def bot_token() -> str:
    return _env("TELEGRAM_BOT_TOKEN")


def chat_id() -> str:
    return _env("TELEGRAM_CHAT_ID")


def api_base() -> str:
    return (_env("TELEGRAM_API_BASE", "https://api.telegram.org")
            or "https://api.telegram.org").rstrip("/")


def send_timeout() -> float:
    try:
        value = float(_env("TELEGRAM_NOTIFY_TIMEOUT_S", "20") or 20)
    except ValueError:
        value = 20.0
    return max(1.0, value)


def as_event(args: dict) -> dict:
    """The core merges the published event under the `event` key (object or JSON
    string). Accept both so the listener works whatever the transport does."""
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


def build_text(event: dict) -> str:
    """EXACT two-line body first, blank line, then optional bounded context."""
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    lines = [HEADER_LINES[0], HEADER_LINES[1], ""]
    session_id = str(payload.get("session_id") or "").strip()
    url = str(payload.get("url") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    hint = str(payload.get("access_hint") or "").strip()
    correlation_id = str(event.get("correlation_id") or "").strip()
    timeout_s = payload.get("timeout_s")
    if session_id:
        lines.append(f"session: {session_id}")
    if url:
        lines.append(f"url: {url}")
    if reason:
        lines.append(f"reason: {reason}")
    if hint:
        lines.append(f"browser: {hint}")
    if timeout_s:
        lines.append(f"timeout_s: {timeout_s}")
    if correlation_id:
        lines.append(f"correlation: {correlation_id}")
    lines.append('reply: any reply in this chat signals that the human acted '
                 '(reply "abort" to stop the flow)')
    return "\n".join(lines).strip() + "\n"


def send_message(text: str) -> dict:
    """ONE standalone Bot API sendMessage. Raises on transport/API failure."""
    token = bot_token()
    chat = chat_id()
    body = json.dumps({"chat_id": chat, "text": text,
                       "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base()}/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=send_timeout()) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"sendMessage -> HTTP {exc.code}: {detail}") from exc
    payload = json.loads(raw.decode("utf-8") or "{}")
    if not payload.get("ok"):
        raise RuntimeError(f"sendMessage failed: {payload.get('description', payload)}")
    return payload


def handle_telegram_notify(args: dict) -> dict:
    event = as_event(args)
    text = build_text(event)
    if args.get("dry_run"):
        return {"ok": True, "dry_run": True, "text": text,
                "first_two_lines_verbatim": text.split("\n")[:2] == list(HEADER_LINES)}
    missing = []
    if not bot_token():
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id():
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        return {"ok": False, "status": "unavailable",
                "reason": f"not configured: {', '.join(missing)}",
                "listener": SERVER_NAME}
    try:
        payload = send_message(text)
    except Exception as exc:
        return {"ok": False, "status": "error", "reason": str(exc),
                "listener": SERVER_NAME}
    result = payload.get("result") or {}
    return {
        "ok": True,
        "listener": SERVER_NAME,
        "standalone": True,
        "thread_associated": False,
        "chat_id": (result.get("chat") or {}).get("id", chat_id()),
        "message_id": result.get("message_id"),
        "correlation_id": event.get("correlation_id"),
        "text": text,
    }


TOOLS = [
    {
        "name": "telegram_notify",
        "description": (
            "LISTENER action for the `solve-captcha` event: send ONE standalone Telegram "
            "message (Bot API sendMessage, no thread/cause) to the operator chat. Bind it "
            "to the event through a tasks.yml hook (mode action); nothing in the solver "
            "knows this listener exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "object", "description": "the published event (injected by the event bus)."},
                "dry_run": {"type": "boolean", "description": "build the message but do not send it."},
            },
        },
    },
]

HANDLERS = {"telegram_notify": handle_telegram_notify}


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
    return make_success(rid, make_tool_result(json.dumps(result, indent=2, sort_keys=True)))


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
