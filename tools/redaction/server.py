#!/usr/bin/env python3
"""redaction MCP server : implements standard MCP JSON-RPC over stdio.

Tool:
  - redact: replace possible secrets in text with a redaction mask.

This plugin replaces the secret-redaction logic that used to live in
omniagent core (src/safety.rs). The patterns below are the SAME patterns
core applied before this extraction (behavior parity, just relocated):

  - API keys (sk-...)
  - JWT / Bearer tokens (eyJ...)
  - PostgreSQL / MySQL connection strings
  - AWS access keys (AKIA...)
  - Private key blocks (-----BEGIN ... PRIVATE KEY-----)
  - Slack tokens (xox...)
  - Generic long base64 strings that look like tokens

Cancellation is detected when stdin closes (EOF).
"""

import json
import os
import re
import sys
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [redaction] %(levelname)s %(message)s",
    stream=sys.stderr,
)

log = logging.getLogger("mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
initialized = False
stdin_closed = threading.Event()
stdout_lock = threading.Lock()

# Same patterns as the former omniagent core src/safety.rs SECRET_PATTERNS.
SECRET_PATTERNS = [
    # API keys: OpenAI, Anthropic, DeepSeek, etc.
    (re.compile(r"(?i)\b(sk-[a-zA-Z0-9]{20,})\b"), "API key"),
    # JWT / Bearer tokens
    (re.compile(r"(?i)\b(eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})\b"), "JWT token"),
    # Database connection strings
    (re.compile(r"(?i)(postgres://[^@]+@)"), "PostgreSQL connection string"),
    (re.compile(r"(?i)(mysql://[^:***@]+@)"), "MySQL connection string"),
    # AWS keys
    (re.compile(r"(?i)\b(AKIA[0-9A-Z]{16})\b"), "AWS access key"),
    # Private keys
    (re.compile(r"-----BEGIN\s?(RSA|EC|DSA|OPENSSH)?\s?PRIVATE KEY-----"), "Private key"),
    # Slack/Hub tokens
    (re.compile(r"(?i)\b(xox[baprs]-[0-9a-z]{10,})\b"), "Slack token"),
    # Generic: long base64 strings that look like tokens
    (re.compile(r"\b([a-zA-Z0-9+/]{40,}={0,2})\b"), "Potential token"),
]


def redact(text, mask=None):
    """Replace possible secrets in text with the mask.

    mask: template with an optional {type} placeholder, e.g.
          "[REDACTED {type}]" (default). The {type} placeholder is
          replaced with the pattern label ("API key", "JWT token", ...).
    """
    if mask is None:
        mask = "[REDACTED {type}]"

    # Collect all matches (start, end, label) across all patterns.
    matches = []
    for regex, label in SECRET_PATTERNS:
        for m in regex.finditer(text):
            # Skip very short pseudo-matches (same guard as core).
            if m.end() - m.start() < 8:
                continue
            matches.append((m.start(), m.end(), label))

    if not matches:
        return text

    # Replace from the END backwards so earlier positions stay valid.
    matches.sort(key=lambda t: t[0], reverse=True)
    result = text
    for start, end, label in matches:
        replacement = mask.replace("{type}", label)
        result = result[:start] + replacement + result[end:]

    return result


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
        "serverInfo": {"name": "redaction", "version": "0.1.0"},
    }
    send_json(make_success(req_id, result))
    log.info("Initialized: redaction v0.1.0")


def handle_tools_list(req_id):
    tools = [
        {
            "name": "redact",
            "description": "[redaction] Replace possible secrets (API keys, JWT tokens, connection strings, private keys, Slack tokens, long base64) in the input text with a redaction mask. Returns the redacted string.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to redact",
                    },
                    "mask": {
                        "type": "string",
                        "description": "Redaction mask template; the {type} placeholder is replaced with the matched secret type (default: '[REDACTED {type}]')",
                    },
                },
                "required": ["text"],
            },
        },
    ]
    send_json(make_success(req_id, {"tools": tools}))
    log.info("tools/list returned 1 tool")


def handle_redact(req_id, arguments):
    args = arguments or {}
    text = args.get("text", "")
    mask = args.get("mask")
    if not isinstance(text, str):
        send_json(
            make_success(
                req_id,
                {
                    "content": [{"type": "text", "text": "Error: 'text' argument must be a string"}],
                    "isError": True,
                },
            )
        )
        return
    redacted = redact(text, mask)
    log.info("redact tool called: %d chars -> %d chars", len(text), len(redacted))
    send_json(
        make_success(
            req_id,
            {
                "content": [{"type": "text", "text": redacted}],
                "isError": False,
            },
        )
    )


def main():
    global initialized

    log.info("redaction MCP server starting (PID=%d)", os.getpid())

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

                if tool_name == "redact":
                    handle_redact(req_id, arguments)
                else:
                    if req_id is not None:
                        send_json(
                            make_error(
                                req_id, -32602, f"Unknown tool: {tool_name}"
                            )
                        )

        else:
            log.warning("Unknown method: %s", method)
            if req_id is not None:
                send_json(make_error(req_id, -32601, f"Method not found: {method}"))

    log.info("redaction MCP server shutting down (stdin closed)")


if __name__ == "__main__":
    main()