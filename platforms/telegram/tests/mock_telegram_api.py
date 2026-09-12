#!/usr/bin/env python3
"""
Mock Telegram Bot API server for testing the omniagent telegram platform
plugin WITHOUT a real bot token.

Implements the Bot API endpoints the plugin uses, backed by in-memory state:

  POST /bot<token>/getUpdates          long-poll; offset-based, confirmed updates removed
  POST /bot<token>/sendMessage         stores a sent message, returns message_id
  POST /bot<token>/editMessageText     updates the stored text
  POST /bot<token>/deleteMessage       removes the stored message
  POST /bot<token>/setMessageReaction  stores the reaction
  POST /bot<token>/sendChatAction      stores the chat action
  POST /bot<token>/getMe               returns a fake bot identity

Admin endpoints (for tests, no token required):
  POST /admin/inject                   inject an inbound update (JSON body)
  GET  /admin/sent                     list outbound messages the plugin sent
  GET  /admin/reactions                list stored reactions
  GET  /admin/updates                  list updates still in the queue
  POST /admin/reset                    clear all in-memory state

Every /bot<token>/... call accepts ANY non-empty token - the point is to
verify the platform's protocol/payloads, not real credentials. No request
ever leaves localhost.

Usage:
    python3 mock_telegram_api.py --port 8091
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# Emojis the REAL Telegram Bot API accepts in setMessageReaction. VERIFIED
# against the production bot token on 2026-09-12 with one API call per
# candidate; anything outside this set answers
# "Bad Request: REACTION_INVALID". The mock enforces the same rule because the
# old mock accepted ANY emoji: that is why the suite stayed green while
# production never replaced the +1 on completed/failed/skipped threads.
VALID_REACTION_EMOJIS = {
    "\U0001f44d",  # 1F44D thumbs up
    "\U0001f44e",  # 1F44E thumbs down
    "\u2764",       # 2764  heart
    "\U0001f525",  # 1F525 fire
    "\U0001f389",  # 1F389 party
    "\U0001f4af",  # 1F4AF hundred
    "\U0001f3c6",  # 1F3C6 trophy
    "\U0001f64f",  # 1F64F pray
    "\U0001f914",  # 1F914 thinking
    "\U0001f622",  # 1F622 crying
    "\U0001f921",  # 1F921 clown
    "\U0001f634",  # 1F634 sleeping
    "\U0001f937",  # 1F937 shrug
    "\U0001f44c",  # 1F44C ok hand
    "\U0001fae1",  # 1FAE1 saluting face
    "\U0001f929",  # 1F929 star struck
    "\U0001f494",  # 1F494 broken heart
    "\U0001f91d",  # 1F91D handshake
    "\U0001f440",  # 1F440 eyes (plugin default)
}


class MockTelegramState:
    """Thread-safe in-memory state shared by all handler threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent_messages = []      # list of dicts, in send order
        self.chat_actions = []      # list of {chat_id, action, date}
        self.reactions = []          # list of {chat_id, message_id, reaction}
        self.updates = {}            # update_id -> update dict (queue)
        self.next_update_id = 1
        self.next_message_id = 1000
        self.get_updates_calls = 0
        self.token = None            # last-seen token (any non-empty accepted)
        self.bot_fail = False      # when True every /bot<token>/* POST returns HTTP 500
        self.bot_delay = 0.0      # seconds each sendMessage sleeps (slow-API simulation)
        # Emojis this mock accepts in setMessageReaction. Defaults to the real
        # Telegram reaction set; /admin/reaction_allow can narrow it to
        # simulate a REJECTED (invalid) mapping.
        self.valid_reaction_emojis = set(VALID_REACTION_EMOJIS)

    # -- outbound message store ----------------------------------------
    def record_sent(self, chat_id, text, extra=None):
        with self.lock:
            mid = self.next_message_id
            self.next_message_id += 1
            entry = {"message_id": mid, "chat_id": chat_id, "text": text,
                     "date": int(time.time())}
            if extra:
                entry.update(extra)
            self.sent_messages.append(entry)
            return mid

    def edit_text(self, chat_id, message_id, text, parse_mode=None):
        with self.lock:
            for m in self.sent_messages:
                if (str(m["message_id"]) == str(message_id)
                        and str(m["chat_id"]) == str(chat_id)):
                    m["text"] = text
                    if parse_mode is not None:
                        m["parse_mode"] = parse_mode
                    return True
            return False

    def delete_message(self, chat_id, message_id):
        with self.lock:
            before = len(self.sent_messages)
            self.sent_messages = [
                m for m in self.sent_messages
                if not (str(m["message_id"]) == str(message_id)
                        and str(m["chat_id"]) == str(chat_id))
            ]
            return len(self.sent_messages) < before

    def record_reaction(self, chat_id, message_id, reaction):
        with self.lock:
            self.reactions.append({"chat_id": chat_id,
                                   "message_id": message_id,
                                   "reaction": reaction})

    def record_chat_action(self, chat_id, action):
        with self.lock:
            self.chat_actions.append({"chat_id": chat_id,
                                      "action": action,
                                      "date": int(time.time())})

    # -- inbound update queue ------------------------------------------
    def inject_update(self, update):
        with self.lock:
            uid = int(update.get("update_id", self.next_update_id))
            update["update_id"] = uid
            if uid >= self.next_update_id:
                self.next_update_id = uid + 1
            self.updates[uid] = update
            return uid

    def poll_updates(self, offset, timeout):
        """Block up to `timeout` seconds for updates with update_id >= offset.
        Returns (updates, next_offset); confirmed updates are removed."""
        if offset is not None:
            try:
                offset = int(offset)
            except (TypeError, ValueError):
                offset = None
        deadline = time.time() + max(0.1, min(timeout, 2.0))
        while time.time() < deadline:
            with self.lock:
                ready = sorted(
                    u for i, u in self.updates.items()
                    if offset is None or i >= offset)
                if ready:
                    for u in ready:
                        self.updates.pop(u["update_id"], None)
                    return ready
            time.sleep(0.05)
        return []


class MockHandler(BaseHTTPRequestHandler):
    state = MockTelegramState()

    def log_message(self, fmt, *args):  # silence request logging
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read the request body. Telegram clients send JSON; urllib-based
        clients (the python platform) send application/x-www-form-urlencoded -
        parse both. Values become plain strings (like the real Bot API)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type", "") or "").lower()
        if "application/json" in ctype:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        # form-encoded fallback (also try JSON parse for loose clients)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        parsed = parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def _path(self):
        return urlparse(self.path).path

    def do_POST(self):
        path = self._path()
        body = self._read_body()

        if path.startswith("/admin/"):
            self._handle_admin(path, body)
            return

        # /bot<token>/<method>
        if not path.startswith("/bot"):
            self._json(404, {"ok": False, "description": "not found"})
            return
        rest = path[len("/bot"):]
        if "/" not in rest:
            self._json(404, {"ok": False, "description": "bad path"})
            return
        token, method = rest.split("/", 1)
        if not token:
            self._json(401, {"ok": False, "description": "Unauthorized: empty token"})
            return
        self.state.token = token

        if self.state.bot_fail:
            self._json(500, {"ok": False,
                           "description": "mock failure mode: bot API 500"})
            return
        if method == "getMe":
            self._json(200, {"ok": True, "result": {
                "id": 424242,
                "is_bot": True,
                "first_name": "MockTelegramBot",
                "username": "mock_telegram_bot",
            }})
            self.state.get_updates_calls += 1
        elif method == "getUpdates":
            offset = body.get("offset")
            try:
                timeout = int(body.get("timeout", 0) or 0)
            except (TypeError, ValueError):
                timeout = 0
            updates = self.state.poll_updates(offset, timeout)
            self._json(200, {"ok": True, "result": updates})
        elif method == "sendMessage":
            if self.state.bot_delay:
                time.sleep(self.state.bot_delay)
            chat_id = body.get("chat_id")
            text = body.get("text", "")
            # Reply-targeting test hook: reply_to_message_id "404" simulates a
            # stale seq-0 message id (the API rejects the reply).
            if "reply_to_message_id" in body and str(body.get("reply_to_message_id")) == "404":
                self._json(400, {"ok": False, "description": "message not found"})
                return
            # HTML rejection hook: a parse_mode=HTML send whose text contains
            # the REJECT_HTML sentinel simulates Telegram refusing to parse
            # the entities, so the plugin's plain-text fallback is testable.
            if body.get("parse_mode") == "HTML" and "REJECT_HTML" in text:
                self._json(400, {"ok": False,
                                 "description": "can't parse entities: mock rejection"})
                return
            # Telegram's real limit: sendMessage text is 1-4096 characters
            # after entities parsing. Enforce it so an over-long delivery is
            # rejected exactly like the real Bot API (regression: completed
            # thread 792's final answer was silently dropped this way).
            if len(text) > 4096:
                self._json(400, {"ok": False,
                                 "description": "message is too long"})
                return
            extra = {"chat": {"id": chat_id, "type": "private"}}
            if "reply_to_message_id" in body:
                extra["reply_to_message_id"] = body.get("reply_to_message_id")
            if "parse_mode" in body:
                extra["parse_mode"] = body.get("parse_mode")
            mid = self.state.record_sent(chat_id, text,
                                         extra)
            self._json(200, {"ok": True, "result": {
                "message_id": mid,
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
                "date": int(time.time()),
            }})
        elif method == "editMessageText":
            ok = self.state.edit_text(body.get("chat_id"),
                                      body.get("message_id"),
                                      body.get("text", ""),
                                      body.get("parse_mode"))
            if not ok:
                self._json(400, {"ok": False, "description": "message not found"})
                return
            self._json(200, {"ok": True, "result": {"ok": True}})
        elif method == "deleteMessage":
            ok = self.state.delete_message(body.get("chat_id"),
                                           body.get("message_id"))
            if not ok:
                self._json(400, {"ok": False, "description": "message not found"})
                return
            self._json(200, {"ok": True, "result": {"ok": True}})
        elif method == "setMessageReaction":
            raw = body.get("reaction")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (TypeError, ValueError):
                parsed = None
            if parsed is None:
                self._json(400, {"ok": False,
                                 "description": "Bad Request: can't parse reaction"})
                return
            rejected = [r.get("emoji") for r in parsed
                        if r.get("type") == "emoji"
                        and r.get("emoji") not in self.state.valid_reaction_emojis]
            if rejected:
                # Mirror the REAL Bot API: an emoji outside Telegram's reaction
                # set is rejected with REACTION_INVALID (incident 2026-09-12).
                self._json(400, {"ok": False,
                                 "description": "Bad Request: REACTION_INVALID"})
                return
            self.state.record_reaction(body.get("chat_id"),
                                       body.get("message_id"),
                                       raw)
            self._json(200, {"ok": True, "result": {"ok": True}})
        elif method == "sendChatAction":
            self.state.record_chat_action(body.get("chat_id"),
                                          body.get("action"))
            self._json(200, {"ok": True, "result": {"ok": True}})
        else:
            self._json(404, {"ok": False, "description": "unknown method: " + method})

    def _handle_admin(self, path, body):
        if path == "/admin/inject":
            uid = self.state.inject_update(body)
            self._json(200, {"ok": True, "update_id": uid})
        elif path == "/admin/sent":
            self._json(200, {"ok": True, "messages": self.state.sent_messages})
        elif path == "/admin/reactions":
            self._json(200, {"ok": True, "reactions": self.state.reactions})
        elif path == "/admin/actions":
            self._json(200, {"ok": True, "actions": self.state.chat_actions})
        elif path == "/admin/updates":
            self._json(200, {"ok": True, "updates": sorted(
                self.state.updates.values(), key=lambda u: u["update_id"])})
        elif path == "/admin/reaction_allow":
            # Narrow (or reset) the accepted reaction emojis. Body
            # {"emojis": [...]} narrows the set; an empty body resets it to the
            # real Telegram reaction set. Used to exercise the REACTION_INVALID
            # fallback without patching the plugin.
            if isinstance(body, dict) and "emojis" in body:
                self.state.valid_reaction_emojis = set(body.get("emojis") or [])
            else:
                self.state.valid_reaction_emojis = set(VALID_REACTION_EMOJIS)
            self._json(200, {"ok": True,
                             "emojis": sorted(self.state.valid_reaction_emojis)})
        elif path == "/admin/fail":
            if isinstance(body, dict) and "on" in body:
                self.state.bot_fail = bool(body.get("on"))
            self._json(200, {"ok": True, "bot_fail": self.state.bot_fail})
        elif path == "/admin/delay":
            if isinstance(body, dict) and "secs" in body:
                try:
                    self.state.bot_delay = max(0.0, float(body.get("secs")))
                except (TypeError, ValueError):
                    pass
            self._json(200, {"ok": True, "bot_delay": self.state.bot_delay})
        elif path == "/admin/reset":
            MockHandler.state = MockTelegramState()
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "description": "unknown admin endpoint"})

    def do_GET(self):
        path = self._path()
        if path == "/admin/sent":
            self._json(200, {"ok": True, "messages": self.state.sent_messages})
        elif path == "/admin/reactions":
            self._json(200, {"ok": True, "reactions": self.state.reactions})
        elif path == "/admin/actions":
            self._json(200, {"ok": True, "actions": self.state.chat_actions})
        elif path == "/admin/updates":
            self._json(200, {"ok": True, "updates": sorted(
                self.state.updates.values(), key=lambda u: u["update_id"])})
        elif path == "/admin/updates_calls":
            self._json(200, {"ok": True, "calls": self.state.get_updates_calls})
        elif path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "description": "not found"})


def main():
    parser = argparse.ArgumentParser(description="Mock Telegram Bot API server")
    parser.add_argument("--port", type=int, default=8091,
                        help="port to listen on (default 8091)")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print("Mock Telegram API listening on http://{}:{}".format(
        args.host, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
