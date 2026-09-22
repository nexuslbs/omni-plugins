#!/usr/bin/env python3
"""Minimal MCP stdio client used by the plugin tests: spawn a plugin server and
call ONE tool, exactly like omniagent's stdio MCP client does."""

from __future__ import annotations

import json
import os
import subprocess
import sys


class McpServer:
    def __init__(self, script: str, env: dict | None = None, cwd: str | None = None):
        child_env = dict(os.environ if env is None else env)
        self.proc = subprocess.Popen(
            [sys.executable, script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=cwd, env=child_env)
        self._next_id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("plugin server closed its stdout")
            line = line.strip()
            if not line:
                continue
            reply = json.loads(line)
            if reply.get("id") == self._next_id:
                return reply

    def initialize(self) -> dict:
        return self._rpc("initialize", {"protocolVersion": "2024-11-05",
                                        "capabilities": {}, "clientInfo": {"name": "test"}})

    def list_tools(self) -> list[dict]:
        reply = self._rpc("tools/list", {})
        return (reply.get("result") or {}).get("tools") or []

    def call(self, name: str, arguments: dict) -> dict:
        reply = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in reply:
            raise RuntimeError(f"tools/call failed: {reply['error']}")
        result = reply.get("result") or {}
        text = "".join(part.get("text", "") for part in result.get("content") or [])
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"_raw": text}
        return {"payload": parsed, "isError": bool(result.get("isError"))}

    def stderr_tail(self) -> str:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return (self.proc.stderr.read() or "")[-2000:]

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()
