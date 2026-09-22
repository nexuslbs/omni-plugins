#!/usr/bin/env python3
"""Tests for the echo-notify LISTENER plugin (second, non-telegram listener).

Run:  python3 tools/echo-notify/tests/test_echo_notify.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
TOOLS_DIR = PLUGIN_DIR.parent
sys.path.insert(0, str(TOOLS_DIR / "human-intervention" / "tests"))

from mcp_client import McpServer  # noqa: E402


def load_server(name="echo_server"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server()

EVENT = {"event": "solve-captcha", "correlation_id": "corr-7",
         "payload": {"session_id": "sess-7", "url": "https://example.test/x",
                     "reason": "challenge", "timeout_s": 900}}


class EchoNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="echo-notify-")
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "notifications.jsonl"
        os.environ["ECHO_NOTIFY_LOG"] = str(self.log)
        os.environ["ECHO_NOTIFY_MODE"] = "log"

    def test_event_is_appended_as_one_jsonl_row(self):
        result = SERVER.handle_echo_notify({"event": EVENT})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "delivered")
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["correlation_id"], "corr-7")
        self.assertEqual(rows[0]["session_id"], "sess-7")
        self.assertEqual(rows[0]["listener"], "echo-notify")

    def test_fail_mode_returns_an_error_result(self):
        os.environ["ECHO_NOTIFY_MODE"] = "fail"
        result = SERVER.handle_echo_notify({"event": EVENT})
        self.assertFalse(result["ok"])
        self.assertFalse(self.log.exists())

    def test_accepts_the_json_string_form_of_the_event(self):
        result = SERVER.handle_echo_notify({"event": json.dumps(EVENT)})
        self.assertTrue(result["ok"])
        self.assertTrue(self.log.exists())

    def test_mcp_stdio_surface(self):
        env = dict(os.environ)
        with McpServer(str(PLUGIN_DIR / "server.py"), env) as server:
            init = server.initialize()
            self.assertEqual(init["result"]["serverInfo"]["name"], "echo-notify")
            self.assertEqual([t["name"] for t in server.list_tools()], ["echo_notify"])
            reply = server.call("echo_notify", {"event": EVENT})
        self.assertTrue(reply["payload"]["ok"])
        self.assertFalse(reply["isError"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
