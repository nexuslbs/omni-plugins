#!/usr/bin/env python3
"""Tests for the SOLVER-side human-intervention plugin.

Run:  python3 -m unittest discover -s tools/human-intervention/tests -v
  or: python3 tools/human-intervention/tests/test_human_intervention.py

Covered: event publication + fan-out to >= 2 listeners, `unavailable` when no
listener delivers, guardrails (per-session cooldown, hard cap), bounded wait
outcomes (solved/aborted/timeout) with the resume marker, and the GENERIC
structural page-state probe/detection (fake CDP browser, no vendor knowledge).
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
sys.path.insert(0, str(HERE))

from fake_core import FakeCore  # noqa: E402
from fake_cdp import FakeBrowser  # noqa: E402


def load_server():
    spec = importlib.util.spec_from_file_location("hi_server", PLUGIN_DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hi_server"] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server()


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hi-test-")
        self.addCleanup(self.tmp.cleanup)
        os.environ["HI_STATE_DIR"] = self.tmp.name
        os.environ["OMNI_API_URL"] = "http://127.0.0.1:1"  # never called by default
        os.environ["HI_COOLDOWN_S"] = "600"
        os.environ["HI_MAX_HANDOFFS"] = "3"
        os.environ["HI_DEFAULT_TIMEOUT_S"] = "900"
        self.received: list[dict] = []

    def listener(self, key):
        def fn(event):
            self.received.append({"listener": key, "event": event})
            return "ok", f"delivered by {key}"
        return fn

    def core_with(self, keys=("telegram-notify", "echo-notify")):
        return FakeCore([(k, self.listener(k)) for k in keys])


class RequestTest(BaseCase):
    def test_request_publishes_solve_captcha_to_every_listener(self):
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            result = SERVER.handle_intervention_request({
                "session_id": "sess-1", "url": "https://example.test/checkout",
                "reason": "blocking challenge overlay",
                "access_hint": "noVNC http://localhost:8080", "timeout_s": 120})
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["event"], "solve-captcha")
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(len(self.received), 2)  # fan-out: BOTH listeners got it
        self.assertEqual({r["listener"] for r in self.received},
                         {"telegram-notify", "echo-notify"})
        for recv in self.received:
            self.assertEqual(recv["event"]["event"], "solve-captcha")
            self.assertEqual(recv["event"]["correlation_id"], result["correlation_id"])
            self.assertEqual(recv["event"]["payload"]["session_id"], "sess-1")
            self.assertEqual(recv["event"]["payload"]["timeout_s"], 120)
        # the published payload is BOUNDED: only the documented keys
        self.assertLessEqual(set(core.published[0]["payload"]),
                             {"session_id", "url", "reason", "access_hint",
                              "timeout_s", "requested_at"})

    def test_no_listener_delivered_returns_unavailable(self):
        with self.core_with(keys=()) as core:
            os.environ["OMNI_API_URL"] = core.url
            result = SERVER.handle_intervention_request({"session_id": "sess-2"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["delivered"], 0)
        self.assertIn("no listener", result["detail"])

    def test_session_id_is_required(self):
        result = SERVER.handle_intervention_request({})
        self.assertEqual(result["status"], "error")

    def test_cooldown_then_hard_cap(self):
        os.environ["HI_COOLDOWN_S"] = "600"
        os.environ["HI_MAX_HANDOFFS"] = "2"
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            first = SERVER.handle_intervention_request({"session_id": "sess-3"})
            self.assertEqual(first["status"], "pending")
            second = SERVER.handle_intervention_request({"session_id": "sess-3"})
            self.assertEqual(second["status"], "cooldown")
            self.assertGreaterEqual(second["retry_after_s"], 1)
            # simulate the cooldown window having elapsed
            state_path = Path(self.tmp.name) / "guardrails.json"
            state = json.loads(state_path.read_text())
            state["sessions"]["sess-3"]["last_ts"] -= 3600
            state_path.write_text(json.dumps(state))
            third = SERVER.handle_intervention_request({"session_id": "sess-3"})
            self.assertEqual(third["status"], "pending")
            fourth = SERVER.handle_intervention_request({"session_id": "sess-3"})
            self.assertEqual(fourth["status"], "capped")
        # one hand-off per session per cooldown: 2 published events, never a storm
        self.assertEqual(len(core.published), 2)

    def test_transport_failure_is_reported_not_raised(self):
        os.environ["OMNI_API_URL"] = "http://127.0.0.1:1"
        result = SERVER.handle_intervention_request({"session_id": "sess-4"})
        self.assertEqual(result["status"], "error")
        self.assertIn("could not publish", result["error"])


class WaitTest(BaseCase):
    def _request_and_wait(self, outcome):
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            requested = SERVER.handle_intervention_request(
                {"session_id": f"sess-{outcome}", "url": "https://example.test/x"})
            core.force_terminal(requested["correlation_id"], outcome,
                                {"session_id": f"sess-{outcome}",
                                 "url": "https://example.test/x"})
            return SERVER.handle_intervention_wait(
                {"correlation_id": requested["correlation_id"], "timeout_s": 5})

    def test_solved_returns_resume_marker(self):
        result = self._request_and_wait("solved")
        self.assertEqual(result["status"], "solved")
        self.assertEqual(result["marker"], "human-intervention: solved")
        self.assertEqual(result["resume"]["next_step"], "continue")
        self.assertEqual(result["resume"]["session_id"], "sess-solved")

    def test_aborted_returns_abort_marker(self):
        result = self._request_and_wait("aborted")
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["marker"], "human-intervention: aborted")

    def test_timeout_is_bounded_and_does_not_hang(self):
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            requested = SERVER.handle_intervention_request({"session_id": "sess-t"})
            started = __import__("time").time()
            result = SERVER.handle_intervention_wait(
                {"correlation_id": requested["correlation_id"], "timeout_s": 1})
            elapsed = __import__("time").time() - started
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["marker"], "human-intervention: timeout")
        self.assertTrue(result["session_left_usable"])
        self.assertLess(elapsed, 30)

    def test_unknown_correlation_id_is_not_an_error(self):
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            result = SERVER.handle_intervention_wait(
                {"correlation_id": "does-not-exist", "timeout_s": 1})
        self.assertIn(result["status"], ("timeout", "pending"))


class PageStateTest(BaseCase):
    def test_probe_reports_structural_blocker(self):
        with FakeBrowser() as browser:
            result = SERVER.handle_intervention_probe({"cdp_url": browser.url})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["blocked"])
        self.assertIn("overlay_count", result["snapshot"])

    def test_resolution_predicate_only_accepts_an_unblocked_loaded_document(self):
        before = {"url": "https://example.test/t", "title": "t", "ready": "complete",
                  "overlay_count": 1, "large_frame_count": 0, "form_count": 0, "text_len": 10}
        still_blocked = dict(before, overlay_count=1, form_count=1, text_len=99)
        solved, reason = SERVER.evaluate_resolution(before, still_blocked)
        self.assertFalse(solved)
        self.assertIn("still shows", reason)
        not_loaded = dict(before, overlay_count=0, large_frame_count=0, ready="loading",
                          form_count=1, text_len=99)
        solved, reason = SERVER.evaluate_resolution(before, not_loaded)
        self.assertFalse(solved)
        self.assertIn("not loaded", reason)
        resolved = dict(before, overlay_count=0, large_frame_count=0, form_count=1,
                        text_len=500, title="t")
        solved, reason = SERVER.evaluate_resolution(before, resolved)
        self.assertTrue(solved)

    def test_detect_publishes_terminal_event_when_page_recovers(self):
        with FakeBrowser() as browser, self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            requested = SERVER.handle_intervention_request(
                {"session_id": "sess-detect", "url": browser.snapshot["url"]})
            correlation_id = requested["correlation_id"]
            before = SERVER.page_snapshot(browser.url, browser.snapshot["url"])
            browser.resolve()  # the human solved it in the watched session
            result = SERVER.handle_intervention_detect(
                {"correlation_id": correlation_id, "session_id": "sess-detect",
                 "cdp_url": browser.url, "target_url": browser.snapshot["url"],
                 "timeout_s": 5, "poll_s": 0.2, "before": before})
            self.assertEqual(result["status"], "solved")
            self.assertEqual(result["marker"], "human-intervention: solved")
            self.assertEqual(result["terminal_event"], "solve-captcha-resolved")
            # the terminal event carries the SAME correlation id -> the core's
            # interaction resolves and the parked flow can resume
            terminal = [p for p in core.published
                        if p["event"] == "solve-captcha-resolved"]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["correlation_id"], correlation_id)
            # and the interaction is now solved for whoever waits on it
            self.assertEqual(core.wait(correlation_id, 5)["state"], "solved")
            self.assertEqual(SERVER.handle_intervention_wait(
                {"correlation_id": correlation_id, "timeout_s": 1})["status"], "solved")

    def test_detect_returns_timeout_when_page_stays_blocked(self):
        with FakeBrowser() as browser, self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            result = SERVER.handle_intervention_detect(
                {"correlation_id": "corr-blocked", "cdp_url": browser.url,
                 "timeout_s": 1, "poll_s": 0.2})
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["after"]["overlay_count"] > 0)


class GuardrailStateTest(BaseCase):
    def test_status_exposes_local_guardrails(self):
        result = SERVER.handle_intervention_status({})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["guardrails"]["cooldown_s"], 600)
        self.assertEqual(result["guardrails"]["max_handoffs"], 3)

    def test_audit_log_records_every_hand_off(self):
        with self.core_with() as core:
            os.environ["OMNI_API_URL"] = core.url
            SERVER.handle_intervention_request({"session_id": "sess-audit"})
        audit = Path(self.tmp.name) / "hand-offs.jsonl"
        self.assertTrue(audit.exists())
        rows = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertTrue(any(r.get("status") == "pending" for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
