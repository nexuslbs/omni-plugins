#!/usr/bin/env python3
"""
Unit tests for the telegram plugin's OUTBOUND RATE LIMITING, retry budget and
duplicate-suppression rules (operator request, 2026-09-12).

Pure stdlib: imports platform.py directly and injects a FAKE CLOCK / fake
sleep / stubbed urlopen, so NOTHING here sleeps for real and no network or bot
token is involved.

Covers:
  * OutboundRateGate.spacing: N rapid calls are spaced (injected clock);
  * try_acquire: the non-blocking path used by the cosmetic typing indicator;
  * _parse_typing_interval: absent/0/empty/positive/invalid normalisation;
  * _send_part DUPLICATE SUPPRESSION: an ANSWERED rejection retries the SAME
    reply once, a CONNECTION-level failure is NOT retried blindly;
  * _api_post 429 handling: waits parameters.retry_after (no immediate retry)
    and succeeds within the bounded budget.

Usage:
    python3 tests/test_rate_limit.py
Exit code 0 on success, 1 on failure.
"""

import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import platform as tg  # noqa: E402

FAILURES = []


def check(cond, label):
    if cond:
        print("PASS: " + label)
    else:
        FAILURES.append(label)
        print("FAIL: " + label)


class FakeClock:
    """Monotonic clock advanced only by the fake sleep."""

    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)
        return False


def test_rate_gate_spacing():
    clock = FakeClock()
    gate = tg.OutboundRateGate(threading.Event(), global_interval=0.05,
                               chat_interval=1.0, now=clock.now,
                               sleep=clock.sleep)
    start = clock.now()
    for _ in range(4):
        check(gate.acquire("chat-1", is_send=True) is True,
              "gate.acquire returns True while running")
    elapsed = clock.now() - start
    # 4 sends to the SAME chat -> 3 intervals of >= 1.0s are enforced.
    check(elapsed >= 3.0,
          "gate: 4 rapid sends to one chat are spaced >= 3x chat_interval "
          "(injected clock: %.2fs)" % elapsed)
    # A different chat is only bound by the much smaller GLOBAL interval.
    start = clock.now()
    gate.acquire("chat-2", is_send=True)
    check(clock.now() - start < 1.0,
          "gate: a second chat is not blocked by the first chat's interval")


def test_gate_try_acquire_is_non_blocking():
    clock = FakeClock()
    gate = tg.OutboundRateGate(threading.Event(), global_interval=0.05,
                               chat_interval=1.0, now=clock.now,
                               sleep=clock.sleep)
    check(gate.try_acquire("chat-1", is_send=True) is True,
          "try_acquire: first call reserves a slot")
    before = clock.now()
    check(gate.try_acquire("chat-1", is_send=True) is False,
          "try_acquire: busy chat -> False (caller DROPS the request)")
    check(clock.now() == before,
          "try_acquire: never waits (no time passed on the fake clock)")
    clock.sleep(1.5)
    check(gate.try_acquire("chat-1", is_send=True) is True,
          "try_acquire: slot frees up after the interval")


def test_gate_aborts_on_shutdown():
    stop = threading.Event()
    stop.set()
    # clock at 0 so the 5s intervals are pending -> acquire really waits
    clock = FakeClock(0.0)
    gate = tg.OutboundRateGate(stop, global_interval=5.0,
                               chat_interval=5.0, now=clock.now,
                               sleep=stop.wait)
    check(gate.acquire("chat-1", is_send=True) is False,
          "gate.acquire: returns False when shutdown interrupts the wait")


def test_parse_typing_interval():
    parse = tg.TelegramPlatform._parse_typing_interval
    check(parse(None) == 0.0,
          "typing_interval: explicit null -> 0 (typing disabled)")
    check(parse("") == 0.0,
          "typing_interval: explicit empty string -> 0 (typing disabled)")
    check(parse("   ") == 0.0,
          "typing_interval: whitespace-only -> 0 (typing disabled)")
    check(parse(0) == 0.0 and parse("0") == 0.0,
          "typing_interval: explicit 0 (int and str) -> 0 (typing disabled)")
    check(parse(7) == 7.0 and parse("2.5") == 2.5 and parse(10) == 10.0,
          "typing_interval: positive values pass through")
    check(parse("abc") == tg.DEFAULT_TYPING_INTERVAL_SECS,
          "typing_interval: non-numeric -> default (never crashes)")
    check(parse(-5) == tg.DEFAULT_TYPING_INTERVAL_SECS,
          "typing_interval: negative -> default (never crashes)")


def test_send_part_duplicate_suppression():
    plat = tg.TelegramPlatform()
    calls = []

    def answered_fail(resource, content, reply):
        calls.append(reply)
        raise tg.TelegramApiError("HTTP 400: mock rejection", answered=True,
                                  status=400)

    plat._send_rendered = answered_fail
    try:
        plat._send_part("chat-1", "hello", 42)
        raised = False
    except tg.TelegramApiError:
        raised = True
    check(raised and calls == [42, 42],
          "duplicate suppression: an ANSWERED rejection retries the SAME "
          "reply once (attempts=%r)" % (calls,))

    calls[:] = []

    def connection_fail(resource, content, reply):
        calls.append(reply)
        raise tg.TelegramApiError("Cannot reach Telegram API: boom",
                                  answered=False)

    plat._send_rendered = connection_fail
    try:
        plat._send_part("chat-1", "hello", 42)
        raised = False
    except tg.TelegramApiError:
        raised = True
    check(raised and calls == [42],
          "duplicate suppression: a CONNECTION-level failure is NOT retried "
          "(delivery unknown, no double-post)")

    # a standalone part (no reply target) is never retried either
    calls[:] = []
    plat._send_rendered = answered_fail
    try:
        plat._send_part("chat-1", "hello", None)
        raised = False
    except tg.TelegramApiError:
        raised = True
    check(raised and calls == [None],
          "duplicate suppression: a standalone part is not double-sent")


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_api_post_honors_429_retry_after():
    plat = tg.TelegramPlatform()
    plat.bot_token = "TEST"
    plat.api_base_url = "http://127.0.0.1:1"
    # never wait for real in this test
    plat._gate = tg.OutboundRateGate(threading.Event(), 0.0, 0.0)
    waits = []
    plat._wait = lambda seconds: waits.append(seconds) or False

    state = {"calls": 0}
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            body = json.dumps({"ok": False, "error_code": 429,
                               "description": "Too Many Requests: retry after 7",
                               "parameters": {"retry_after": 7}}).encode()
            raise urllib.error.HTTPError(req.full_url, 429,
                                         "Too Many Requests", None,
                                         io.BytesIO(body))
        return FakeResponse({"ok": True, "result": {"message_id": 1}})

    urllib.request.urlopen = fake_urlopen
    try:
        result = plat._api_post("sendMessage", {"chat_id": "1", "text": "x"})
    finally:
        urllib.request.urlopen = real_urlopen

    check(result == {"message_id": 1},
          "429: the call succeeds after honoring retry_after")
    check(waits == [7.0],
          "429: the plugin WAITED parameters.retry_after=7 before retrying "
          "(waits=%r)" % (waits,))
    check(state["calls"] == 2,
          "429: exactly one retry, never an immediate second attempt")

    # 5xx keeps the bounded backoff, and the budget is finite
    plat2 = tg.TelegramPlatform()
    plat2.bot_token = "TEST"
    plat2.api_base_url = "http://127.0.0.1:1"
    plat2._gate = tg.OutboundRateGate(threading.Event(), 0.0, 0.0)
    waits2 = []
    plat2._wait = lambda seconds: waits2.append(seconds) or False
    n = {"calls": 0}

    def always_500(req, timeout=None):
        n["calls"] += 1
        body = json.dumps({"ok": False, "description": "mock 500"}).encode()
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", None,
                                     io.BytesIO(body))

    urllib.request.urlopen = always_500
    try:
        try:
            plat2._api_post("sendMessage", {"chat_id": "1", "text": "x"})
            raised = False
        except tg.TelegramApiError:
            raised = True
    finally:
        urllib.request.urlopen = real_urlopen
    check(raised and n["calls"] == tg.API_MAX_ATTEMPTS,
          "5xx: bounded retry budget (%d attempts, then the error is raised)"
          % tg.API_MAX_ATTEMPTS)
    check(len(waits2) == tg.API_MAX_ATTEMPTS - 1
          and all(w > 0 for w in waits2),
          "5xx: waits a positive backoff before each retry (%r)" % (waits2,))


def main():
    test_rate_gate_spacing()
    test_gate_try_acquire_is_non_blocking()
    test_gate_aborts_on_shutdown()
    test_parse_typing_interval()
    test_send_part_duplicate_suppression()
    test_api_post_honors_429_retry_after()
    print("")
    if FAILURES:
        print("RATE LIMIT TESTS FAILED: {} assertion(s) failed".format(
            len(FAILURES)))
        return 1
    print("RATE LIMIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
