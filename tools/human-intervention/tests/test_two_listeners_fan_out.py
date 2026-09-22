#!/usr/bin/env python3
"""Agnostic/fan-out proof (operator requirement, telegram thread 2794).

The solver module must contain ZERO delivery code, and delivery must be
pluggable: SEVERAL listeners can be bound to the same `solve-captcha` event and
each handles it independently, including with the telegram listener DISABLED.

The listeners here are the REAL plugin servers (spawned as MCP stdio children
exactly like omniagent spawns them), driven with the real event payload.

Run:  python3 tools/human-intervention/tests/test_two_listeners_fan_out.py
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS_DIR / "telegram-notify" / "tests"))

from fake_core import FakeCore  # noqa: E402
from mcp_client import McpServer  # noqa: E402
from test_telegram_notify import FakeBotApi  # noqa: E402

TG_SERVER = TOOLS_DIR / "telegram-notify" / "server.py"
ECHO_SERVER = TOOLS_DIR / "echo-notify" / "server.py"

# Delivery words that must NEVER appear in the solver module.
FORBIDDEN_IN_SOLVER = ["telegram", "sendmessage", "bot_token", "api.telegram.org",
                       "smtp", "twilio", "chat_id"]


def load_solver():
    spec = importlib.util.spec_from_file_location("hi_solver_fanout", PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hi_solver_fanout"] = module
    spec.loader.exec_module(module)
    return module


SOLVER = load_solver()


class FanOutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hi-fanout-")
        self.addCleanup(self.tmp.cleanup)
        os.environ["HI_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        os.environ["HI_COOLDOWN_S"] = "600"
        os.environ["HI_MAX_HANDOFFS"] = "5"

    # ── helpers ────────────────────────────────────────────────────────────
    def _echo_listener(self, base_env, results):
        env = dict(base_env)
        env["ECHO_NOTIFY_LOG"] = str(Path(self.tmp.name) / "notifications.jsonl")
        env["ECHO_NOTIFY_MODE"] = os.environ.get("ECHO_NOTIFY_MODE_TEST", "log")
        server = McpServer(str(ECHO_SERVER), env)

        def listen(event):
            reply = server.call("echo_notify", {"event": event})
            results.append(("echo-notify", reply))
            payload = reply["payload"]
            return ("ok", json.dumps(payload)) if payload.get("ok") else ("error", str(payload))

        return server, listen

    def _telegram_listener(self, base_env, bot_url, results, enabled=True):
        env = dict(base_env)
        env["TELEGRAM_API_BASE"] = bot_url
        if enabled:
            env["TELEGRAM_BOT_TOKEN"] = "123:fake"
            env["TELEGRAM_CHAT_ID"] = "8969054376"
        else:
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env.pop("TELEGRAM_CHAT_ID", None)
        server = McpServer(str(TG_SERVER), env)

        def listen(event):
            reply = server.call("telegram_notify", {"event": event})
            results.append(("telegram-notify", reply))
            payload = reply["payload"]
            return ("ok", json.dumps(payload)) if payload.get("ok") else ("error", str(payload))

        return server, listen

    # ── tests ──────────────────────────────────────────────────────────────
    def test_solver_has_zero_delivery_code(self):
        text = ""
        for name in ("server.py", "cdp.py", "plugin.json", "README.md"):
            path = PLUGIN_DIR / name
            if path.exists():
                text += path.read_text().lower()
        hits = [word for word in FORBIDDEN_IN_SOLVER if word in text]
        self.assertEqual(hits, [], f"solver module mentions delivery channels: {hits}")

    def test_two_listeners_receive_the_same_event(self):
        results: list[tuple[str, dict]] = []
        base_env = dict(os.environ)
        with FakeBotApi() as bot:
            tg_proc, tg_listen = self._telegram_listener(base_env, bot.url, results)
            echo_proc, echo_listen = self._echo_listener(base_env, results)
            with tg_proc, echo_proc:
                with FakeCore([("telegram-notify", tg_listen), ("echo-notify", echo_listen)]) as core:
                    os.environ["OMNI_API_URL"] = core.url
                    result = SOLVER.handle_intervention_request(
                        {"session_id": "sess-fanout", "url": "https://example.test/x",
                         "reason": "challenge overlay", "access_hint": "noVNC :8080"})
                    correlation_id = result["correlation_id"]
                    self.assertEqual(result["delivered"], 2)
                    self.assertEqual(sorted(name for name, _reply in results),
                                     ["echo-notify", "telegram-notify"])
                    # BOTH listeners saw the SAME correlation id
                    for listener, reply in results:
                        self.assertEqual(reply["payload"].get("correlation_id") or correlation_id,
                                         correlation_id)
                    # the telegram listener really sent ONE standalone message
                    self.assertEqual(len(bot.calls), 1)
                    body = bot.calls[0]["body"]["text"]
                    self.assertEqual(body.split("\n")[0:2],
                                     ["Human intervention required", "[Open browser session]"])
                    # the non-telegram listener logged the same event
                    rows = [json.loads(line) for line in
                            (Path(self.tmp.name) / "notifications.jsonl").read_text().splitlines()]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["correlation_id"], correlation_id)

    def test_telegram_listener_disabled_still_delivers_to_the_other(self):
        results: list[tuple[str, dict]] = []
        base_env = dict(os.environ)
        with FakeBotApi() as bot:
            tg_proc, tg_listen = self._telegram_listener(base_env, bot.url, results, enabled=False)
            echo_proc, echo_listen = self._echo_listener(base_env, results)
            with tg_proc, echo_proc:
                with FakeCore([("telegram-notify", tg_listen), ("echo-notify", echo_listen)]) as core:
                    os.environ["OMNI_API_URL"] = core.url
                    result = SOLVER.handle_intervention_request(
                        {"session_id": "sess-no-telegram"})
        self.assertEqual(result["delivered"], 1)
        statuses = {name: reply["payload"].get("status") or ("ok" if reply["payload"].get("ok") else "error")
                    for name, reply in results}
        self.assertIn(statuses["telegram-notify"], ("unavailable", "error"))
        self.assertTrue(results[1][1]["payload"]["ok"])
        self.assertEqual(len(bot.calls), 0)  # notifications always go through a listener

    def test_one_failing_listener_does_not_break_the_other(self):
        results: list[tuple[str, dict]] = []
        base_env = dict(os.environ)
        os.environ["ECHO_NOTIFY_MODE_TEST"] = "fail"  # echo listener fails on purpose
        try:
            with FakeBotApi() as bot:
                tg_proc, tg_listen = self._telegram_listener(base_env, bot.url, results)
                echo_proc, echo_listen = self._echo_listener(base_env, results)
                with tg_proc, echo_proc:
                    with FakeCore([("telegram-notify", tg_listen),
                                   ("echo-notify", echo_listen)]) as core:
                        os.environ["OMNI_API_URL"] = core.url
                        result = SOLVER.handle_intervention_request(
                            {"session_id": "sess-iso"})
            self.assertEqual(result["delivered"], 1)
            by_listener = dict(results)
            self.assertFalse(by_listener["echo-notify"]["payload"]["ok"])
            self.assertTrue(by_listener["telegram-notify"]["payload"]["ok"])
            self.assertEqual(len(bot.calls), 1)
        finally:
            os.environ.pop("ECHO_NOTIFY_MODE_TEST", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
