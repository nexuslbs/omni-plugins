#!/usr/bin/env python3
"""
Smoke test for the omniagent telegram platform plugin - runs entirely
against the MOCK Telegram Bot API (tests/mock_telegram_api.py). No real
bot token is used anywhere.

Covers:
  * initialize        -> name "telegram", capabilities inbound+outbound
  * configure         -> api_base_url override to the mock
  * deliver           -> sendMessage payload hits the mock (chat_id/text)
  * edit_message      -> editMessageText payload hits the mock
  * delete_message    -> deleteMessage payload hits the mock
  * react             -> setMessageReaction payload hits the mock
  * inbound           -> injected getUpdates flow back as inbound_message
                         notifications on stdout
  * parent_by_chat    -> config false (default): no parent external id;
                         config true: inbound messages carry the chat id as
                         metadata["root_id"] (the envelope key omniagent
                         reads as the parent external id)

Usage:
    python3 tests/smoke_test.py [--port 8091]
Exit code 0 on success, 1 on failure.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_PY = os.path.join(os.path.dirname(HERE), "platform.py")
MOCK_PY = os.path.join(HERE, "mock_telegram_api.py")
MOCK_TOKEN = "123456:MOCKTESTTOKEN-omniagent"  # fake; mock accepts any token


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


class PlatformProc:
    """Subprocess wrapper driving platform.py over stdin/stdout JSON-lines."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, PLATFORM_PY],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.next_id = 1

    def call(self, method, params=None, timeout=15):
        req_id = self.next_id
        self.next_id += 1
        req = {"id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == req_id:
                return resp
        raise AssertionError("no response for {} within {}s".format(method, timeout))

    def expect_notification(self, method, timeout=20):
        """Wait for a notification (no id) with the given method on stdout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                notif = json.loads(line)
            except json.JSONDecodeError:
                continue
            if notif.get("method") == method:
                return notif
        raise AssertionError("no '{}' notification within {}s".format(method, timeout))

    def stop(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=free_port())
    args = parser.parse_args()

    mock_port = args.port
    mock = None
    plat = None
    failures = 0

    def check(cond, label):
        nonlocal failures
        if cond:
            print("PASS: " + label)
        else:
            failures += 1
            print("FAIL: " + label)

    try:
        # ── Boot the mock Telegram API ────────────────────────────────
        mock = subprocess.Popen(
            [sys.executable, MOCK_PY, "--port", str(mock_port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        base = "http://127.0.0.1:{}".format(mock_port)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if http_get(base + "/health").get("ok") is True:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("mock server did not come up")

        # ── Boot the platform plugin ──────────────────────────────────
        plat = PlatformProc()

        # 1. initialize
        r = plat.call("initialize")
        result = r.get("result", {})
        check(result.get("name") == "telegram",
              "initialize -> name 'telegram'")
        caps = result.get("capabilities", {})
        check(caps.get("inbound") is True and caps.get("outbound") is True,
              "initialize -> capabilities inbound+outbound")

        # 2. configure (point at the MOCK via api_base_url)
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": True,
            "poll_interval_secs": 1,
        }})
        check(r.get("result", {}).get("configured") is True,
              "configure -> configured:true")

        # 3. deliver -> sendMessage
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "Hello from omniagent",
            "msg_type": "chat",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "deliver -> delivered:true + external_id")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(len(sent) == 1 and sent[0]["chat_id"] == "123456789"
              and sent[0]["text"] == "Hello from omniagent",
              "mock received sendMessage with correct chat_id/text")

        # 4. edit_message -> editMessageText
        ext_id = res["external_id"]
        r = plat.call("edit_message", {
            "resource_identifier": "123456789",
            "external_id": ext_id,
            "content": "Edited hello",
        })
        check(r.get("result", {}).get("edited") is True,
              "edit_message -> edited:true")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[0]["text"] == "Edited hello",
              "mock message text updated by editMessageText")

        # 5. delete_message -> deleteMessage
        r = plat.call("delete_message", {
            "resource_identifier": "123456789",
            "external_id": ext_id,
        })
        check(r.get("result", {}).get("deleted") is True,
              "delete_message -> deleted:true")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(not any(str(m["message_id"]) == str(ext_id) for m in sent),
              "mock message removed by deleteMessage")

        # 6. react -> setMessageReaction
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "React target",
        })
        ext2 = r.get("result", {}).get("external_id")
        r = plat.call("react", {
            "resource_identifier": "123456789",
            "external_id": ext2,
            "emoji": "\U0001f44d",
        })
        check(r.get("result", {}).get("reacted") is True,
              "react -> reacted:true")
        reactions = http_get(base + "/admin/reactions").get("reactions", [])
        check(reactions and str(reactions[-1]["message_id"]) == str(ext2),
              "mock stored setMessageReaction")

        # 7. inbound: inject a getUpdates payload -> inbound_message
        #    (parent_by_chat defaults to false -> NO parent external id)
        http_post(base + "/admin/inject", {
            "update_id": 9001,
            "message": {
                "message_id": 777,
                "date": 1700000000,
                "chat": {"id": -1002003004, "type": "channel"},
                "from": {"id": 555, "first_name": "Mock"},
                "text": "inbound hello from telegram",
            },
        })
        n = plat.expect_notification("inbound_message", timeout=25)
        p = n.get("params", {})
        check(p.get("resource_identifier") == "-1002003004"
              and p.get("text") == "inbound hello from telegram"
              and p.get("external_id") == "777",
              "inbound_message notification carries chat/text/external_id")
        check("parent_external_id" not in p
              and "root_id" not in p.get("metadata", {}),
              "default config (parent_by_chat=false): no parent external id")

        # 7b. parent_by_chat=true: inbound messages carry the chat id as the
        #     parent external id - delivered via metadata["root_id"], the
        #     envelope key omniagent reads as parent_external_id (same value
        #     for every message from the same chat).
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": True,
            "poll_interval_secs": 1,
            "parent_by_chat": True,
        }})
        check(r.get("result", {}).get("configured") is True,
              "configure(parent_by_chat=true) -> configured:true")
        http_post(base + "/admin/inject", {
            "update_id": 9003,
            "message": {
                "message_id": 778,
                "date": 1700000001,
                "chat": {"id": -1002003004, "type": "channel"},
                "from": {"id": 555, "first_name": "Mock"},
                "text": "second inbound hello",
            },
        })
        n = plat.expect_notification("inbound_message", timeout=25)
        p = n.get("params", {})
        check(p.get("resource_identifier") == "-1002003004"
              and p.get("external_id") == "778",
              "parent_by_chat=true: inbound message carries chat/text/external_id")
        check(p.get("metadata", {}).get("root_id") == "-1002003004",
              "parent_by_chat=true: parent external id = chat id "
              "(same value for all messages from the chat)")

        # 7c. toggle back to false: parent external id disappears again
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": True,
            "poll_interval_secs": 1,
            "parent_by_chat": False,
        }})
        check(r.get("result", {}).get("configured") is True,
              "configure(parent_by_chat=false) -> configured:true")
        http_post(base + "/admin/inject", {
            "update_id": 9004,
            "message": {
                "message_id": 779,
                "date": 1700000002,
                "chat": {"id": -1002003004, "type": "channel"},
                "from": {"id": 555, "first_name": "Mock"},
                "text": "third inbound hello",
            },
        })
        n = plat.expect_notification("inbound_message", timeout=25)
        p = n.get("params", {})
        check(p.get("external_id") == "779",
              "parent_by_chat=false: inbound message still delivered")
        check("parent_external_id" not in p
              and "root_id" not in p.get("metadata", {}),
              "parent_by_chat=false: no parent external id on inbound message")

        # 8. inbound edited message -> message_edited.
        #    update_id must be >= the platform's current offset (the mock's
        #    offset-based queue skips older update_ids; 9003/9004 were already
        #    consumed above, so use 9005).
        http_post(base + "/admin/inject", {
            "update_id": 9005,
            "edited_message": {
                "message_id": 777,
                "date": 1700000000,
                "chat": {"id": -1002003004, "type": "channel"},
                "from": {"id": 555},
                "text": "edited inbound hello",
            },
        })
        n = plat.expect_notification("message_edited", timeout=25)
        p = n.get("params", {})
        check(p.get("external_id") == "777"
              and p.get("text") == "edited inbound hello",
              "message_edited notification carries external_id/text")

        # 9. unknown method -> protocol error
        r = plat.call("no_such_method")
        check(r.get("error", {}).get("code") == -1,
              "unknown method -> error {code:-1}")

    finally:
        if plat:
            plat.stop()
        if mock:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except Exception:
                mock.kill()

    print("")
    if failures:
        print("SMOKE TEST FAILED: {} assertion(s) failed".format(failures))
        return 1
    print("SMOKE TEST PASSED - telegram platform works against the mock "
          "(no real token used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
