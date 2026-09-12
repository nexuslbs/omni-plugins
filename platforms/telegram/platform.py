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

Outbound reliability + rate limiting (2026-09-12):
  * EVERY call passes through ONE shared outbound rate gate (a global
    min-interval plus a per-chat min-interval for the send* methods) and HTTP
    429 is honored (parameters.retry_after) with a bounded, interruptible
    retry budget - the plugin must not hammer the Bot API into a temporary
    connection block;
  * a reply delivery that the API REJECTED is retried once with the SAME
    reply_to_message_id (threading preserved) before the standalone fallback;
    a connection-level failure is never blind-retried (delivery is UNKNOWN,
    so a retry could duplicate the message);
  * the typing signal is throttled per chat (`typing_interval_seconds`,
    default 5 s; an explicit 0/empty disables it) and is DROPPED - never
    queued - when the rate gate is busy, because it is purely cosmetic.

Inbound: when polling_enabled is true a background thread long-polls
getUpdates (offset-based) and emits `inbound_message` notifications to
stdout, exactly like the mattermost platform does.

Config flag `first_last_only` (boolean, default true): telegram-specific
delivery collapse implemented INSIDE this plugin. When true, the plugin
delivers only the thread's FIRST message (seq-0, the prompt/cause) and the
FINAL message of the run; every intermediate delivery is suppressed (the
message stays persisted in the thread history, only the chat delivery is
skipped). Core message delivery is plugin-agnostic: it sends the full
message stream and the platform plugin decides what to send. The plugin
parses and echoes the flag so the configure round-trip is complete and
testable.

Mock support: the `api_base_url` config override lets the whole plugin run
against a mock Telegram Bot API server (see tests/mock_telegram_api.py) -
no real bot token is ever needed for tests.

Uses only the Python standard library (urllib) - no external dependencies.
"""

import json
import logging
import os
import re
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
TG_MSG_LIMIT = 4096    # sendMessage hard limit: 1-4096 chars after parsing
TG_MSG_BUDGET = 3800   # raw markdown budget per chunk (HTML rendering inflates)


# ----------------------------------------------------------------------
# Outbound rate limiting + retry budget (operator request, 2026-09-12)
# ----------------------------------------------------------------------
# Telegram throttles bots and answers HTTP 429 ({"ok": false,
# "parameters": {"retry_after": N}}) when a bot is too fast; sustained abuse
# escalates to a temporary block of the connection (every call answers "Too
# Many Requests: retry after N"), which is what a polling plugin can hit.
# EVERY outbound call in this plugin therefore passes through ONE shared gate:
#   * GLOBAL_MIN_INTERVAL_SECS: minimum spacing between ANY two API calls
#     (far below Telegram's ~30 calls/second ceiling);
#   * CHAT_SEND_MIN_INTERVAL_SECS: minimum spacing between two send* calls to
#     the SAME chat (Telegram allows about 1 message/second in a private chat
#     and about 20/minute in a group).
# The gate is a per-chat min-interval gate, not a queue: a caller waits for
# its own slot, so a burst can never build an unbounded backlog. The cosmetic
# typing indicator is special-cased: when the gate is busy it is DROPPED
# instead of waiting (see handle_typing) - it must never delay real messages.
GLOBAL_MIN_INTERVAL_SECS = 0.05
CHAT_SEND_MIN_INTERVAL_SECS = 1.0
# Bounded retry budget for transient Telegram failures (429 / 5xx / network):
# small and explicit - never an infinite loop.
API_MAX_ATTEMPTS = 3
API_RETRY_BACKOFF_SECS = 1.0
# A single 429 retry_after wait is capped so an absurd value can never stall
# the plugin indefinitely; the wait itself is interruptible by shutdown.
MAX_RETRY_AFTER_SECS = 60.0
API_TIMEOUT_SECS = 60
# Typing throttle default: at most ONE sendChatAction per chat every N seconds,
# no matter how many typing requests core enqueues (core enqueues one every 5 s
# for the whole run, ~12/min per active thread - the plugin's biggest
# contributor to Bot API call volume). 5 s matches Telegram's own typing-action
# lifetime, so the indicator stays visible while the agent works. An explicit 0
# (or empty) config value suppresses the typing signal entirely.
DEFAULT_TYPING_INTERVAL_SECS = 5.0
# Methods whose CONNECTION-level failure is safe to retry: they are reads or
# idempotent replacements, so a retry cannot duplicate a user-visible message.
# send* methods are deliberately absent: for those a connection failure leaves
# delivery UNKNOWN and a blind retry could double-post the message.
CONNECTION_RETRY_METHODS = frozenset({
    "getUpdates", "editMessageText", "setMessageReaction",
})


def _parse_retry_after(raw):
    """Extract Telegram's parameters.retry_after (seconds) from an error body.

    Accepts a raw JSON string or an already-decoded body; returns None when the
    body carries no usable retry_after (the common case).
    """
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        try:
            raw = json.dumps(raw)
        except (TypeError, ValueError):
            return None
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    params = body.get("parameters")
    if not isinstance(params, dict):
        return None
    try:
        value = float(params.get("retry_after"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _read_error_body(err):
    """Read an HTTPError body once: used for the log message AND for parsing
    parameters.retry_after. Truncated; "" when unreadable."""
    try:
        return err.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        return ""


class OutboundRateGate:
    """ONE shared outbound rate-limit gate for every Telegram API call.

    See the module-level constants for the rationale. `now` and `sleep` are
    injectable so the throttle is testable with a fake clock (no real sleeping
    in tests); `sleep(seconds)` must return True when shutdown was requested.
    """

    def __init__(self, stop_event, global_interval=GLOBAL_MIN_INTERVAL_SECS,
                 chat_interval=CHAT_SEND_MIN_INTERVAL_SECS,
                 now=time.monotonic, sleep=None):
        self._lock = threading.Lock()
        self._stop = stop_event
        self.global_interval = float(global_interval)
        self.chat_interval = float(chat_interval)
        self._now = now
        self._sleep = sleep or stop_event.wait
        self._last_global = 0.0
        self._last_chat = {}

    def _wait_needed(self, chat_id, is_send):
        now = self._now()
        wait = self.global_interval - (now - self._last_global)
        if is_send and chat_id is not None:
            wait = max(wait, self.chat_interval
                       - (now - self._last_chat.get(chat_id, 0.0)))
        # Waits below the float-precision floor count as satisfied: a coarse or
        # injected clock can otherwise leave a ~1e-14 remainder that never
        # reaches 0 and spins the acquire loop forever.
        return 0.0 if wait <= 1e-6 else wait

    def _stamp(self, chat_id, is_send):
        now = self._now()
        self._last_global = now
        if is_send and chat_id is not None:
            self._last_chat[chat_id] = now

    def try_acquire(self, chat_id=None, is_send=True):
        """Reserve a slot WITHOUT waiting.

        False means the gate is busy and the caller must DROP the request -
        used for the cosmetic typing indicator, which must never queue, never
        wait and never delay deliver/edit/react.
        """
        with self._lock:
            if self._wait_needed(chat_id, is_send) > 0:
                return False
            self._stamp(chat_id, is_send)
            return True

    def acquire(self, chat_id=None, is_send=True):
        """Block until the caller's slot is free.

        Returns False when shutdown interrupted the wait, so the caller aborts
        the API call instead of proceeding after a stop request.
        """
        while True:
            with self._lock:
                wait = self._wait_needed(chat_id, is_send)
                if wait <= 0:
                    self._stamp(chat_id, is_send)
                    return True
            # Sleep in small slices so shutdown stays responsive, and re-check
            # the clock on every wake-up (another caller may have moved it).
            if self._sleep(min(wait, 0.25)):
                return False


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
        # Schema defaults (plugin.json) are true: the messaging platform must
        # work out of the box. An explicit false in plugins.yml (an unchecked
        # dashboard box) still disables each flag.
        self.parent_by_chat = True
        self.first_last_only = True
        self._offset = None
        self._poll_thread = None
        # Plugin-wide shutdown signal (stdin closed / "shutdown" method). The
        # outbound rate gate and the 429 backoff wait on THIS event.
        self._stop = threading.Event()
        # Polling has its OWN stop event: a configure that disables polling (or
        # reconfigures the token) must stop the getUpdates thread WITHOUT
        # signalling a plugin shutdown - otherwise the outbound gate would
        # abort every later API call ("shutdown while waiting for the outbound
        # rate gate") and deliveries would be dropped. Regression fixed
        # 2026-09-12 (the gate originally shared the polling event).
        self._poll_stop = threading.Event()
        self._stdout_lock = threading.Lock()
        self._configured = False
        # Outbound rate limiting + typing throttle (2026-09-12).
        self._gate = OutboundRateGate(self._stop)
        self.typing_interval_secs = DEFAULT_TYPING_INTERVAL_SECS
        self._typing_lock = threading.Lock()
        self._last_typing = {}

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
    def _api_post(self, method, params, attempts=None, gate_acquired=False):
        """POST {api_base}/bot{token}/{method} with form-encoded params.

        Every call goes through the ONE shared outbound rate gate and carries a
        bounded retry budget for transient failures (Part B, 2026-09-12):

          * HTTP 429 -> honor ``parameters.retry_after`` (interruptible wait,
            capped at MAX_RETRY_AFTER_SECS) and retry. A 429 is NEVER turned
            into an immediate second attempt.
          * HTTP 5xx -> retry after a short backoff: the API ANSWERED with an
            error, so delivery provably did not happen and a retry cannot
            duplicate a message.
          * connection-level failure (urllib URLError / timeout) -> delivery is
            UNKNOWN, so only methods whose retry cannot duplicate a
            user-visible message are retried (CONNECTION_RETRY_METHODS).
          * any other 4xx (parse/entity rejection, stale reply id) is raised
            immediately: the caller owns those fallbacks.

        attempts: explicit retry budget (default API_MAX_ATTEMPTS).
        gate_acquired: the caller already reserved a gate slot for its FIRST
        attempt (the non-blocking typing path).

        Returns the decoded JSON body's ``result``; raises TelegramApiError.
        """
        if not self.bot_token:
            raise TelegramApiError("bot_token is not configured")
        url = "{}/bot{}/{}".format(self.api_base_url.rstrip("/"),
                                   self.bot_token, method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        is_send = method.startswith("send")
        chat_id = params.get("chat_id")
        budget = API_MAX_ATTEMPTS if attempts is None else max(1, int(attempts))
        last_err = None
        for attempt in range(1, budget + 1):
            if not (gate_acquired and attempt == 1):
                if not self._gate.acquire(chat_id if is_send else None,
                                          is_send=is_send):
                    raise TelegramApiError(
                        "shutdown while waiting for the outbound rate gate "
                        "on {}".format(method), answered=False)
            req = urllib.request.Request(url, data=data, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECS) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise TelegramApiError(
                        "Telegram API returned invalid JSON for {}".format(method),
                        answered=True)
            except urllib.error.HTTPError as e:
                code = e.code
                detail = _read_error_body(e)
                err = TelegramApiError(
                    "HTTP {} from Telegram API: {}".format(code, detail),
                    answered=True, status=code,
                    retry_after=_parse_retry_after(detail))
                wait = None
                if attempt < budget:
                    if code == 429:
                        # Honor Telegram's own back-pressure signal.
                        wait = err.retry_after or API_RETRY_BACKOFF_SECS
                    elif 500 <= code < 600:
                        wait = API_RETRY_BACKOFF_SECS * attempt
                if wait is None:
                    raise err
                wait = min(wait, MAX_RETRY_AFTER_SECS)
                log.warning(
                    "%s rejected with HTTP %s - waiting %.1fs before retry "
                    "%d/%d", method, code, wait, attempt, budget)
                if self._wait(wait):
                    raise TelegramApiError(
                        "shutdown while waiting to retry {}".format(method),
                        answered=False, status=code)
                last_err = err
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                err = TelegramApiError(
                    "Cannot reach Telegram API: {}".format(reason),
                    answered=False)
                # Delivery is UNKNOWN: never blind-retry a send* call.
                if attempt < budget and method in CONNECTION_RETRY_METHODS:
                    wait = min(API_RETRY_BACKOFF_SECS * attempt,
                               MAX_RETRY_AFTER_SECS)
                    log.warning("%s unreachable (%s) - retrying in %.1fs "
                                "(%d/%d)", method, err, wait, attempt, budget)
                    if self._wait(wait):
                        raise TelegramApiError(
                            "shutdown while waiting to retry {}".format(method),
                            answered=False)
                    last_err = err
                    continue
                raise err
            if not body.get("ok"):
                desc = body.get("description", str(body))
                raise TelegramApiError(
                    "Telegram API error on {}: {}".format(method, desc),
                    answered=True, retry_after=_parse_retry_after(body))
            return body.get("result")
        raise last_err or TelegramApiError(
            "Telegram API error on {}: retry budget exhausted".format(method))

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
                # V-5: this plugin OWNS its platform's formatting rules; the
                # core forwards this hint to the prompt tool (`platform_hint`).
                "prompt_hint": "You are on a text messaging communication platform, Telegram. Standard markdown is automatically converted to Telegram format. Supported: **bold**, *italic*, ~~strikethrough~~, ||spoiler||, `inline code`, ```code blocks```, [links](url), and ## headers. Telegram has NO table syntax: prefer bullet lists or labeled key: value pairs over pipe tables (any tables you do emit are auto-rewritten into row-group bullets, which you can produce directly for cleaner output). You can send media files natively: to deliver a file to the user, include MEDIA:/absolute/path/to/file in your response. Images (.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice bubbles, and videos (.mp4) play inline. You can also include image URLs in markdown format ![alt](url) and they will be sent as native photos.",
                "commands": {
                    # Telegram Bot API style commands only: `$new` is NOT a
                    # command here because this plugin declares what it accepts.
                    "new": ["/new"],
                },
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
        # Absent key -> the schema default (plugin.json) is true.
        self.parent_by_chat = _as_bool(config.get("parent_by_chat", True))
        # Absent key -> the schema default (plugin.json) is true.
        self.first_last_only = _as_bool(config.get("first_last_only", True))
        # TYPING THROTTLE (operator update 2026-09-12): at most ONE
        # sendChatAction per chat every N seconds no matter how often core
        # requests typing. The key ABSENT -> default (5 s); an explicit 0 (or
        # null/empty) -> typing is SUPPRESSED entirely for this platform; a
        # positive value -> the minimum interval for one chat. Invalid values
        # warn and fall back to the default - a bad config never crashes the
        # plugin.
        if "typing_interval_seconds" in config:
            self.typing_interval_secs = self._parse_typing_interval(
                config.get("typing_interval_seconds"))
        else:
            self.typing_interval_secs = DEFAULT_TYPING_INTERVAL_SECS
        self._configured = True
        if self.polling_enabled and not self.bot_token:
            # Loud, never silent: this is the production failure mode where a
            # restarted/updated plugin has no token -> no getUpdates long-poll
            # -> inbound messages never reach omniagent and no thread is ever
            # created. Outbound delivery still works; only inbound is dead.
            log.error(
                "bot_token is EMPTY/missing - inbound polling is DISABLED: "
                "messages sent in the Telegram chat will NOT create threads "
                "in omniagent. Set bot_token in the telegram plugin config "
                "(token from @BotFather) and restart the plugin."
            )
        log.info("Configured: api_base=%s polling=%s interval=%ss "
                 "parent_by_chat=%s first_last_only=%s typing_interval=%ss "
                 "token_set=%s",
                 self.api_base_url, self.polling_enabled,
                 self.poll_interval_secs, self.parent_by_chat,
                 self.first_last_only, self.typing_interval_secs,
                 bool(self.bot_token))

        if self.polling_enabled and self.bot_token:
            self._start_polling()
        elif not self.polling_enabled:
            # A re-configure with polling disabled must STOP a running poll
            # thread (otherwise getUpdates keeps consuming the user's updates
            # and the plugin keeps polling after the flag is turned off).
            self._stop_polling()

        response = {
            "configured": True,
            "polling_enabled": self.polling_enabled and bool(self.bot_token),
            "first_last_only": self.first_last_only,
            # Effective typing throttle (0.0 = typing suppressed); echoed so the
            # round-trip is complete and testable.
            "typing_interval_seconds": self.typing_interval_secs,
            "typing_enabled": self.typing_interval_secs > 0,
        }
        if self.polling_enabled and not self.bot_token:
            response["warning"] = (
                "bot_token is empty - inbound polling disabled; "
                "set bot_token in the telegram plugin config and restart"
            )
        self._respond(req_id, result=response)

    @staticmethod
    def _parse_typing_interval(raw):
        """Normalise the typing_interval_seconds config value.

        ABSENT is handled by the caller (default). An explicit 0 / null / empty
        value DISABLES the typing signal (returns 0.0); a positive number is the
        minimum interval between two sendChatAction calls for one chat; anything
        else (non-numeric, negative) falls back to the default with a warning -
        a bad config must never crash the plugin.
        """
        if raw is None:
            return 0.0                      # explicit null -> typing disabled
        if isinstance(raw, str) and not raw.strip():
            return 0.0                      # explicit empty -> typing disabled
        try:
            value = float(raw)
        except (TypeError, ValueError):
            log.warning("typing_interval_seconds=%r is not a number - using the "
                        "default %ss", raw, DEFAULT_TYPING_INTERVAL_SECS)
            return DEFAULT_TYPING_INTERVAL_SECS
        if value < 0:
            log.warning("typing_interval_seconds=%r is negative - using the "
                        "default %ss", raw, DEFAULT_TYPING_INTERVAL_SECS)
            return DEFAULT_TYPING_INTERVAL_SECS
        return value

    def handle_deliver(self, req_id, params):
        resource = params.get("resource_identifier", "")
        content = params.get("content", "")
        thread_sequence = params.get("thread_sequence", 0)
        is_final = params.get("is_final", False)
        cause_external_id = params.get("cause_external_id") or None

        # Telegram first/last-only collapse (plugin-scoped, driven by the
        # telegram first_last_only config flag): only the thread's FIRST
        # message (seq-0, the prompt/cause) and the FINAL message of the run
        # reach the chat; every intermediate delivery is suppressed. Core
        # sends the full message stream to every platform; this filtering is
        # telegram-specific and lives here, never in core.
        if self.first_last_only:
            if "thread_sequence" not in params or "is_final" not in params:
                # Old core: deliver requests without the delivery metadata
                # (thread_sequence/is_final) cannot be collapsed - every
                # message looks like seq-0 and nothing is suppressed. Warn
                # instead of failing silently (the reported "all messages"
                # symptom on Telegram).
                log.warning(
                    "first_last_only: deliver request lacks thread_sequence/"
                    "is_final metadata (core too old?) - delivering"
                )
            try:
                seq = int(thread_sequence)
            except (TypeError, ValueError):
                seq = 0
            if seq != 0 and not is_final:
                log.info(
                    "first_last_only: suppressing intermediate delivery "
                    "(seq=%s, is_final=%s) to chat %s",
                    seq, is_final, resource,
                )
                self._respond(req_id, result={
                    "delivered": False,
                    "external_id": "",
                    "suppressed": True,
                })
                return

        # The FINAL message of a thread must be sent as a REPLY to the
        # thread's seq-0 (first) message, never as a standalone top-level
        # message. The core carries reply_to_message_id for backward
        # compatibility; when absent, the plugin derives it from
        # cause_external_id (the seq-0 message's external id) on final
        # deliveries. Telegram requires an integer message_id.
        reply_to = params.get("reply_to_message_id") or None
        if reply_to is None and is_final and cause_external_id:
            reply_to = cause_external_id
        reply_to_int = None
        if reply_to is not None:
            try:
                reply_to_int = int(str(reply_to).strip())
            except (TypeError, ValueError):
                log.warning(
                    "deliver: invalid reply_to_message_id %r - sending standalone",
                    reply_to,
                )
        parts = chunk_telegram_text(content)

        try:
            external_id = self._deliver_parts(resource, parts, reply_to_int)
            self._respond(req_id, result={
                "delivered": True,
                "external_id": external_id,
            })
            log.info("Delivered message %s to chat %s (reply_to=%s, parts=%d)",
                     external_id, resource, reply_to_int, len(parts))
            return
        except TelegramApiError as e:
            delivered = int(getattr(e, "delivered_parts", 0) or 0)
            if reply_to_int is None:
                log.error("deliver failed after %d/%d part(s): %s",
                          delivered, len(parts), e)
                self._respond(req_id, error={"code": -2, "message": str(e)})
                return
            if not e.answered:
                # CONNECTION-LEVEL failure: Telegram never answered, so we do
                # NOT know whether the reply landed. Re-sending the reply, or
                # posting a standalone copy, could DOUBLE-POST the message, so
                # neither is attempted (duplicate suppression). Nothing is
                # lost: the message stays persisted in the thread history and
                # the error is reported to core.
                log.error(
                    "deliver reply to %s failed at the connection level "
                    "(delivery UNKNOWN, no retry to avoid a duplicate): %s",
                    reply_to_int, e,
                )
                self._respond(req_id, error={"code": -2, "message": str(e)})
                return
            # Telegram ANSWERED with a rejection (e.g. a stale seq-0 id, or a
            # 429 that exhausted its retry budget). The reply was already
            # retried once with the same reply target (see _send_part) and
            # provably did not land, so fall back to standalone sends - resuming
            # at the part that failed and NEVER re-sending the parts that
            # already landed (no duplicates).
            remaining = parts[delivered:]
            log.warning(
                "deliver reply to %s failed (%s) - falling back to standalone "
                "for %d remaining part(s)",
                reply_to_int, e, len(remaining),
            )
            try:
                external_id = self._deliver_parts(resource, remaining, None)
                self._respond(req_id, result={
                    "delivered": True,
                    "external_id": external_id,
                })
                log.info(
                    "Delivered message %s to chat %s (standalone fallback, "
                    "parts=%d)",
                    external_id, resource, len(remaining),
                )
            except TelegramApiError as e2:
                log.error("deliver failed: %s", e2)
                self._respond(req_id, error={"code": -2, "message": str(e2)})

    def _deliver_parts(self, resource, parts, reply_to_int):
        """Send every part of a delivery to the chat.

        Content is pre-split into Telegram-sized parts (<= 4096 chars each,
        see chunk_telegram_text). The FIRST part carries the reply target
        (the thread's seq-0 message for final deliveries); each later part is
        a standalone follow-up message, because Telegram has no multi-part
        message container. Returns the external message id of the LAST part,
        which is the id core records on the message row.

        PART A (2026-09-12): the reply-carrying part is retried ONCE with the
        SAME reply_to_message_id when Telegram ANSWERED with a rejection, so a
        single transient rejection no longer costs the thread's threading.
        On failure the raised error carries ``delivered_parts`` (the number of
        parts Telegram already accepted) so the caller's standalone fallback
        can resume at the failed part instead of re-sending - and duplicating -
        the parts that already landed.
        """
        sent = 0
        external_id = ""
        for i, part in enumerate(parts):
            reply = reply_to_int if i == 0 else None
            try:
                result = self._send_part(resource, part, reply)
            except TelegramApiError as e:
                e.delivered_parts = sent
                raise
            sent += 1
            external_id = str(result.get("message_id", ""))
        return external_id

    def _send_part(self, resource, part, reply_to_int):
        """Send one part; retry the REPLY part once on an ANSWERED rejection.

        DUPLICATE-SUPPRESSION RULE: the same reply is only re-sent when
        Telegram ANSWERED the first attempt with an error status (HTTP 4xx/5xx
        with a body, e.g. 429 too-many-requests or 400 'message not found'),
        because an answered rejection proves delivery did NOT happen. On a
        connection-level failure (urllib URLError/timeout) delivery is UNKNOWN,
        so the message is NOT retried blindly - that could double-post the
        reply - and the error is propagated to the caller instead.
        """
        try:
            return self._send_rendered(resource, part, reply_to_int)
        except TelegramApiError as e:
            if reply_to_int is None or not e.answered:
                raise
            log.warning(
                "deliver reply to %s failed (%s) - retrying the SAME reply "
                "once before falling back to standalone",
                reply_to_int, e,
            )
            return self._send_rendered(resource, part, reply_to_int)

    def _send_rendered(self, resource, content, reply_to_int):
        """sendMessage with the content rendered from markdown to Telegram HTML.

        The message is sent with parse_mode=HTML so **bold**, *italic*,
        `code`, fenced blocks, links, headings and lists render as real
        Telegram formatting instead of raw markdown characters. If Telegram
        rejects the HTML (parse error on unusual input), the message is
        re-sent once as plain text with the markdown markers stripped, so a
        message is never dropped and never shows raw syntax.
        """
        try:
            params = {
                "chat_id": self._chat_id(resource),
                "text": markdown_to_html(content),
                "parse_mode": "HTML",
            }
            if reply_to_int is not None:
                params["reply_to_message_id"] = reply_to_int
            return self._api_post("sendMessage", params)
        except TelegramApiError as html_err:
            if not _is_parse_error(html_err):
                raise
            log.warning(
                "sendMessage HTML rejected (%s) - retrying plain text",
                html_err,
            )
            params = {
                "chat_id": self._chat_id(resource),
                "text": strip_markdown(content),
            }
            if reply_to_int is not None:
                params["reply_to_message_id"] = reply_to_int
            return self._api_post("sendMessage", params)

    def handle_edit_message(self, req_id, params):
        resource = params.get("resource_identifier", "")
        external_id = params.get("external_id", "")
        content = params.get("content", "")
        try:
            try:
                self._api_post("editMessageText", {
                    "chat_id": self._chat_id(resource),
                    "message_id": external_id,
                    "text": markdown_to_html(content),
                    "parse_mode": "HTML",
                })
            except TelegramApiError as e:
                if not _is_parse_error(e):
                    raise
                log.warning(
                    "editMessageText HTML rejected (%s) - retrying plain text",
                    e,
                )
                self._api_post("editMessageText", {
                    "chat_id": self._chat_id(resource),
                    "message_id": external_id,
                    "text": strip_markdown(content),
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

    def _reserve_typing_slot(self, chat_id):
        """True when a sendChatAction slot is free for this chat right now.

        The slot is reserved atomically BEFORE the call, so a burst of typing
        requests cannot all pass the check and a failed send does not
        immediately allow another one: the interval is between ATTEMPTS, which
        is what keeps the call volume bounded. No queue, no backlog.
        """
        now = time.monotonic()
        with self._typing_lock:
            last = self._last_typing.get(chat_id)
            if last is not None and (now - last) < self.typing_interval_secs:
                return False
            self._last_typing[chat_id] = now
            return True

    def handle_typing(self, req_id, params):
        resource = params.get("resource_identifier", "")
        chat_id = self._chat_id(resource)
        # TYPING THROTTLE (operator update 2026-09-12): core enqueues a typing
        # indicator every 5 s for the WHOLE run (~12 sendChatAction/min per
        # active thread), which is by far the plugin's biggest contributor to
        # Telegram API call volume. At most ONE sendChatAction per chat every
        # typing_interval_seconds; every excess request is DROPPED - never
        # queued and never delayed - because typing is purely cosmetic. An
        # explicit 0/empty typing_interval_seconds suppresses typing entirely.
        if self.typing_interval_secs <= 0:
            self._respond(req_id, result={"typing": False, "suppressed": True})
            return
        if not self._reserve_typing_slot(chat_id):
            self._respond(req_id, result={"typing": False, "throttled": True})
            return
        # Typing is the FIRST thing dropped when the shared outbound gate is
        # saturated: try_acquire NEVER waits, because the plugin's request loop
        # is single-threaded and a wait here would delay deliver/edit/react.
        if not self._gate.try_acquire(chat_id, is_send=True):
            self._respond(req_id, result={"typing": False, "throttled": True})
            return
        try:
            # attempts=1: a cosmetic indicator is never retried - a 429 wait
            # would block the request loop and delay real deliveries. The
            # per-chat throttle above is what keeps this call rate bounded.
            self._api_post("sendChatAction", {
                "chat_id": chat_id,
                "action": "typing",
            }, attempts=1, gate_acquired=True)
            self._respond(req_id, result={"typing": True})
            log.info("Typing indicator sent to chat %s", resource)
        except TelegramApiError as e:
            log.warning("typing failed (cosmetic, dropped): %s", e)
            self._respond(req_id, error={"code": -2, "message": str(e)})

    def handle_react(self, req_id, params):
        resource = params.get("resource_identifier", "")
        external_id = params.get("external_id", "")
        # The core sends the RAW thread STATUS name ("processing", "completed",
        # "failed", "interrupted", "skipped", "merged", ...); THIS plugin owns
        # the status -> reaction mapping. `emoji` is the legacy field an older
        # core still sends (Mattermost-style shortcode) and is honored during
        # the rollout window only.
        status = (params.get("status") or "").strip()
        raw = status if status else (params.get("emoji") or "").strip()
        # A status with no explicit mapping (e.g. a NEW thread status added
        # later) MUST still get a reaction: fall back to the plugin default
        # instead of dropping the reaction, logging only, or erroring.
        emoji = STATUS_TO_EMOJI.get(raw) or SHORTCODE_TO_EMOJI.get(raw)
        if emoji is None:
            emoji = DEFAULT_EMOJI
            log.info(
                "No explicit reaction mapping for status %r - using default %s",
                raw, emoji,
            )
        chat_id = self._chat_id(resource)
        # OVERRIDE semantics (2026-09-04): a status transition REPLACES the
        # bot's previous reaction on the SAME message instead of accumulating
        # a second one. A bot has ONE reaction per message in Telegram and
        # setMessageReaction replaces the whole reaction set, so sending the
        # single new emoji IS the override: the initial +1 (processing) is
        # replaced by the terminal emoji (e.g. the check mark) when the
        # thread finishes. This supersedes the earlier accumulate behavior
        # (commit 866b1d6) which kept the +1 forever.
        # ROBUSTNESS (2026-09-12): Telegram only accepts emojis from its own
        # fixed reaction set; anything else answers "REACTION_INVALID". If the
        # API rejects the mapped emoji we must NEVER drop the update and NEVER
        # leave the stale +1 on the message: retry ONCE with the
        # guaranteed-valid DEFAULT_EMOJI and log it at WARNING level.
        def _send(candidate):
            self._api_post("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": external_id,
                "reaction": json.dumps([{"type": "emoji", "emoji": candidate}]),
            })

        try:
            _send(emoji)
        except TelegramApiError as e:
            if emoji == DEFAULT_EMOJI:
                log.error("react failed (even the default %s): %s", emoji, e)
                self._respond(req_id, error={"code": -2, "message": str(e)})
                return
            log.warning(
                "Reaction %s rejected by Telegram for status %r (%s) - "
                "falling back to the valid default %s",
                emoji, raw, e, DEFAULT_EMOJI,
            )
            try:
                _send(DEFAULT_EMOJI)
            except TelegramApiError as e2:
                log.error("react fallback failed: %s", e2)
                self._respond(req_id, error={"code": -2, "message": str(e2)})
                return
            emoji = DEFAULT_EMOJI
        self._respond(req_id, result={"reacted": True})
        log.info("Reacted %s to message %s in chat %s (replaced prior "
                 "reaction)", emoji, external_id, resource)

    # ------------------------------------------------------------------
    # Inbound: long-poll getUpdates
    # ------------------------------------------------------------------
    def _wait(self, seconds):
        """Interruptible wait; returns True when shutdown was requested.

        Tests stub this to avoid real waiting (there is no real sleep in the
        unit tests of the retry/backoff paths).
        """
        return self._stop.wait(max(0.0, seconds))

    def _poll_wait(self, seconds):
        """Interruptible wait for the POLL thread only.

        Distinct from _wait (the plugin-wide shutdown wait used by the 429
        backoff and by the outbound rate gate): polling has its own stop event,
        so disabling/reconfiguring polling can never signal a plugin shutdown.
        """
        if self._stop.is_set() or self._poll_stop.is_set():
            return True
        return self._poll_stop.wait(max(0.0, seconds)) or self._stop.is_set()

    def _stop_polling(self):
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._poll_thread = None

    def _start_polling(self):
        if not self.bot_token:
            log.error(
                "Cannot start inbound polling: bot_token is empty. "
                "Set bot_token in the telegram plugin config and restart."
            )
            return
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram-poll")
        self._poll_thread.start()
        log.info("Inbound polling started (interval=%ss)", self.poll_interval_secs)

    def _poll_loop(self):
        while not self._poll_stop.is_set():
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
                # Error backoff: don't hot-loop on persistent failures. A 429
                # that exhausted its retry budget tells us exactly how long to
                # wait (parameters.retry_after); honor it, capped.
                backoff = min(self.poll_interval_secs, 30)
                if e.retry_after:
                    backoff = max(backoff,
                                  min(e.retry_after, MAX_RETRY_AFTER_SECS))
                self._poll_wait(backoff)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("poll loop error: %s", e)
                self._poll_wait(min(self.poll_interval_secs, 30))
            else:
                # Short sleep between long-polls to keep offset commits sane.
                self._poll_wait(max(0.5, min(self.poll_interval_secs, 5)))

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
        text = _inbound_text(msg)
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
            # metadata["parent_external_id"] as the protocol-level parent
            # external id for inbound messages (the same envelope key the
            # mattermost platform uses for its thread root post id) - the
            # existing pending/merge machinery then merges a pending same-parent
            # message into a processing thread per its percent / char-amount
            # thresholds. metadata["root_id"] carries the same value as a
            # one-release compatibility alias for omniagent cores that predate
            # the rename. When parent_by_chat is false no parent id is set (one
            # thread per message).
            metadata["parent_external_id"] = str(chat_id)
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
        text = _inbound_text(msg)
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
                self._poll_stop.set()
                self._respond(req_id, result={"shutdown": True})
            else:
                log.warning("Unknown method: %s", method)
                if req_id is not None:
                    self._respond(req_id, error={
                        "code": -1,
                        "message": "Unknown method: {}".format(method),
                    })
        self._stop.set()
        self._poll_stop.set()
        log.info("Telegram platform plugin shutting down (stdin closed)")


# Thread STATUS name -> Telegram unicode emoji.
#
# ARCHITECTURE (2026-09-10): the core sends the RAW thread STATUS name
# ("processing", "completed", "skipped", "merged", ...) in the reaction
# envelope and never maps it to an emoji or to a Mattermost shortcode. The
# status -> reaction mapping is OWNED BY THIS PLUGIN: Telegram's
# setMessageReaction takes a unicode emoji, not a shortcode.
# ONLY emojis from Telegram's allowed setMessageReaction set may appear here.
# VERIFIED against the REAL Bot API (2026-09-12, bot @omnilbsbot, production
# token) with one setMessageReaction call per candidate; the API answered
# "Bad Request: REACTION_INVALID" for \u2705 (0x2705), \u274c (0x274C) and
# \U0001f17e\ufe0f (0x1F17E), which is why completed/failed/skipped never
# replaced the +1 before. Valid in the same probe: 1F44D, 1F91D, 1F494,
# 1F440, 2764, 1F525, 1F389, 1F4AF, 1F3C6, 1F64F, 1F914, 1F622, 1F921,
# 1F634, 1F937, 1F44C, 1FAE1, 1F44E, 1F929.
# (Also rejected: 1F5D1, 26D4, 26A0, 1F6D1, 1F480, 1F916.)
# tests/mock_telegram_api.py validates every value against the same set.
STATUS_TO_EMOJI = {
    "pending": "\U0001f44d",       # 1F44D  thumbs up (+1 on arrival)
    "processing": "\U0001f44d",    # 1F44D  thumbs up (+1 on arrival)
    "completed": "\U0001f3c6",     # 1F3C6  trophy (the check mark is INVALID)
    "failed": "\U0001f622",        # 1F622  crying face (the cross mark is INVALID)
    "interrupted": "\U0001f494",   # 1F494  broken heart
    "skipped": "\U0001f937",       # 1F937  shrug (the O symbol is INVALID)
    "merged": "\U0001f91d",        # 1F91D  handshake
}

# DEFAULT reaction for a status with no explicit mapping (e.g. a NEW thread
# status added later): the plugin must still react - never drop the reaction,
# never error. \U0001f440 ("eyes") is in Telegram's valid reaction set, so it
# also serves as the REACTION_INVALID fallback in handle_react.
DEFAULT_EMOJI = "\U0001f440"

# Legacy (rollout window only): Mattermost-style emoji shortcodes sent by an
# OLDER core that still mapped status -> shortcode itself. Mapped to their
# unicode glyphs so an old core keeps working until it is upgraded. The glyphs
# come from the same verified VALID set as STATUS_TO_EMOJI.
SHORTCODE_TO_EMOJI = {
    ":white_check_mark:": "\U0001f3c6",
    ":heavy_check_mark:": "\U0001f3c6",
    ":x:": "\U0001f622",
    ":cross_mark:": "\U0001f622",
    ":broken_heart:": "\U0001f494",
    ":o:": "\U0001f937",
    ":handshake:": "\U0001f91d",
    ":+1:": "\U0001f44d",
    ":thumbsup:": "\U0001f44d",
    ":thumbs_up:": "\U0001f44d",
}


# ----------------------------------------------------------------------
# Inbound: Telegram entities -> markdown (quote/citation preservation)
# ----------------------------------------------------------------------
# Telegram delivers a reply-with-quote/citation as PLAIN text whose
# quoted span is marked by a 'blockquote' message entity (offset/length
# in UTF-16 code units, the Telegram Bot API unit). Without conversion
# the citation structure is lost: the plugin forwarded only the raw text
# and core stored it with no '>' markers, unlike Mattermost where quoted
# text arrives as literal markdown '> ' lines. The outbound path maps
# markdown '> ' lines to <blockquote> HTML, so this inbound conversion
# is its exact inverse: every line covered by a blockquote entity is
# prefixed with '> '. Only blockquote spans are converted (quote/
# citation preservation is the scope); all other text is preserved
# byte-for-byte.


def _inbound_text(msg):
    """Inbound message text with blockquote entities rendered as markdown.

    Selects the text the same way the pre-fix code did (text first,
    caption fallback) and pairs it with the matching entity list
    (entities vs caption_entities). Returns "" when neither is present.
    """
    text = msg.get("text")
    entities = msg.get("entities")
    if not text:
        text = msg.get("caption") or ""
        entities = msg.get("caption_entities")
    if not text:
        return ""
    return apply_blockquote_entities(text, entities or [])


def apply_blockquote_entities(text, entities):
    """Prefix every line covered by a Telegram blockquote entity with '> '.

    entities: iterable of Telegram MessageEntity dicts; only entries with
    type == "blockquote" are used. offset/length are UTF-16 code units.
    Multi-line spans (offset/length crossing newlines) quote every covered
    line. Non-quoted text is preserved byte-for-byte.
    """
    if not text:
        return text
    spans = []
    for ent in entities or []:
        if ent.get("type") != "blockquote":
            continue
        try:
            offset = int(ent.get("offset", 0))
            length = int(ent.get("length", 0))
        except (TypeError, ValueError):
            continue
        start = _utf16_to_char_index(text, offset)
        end = _utf16_to_char_index(text, offset + length)
        if end > start:
            spans.append((start, end))
    if not spans:
        return text
    # Apply back-to-front so earlier entity offsets stay valid after the
    # inserted markers shift the string.
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        text = _prefix_quote_lines(text, start, end)
    return text


def _utf16_to_char_index(text, utf16_pos):
    """Map a Telegram entity offset (UTF-16 code units) to a python char
    index (Telegram counts astral chars such as emoji as 2 units)."""
    units = 0
    for i, ch in enumerate(text):
        if units >= utf16_pos:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def _prefix_quote_lines(text, start, end):
    """Prefix '> ' on every line of text falling inside [start, end)."""
    out = []
    line_start = 0
    total = len(text)
    while True:
        nl = text.find("\n", line_start)
        line_end = total if nl == -1 else nl
        if line_end == line_start:
            # Empty line (blank line inside a multi-line quote): quoted
            # when the span strictly crosses it.
            quoted = start < line_start < end
        else:
            quoted = line_start < end and line_end > start
        if quoted:
            out.append("> ")
        out.append(text[line_start:line_end])
        if nl == -1:
            break
        out.append("\n")
        line_start = nl + 1
    return "".join(out)


# ----------------------------------------------------------------------
# Markdown -> Telegram HTML rendering (stdlib only)
# ----------------------------------------------------------------------
# Outbound messages carry CommonMark-ish markdown (the LLM output). Telegram
# renders formatting only when a message is sent with parse_mode set, so the
# markdown is converted to Telegram's HTML dialect here. Supported constructs
# render as real formatting; unbalanced or unsupported input degrades
# gracefully (markers stripped, text HTML-escaped) and can never expose raw
# markdown syntax or break out of the HTML context.
_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}
_HTML_ESCAPE_RE = re.compile(r'[&<>"]')
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_UNDER_BOLD_RE = re.compile(r"__([^_]+)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_UNDER_ITALIC_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\s*\1\s*\1\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.+)$")
_CODE_PLACEHOLDER = "\x00CODE{}\x00"
_LINK_PLACEHOLDER = "\x00LINK{}\x00"


def _escape_html(text):
    return _HTML_ESCAPE_RE.sub(lambda m: _HTML_ESCAPES[m.group(0)], text)


def _convert_inline(text):
    """Convert inline markdown in ALREADY-ESCAPED text to Telegram HTML."""
    code_spans = []
    links = []

    def _keep_code(m):
        code_spans.append(m.group(1))
        return _CODE_PLACEHOLDER.format(len(code_spans) - 1)

    def _keep_link(m):
        links.append((m.group(1), m.group(2)))
        return _LINK_PLACEHOLDER.format(len(links) - 1)

    s = _CODE_SPAN_RE.sub(_keep_code, text)
    s = _LINK_RE.sub(_keep_link, s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _UNDER_BOLD_RE.sub(r"<b>\1</b>", s)
    s = _ITALIC_RE.sub(r"<i>\1</i>", s)
    s = _UNDER_ITALIC_RE.sub(r"<i>\1</i>", s)
    s = _STRIKE_RE.sub(r"<s>\1</s>", s)
    # Unbalanced leftovers: strip the markers so no raw syntax is shown.
    s = s.replace("**", "").replace("__", "").replace("`", "")
    for i, (label, url) in enumerate(links):
        # Telegram disallows nested tags inside <a>, so the label is plain.
        clean_label = label.replace("**", "").replace("__", "").replace("`", "")
        s = s.replace(_LINK_PLACEHOLDER.format(i),
                      '<a href="' + url + '">' + clean_label + "</a>")
    for i, content in enumerate(code_spans):
        s = s.replace(_CODE_PLACEHOLDER.format(i), "<code>" + content + "</code>")
    return s


def _convert_line(line):
    """Convert one non-code line (raw markdown) to Telegram HTML."""
    m = _HEADING_RE.match(line)
    if m:
        return "<b>" + _convert_inline(_escape_html(m.group(1))) + "</b>"
    m = _BLOCKQUOTE_RE.match(line)
    if m:
        return "<blockquote>" + _convert_inline(_escape_html(m.group(1))) \
            + "</blockquote>"
    if _HR_RE.match(line):
        return "\u2015" * 8
    m = _UL_RE.match(line)
    if m:
        return m.group(1) + "\u2022 " \
            + _convert_inline(_escape_html(m.group(2)))
    m = _OL_RE.match(line)
    if m:
        return m.group(1) + m.group(2) + ". " \
            + _convert_inline(_escape_html(m.group(3)))
    return _convert_inline(_escape_html(line))


def markdown_to_html(text):
    """Convert markdown text to Telegram-compatible HTML.

    Fenced code blocks become <pre>, headings become bold, blockquotes use
    the <blockquote> tag, list markers become bullets, and inline formatting
    (bold/italic/code/links/strikethrough) renders as the matching HTML tag.
    All other text is HTML-escaped, so a message can never expose raw
    markdown characters or break out of the HTML context.
    """
    lines = text.split("\n")
    out = []
    i = 0
    in_fence = False
    fence_buf = []
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            if not in_fence:
                in_fence = True
                fence_buf = []
            else:
                in_fence = False
                out.append("<pre>" + _escape_html("\n".join(fence_buf))
                           + "</pre>")
            i += 1
            continue
        if in_fence:
            fence_buf.append(line)
            i += 1
            continue
        out.append(_convert_line(line))
        i += 1
    if in_fence:
        # Unbalanced fence: render the collected lines as a code block anyway
        # (never show the raw ``` marker).
        out.append("<pre>" + _escape_html("\n".join(fence_buf)) + "</pre>")
    return "\n".join(out)


def strip_markdown(text):
    """Plain-text fallback: remove markdown markers, keep the text readable.

    Used when Telegram rejects the HTML-rendered message; the message is then
    sent without parse_mode and without raw markdown syntax.
    """
    lines = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
            continue
        s = _HEADING_RE.sub(r"\1", line)
        s = _BLOCKQUOTE_RE.sub(r"\1", s)
        s = _HR_RE.sub("", s)
        s = _UL_RE.sub(lambda m: m.group(1) + "\u2022 " + m.group(2), s)
        s = _OL_RE.sub(r"\1\2. \3", s)
        s = _LINK_RE.sub(r"\1 (\2)", s)
        s = s.replace("**", "").replace("__", "").replace("`", "")
        lines.append(s)
    return "\n".join(lines)


def _split_part(part_text, budget):
    """Split part_text (longer than budget) into pieces each <= budget chars.

    Iterative (never recursive, so no stack-depth risk on huge inputs). Cuts
    fall on line boundaries, so concatenating the returned pieces reproduces
    part_text exactly whenever no fence markers had to be inserted. Fenced
    code blocks are kept balanced inside each piece: when a cut lands inside
    an open fence, the piece is closed with a ``` marker and the next piece
    is re-opened with one. A single line longer than the budget is hard-split
    by characters (its text is never dropped).
    """
    if len(part_text) <= budget:
        return [part_text]
    lines = part_text.split("\n")
    out = []
    buf = []
    buf_chars = 0
    fence_open = False  # inside a fenced block?
    n = len(lines)

    def flush(with_close, trailing_newline):
        nonlocal buf, buf_chars
        if not buf:
            return
        piece = "\n".join(buf)
        if with_close:
            piece += "\n```"
        elif trailing_newline:
            piece += "\n"
        out.append(piece)
        buf = []
        buf_chars = 0

    for idx, line in enumerate(lines):
        is_marker = bool(_FENCE_RE.match(line.strip()))
        more_lines = idx < n - 1
        if is_marker:
            if buf and buf_chars + len(line) + 1 > budget and not fence_open:
                flush(False, more_lines)
            buf.append(line)
            buf_chars += len(line) + 1
            fence_open = not fence_open
            continue
        # non-marker content line
        if buf and buf_chars + len(line) + 1 > budget:
            if fence_open:
                flush(True, False)   # close the fence on the current piece
                buf = ["```"]        # and reopen on the next one
                buf_chars = 4
            else:
                flush(False, True)
        if line == "":
            # Blank separator line: keep it, it is the paragraph spacing.
            buf.append("")
            buf_chars += 1
            continue
        # a line longer than the whole budget is hard-split by characters
        while buf_chars + len(line) + 1 > budget:
            room = budget - buf_chars
            if room < 1:
                if fence_open:
                    flush(True, False)
                    buf = ["```"]
                    buf_chars = 4
                else:
                    flush(False, True)
                continue
            take, line = line[:room], line[room:]
            if take:
                buf.append(take)
                buf_chars += len(take) + 1
            if buf_chars >= budget:
                if fence_open:
                    flush(True, False)
                    buf = ["```"]
                    buf_chars = 4
                else:
                    flush(False, True)
        if line:
            buf.append(line)
            buf_chars += len(line) + 1
    flush(False, False)
    return out


def chunk_telegram_text(text):
    """Split content into parts whose RENDERED HTML never exceeds Telegram's
    4096-character sendMessage limit.

    Short content is returned unchanged as one part. Long content is split at
    line boundaries (fenced code blocks stay whole whenever possible) so that
    every part, once converted by markdown_to_html, fits the limit. The parts
    concatenate back to the original text exactly, except for an oversized
    fenced block which is emitted as several complete fenced blocks. This is
    the guarantee that a finished thread's final message is never silently
    dropped by a sendMessage "message is too long" rejection.
    """
    if len(markdown_to_html(text)) <= TG_MSG_LIMIT:
        return [text]
    parts = _split_part(text, TG_MSG_BUDGET)
    for _ in range(32):
        oversize = [p for p in parts
                    if len(markdown_to_html(p)) > TG_MSG_LIMIT]
        if not oversize:
            return parts
        budget = max(300, int(TG_MSG_BUDGET * 0.7))
        new_parts = []
        for p in parts:
            if len(markdown_to_html(p)) <= TG_MSG_LIMIT:
                new_parts.append(p)
            else:
                new_parts.extend(_split_part(p, budget))
        parts = new_parts
    return parts


def _is_parse_error(err):
    """True when a TelegramApiError is a parse_mode/entity rejection (the
    case where retrying as plain text can succeed)."""
    msg = str(err).lower()
    return "parse" in msg or "entity" in msg


class TelegramApiError(Exception):
    """A Telegram API call failure.

    ``answered`` is True when Telegram ANSWERED the request with an error
    status (HTTP 4xx/5xx with a body) - delivery provably did NOT happen, so a
    retry cannot duplicate a message. It is False for connection-level
    failures (urllib URLError/timeout, shutdown) where delivery is UNKNOWN and
    a blind retry could double-post the message.

    ``status``/``retry_after`` carry the HTTP status and Telegram's
    parameters.retry_after (seconds) when present.
    """

    def __init__(self, message, answered=True, status=None, retry_after=None):
        super().__init__(message)
        self.answered = answered
        self.status = status
        self.retry_after = retry_after


def main():
    platform = TelegramPlatform()
    platform.run()


if __name__ == "__main__":
    main()
