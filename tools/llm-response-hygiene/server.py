#!/usr/bin/env python3
"""llm-response-hygiene MCP server : standard MCP JSON-RPC over stdio.

Tool:
  - classify: detect a provider-specific MALFORMED assistant message (e.g.
    DeepSeek "DSML" text-mode tool-call markup) and return the cleaned text.

Why this plugin exists: the omniagent core used to hardcode the detection of
DeepSeek DSML/XML text-mode tool-call markup (the full-width bar U+FF5C and the
token separator U+2581, `<tool_calls>` envelopes, markdown ```tool_call```
fences) plus the continuation-prose heuristic, in
`src/agent/terminal_summary.rs`. That knowledge is PROVIDER-SPECIFIC and must
not pollute the core: it lives here instead, behind the global setting
`malformed_response_tool` (empty default = core keeps its own provider-neutral
heuristic).

The detection/sanitizing logic below is a 1:1 port of the core functions that
were removed from the runtime path (`contains_dsml_markup`,
`sanitize_terminal_content`, `is_continuation_intent`), so a verdict reproduces
the core's previous decision on the same input (reference equivalence; the
corpus is asserted both here and in the Rust tests).

Contract (called by the core, `redaction_tool`-style):
  input  : {"text": <raw assistant message text>}
  output : {"malformed": bool, "reason": str, "cleaned": str,
            "continuation_intent": bool}

PURITY: this is a PURE function - deterministic, no I/O, no network, no LLM
call, no filesystem access. Identical input -> identical output. Run
`python3 server.py --selftest` to assert the corpus, purity and latency.

Cancellation is detected when stdin closes (EOF).
"""

import json
import os
import re
import sys
import time
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [llm-response-hygiene] %(levelname)s %(message)s",
    stream=sys.stderr,
)

log = logging.getLogger("mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
initialized = False
stdin_closed = threading.Event()
stdout_lock = threading.Lock()

# ── Provider-specific delimiters (DeepSeek DSML) ────────────────────────────
# Delimiter of DeepSeek text-mode tool-call markup: instead of XML angle
# brackets the model writes special tokens with a FULL-WIDTH VERTICAL BAR
# (U+FF5C), e.g. `<|DSML| calls>`.
DSML_BAR = "\uff5c"
# Token-name separator (U+2581) used inside DeepSeek special tokens
# (`<|tool_1calls_1begin|>`).
DSML_SEP = "\u2581"

MARKDOWN_TOOL_FENCE_RE = re.compile(r"^[ \t]*```.*(tool_call|dsml)", re.IGNORECASE | re.MULTILINE)

# ── Continuation-intent heuristics (port of is_continuation_intent) ─────────
CONTINUATION_OPENERS = [
    "let me ",
    "i'll ",
    "i will ",
    "i'm going to ",
    "i am going to ",
    "i need to ",
    "i must ",
    "i should ",
    "i want to ",
    "i'm in ",
    "i am in ",
    "i can now ",
    "i'd like to ",
    "i would like to ",
    "i have all the evidence",
    "i have the evidence",
    "i have enough",
    "proceeding to ",
    "first batch",
]

CONTINUATION_TODO_MARKERS = [
    " then ",
    " first ",
    " now ",
    " next ",
    " verify ",
    " inspect ",
    " check ",
    " update ",
    " close out ",
    " commit ",
    " push ",
    " run ",
    " proceed ",
    " continue ",
    " finish ",
    " deliver ",
    " gather ",
    " investigate ",
    " do the ",
    " begin ",
    " remaining work",
    " remaining steps",
    " one more ",
    " fresh budget",
    " final answer now",
    " give the final answer",
    " then give ",
]

SUMMARY_INTROS = [
    "summary:",
    "to summarize",
    "in summary",
    "summarize",
    "here's a summary",
    "here is a summary",
    "here's what happened",
    "here is what happened",
    "here's what was done",
    "here is what was done",
    "i summarize",
]

XML_TOOL_TAG_PREFIXES = (
    "<invoke",
    "</invoke>",
    "<parameter",
    "</parameter>",
    "<tool_calls",
    "</tool_calls>",
    "<antml:",
    "</antml:",
)


def contains_dsml_markup(text):
    """True when `text` carries DeepSeek DSML special-token markup."""
    return DSML_BAR in text or DSML_SEP in text


def sanitize_terminal_content(raw):
    """Strip DSML/XML tool-call envelopes and markdown `tool_call` fences.

    Port of the core `sanitize_terminal_content`: removes complete
    `<tool_calls>...</tool_calls>` blocks (dropping the tail when the block is
    unterminated, i.e. the model was cut off mid-block), markdown-fenced
    ```tool_call``` blocks, and leftover lone XML tool-tag lines. Ordinary prose
    around the blocks is preserved.
    """
    # Pass 1: remove <tool_calls> ... </tool_calls> envelopes.
    parts = []
    rest = raw
    while True:
        start = rest.find("<tool_calls")
        if start == -1:
            parts.append(rest)
            break
        parts.append(rest[:start])
        rel = rest[start:].find("</tool_calls>")
        if rel == -1:
            # Unterminated block: the model was cut off mid-envelope.
            rest = ""
        else:
            rest = rest[start + rel + len("</tool_calls>"):]
    text = "".join(parts)

    # Pass 2: drop markdown tool_call fences, lone XML tool-tag lines, and any
    # line carrying DSML special tokens; collapse excess blank lines.
    result = []
    in_tool_fence = False
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("```"):
            if in_tool_fence:
                in_tool_fence = False
            elif "tool_call" in trimmed or "dsml" in trimmed:
                in_tool_fence = True
            else:
                result.append(line)
            continue
        if in_tool_fence:
            continue
        if (
            trimmed == ""
            or DSML_BAR in trimmed
            or DSML_SEP in trimmed
            or trimmed.startswith(XML_TOOL_TAG_PREFIXES)
        ):
            continue
        result.append(line)

    # Pass 3: collapse runs of blank lines, trim.
    collapsed = []
    blank_run = 0
    for line in result:
        if line.strip() == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        collapsed.append(line.rstrip())
    return "\n".join(collapsed).strip()


def is_continuation_intent(text):
    """Heuristic gate: is `text` continuation intent rather than a summary?"""
    head = text[:300]
    lower = head.lower()
    if any(s in lower for s in SUMMARY_INTROS):
        return False
    let_me_know = "let me know" in lower
    has_opener = any(
        o in lower and not (let_me_know and o == "let me ") for o in CONTINUATION_OPENERS
    )
    if not has_opener:
        return False
    return any(m in lower for m in CONTINUATION_TODO_MARKERS)


def detect_malformed(text):
    """Return (malformed, reason) for a raw assistant message.

    `malformed` is the PRIMARY verdict the core consumes: the message is not a
    valid assistant answer but provider-specific tool-call markup (or a
    markdown tool_call fence).
    """
    if contains_dsml_markup(text):
        return True, "dsml_markup"
    if (
        "<tool_calls" in text
        or "<invoke" in text
        or "<parameter" in text
        or "<antml:" in text
    ):
        return True, "xml_tool_call_envelope"
    if MARKDOWN_TOOL_FENCE_RE.search(text):
        return True, "markdown_tool_call_fence"
    return False, "clean"


def classify(text):
    """Pure function: classify + clean a raw assistant message.

    Deterministic, no I/O, no network, no LLM call.
    """
    malformed, reason = detect_malformed(text)
    cleaned = sanitize_terminal_content(text)
    continuation = is_continuation_intent(cleaned)
    if not malformed and continuation:
        reason = "continuation_intent"
    return {
        "malformed": malformed,
        "reason": reason,
        "cleaned": cleaned,
        "continuation_intent": continuation,
    }


def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_initialize(req_id):
    result = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "llm-response-hygiene", "version": "0.1.0"},
    }
    send_json(make_success(req_id, result))
    log.info("Initialized: llm-response-hygiene v0.1.0")


def handle_tools_list(req_id):
    tools = [
        {
            "name": "classify",
            "description": (
                "[llm-response-hygiene] Detect a provider-specific MALFORMED LLM "
                "response (DeepSeek DSML/XML text-mode tool-call markup, markdown "
                "```tool_call``` fences) and return the cleaned text plus a "
                "continuation-intent flag. Pure function: no I/O, no network, no "
                "LLM call. Returns JSON: {malformed, reason, cleaned, "
                "continuation_intent}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The raw assistant message text to classify and clean",
                    },
                },
                "required": ["text"],
            },
        },
    ]
    send_json(make_success(req_id, {"tools": tools}))
    log.info("tools/list returned 1 tool")


def handle_classify(req_id, arguments):
    args = arguments or {}
    text = args.get("text", "")
    if not isinstance(text, str):
        send_json(
            make_success(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: 'text' argument must be a string",
                        }
                    ],
                    "isError": True,
                },
            )
        )
        return
    verdict = classify(text)
    log.info(
        "classify tool called: %d chars -> malformed=%s reason=%s cleaned=%d chars",
        len(text),
        verdict["malformed"],
        verdict["reason"],
        len(verdict["cleaned"]),
    )
    send_json(
        make_success(
            req_id,
            {
                "content": [{"type": "text", "text": json.dumps(verdict)}],
                "isError": False,
            },
        )
    )


def main():
    global initialized

    log.info("llm-response-hygiene MCP server starting (PID=%d)", os.getpid())

    def monitor_stdin():
        stdin_closed.wait()
        log.info("stdin closed detected - will cancel on next iteration")

    monitor = threading.Thread(target=monitor_stdin, daemon=True)
    monitor.start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        if line == "__EOF__":
            stdin_closed.set()
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            log.error("Failed to parse JSON-RPC: %s", e)
            continue

        method = request.get("method", "")
        req_id = request.get("id")

        if method == "initialize":
            if req_id is not None:
                handle_initialize(req_id)
                initialized = True

        elif method == "notifications/initialized":
            log.info("Client initialized notification received")

        elif method == "tools/list":
            if not initialized:
                if req_id is not None:
                    send_json(make_error(req_id, -32000, "Server not initialized"))
                continue
            if req_id is not None:
                handle_tools_list(req_id)

        elif method == "tools/call":
            if not initialized:
                if req_id is not None:
                    send_json(make_error(req_id, -32000, "Server not initialized"))
                continue
            if req_id is not None:
                params = request.get("params", {})
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})

                if tool_name == "classify":
                    handle_classify(req_id, arguments)
                else:
                    send_json(make_error(req_id, -32602, f"Unknown tool: {tool_name}"))

        else:
            log.warning("Unknown method: %s", method)
            if req_id is not None:
                send_json(make_error(req_id, -32601, f"Method not found: {method}"))

    log.info("llm-response-hygiene MCP server shutting down (stdin closed)")


# ── Reference-equivalence corpus + self-test ────────────────────────────────
# The SAME corpus is asserted by the Rust side
# (`src/agent/response_hygiene.rs` tests) - a tool verdict must reproduce the
# decision the core made before this tool existed.

DSML_ENVELOPE = (
    "<\uff5cDSML\uff5ccalls>\n"
    "<\uff5cDSML\uff5cinvoke name=\"subtasks_manage-subtasks\">\n"
    "<\uff5cDSML\uff5cparameter name=\"action\" string=\"true\">update</\uff5cDSML\uff5cparameter>\n"
    "<\uff5cDSML\uff5cparameter name=\"subtask_id\" string=\"false\">1</\uff5cDSML\uff5cparameter>\n"
    "</\uff5cDSML\uff5cinvoke>\n"
    "</\uff5cDSML\uff5ccalls>"
)

DSML_WITH_PROSE = (
    "The task hit its iteration limit before the final check.\n"
    "<\uff5cDSML\uff5cinvoke name=\"x\">\n"
    "</\uff5cDSML\uff5cinvoke>\n"
    "Remaining: the reproduction run was not executed."
)

XML_ENVELOPE = (
    "<tool_calls>\n<invoke name=\"git_run-command\">\n"
    "<parameter name=\"args\">[\"diff\"]</parameter>\n</invoke>\n</tool_calls>"
)

MARKDOWN_FENCE = "start\n```tool_call\n{\"tool\": \"x\"}\n```\nend"

CONTINUATION = (
    "I'll update the subtasks with what I've established and deliver the final "
    "answer. First batch: inspect the last commits."
)

PLAIN_SUMMARY = (
    "Committed abc123 and pushed to origin/main; the reproduction run was not executed."
)

# (input, expected malformed, expected reason, expected cleaned-empty,
#  expected continuation_intent)
SELFTEST_CORPUS = [
    (DSML_ENVELOPE, True, "dsml_markup", True, False),
    (DSML_WITH_PROSE, True, "dsml_markup", False, False),
    (XML_ENVELOPE, True, "xml_tool_call_envelope", True, False),
    (MARKDOWN_FENCE, True, "markdown_tool_call_fence", False, False),
    (CONTINUATION, False, "continuation_intent", False, True),
    (PLAIN_SUMMARY, False, "clean", False, False),
]


def selftest():
    failures = []
    for raw, exp_malformed, exp_reason, exp_empty, exp_cont in SELFTEST_CORPUS:
        got = classify(raw)
        got_empty = not got["cleaned"].strip()
        if (
            got["malformed"] != exp_malformed
            or got["reason"] != exp_reason
            or got_empty != exp_empty
            or got["continuation_intent"] != exp_cont
        ):
            failures.append((raw[:60], got, exp_malformed, exp_reason, exp_empty, exp_cont))

    # Purity: identical input -> identical output (10 consecutive calls).
    for _ in range(10):
        if classify(DSML_WITH_PROSE) != classify(DSML_WITH_PROSE):
            failures.append(("purity", DSML_WITH_PROSE[:40], "", "", "", ""))

    # Latency on a large (64 KB) message.
    big = ("filler line of prose that is not markup. " * 2000)
    t0 = time.perf_counter()
    for _ in range(10):
        classify(big)
    per_call_ms = (time.perf_counter() - t0) * 100.0

    print(f"corpus cases: {len(SELFTEST_CORPUS)}, failures: {len(failures)}")
    print(f"latency: {per_call_ms:.2f} ms/call for a {len(big)}-char message")
    for f in failures:
        print("FAIL:", f)
    if failures:
        sys.exit(1)
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        selftest()
    else:
        main()
