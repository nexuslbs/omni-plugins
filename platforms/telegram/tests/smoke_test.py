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
  * typing            -> sendChatAction(action=typing) hits the mock
  * inbound           -> injected getUpdates flow back as inbound_message
                         notifications on stdout
  * parent_by_chat    -> config false (default): no parent external id;
                         config true: inbound messages carry the chat id as
                         metadata["root_id"] (the envelope key omniagent
                         reads as the parent external id)
  * flat configure    -> configure with FLAT params (no "config" key),
                         exactly like the real core sends (string values):
                         bot_token is stored, polling starts, and an
                         injected update flows back as inbound_message

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

        # 3b. deliver with reply_to_message_id -> the final message is sent as
        #     a REPLY to the thread's seq-0 message (Telegram reply threading).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "Final reply",
            "msg_type": "summary",
            "reply_to_message_id": "101",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "deliver(reply_to_message_id) -> delivered:true + external_id")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "Final reply"
              and str(sent[-1].get("reply_to_message_id")) == "101",
              "mock received sendMessage with reply_to_message_id=101")

        # 3c. standalone when reply_to_message_id is absent (default): the
        #     seq-0 id is only attached to final deliveries.
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "Standalone message",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "deliver (no reply_to) -> delivered:true + external_id")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "Standalone message"
              and "reply_to_message_id" not in sent[-1],
              "deliver without reply_to_message_id -> standalone send")

        # 3d. invalid reply_to_message_id falls back to a standalone send
        #     (the message is never dropped).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "Invalid reply id",
            "reply_to_message_id": "not-a-number",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "deliver(invalid reply_to) -> delivered:true (standalone fallback)")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "Invalid reply id"
              and "reply_to_message_id" not in sent[-1],
              "invalid reply_to_message_id -> standalone send, message kept")

        # 3e. stale seq-0 id (API rejects the reply): the plugin retries once
        #     as a standalone send - the message is never dropped.
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "Stale reply id",
            "reply_to_message_id": "404",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "deliver(stale reply id) -> delivered:true after standalone retry")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "Stale reply id"
              and "reply_to_message_id" not in sent[-1],
              "stale reply id -> standalone retry keeps the message")

        # 3f. markdown rendering: deliver content with markdown constructs and
        #     verify the mock receives parse_mode=HTML with the markdown
        #     converted to Telegram HTML (no raw ** markers).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "**bold** and _italic_ and `code`",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True,
              "deliver(markdown) -> delivered:true")
        sent = http_get(base + "/admin/sent").get("messages", [])
        m = sent[-1]
        check(m.get("parse_mode") == "HTML"
              and m["text"] == "<b>bold</b> and <i>italic</i> and <code>code</code>",
              "deliver(markdown) -> parse_mode=HTML, markdown converted to HTML")
        check("**" not in m["text"],
              "deliver(markdown) -> no raw ** in sent text")

        # 3g. mixed block-level markdown (heading, fenced code, list,
        #     blockquote, link) renders as Telegram HTML; inside a fenced
        #     code block ** stays literal code (correct for code content).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": (
                "# Heading\n"
                "```\nfenced **code**\n```\n"
                "- item one\n"
                "> quoted\n"
                "[link](https://example.com)"
            ),
        })
        res = r.get("result", {})
        check(res.get("delivered") is True,
              "deliver(blocks) -> delivered:true")
        sent = http_get(base + "/admin/sent").get("messages", [])
        m = sent[-1]
        check(m.get("parse_mode") == "HTML" and "```" not in m["text"],
              "deliver(blocks) -> parse_mode=HTML, no raw fence markers")
        check(m["text"].startswith("<b>Heading</b>")
              and "<pre>fenced **code**</pre>" in m["text"]
              and "\u2022 item one" in m["text"]
              and "<blockquote>quoted</blockquote>" in m["text"]
              and '<a href="https://example.com">link</a>' in m["text"],
              "deliver(blocks) -> heading/fence/list/quote/link rendered as HTML")

        # 3h. degenerate input: unbalanced markers degrade (markers stripped,
        #     message still delivered, never a send error).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "unclosed **bold and `tick and _under",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True,
              "deliver(unbalanced) -> delivered:true (no error)")
        sent = http_get(base + "/admin/sent").get("messages", [])
        m = sent[-1]
        check("**" not in m["text"] and "`" not in m["text"],
              "deliver(unbalanced) -> raw markers stripped from sent text")

        # 3i. HTML rejection fallback: the mock rejects the parse_mode=HTML
        #     send (REJECT_HTML sentinel); the plugin retries as plain text
        #     with markers stripped, so the message is never dropped and
        #     never shows raw syntax.
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "REJECT_HTML with **bold** here",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True,
              "deliver(HTML rejected) -> delivered:true via plain fallback")
        sent = http_get(base + "/admin/sent").get("messages", [])
        m = sent[-1]
        check("parse_mode" not in m
              and m["text"] == "REJECT_HTML with bold here",
              "deliver(HTML rejected) -> plain-text retry strips markers, "
              "no parse_mode")

        # 4. edit_message -> editMessageText
        ext_id = res["external_id"]
        r = plat.call("edit_message", {
            "resource_identifier": "123456789",
            "external_id": ext_id,
            "content": "Edited **hello**",
        })
        check(r.get("result", {}).get("edited") is True,
              "edit_message -> edited:true")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "Edited <b>hello</b>"
              and sent[-1].get("parse_mode") == "HTML",
              "editMessageText -> markdown rendered to HTML, parse_mode=HTML")

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

        # 6b. shortcode mapping: ":handshake:" -> the unicode handshake emoji
        r = plat.call("react", {
            "resource_identifier": "123456789",
            "external_id": ext2,
            "emoji": ":handshake:",
        })
        check(r.get("result", {}).get("reacted") is True,
              "react shortcode -> reacted:true")
        reactions = http_get(base + "/admin/reactions").get("reactions", [])
        # json.dumps uses ensure_ascii by default, so the emoji arrives in
        # escaped surrogate form; parse the stored reaction JSON to compare.
        # handle_react ACCUMULATES: the stored set now holds BOTH the earlier
        # thumbs-up and the handshake (start reaction is preserved).
        check(
            reactions
            and [e["emoji"] for e in json.loads(reactions[-1]["reaction"])]
            == ["\U0001f44d", "\U0001f91d"],
            "mock stored accumulated reactions [thumbs_up, handshake]",
        )

        # 6b2. ":+1:" start-reaction shortcode maps to the unicode thumbs-up,
        #      and a terminal reaction on the SAME message is sent as the
        #      combined set (start + terminal) instead of overwriting it.
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "React target 2",
        })
        ext3 = r.get("result", {}).get("external_id")
        r = plat.call("react", {
            "resource_identifier": "123456789",
            "external_id": ext3,
            "emoji": ":+1:",
        })
        check(r.get("result", {}).get("reacted") is True,
              "react :+1: shortcode -> reacted:true")
        reactions = http_get(base + "/admin/reactions").get("reactions", [])
        check(
            reactions
            and json.loads(reactions[-1]["reaction"])[0]["emoji"] == "\U0001f44d",
            ":+1: shortcode mapped to unicode thumbs-up",
        )
        r = plat.call("react", {
            "resource_identifier": "123456789",
            "external_id": ext3,
            "emoji": ":white_check_mark:",
        })
        check(r.get("result", {}).get("reacted") is True,
              "react :white_check_mark: -> reacted:true")
        reactions = http_get(base + "/admin/reactions").get("reactions", [])
        check(
            reactions
            and [e["emoji"] for e in json.loads(reactions[-1]["reaction"])]
            == ["\U0001f44d", "\u2705"],
            "terminal reaction sent as combined set [thumbs_up, check]",
        )

        # 6b3. every terminal status shortcode the core sends maps to a valid
        #      unicode emoji; each is reacted on a FRESH message and the stored
        #      reaction JSON carries the mapped glyph.
        status_codes = [
            (":white_check_mark:", "\u2705"),
            (":x:", "\u274c"),
            (":broken_heart:", "\U0001f494"),
            (":o:", "\U0001f17e\ufe0f"),
            (":handshake:", "\U0001f91d"),
        ]
        for shortcode, expected in status_codes:
            r = plat.call("deliver", {
                "resource_identifier": "123456789",
                "content": "Status react " + shortcode,
            })
            ext_s = r.get("result", {}).get("external_id")
            r = plat.call("react", {
                "resource_identifier": "123456789",
                "external_id": ext_s,
                "emoji": shortcode,
            })
            check(r.get("result", {}).get("reacted") is True,
                  "react " + shortcode + " -> reacted:true")
            reactions = http_get(base + "/admin/reactions").get("reactions", [])
            check(
                reactions
                and json.loads(reactions[-1]["reaction"])[-1]["emoji"] == expected,
                shortcode + " mapped to " + expected + " in setMessageReaction",
            )

        # 6b4. ":thumbsup:" and ":thumbs_up:" aliases map to the same thumbs-up
        #      glyph as ":+1:".
        for alias in (":thumbsup:", ":thumbs_up:"):
            r = plat.call("deliver", {
                "resource_identifier": "123456789",
                "content": "Alias react " + alias,
            })
            ext_a = r.get("result", {}).get("external_id")
            r = plat.call("react", {
                "resource_identifier": "123456789",
                "external_id": ext_a,
                "emoji": alias,
            })
            check(r.get("result", {}).get("reacted") is True,
                  "react " + alias + " -> reacted:true")
            reactions = http_get(base + "/admin/reactions").get("reactions", [])
            check(
                reactions
                and json.loads(reactions[-1]["reaction"])[-1]["emoji"]
                == "\U0001f44d",
                alias + " mapped to thumbs-up",
            )

        # 6b5. unknown shortcode -> reacted:false and NO setMessageReaction
        #      call: an invalid emoji string must never reach the API.
        before = len(http_get(base + "/admin/reactions").get("reactions", []))
        r = plat.call("react", {
            "resource_identifier": "123456789",
            "external_id": ext3,
            "emoji": ":not_a_real_emoji:",
        })
        check(r.get("result", {}).get("reacted") is False,
              "react unknown shortcode -> reacted:false")
        after = len(http_get(base + "/admin/reactions").get("reactions", []))
        check(after == before,
              "unknown shortcode -> no setMessageReaction call "
              "(no invalid emoji sent)")

        # 6c. typing -> sendChatAction (action=typing)
        r = plat.call("typing", {"resource_identifier": "123456789"})
        check(r.get("result", {}).get("typing") is True,
              "typing -> typing:true")
        actions = http_get(base + "/admin/actions").get("actions", [])
        check(actions and actions[-1]["action"] == "typing"
              and str(actions[-1]["chat_id"]) == "123456789",
              "mock stored sendChatAction(typing) for correct chat")

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

        # 10. FLAT configure (core protocol): the real core sends the whole
        #     plugins.yml env map FLAT with STRING values and NO "config" key
        #     (build_configure_request -> serde_json::json!(env)). Verify a
        #     fresh plugin instance configured this way stores bot_token,
        #     starts polling and delivers an injected update as
        #     inbound_message. Regression test for the prod bug where the
        #     plugin read params["config"] (absent), so the flat bot_token
        #     was dropped and polling never started.
        plat.stop()
        plat = PlatformProc()
        r = plat.call("configure", {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": "on",
            "poll_interval_secs": "1",
        })
        check(r.get("result", {}).get("configured") is True,
              "flat configure (core protocol) -> configured:true")
        http_post(base + "/admin/inject", {
            "update_id": 9500,
            "message": {
                "message_id": 790,
                "date": 1700000010,
                "chat": {"id": 123456, "type": "private"},
                "from": {"id": 555, "first_name": "Mock"},
                "text": "flat protocol inbound hello",
            },
        })
        n = plat.expect_notification("inbound_message", timeout=25)
        p = n.get("params", {})
        check(p.get("resource_identifier") == "123456"
              and p.get("text") == "flat protocol inbound hello"
              and p.get("external_id") == "790",
              "flat configure: injected update emitted as inbound_message")


        # 11. first_last_only config flag round-trip: the flag is parsed from
        #     both the nested "config" dict and the FLAT core protocol map
        #     (string values), defaults to false when absent, and is echoed
        #     back in the configure result so the core can rely on it.
        plat.stop()
        plat = PlatformProc()
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": True,
            "poll_interval_secs": 1,
            "first_last_only": True,
        }})
        res = r.get("result", {})
        check(res.get("configured") is True
              and res.get("first_last_only") is True,
              "configure(first_last_only=true) -> first_last_only:true")

        plat.stop()
        plat = PlatformProc()
        r = plat.call("configure", {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": "on",
            "poll_interval_secs": "1",
            "first_last_only": "on",
        })
        res = r.get("result", {})
        check(res.get("configured") is True
              and res.get("first_last_only") is True,
              "flat configure(first_last_only=on) -> first_last_only:true")

        plat.stop()
        plat = PlatformProc()
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": True,
            "poll_interval_secs": 1,
        }})
        res = r.get("result", {})
        check(res.get("configured") is True
              and res.get("first_last_only") is False,
              "configure (flag absent) -> first_last_only defaults to false")

        # 12. first_last_only collapse (plugin-scoped): with the flag on, only
        #     the thread's FIRST message (seq-0) and the FINAL message
        #     (is_final=true) reach the chat; intermediate deliveries are
        #     suppressed without any Telegram API call. With the flag off
        #     (default) every delivery is sent - core sends the full stream.
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": False,
            "poll_interval_secs": 1,
            "first_last_only": True,
        }})
        check(r.get("result", {}).get("configured") is True,
              "configure(first_last_only=true) -> configured:true")
        sent_before = len(http_get(base + "/admin/sent").get("messages", []))

        # intermediate delivery (seq>0, not final) -> suppressed, no API call
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "tool call record",
            "msg_type": "tool",
            "thread_sequence": 5,
            "is_final": False,
        })
        res = r.get("result", {})
        check(res.get("delivered") is False and res.get("suppressed") is True,
              "first_last_only: intermediate delivery suppressed")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(len(sent) == sent_before,
              "first_last_only: suppressed delivery makes NO Telegram API call")

        # first message by position (seq-0, the prompt/cause) -> delivered
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "seq0 prompt",
            "thread_sequence": 0,
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "first_last_only: seq-0 (first) message delivered")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "seq0 prompt",
              "first_last_only: seq-0 message reaches the chat")

        # final message (is_final=true) -> delivered even when seq>0
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "final summary",
            "msg_type": "summary",
            "thread_sequence": 7,
            "is_final": True,
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "first_last_only: final message delivered")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "final summary",
              "first_last_only: final message reaches the chat")

        # final delivery derives reply_to from cause_external_id when the
        # core does not carry reply_to_message_id (plugin-agnostic core).
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "final with derived reply",
            "msg_type": "summary",
            "thread_sequence": 8,
            "is_final": True,
            "cause_external_id": "202",
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "first_last_only: final delivery with cause_external_id delivered")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "final with derived reply"
              and str(sent[-1].get("reply_to_message_id")) == "202",
              "final delivery replies to seq-0 via cause_external_id (reply_to=202)")

        # flag off (default): every delivery is sent, including intermediates
        r = plat.call("configure", {"config": {
            "bot_token": MOCK_TOKEN,
            "api_base_url": base,
            "polling_enabled": False,
            "poll_interval_secs": 1,
            "first_last_only": False,
        }})
        check(r.get("result", {}).get("configured") is True,
              "configure(first_last_only=false) -> configured:true")
        r = plat.call("deliver", {
            "resource_identifier": "123456789",
            "content": "intermediate when flag off",
            "thread_sequence": 5,
            "is_final": False,
        })
        res = r.get("result", {})
        check(res.get("delivered") is True and res.get("external_id"),
              "first_last_only=false: intermediate delivery sent (full stream)")
        sent = http_get(base + "/admin/sent").get("messages", [])
        check(sent and sent[-1]["text"] == "intermediate when flag off",
              "first_last_only=false: intermediate message reaches the chat")

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
