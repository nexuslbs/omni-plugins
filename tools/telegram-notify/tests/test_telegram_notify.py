#!/usr/bin/env python3
"""Tests for the telegram-notify LISTENER plugin.

Run:  python3 tools/telegram-notify/tests/test_telegram_notify.py

Covered: the EXACT two-line body, ONE standalone Bot API sendMessage call (no
thread/cause), the `unavailable` result when the operator chat id / token is not
configured, and the MCP stdio surface (tools/list + tools/call).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
TOOLS_DIR = PLUGIN_DIR.parent
sys.path.insert(0, str(TOOLS_DIR / "human-intervention" / "tests"))

from mcp_client import McpServer  # noqa: E402

EXPECTED_HEADER = ["Human intervention required", "[Open browser session]"]


def load_server(name="tg_server"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server()

EVENT = {
    "event": "solve-captcha",
    "correlation_id": "corr-42",
    "payload": {
        "session_id": "sess-1",
        "url": "https://example.test/checkout",
        "reason": "a blocking challenge overlay needs a human",
        "access_hint": "noVNC http://127.0.0.1:8080/vnc.html",
        "timeout_s": 900,
    },
}


class FakeBotApi:
    """Records sendMessage calls; answers like the Bot API."""

    def __init__(self, ok=True):
        self.calls: list[dict] = []
        self.ok = ok
        self.server = None
        self.url = ""
        self.thread = None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                fake.calls.append({"path": self.path, "body": body,
                                   "content_type": self.headers.get("Content-Type")})
                if fake.ok:
                    reply = {"ok": True, "result": {"message_id": 4242,
                                                    "chat": {"id": body.get("chat_id")}}}
                else:
                    reply = {"ok": False, "description": "chat not found"}
                raw = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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


class BuildTextTest(unittest.TestCase):
    def test_header_is_verbatim_and_context_follows_after_a_blank_line(self):
        text = SERVER.build_text(EVENT)
        lines = text.split("\n")
        self.assertEqual(lines[0:2], EXPECTED_HEADER)
        self.assertEqual(lines[2], "")
        self.assertIn("session: sess-1", text)
        self.assertIn("url: https://example.test/checkout", text)
        self.assertIn("correlation: corr-42", text)

    def test_header_survives_an_empty_event(self):
        lines = SERVER.build_text({}).split("\n")
        self.assertEqual(lines[0:2], EXPECTED_HEADER)

    def test_event_accepts_the_json_string_form(self):
        event = SERVER.as_event({"event": json.dumps(EVENT)})
        self.assertEqual(event["correlation_id"], "corr-42")


class SendTest(unittest.TestCase):
    def setUp(self):
        self.env = {"TELEGRAM_BOT_TOKEN": "123:fake", "TELEGRAM_CHAT_ID": "8969054376"}
        for key, value in self.env.items():
            self._old = getattr(self, "_old", {})
        import os
        self.os = os
        self.saved = {k: os.environ.get(k) for k in list(self.env) + ["TELEGRAM_API_BASE"]}
        for key, value in self.env.items():
            os.environ[key] = value

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                self.os.environ.pop(key, None)
            else:
                self.os.environ[key] = value

    def test_one_standalone_sendmessage_with_the_exact_body(self):
        with FakeBotApi() as bot:
            self.os.environ["TELEGRAM_API_BASE"] = bot.url
            result = SERVER.handle_telegram_notify({"event": EVENT})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["message_id"], 4242)
        self.assertTrue(result["standalone"])
        self.assertFalse(result["thread_associated"])
        self.assertEqual(len(bot.calls), 1)  # ONE message per event: never a storm
        call = bot.calls[0]
        self.assertEqual(call["path"], "/bot123:fake/sendMessage")
        self.assertEqual(call["body"]["chat_id"], "8969054376")
        self.assertNotIn("parse_mode", call["body"])  # the two lines stay literal
        self.assertEqual(call["body"]["text"].split("\n")[0:2], EXPECTED_HEADER)
        self.assertNotIn("123:fake", call["body"]["text"])  # no secret in the message

    def test_missing_config_returns_unavailable_without_raising(self):
        self.os.environ.pop("TELEGRAM_CHAT_ID", None)
        result = SERVER.handle_telegram_notify({"event": EVENT})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("TELEGRAM_CHAT_ID", result["reason"])

    def test_api_error_is_reported_as_a_listener_error(self):
        with FakeBotApi(ok=False) as bot:
            self.os.environ["TELEGRAM_API_BASE"] = bot.url
            result = SERVER.handle_telegram_notify({"event": EVENT})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")

    def test_dry_run_builds_the_body_without_sending(self):
        result = SERVER.handle_telegram_notify({"event": EVENT, "dry_run": True})
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["first_two_lines_verbatim"])

    def test_mcp_stdio_surface(self):
        with FakeBotApi() as bot:
            env = dict(**self.os.environ)
            env["TELEGRAM_API_BASE"] = bot.url
            with McpServer(str(PLUGIN_DIR / "server.py"), env) as server:
                init = server.initialize()
                self.assertEqual(init["result"]["serverInfo"]["name"], "telegram-notify")
                tools = server.list_tools()
                self.assertEqual([t["name"] for t in tools], ["telegram_notify"])
                reply = server.call("telegram_notify", {"event": EVENT})
            self.assertTrue(reply["payload"]["ok"])
        self.assertEqual(len(bot.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
