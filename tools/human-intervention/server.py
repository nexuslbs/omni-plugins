#!/usr/bin/env python3
"""human-intervention MCP server: the SOLVER-SIDE half of the solve-captcha hand-off.

Responsibility split (operator requirement, thread 2794):

  * the SOLVER (this plugin: the code a browser-driving flow calls when it hits a
    challenge it must not solve itself) PUBLISHES the canonical event
    `solve-captcha` onto the omniagent published-event bus, with a BOUNDED payload
    (session_id, url, reason, access hint, timeout, correlation id), then WAITS
    for the interaction outcome and DETECTS that the page left the challenged
    state so the parked flow can resume.
  * the LISTENERS (separate plugins, e.g. tools/echo-notify) subscribe to the
    event and do the DELIVERY. Several listeners may be bound to the same event
    and each handles it independently.

Channel agnosticism is a hard rule: this file contains ZERO delivery code - no
messaging integration, no HTTP send call, no credential, no recipient id.
Delivery is the listener's job, exactly like a mail handler or a log handler.

Tools:
  - intervention_request : guardrails + publish `solve-captcha`, return a handle
  - intervention_wait    : bounded wait -> solved | aborted | timeout (+ resume marker)
  - intervention_detect  : generic page-state poll -> publish the terminal
                           `solve-captcha-resolved` event when the challenge is gone
  - intervention_probe   : ONE generic structural page-state snapshot (CDP)
  - intervention_status  : describe one interaction + local guardrail state

Configuration (plugin config_schema, resolved by the core): OMNI_API_URL,
HI_STATE_DIR, HI_COOLDOWN_S, HI_MAX_HANDOFFS, HI_DEFAULT_TIMEOUT_S, HI_CDP_URL.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cdp  # noqa: E402  (same-directory module, no install needed)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "human-intervention"
SERVER_VERSION = "0.1.0"

# Canonical event contract (must stay in sync with omniagent src/events.rs).
EVENT_SOLVE = "solve-captcha"
OUTCOME_SOLVED = "solved"
OUTCOME_ABORTED = "aborted"
OUTCOME_TIMEOUT = "timeout"
PEER_ABORT_WORDS = ("abort", "cancel", "stop", "no", "nevermind", "never mind")

RESUME_MARKER = "human-intervention: solved"
ABORT_MARKER = "human-intervention: aborted"
TIMEOUT_MARKER = "human-intervention: timeout"

STATE_LOCK = threading.Lock()


# ── config ──────────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def api_base() -> str:
    return (_env("OMNI_API_URL", "http://127.0.0.1:8080") or "http://127.0.0.1:8080").rstrip("/")


def state_dir() -> Path:
    path = Path(_env("HI_STATE_DIR", "/opt/omni/data/human-intervention")
                or "/opt/omni/data/human-intervention")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cooldown_s() -> int:
    return _env_int("HI_COOLDOWN_S", 600)


def max_handoffs() -> int:
    return _env_int("HI_MAX_HANDOFFS", 3)


def default_timeout_s() -> int:
    return _env_int("HI_DEFAULT_TIMEOUT_S", 900)


def cdp_url() -> str:
    return _env("HI_CDP_URL", "http://browser:9222") or "http://browser:9222"


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), tz=timezone.utc) \
        .isoformat().replace("+00:00", "Z")


# ── HTTP (published-event bus) ──────────────────────────────────────────────


def _post_json(path: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base()}{path}", data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # surface the API's error body
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"POST {path} -> HTTP {exc.code}: {detail}") from exc
    return json.loads(raw.decode("utf-8") or "{}")


def _get_json(path: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{api_base()}{path}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GET {path} -> HTTP {exc.code}: {detail}") from exc
    return json.loads(raw.decode("utf-8") or "{}")


def publish_event(event: str, correlation_id: str | None, payload: dict) -> dict:
    body = {"event": event, "payload": payload}
    if correlation_id:
        body["correlation_id"] = correlation_id
    return _post_json("/events/publish", body, timeout=30.0)


def wait_event(correlation_id: str, timeout_s: int) -> dict:
    return _post_json("/events/wait",
                      {"correlation_id": correlation_id, "timeout_s": timeout_s},
                      timeout=float(timeout_s) + 15.0)


def describe_event(correlation_id: str) -> dict | None:
    try:
        body = _get_json(f"/events/{urllib_request_quote(correlation_id)}", timeout=15.0)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    return body.get("interaction")


def urllib_request_quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


# ── guardrails (local state: one hand-off per session per cooldown, hard cap) ─


def _state_path() -> Path:
    return state_dir() / "guardrails.json"


def _audit_path() -> Path:
    return state_dir() / "hand-offs.jsonl"


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {"sessions": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"sessions": {}}
    if not isinstance(data, dict):
        return {"sessions": {}}
    data.setdefault("sessions", {})
    return data


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def _audit(entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("at", _iso())
    with _audit_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def guardrail_check(session_id: str, now: float | None = None) -> dict:
    """Return the guardrail verdict for one hand-off in `session_id`."""
    now = now if now is not None else _now()
    with STATE_LOCK:
        state = _load_state()
        row = (state.get("sessions") or {}).get(session_id) or {}
        count = int(row.get("count") or 0)
        last = float(row.get("last_ts") or 0.0)
    if count >= max_handoffs():
        return {"allowed": False, "status": "capped",
                "detail": f"hard cap of {max_handoffs()} hand-offs reached for this session",
                "handoffs": count}
    remaining = cooldown_s() - (now - last)
    if last and remaining > 0:
        return {"allowed": False, "status": "cooldown",
                "detail": "one hand-off per session per cooldown window",
                "retry_after_s": int(remaining) + 1, "handoffs": count}
    return {"allowed": True, "status": "pending", "handoffs": count}


def guardrail_record(session_id: str) -> None:
    now = _now()
    with STATE_LOCK:
        state = _load_state()
        sessions = state.setdefault("sessions", {})
        row = sessions.setdefault(session_id, {})
        row["count"] = int(row.get("count") or 0) + 1
        row["last_ts"] = now
        sessions[session_id] = row
        _save_state(state)


def guardrail_state() -> dict:
    with STATE_LOCK:
        state = _load_state()
    return {"cooldown_s": cooldown_s(), "max_handoffs": max_handoffs(),
            "sessions": state.get("sessions") or {}}


# ── generic page-state probe (structural, site-knowledge-free) ──────────────

# NO page-text classification and NO vendor vocabulary: only STRUCTURE
# (viewport-covering overlay, large embedded frame, document identity/readiness).
SNAPSHOT_JS = """
(() => {
  const vw = Math.max(1, window.innerWidth);
  const vh = Math.max(1, window.innerHeight);
  const area = vw * vh;
  const shown = (el) => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const big = (el, frac) => {
    const r = el.getBoundingClientRect();
    return r.width * r.height >= frac * area;
  };
  let overlay_count = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!shown(el) || !big(el, 0.2)) continue;
    const s = getComputedStyle(el);
    if (s.position === 'fixed' || s.position === 'absolute') overlay_count += 1;
  }
  let large_frame_count = 0;
  for (const el of document.querySelectorAll('iframe')) {
    if (shown(el) && big(el, 0.2)) large_frame_count += 1;
  }
  let text_len = 0;
  try { text_len = document.body ? document.body.innerText.length : 0; } catch (e) {}
  return {
    url: location.href,
    title: document.title,
    ready: document.readyState,
    overlay_count: overlay_count,
    large_frame_count: large_frame_count,
    form_count: document.forms.length,
    text_len: text_len,
  };
})()
""".strip()


def page_snapshot(cdp_base: str | None = None, target_url: str | None = None,
                  timeout: float = 15.0) -> dict:
    """ONE structural snapshot of the page the agent drives."""
    snap = cdp.evaluate(SNAPSHOT_JS, url=cdp_base or cdp_url(),
                        target_url=target_url, timeout=timeout)
    if not isinstance(snap, dict):
        raise cdp.CdpError(f"page returned a non-object snapshot: {snap!r}")
    return snap


def is_blocked(snapshot: dict) -> bool:
    """A page is in the CHALLENGED state when a viewport-covering overlay or a
    large embedded frame is present (structure only, no page text)."""
    if not snapshot:
        return False
    return bool(snapshot.get("overlay_count", 0)) or bool(snapshot.get("large_frame_count", 0))


def document_identity(snapshot: dict) -> str:
    return "|".join(str(snapshot.get(k, "")) for k in ("url", "title", "ready"))


def evaluate_resolution(before: dict, after: dict, target_url: str | None = None) -> tuple[bool, str]:
    """Generic resolution predicate: the challenged state ended AND the original
    document is there again with its own content."""
    if is_blocked(after):
        return False, "page still shows a blocking overlay/frame"
    if str(after.get("ready")) not in ("interactive", "complete"):
        return False, f"document not loaded yet (readyState={after.get('ready')})"
    same_document = bool(target_url) and after.get("url") == target_url
    same_document = same_document or after.get("url") == before.get("url")
    if not same_document:
        return False, "the top document is not the original target"
    has_content = bool(after.get("text_len", 0)) or bool(after.get("form_count", 0))
    if not has_content:
        return False, "no page content present yet"
    changed = document_identity(after) != document_identity(before)
    if changed:
        return True, "blocking overlay/frame gone and the document state changed"
    return True, "blocking overlay/frame gone on the original document"


# ── tool handlers ───────────────────────────────────────────────────────────


def handle_intervention_request(args: dict) -> dict:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        return {"status": "error", "error": "session_id is required"}
    timeout_s = int(args.get("timeout_s") or default_timeout_s())
    verdict = guardrail_check(session_id)
    if not verdict["allowed"]:
        _audit({"tool": "intervention_request", "session_id": session_id,
                "status": verdict["status"], "detail": verdict["detail"]})
        return {
            "status": verdict["status"],
            "detail": verdict["detail"],
            "session_id": session_id,
            "handoffs": verdict.get("handoffs"),
            "retry_after_s": verdict.get("retry_after_s"),
        }
    payload = {
        "session_id": session_id,
        "url": args.get("url") or "",
        "reason": args.get("reason") or "a browser challenge needs a human",
        "access_hint": args.get("access_hint") or "",
        "timeout_s": timeout_s,
        "requested_at": _iso(),
    }
    try:
        published = publish_event(EVENT_SOLVE, args.get("correlation_id"), payload)
    except Exception as exc:  # transport failure: no hand-off happened
        _audit({"tool": "intervention_request", "session_id": session_id,
                "status": "error", "detail": str(exc)})
        return {"status": "error", "error": f"could not publish {EVENT_SOLVE}: {exc}",
                "session_id": session_id}
    delivered = int(published.get("delivered") or 0)
    listeners = [
        {"listener": d.get("listener"), "tool": d.get("tool"), "status": d.get("status")}
        for d in (published.get("listeners") or [])
    ]
    if delivered == 0:
        # Nobody handled it (e.g. no operator chat id configured on any
        # listener): report it, do NOT fail the whole flow.
        _audit({"tool": "intervention_request", "session_id": session_id,
                "status": "unavailable", "correlation_id": published.get("correlation_id"),
                "listeners": listeners})
        return {"status": "unavailable",
                "detail": "no listener delivered the event (no operator channel configured)",
                "session_id": session_id,
                "correlation_id": published.get("correlation_id"),
                "event": EVENT_SOLVE, "listeners": listeners, "delivered": delivered}
    guardrail_record(session_id)
    _audit({"tool": "intervention_request", "session_id": session_id, "status": "pending",
            "correlation_id": published.get("correlation_id"), "listeners": listeners})
    return {"status": "pending", "session_id": session_id,
            "correlation_id": published.get("correlation_id"), "event": EVENT_SOLVE,
            "timeout_s": timeout_s, "delivered": delivered, "listeners": listeners,
            "wait_with": "intervention_wait"}


def handle_intervention_wait(args: dict) -> dict:
    correlation_id = str(args.get("correlation_id") or "").strip()
    if not correlation_id:
        return {"status": "error", "error": "correlation_id is required"}
    try:
        bound = int(args.get("timeout_s") or default_timeout_s())
    except (TypeError, ValueError):
        bound = default_timeout_s()
    bound = max(1, bound)
    deadline = _now() + bound
    started = _now()
    outcome: dict = {}
    while True:
        remaining = max(1, int(deadline - _now()))
        chunk = min(60, remaining)
        try:
            outcome = wait_event(correlation_id, chunk)
        except Exception as exc:
            return {"status": "error", "error": f"wait failed: {exc}",
                    "correlation_id": correlation_id}
        state = str(outcome.get("state") or "pending")
        if state != "pending":
            break
        if _now() >= deadline:
            break
    state = str(outcome.get("state") or "pending")
    if state == "pending":
        state = OUTCOME_TIMEOUT
    result = {
        "status": state,
        "correlation_id": correlation_id,
        "event": outcome.get("event"),
        "payload": outcome.get("payload"),
        "elapsed_ms": int((_now() - started) * 1000),
        "bounded_by_s": bound,
    }
    if state == OUTCOME_SOLVED:
        result["marker"] = RESUME_MARKER
        result["resume"] = {
            "session_id": (outcome.get("payload") or {}).get("session_id"),
            "url": (outcome.get("payload") or {}).get("url"),
            "next_step": "continue",
            "note": "the browser session is unchanged: re-run the parked step",
        }
    elif state == OUTCOME_ABORTED:
        result["marker"] = ABORT_MARKER
    else:
        result["marker"] = TIMEOUT_MARKER
        result["session_left_usable"] = True
    _audit({"tool": "intervention_wait", "correlation_id": correlation_id,
            "status": state, "elapsed_ms": result["elapsed_ms"]})
    return result


def handle_intervention_detect(args: dict) -> dict:
    correlation_id = str(args.get("correlation_id") or "").strip()
    if not correlation_id:
        return {"status": "error", "error": "correlation_id is required"}
    cdp_base = args.get("cdp_url") or cdp_url()
    target_url = args.get("target_url") or None
    try:
        bound = int(args.get("timeout_s") or default_timeout_s())
        poll_s = max(0.2, float(args.get("poll_s") or 2))
    except (TypeError, ValueError):
        bound, poll_s = default_timeout_s(), 2.0
    before = args.get("before") if isinstance(args.get("before"), dict) else None
    try:
        if not before:
            before = page_snapshot(cdp_base, target_url)
        deadline = _now() + bound
        after = before
        reason = "not evaluated"
        while True:
            after = page_snapshot(cdp_base, target_url)
            solved, reason = evaluate_resolution(before, after, target_url)
            if solved:
                break
            if _now() >= deadline:
                break
            time.sleep(poll_s)
    except Exception as exc:
        return {"status": "error", "error": f"page-state probe failed: {exc}",
                "correlation_id": correlation_id}
    if solved:
        payload = {"session_id": (args.get("session_id") or ""),
                   "url": after.get("url"), "reason": reason,
                   "before": before, "after": after}
        try:
            published = publish_event(f"{EVENT_SOLVE}-resolved", correlation_id, payload)
        except Exception as exc:
            return {"status": "error",
                    "error": f"page resolved but the terminal event failed: {exc}",
                    "correlation_id": correlation_id, "before": before, "after": after}
        _audit({"tool": "intervention_detect", "correlation_id": correlation_id,
                "status": OUTCOME_SOLVED, "detail": reason})
        return {"status": OUTCOME_SOLVED, "correlation_id": correlation_id,
                "marker": RESUME_MARKER, "detail": reason,
                "before": before, "after": after,
                "terminal_event": f"{EVENT_SOLVE}-resolved",
                "listeners": [{"listener": d.get("listener"), "status": d.get("status")}
                              for d in (published.get("listeners") or [])]}
    state = OUTCOME_TIMEOUT if _now() >= deadline else "pending"
    return {"status": state, "correlation_id": correlation_id, "detail": reason,
            "before": before, "after": after}


def handle_intervention_probe(args: dict) -> dict:
    cdp_base = args.get("cdp_url") or cdp_url()
    target_url = args.get("target_url") or None
    try:
        snap = page_snapshot(cdp_base, target_url)
    except Exception as exc:
        return {"status": "error", "error": f"page-state probe failed: {exc}",
                "cdp_url": cdp_base}
    return {"status": "ok", "cdp_url": cdp_base, "blocked": is_blocked(snap),
            "document_identity": document_identity(snap), "snapshot": snap}


def handle_intervention_status(args: dict) -> dict:
    correlation_id = str(args.get("correlation_id") or "").strip()
    result = {"guardrails": guardrail_state()}
    if correlation_id:
        try:
            result["interaction"] = describe_event(correlation_id)
        except Exception as exc:
            return {"status": "error", "error": f"describe failed: {exc}",
                    "correlation_id": correlation_id}
    result["status"] = "ok"
    return result


TOOLS = [
    {
        "name": "intervention_request",
        "description": (
            "Publish the canonical `solve-captcha` event asking a human to act in the "
            "browser session (channel agnostic: no messaging call here, delivery is a "
            "listener's job) and return a correlation handle. Applies the per-session "
            "cooldown and hard cap; returns status `unavailable` when no listener "
            "delivered the event (e.g. no operator channel configured)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "browser session id (required)."},
                "url": {"type": "string", "description": "URL/screen the human should look at."},
                "reason": {"type": "string", "description": "short reason the flow parked."},
                "access_hint": {"type": "string", "description": "how to reach the session (noVNC link/port)."},
                "timeout_s": {"type": "integer", "description": "interaction deadline in seconds (default 900)."},
                "correlation_id": {"type": "string", "description": "optional caller-supplied correlation id."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "intervention_wait",
        "description": (
            "Bounded wait for the human-intervention outcome of one correlation id: "
            "returns solved | aborted | timeout (never hangs). On `solved` the result "
            "carries the `human-intervention: solved` marker plus the resume context so "
            "the parked flow continues from the same step against the same session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "handle from intervention_request (required)."},
                "timeout_s": {"type": "integer", "description": "bound for this wait (default 900)."},
            },
            "required": ["correlation_id"],
        },
    },
    {
        "name": "intervention_detect",
        "description": (
            "Generic page-state detection: poll the session's structural state until the "
            "blocking overlay/large frame is gone and the original document is back with "
            "its own content, then publish the terminal `solve-captcha-resolved` event for "
            "the same correlation id. Structure only, no page-text or vendor knowledge."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "handle from intervention_request (required)."},
                "session_id": {"type": "string", "description": "session id (carried into the terminal payload)."},
                "cdp_url": {"type": "string", "description": "CDP base URL (default from config)."},
                "target_url": {"type": "string", "description": "the original target URL, when known."},
                "timeout_s": {"type": "integer", "description": "how long to watch (default 900)."},
                "poll_s": {"type": "number", "description": "poll interval in seconds (default 2)."},
                "before": {"type": "object", "description": "optional snapshot taken before the hand-off."},
            },
            "required": ["correlation_id"],
        },
    },
    {
        "name": "intervention_probe",
        "description": "ONE generic structural snapshot of the page the agent drives (blocking overlay/large frame counts, document identity, readiness).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cdp_url": {"type": "string", "description": "CDP base URL (default from config)."},
                "target_url": {"type": "string", "description": "prefer this page target URL."},
            },
        },
    },
    {
        "name": "intervention_status",
        "description": "Describe one interaction (by correlation id) plus the local guardrail state (cooldown, per-session hand-off counts).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "optional handle to describe."},
            },
        },
    },
]

HANDLERS = {
    "intervention_request": handle_intervention_request,
    "intervention_wait": handle_intervention_wait,
    "intervention_detect": handle_intervention_detect,
    "intervention_probe": handle_intervention_probe,
    "intervention_status": handle_intervention_status,
}


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


def handle_tools_list(req: dict) -> dict:
    return make_success(req.get("id"), {"tools": TOOLS})


def handle_tools_call(msg: dict) -> dict:
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in HANDLERS:
        return make_error(rid, -32601, f"Unknown tool: {name}")
    try:
        result = HANDLERS[name](args)
    except Exception as exc:  # never crash the server on one bad call
        result = {"status": "error", "error": f"{name} error: {exc}"}
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
        elif method in ("notifications/initialized", None) or str(method).startswith("notifications/"):
            continue
        elif method == "tools/list":
            send_json(handle_tools_list(msg))
        elif method == "tools/call":
            send_json(handle_tools_call(msg))
        else:
            send_json(make_error(msg.get("id"), -32601, f"Unknown method: {method}"))


if __name__ == "__main__":
    main()
