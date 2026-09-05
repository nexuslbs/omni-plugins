#!/usr/bin/env python3
"""
Unit tests for Telegram INBOUND quote/citation preservation.

Telegram delivers a reply-with-quote/citation as plain text whose quoted
span is marked by a 'blockquote' message entity; the plugin must convert
that span to markdown '> ' line prefixes before forwarding to core (so the
citation survives in messages.content like it does on Mattermost). A plain
(non-quote) message must be forwarded byte-for-byte unchanged.

Covers the exact operator scenario shape (msg 514/thread 972): first line
quoted, second line the operator's own text.

Pure stdlib, no network, no subprocess.

Usage:
    python3 tests/test_inbound_blockquote.py
Exit code 0 on success, 1 on failure.
"""

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from platform import (TelegramPlatform,  # noqa: E402
                      apply_blockquote_entities)

FAILURES = []


def check(cond, label):
    if cond:
        print("PASS: " + label)
    else:
        FAILURES.append(label)
        print("FAIL: " + label)


def emit_inbound(msg):
    """Drive _emit_inbound_message and return the forwarded text."""
    plat = TelegramPlatform()
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        plat._emit_inbound_message(msg)
    finally:
        sys.stdout = old
    for line in buf.getvalue().strip().splitlines():
        obj = json.loads(line)
        if obj.get("method") == "inbound_message":
            return obj["params"]["text"]
    raise AssertionError("no inbound_message emitted for {!r}".format(msg))


def emit_edited(msg):
    """Drive _emit_edited_message and return the forwarded text."""
    plat = TelegramPlatform()
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        plat._emit_edited_message(msg)
    finally:
        sys.stdout = old
    for line in buf.getvalue().strip().splitlines():
        obj = json.loads(line)
        if obj.get("method") == "message_edited":
            return obj["params"]["text"]
    raise AssertionError("no message_edited emitted for {!r}".format(msg))


def base_msg():
    return {
        "message_id": 514,
        "date": 1700000000,
        "chat": {"id": -1002003004, "type": "channel"},
        "from": {"id": 555, "first_name": "Mock"},
    }


# -- function-level: apply_blockquote_entities ---------------------------
_quoted = "Since mattermost is working now"
_own = "It never stopped"
full = _quoted + "\n" + _own
res = apply_blockquote_entities(full, [
    {"offset": 0, "length": len(_quoted), "type": "blockquote"},
])
check(res == "> " + _quoted + "\n" + _own,
      "single-line leading blockquote entity -> first line prefixed '> ', "
      "second line (own text) unchanged")

_quoted2 = "line one\nline two"
res = apply_blockquote_entities(_quoted2 + "\nmy own line", [
    {"offset": 0, "length": len(_quoted2), "type": "blockquote"},
])
check(res == "> line one\n> line two\nmy own line",
      "multi-line blockquote span (crosses newline) -> every covered line "
      "prefixed, trailing own line untouched")

res = apply_blockquote_entities("no entities here", None)
check(res == "no entities here",
      "plain text / no entities -> byte-for-byte unchanged")

res = apply_blockquote_entities("**b** and `c` stay as-is", [])
check(res == "**b** and `c` stay as-is",
      "empty entity list -> unchanged")

res = apply_blockquote_entities("just bold text", [
    {"offset": 5, "length": 4, "type": "bold"},
])
check(res == "just bold text",
      "non-blockquote entity (bold) -> ignored (scope: quote preservation)")

# UTF-16 entity offsets: Telegram counts astral chars as 2 code units.
# "ok \U0001F600\nquoted line": quoted starts at utf16 offset 6 (o,k,space,emoji x2,\n).
_utf = "ok \U0001f600\nquoted line"
res = apply_blockquote_entities(_utf, [
    {"offset": 6, "length": len("quoted line"), "type": "blockquote"},
])
check(res == "ok \U0001f600\n> quoted line",
      "UTF-16 unit offsets (emoji = 2 units) map to the right span")

res = apply_blockquote_entities("para1\n\npara2", [
    {"offset": 0, "length": len("para1\n\npara2"), "type": "blockquote"},
])
check(res == "> para1\n> \n> para2",
      "blank line inside a quoted span keeps the quote continuous")

# -- message level: _emit_inbound_message (reply-with-quote shape) -------
msg = base_msg()
msg["text"] = full
msg["entities"] = [
    {"offset": 0, "length": len(_quoted), "type": "blockquote"},
]
out = emit_inbound(msg)
check(out.startswith("> " + _quoted + "\n") and out.endswith(_own)
      and out.count("\n") == 1,
      "inbound reply-with-quote update -> content first line starts '> ' "
      "(operator scenario shape preserved)")

msg = base_msg()
msg["text"] = "plain hello without quote"
out = emit_inbound(msg)
check(out == "plain hello without quote",
      "inbound plain message -> forwarded byte-for-byte unchanged")

# caption + caption_entities path
msg = base_msg()
msg.pop("text", None)
msg["caption"] = "quoted cap line\nmy caption text"
msg["caption_entities"] = [
    {"offset": 0, "length": len("quoted cap line"), "type": "blockquote"},
]
out = emit_inbound(msg)
check(out == "> quoted cap line\nmy caption text",
      "caption + caption_entities blockquote -> first line prefixed '> '")

# media message with no text/caption -> empty, no crash
msg = base_msg()
msg["photo"] = [{"file_id": "x"}]
check(emit_inbound(msg) == "", "photo message (no text/caption) -> empty text")

# -- message level: _emit_edited_message ----------------------------------
msg = base_msg()
msg["text"] = "edited quoted\nedited own"
msg["entities"] = [
    {"offset": 0, "length": len("edited quoted"), "type": "blockquote"},
]
out = emit_edited(msg)
check(out == "> edited quoted\nedited own",
      "edited_message with blockquote entity -> quote markers preserved")

print("")
if FAILURES:
    print("UNIT TEST FAILED: {} failure(s)".format(len(FAILURES)))
    sys.exit(1)
print("UNIT TEST PASSED - inbound blockquote/citation preservation works")
sys.exit(0)
