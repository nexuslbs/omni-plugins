#!/usr/bin/env python3
"""Noop-provider: OpenAI-compatible chat completions server for OmniAgent testing.

Implements POST /v1/chat/completions returning a fake response quoting the user's
last message. No actual LLM call is made — purely for testing channel/provider flows.

Usage:
    python3 server.py [--port PORT]

Environment:
    PORT (default: 9090) — listen port
"""

import json
import os
import uuid
import time
from http.server import HTTPServer, BaseHTTPRequestHandler


class NoopHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing /v1/chat/completions."""

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "test-model-1", "object": "model", "created": int(time.time()), "owned_by": "noop"},
                    {"id": "test-model-2", "object": "model", "created": int(time.time()), "owned_by": "noop"},
                ]
            })
        elif self.path == "/health" or self.path == "/":
            self._send_json(200, {"status": "ok", "provider": "noop"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_chat_completions(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length else b"{}"

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        model = body.get("model", "test-model-1")
        messages = body.get("messages", [])

        # Extract the last user message
        user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break

        # Build quoted response
        quoted_lines = [f"> {line}" for line in user_msg.split("\n")]
        quoted = "\n".join(quoted_lines)

        content = (
            f"This is a reply to your message from the **test provider** `noop` "
            f"using the model **{model}**.\n\n"
            f"Your original message:\n\n"
            f"{quoted}\n\n"
            f"You can enable and configure other providers in the provider "
            f"settings of omni-dashboard."
        )

        response = {
            "id": f"noop-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }

        self._send_json(200, response)

    def _send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Override default logging — quieter."""
        if "GET /health" not in str(args) and "GET / " not in str(args):
            super().log_message(format, *args)


def main():
    port = int(os.environ.get("PORT", "9090"))
    server = HTTPServer(("0.0.0.0", port), NoopHandler)
    print(f"[noop-provider] Listening on http://0.0.0.0:{port}")
    print(f"[noop-provider] POST /v1/chat/completions — returns fake response")
    print(f"[noop-provider] GET  /v1/models           — lists test models")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[noop-provider] Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
