#!/usr/bin/env python3
"""
Telegram platform plugin for OmniAgent.

Implements the OmniAgent platform plugin protocol (JSON lines over stdin/stdout):

  * initialize      -> reports name "telegram" + capabilities (inbound + outbound)
  * configure       -> stores bot_token, api_base_url, polling settings
  * deliver         -> Telegram Bot API sendMessage to a chat
  * edit_message    -> Telegram Bot API editMessageText
  * delete_message  -> Telegram Bot API deleteMessage
  * react           -> Telegram Bot API setMessageReaction
  * typing          -> Telegram Bot API sendChatAction (action=typing)

Inbound: when polling_enabled is true a background thread long-polls
getUpdates (offset-based) and emits `inbound_message` notifications to
stdout, exactly like the mattermost platform does.

Config flag `first_last_only` (boolean, default false): the omniagent core
reads this flag from the telegram plugin config and, when true, delivers only
the FIRST and LAST messages of a thread run to this plugin (intermediate
thread messages are collapsed). The plugin itself parses and echoes the flag
so the configure round-trip is complete and testable.

Mock support: the `api_base_url` config override lets the whole plugin run
against a mock Telegram Bot API server (see tests/mock_telegram_api.py) -
no real bot token is ever needed for tests.

Uses only the Python standard library (urllib) - no external dependencies.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [telegram-platform] %(levelname)s %(message)s",
)
log = logging.getLogger("telegram-platform")

DEFAULT_API_BASE = "https://api.telegram.org"
POLL_LONGPOLL_SECS = 30  # Telegram getUpdates long-poll upper bound


def _as_bool(value, default=False):
    """Coerce a configure value to bool.

    The core sends configure params as the FLAT plugins.yml env map with
    string values (e.g. "on", "false"); the plugin's own tests send real
    booleans inside a "config" dict. Both must behave identically.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "on", "yes")


class TelegramPlatform:
    def __init__(self):
        self.bot_token = ""
        self.api_base_url = DEFAULT_API_BASE
        self.polling_enabled = False
        self.poll_interval_secs = 5
        self.parent_by_chat = False
        self.first_last_only = False
        self._offset = None
        self._poll_thread = None
        self._stop = threading.Event()
        self._stdout_lock = threading.Lock()
        self._configured = False

    # ------------------------------------------------------------------
    # stdout helpers (single write per line so polling thread + main
    # thread never interleave partial JSON)
    # ------------------------------------------------------------------
    def _write_json(self, obj):
        line = json.dumps(obj)
        with self._stdout_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _respond(self, req_id, result=None, error=None):
        obj = {"id": req_id}
        if error is not None:
            obj["error"] = error
        else:
            obj["result"] = result if result is not None else {}
        self._write_json(obj)

    # ------------------------------------------------------------------
    # Telegram Bot API client (stdlib only)
    # ------------------------------------------------------------------
    def _api_post(self, method, params):
        """POST {api_base}/bot{token}/{method} with form-encoded params.

        Returns the decoded JSON body. Raises TelegramApiError on HTTP or
        API-level errors (ok:false).
        """
        if not self.bot_token:
            raise TelegramApiError("bot_token is not configured")
        url = "{}/bot{}/{}".format(self.api_base_url.rstrip("/"),
                                   self.bot_token, method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise TelegramApiError("HTTP {} from Telegram API: {}".format(
                e.code, detail))
        except urllib.error.URLError as e:
            raise TelegramApiError("Cannot reach Telegram API: {}".format(e.reason))
        if not body.get("ok"):
            desc = body.get("description", str(body))
            raise TelegramApiError("Telegram API error on {}: {}".format(method, desc))
        return body.get("result")

    def _chat_id(self, resource_identifier):
        """Telegram chat ids are integers or @-prefixed usernames; keep as-is."""
        return resource_identifier

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------
    def handle_initialize(self, req_id):
        self._respond(req_id, result={
            "name": "telegram",
            "capabilities": {
                "inbound": True,
                "outbound": True,
            },
        })
        log.info("Initialized: telegram")

    def handle_configure(self, req_id, params):
        # The core sends configure params FLAT: the resolved plugins.yml env
        # map, e.g. {"bot_token": "...", "polling_enabled": "on"} (see
        # build_configure_request in src/platform/external/mod.rs). Older
        # callers and the plugin's own tests wrap the values under a "config"
        # key; accept both shapes.
        config = params.get("config") or params
        self.bot_token = str(config.get("bot_token", "") or "").strip()
        self.api_base_url = str(config.get("api_base_url", "") or "").strip() \
            or DEFAULT_API_BASE
        self.polling_enabled = _as_bool(config.get("polling_enabled", True))
        try:
            interval = int(config.get("poll_interval_secs", 5))
            self.poll_interval_secs = max(1, min(300, interval))
        except (TypeError, ValueError):
            self.poll_interval_secs = 5
        self.parent_by_chat = _as_bool(config.get("parent_by_chat", False))
        self.first_last_only = _as_bool(config.get("first_last_only", False))
        self._configured = True
        log.info("Configured: api_base=%s polling=%s interval=%ss "
                 "parent_by_chat=%s first_last_only=%s token_set=%s",
                 self.api_base_url, self.polling_enabled,
                 self.poll_interval_secs, self.parent_by_chat,
                 self.first_last_only, bool(self.bot_token))

        if self.polling_enabled and self.bot_token:
            self._start_polling()

        self._respond(req_id, result={
            "configured": True,
            "polling_enabled": self.polling_enabled and bool(self.bot_token),
            "first_last_only": self.first_last_only,
        })

    def handle_deliver(self, req_id, params):
        resource = params.get("resource_identifier", "")
        content = params.get("content", "")
        try:
            result = self._api_post("sendMessage", {
                "chat_id": self._chat_id(resource),
                "text": content,
            })
            external_id = str(result.get("message_id", ""))
            self._respond(req_id, result={
                "delivered": True,
                "external_id": external_id,
            })
            log.info("Delivered message %s to chat %s", external_id, resource)
        except TelegramApiError as e:
            log.error("deliver failed: %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    def handle_edit_message(self, req_id, params):
        resource = params.get("resource_identifier", "")
        external_id = params.get("external_id", "")
        content = params.get("content", "")
        try:
            self._api_post("editMessageText", {
                "chat_id": self._chat_id(resource),
                "message_id": external_id,
                "text": content,
            })
            self._respond(req_id, result={"edited": True})
            log.info("Edited message %s in chat %s", external_id, resource)
        except TelegramApiError as e:
            log.error("edit_message failed: %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    def handle_delete_message(self, req_id, params):
        resource = params.get("resource_identifier", "")
        external_id = params.get("external_id", "")
        try:
            self._api_post("deleteMessage", {
                "chat_id": self._chat_id(resource),
                "message_id": external_id,
            })
            self._respond(req_id, result={"deleted": True})
            log.info("Deleted message %s in chat %s", external_id, resource)
        except TelegramApiError as e:
            log.error("delete_message failed: %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    def handle_typing(self, req_id, params):
        resource = params.get("resource_identifier", "")
        try:
            self._api_post("sendChatAction", {
                "chat_id": self._chat_id(resource),
                "action": "typing",
            })
            self._respond(req_id, result={"typing": True})
            log.info("Typing indicator sent to chat %s", resource)
        except TelegramApiError as e:
            log.error("typing failed: %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    def handle_react(self, req_id, params):
        resource = params.get("resource_identifier", "")
        external_id = params.get("external_id", "")
        emoji = params.get("emoji", "")
        # Map Mattermost-style shortcodes to the unicode emoji the Telegram
        # Bot API requires; unknown values fall back to the bare name.
        emoji = SHORTCODE_TO_EMOJI.get(emoji, emoji.strip(":"))
        try:
            reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
            self._api_post("setMessageReaction", {
                "chat_id": self._chat_id(resource),
                "message_id": external_id,
                "reaction": json.dumps(reaction),
            })
            self._respond(req_id, result={"reacted": True})
            log.info("Reacted %s to message %s in chat %s",
                     emoji, external_id, resource)
        except TelegramApiError as e:
            log.error("react failed: %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    # ------------------------------------------------------------------
    # Inbound: long-poll getUpdates
    # ------------------------------------------------------------------
    def _start_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram-poll")
        self._poll_thread.start()
        log.info("Inbound polling started (interval=%ss)", self.poll_interval_secs)

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                params = {
                    "timeout": POLL_LONGPOLL_SECS,
                    # message + channel_post cover private chats and channels;
                    # edited_message lets us forward edits to the agent.
                    "allowed_updates": json.dumps([
                        "message", "channel_post",
                        "edited_message", "edited_channel_post",
                    ]),
                }
                if self._offset is not None:
                    params["offset"] = self._offset
                updates = self._api_post("getUpdates", params)
                for update in updates or []:
                    self._handle_update(update)
                    upd_id = update.get("update_id")
                    if upd_id is not None:
                        self._offset = int(upd_id) + 1
            except TelegramApiError as e:
                log.warning("getUpdates failed: %s", e)
                # Error backoff: don't hot-loop on persistent failures.
                self._stop.wait(min(self.poll_interval_secs, 30))
            except Exception as e:  # pragma: no cover - defensive
                log.warning("poll loop error: %s", e)
                self._stop.wait(min(self.poll_interval_secs, 30))
            else:
                # Short sleep between long-polls to keep offset commits sane.
                self._stop.wait(max(0.5, min(self.poll_interval_secs, 5)))

    def _handle_update(self, update):
        for kind in ("message", "channel_post"):
            msg = update.get(kind)
            if msg:
                self._emit_inbound_message(msg)
                return
        for kind in ("edited_message", "edited_channel_post"):
            msg = update.get(kind)
            if msg:
                self._emit_edited_message(msg)
                return

    def _emit_inbound_message(self, msg):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = msg.get("text") or msg.get("caption") or ""
        external_id = str(msg.get("message_id", ""))
        metadata = {
            "chat_id": chat_id,
            "chat_type": chat.get("type", ""),
            "from_id": (msg.get("from") or {}).get("id"),
            "date": msg.get("date"),
            "message_id": msg.get("message_id"),
        }
        if self.parent_by_chat:
            # Deliver the chat id as the parent external id (the SAME value for
            # every message from this chat), so threads created from messages in
            # this chat always share one parent. omniagent reads
            # metadata["root_id"] as the parent external id for inbound messages
            # (the same envelope key the mattermost platform uses for its thread
            # root) - the existing pending/merge machinery then merges a pending
            # same-parent message into a processing thread per its percent /
            # char-amount thresholds. When parent_by_chat is false (default) no
            # parent id is set: identical to current behavior.
            metadata["root_id"] = str(chat_id)
        self._write_json({
            "method": "inbound_message",
            "params": {
                "resource_identifier": str(chat_id),
                "text": text,
                "external_id": external_id,
                "files": [],
                "metadata": metadata,
            },
        })
        log.info("Inbound message %s from chat %s: %s",
                 external_id, chat_id, text[:60])

    def _emit_edited_message(self, msg):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = msg.get("text") or msg.get("caption") or ""
        external_id = str(msg.get("message_id", ""))
        self._write_json({
            "method": "message_edited",
            "params": {
                "resource_identifier": str(chat_id),
                "external_id": external_id,
                "text": text,
            },
        })
        log.info("Edited message %s in chat %s: %s",
                 external_id, chat_id, text[:60])

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        log.info("Telegram platform plugin starting (PID=%d)", os.getpid())
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                log.error("Failed to parse JSON: %s", e)
                continue
            method = request.get("method", "")
            req_id = request.get("id")
            params = request.get("params") or {}

            if method == "initialize":
                self.handle_initialize(req_id)
            elif method == "configure":
                self.handle_configure(req_id, params)
            elif method == "deliver":
                self.handle_deliver(req_id, params)
            elif method == "edit_message":
                self.handle_edit_message(req_id, params)
            elif method == "delete_message":
                self.handle_delete_message(req_id, params)
            elif method == "react":
                self.handle_react(req_id, params)
            elif method == "typing":
                self.handle_typing(req_id, params)
            elif method == "shutdown":
                self._stop.set()
                self._respond(req_id, result={"shutdown": True})
            else:
                log.warning("Unknown method: %s", method)
                if req_id is not None:
                    self._respond(req_id, error={
                        "code": -1,
                        "message": "Unknown method: {}".format(method),
                    })
        self._stop.set()
        log.info("Telegram platform plugin shutting down (stdin closed)")


# Mattermost-style emoji shortcodes used by the core for status reactions
# (e.g. ":o:", ":handshake:") are not valid Telegram emoji. Map the known
# ones to their unicode glyphs before calling setMessageReaction.
SHORTCODE_TO_EMOJI = {
    ":white_check_mark:": "\u2705",
    ":x:": "\u274c",
    ":broken_heart:": "\U0001f494",
    ":o:": "\U0001f17e\ufe0f",
    ":handshake:": "\U0001f91d",
}


class TelegramApiError(Exception):
    pass


def main():
    platform = TelegramPlatform()
    platform.run()


if __name__ == "__main__":
    main()
