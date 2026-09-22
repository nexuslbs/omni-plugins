#!/usr/bin/env python3
"""Fake published-event bus: a stdlib HTTP server implementing the omniagent
`/events/*` contract, with pluggable LISTENER callbacks so a test can reproduce
fan-out/delivery without the real core.

Contract reproduced (see omniagent src/server/events.rs):
  POST /events/publish {event, correlation_id?, payload}
      -> {event, correlation_id, outcome, listeners:[{listener,action,tool,status,detail,duration_ms}], delivered}
  POST /events/wait {correlation_id, timeout_s}
      -> {state: solved|aborted|timeout|pending, correlation_id, event, payload, elapsed_ms}
  GET  /events/{correlation_id} -> {interaction: {...}}
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCore:
    """A fake core whose listener list is a list of (listener_key, callable).
    Each callable receives the event dict and returns (status, detail)."""

    def __init__(self, listeners=None):
        self.listeners = list(listeners or [])
        self.published: list[dict] = []
        self.interactions: dict[str, dict] = {}
        self.terminal: dict[str, dict] = {}
        self.server = None
        self.thread = None
        self.url = ""

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        handler = self._handler_class()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()

    # ── event API ──────────────────────────────────────────────────────────
    def publish(self, event: str, correlation_id: str | None, payload: dict) -> dict:
        correlation_id = correlation_id or str(uuid.uuid4())
        deliveries = []
        for key, fn in self.listeners:
            started = time.time()
            try:
                status, detail = fn({"event": event, "correlation_id": correlation_id,
                                     "payload": payload})
            except Exception as exc:  # listener failures are isolated
                status, detail = "error", str(exc)
            deliveries.append({"listener": key, "action": key, "tool": f"{key}__notify",
                               "status": status, "detail": detail,
                               "duration_ms": int((time.time() - started) * 1000)})
        delivered = sum(1 for d in deliveries if d["status"] == "ok")
        row = self.interactions.setdefault(correlation_id, {
            "correlation_id": correlation_id, "event": event, "payload": payload,
            "deliveries": [], "outcome": None, "outcome_event": None, "outcome_payload": None,
        })
        row["deliveries"].extend(deliveries)
        if event.endswith(("-resolved", "-aborted", "-timeout")):
            suffix = event.rsplit("-", 1)[1]
            # The core names the SOLVED terminal event `-resolved`, while the
            # state it reports from /events/wait is `solved` (mirrors
            # omniagent src/events.rs::outcome_for_event).
            outcome = "solved" if suffix == "resolved" else suffix
            row["outcome"] = outcome
            row["outcome_event"] = event
            row["outcome_payload"] = payload
            self.terminal[correlation_id] = {"outcome": outcome, "event": event, "payload": payload}
        record = {"event": event, "correlation_id": correlation_id, "payload": payload,
                  "listeners": deliveries, "delivered": delivered}
        self.published.append(record)
        return record

    def force_terminal(self, correlation_id: str, outcome: str, payload: dict | None = None):
        event = f"solve-captcha-{'resolved' if outcome == 'solved' else outcome}"
        self.terminal[correlation_id] = {"outcome": outcome, "event": event,
                                         "payload": payload or {}}
        row = self.interactions.setdefault(correlation_id, {"correlation_id": correlation_id})
        row.update({"outcome": outcome, "outcome_event": event, "outcome_payload": payload or {}})

    def wait(self, correlation_id: str, timeout_s: int) -> dict:
        term = self.terminal.get(correlation_id)
        if term is None:
            return {"state": "pending", "correlation_id": correlation_id, "event": None,
                    "payload": None, "elapsed_ms": 0}
        return {"state": term["outcome"], "correlation_id": correlation_id,
                "event": term["event"], "payload": term["payload"], "elapsed_ms": 1}

    # ── HTTP plumbing ──────────────────────────────────────────────────────
    def _handler_class(self):
        core = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # keep the test output quiet
                pass

            def _json(self, code: int, body: dict):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    return {}

            def do_POST(self):
                body = self._read_body()
                if self.path == "/events/publish":
                    record = core.publish(body.get("event", ""), body.get("correlation_id"),
                                          body.get("payload") or {})
                    self._json(200, {"event": record["event"],
                                     "correlation_id": record["correlation_id"],
                                     "outcome": None, "listeners": record["listeners"],
                                     "delivered": record["delivered"]})
                elif self.path == "/events/wait":
                    self._json(200, core.wait(body.get("correlation_id", ""),
                                              int(body.get("timeout_s") or 60)))
                else:
                    self._json(404, {"error": "unknown path", "path": self.path})

            def do_GET(self):
                if self.path.startswith("/events/"):
                    correlation_id = self.path[len("/events/"):]
                    row = core.interactions.get(correlation_id)
                    if row is None:
                        self._json(404, {"error": "unknown correlation_id"})
                    else:
                        self._json(200, {"interaction": row})
                else:
                    self._json(404, {"error": "unknown path", "path": self.path})

        return Handler
