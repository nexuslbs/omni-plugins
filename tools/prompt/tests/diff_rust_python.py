#!/usr/bin/env python3
"""Differential parity harness: the BUILTIN Rust prompt plugin vs the Python remote port.

Drives BOTH MCP servers (the compiled builtin binary and ``python3 server.py``) over
identical inputs with identical plugin config and compares the returned payloads
BYTE-FOR-BYTE.  It is the executable form of the parity contract documented in
``tools/prompt/README.md``.

Usage (inside the omnidev dev container, where the builtin binary is built at
/target/release/mcp-server-prompt):

    python3 tests/diff_rust_python.py \
        --rust-bin /target/release/mcp-server-prompt \
        --thread-id 2050 --channel-id omnidev --profile omni

Exit code 0 = ZERO unexplained diffs.  Every difference is printed as a unified
diff and counted; the report ends with the case count and the diff count.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpServer:
    """A line-delimited JSON-RPC MCP server over stdio."""

    def __init__(self, argv, label, env=None, cwd=PLUGIN_DIR):
        self.label = label
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=full_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._id = 0

    def send(self, method, params=None, notify=False):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"{self.label}: server closed stdout on {method}")
        return json.loads(line)

    def initialize(self):
        self.send("initialize", {"protocolVersion": "2024-11-05"})

    def configure(self, cfg):
        # Notification: the builtin receives config via run_server_with_config's
        # configure callback, the Python port accepts the same message.
        self.send("configure", cfg, notify=True)

    def tools_list(self):
        return self.send("tools/list", {})["result"]["tools"]

    def call_tool(self, name, arguments):
        resp = self.send("tools/call", {"name": name, "arguments": arguments})
        result = resp["result"]
        return result.get("isError", False), result["content"][0]["text"]

    def close(self):
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def plugin_config():
    """The plugin config both servers receive: every declared key, with its
    default; ``$env:NAME`` defaults resolve from the environment."""
    schema = json.loads((Path(PLUGIN_DIR) / "plugin.json").read_text())["config_schema"]
    cfg = {}
    for entry in schema:
        value = entry.get("default")
        if isinstance(value, str) and value.startswith("$env:"):
            value = os.environ.get(value[5:], "")
        cfg[entry["key"]] = value
    return cfg


def compact_corpus(read_tool="filesystem__read", non_read_tool="docker__compose"):
    """Deterministic over-budget conversation with two tool-call turns."""
    messages = [{"role": "system", "content": "S" * 200}]
    for i in range(40):
        messages.append({"role": "user", "content": f"question {i} " + "u" * 200})
        messages.append({"role": "assistant", "content": f"answer {i} " + "a" * 200})
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": read_tool, "arguments": "{}"}}],
    })
    messages.append({"role": "tool", "name": read_tool, "tool_call_id": "c1",
                     "content": "R" * 6000})
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c2", "type": "function",
                        "function": {"name": non_read_tool, "arguments": "{}"}}],
    })
    messages.append({"role": "tool", "name": non_read_tool, "tool_call_id": "c2",
                     "content": "D" * 6000})
    messages.append({"role": "user", "content": "latest question"})
    messages.append({"role": "assistant", "content": "latest answer"})
    return messages


class Report:
    def __init__(self):
        self.cases = 0
        self.diffs = 0
        self.justified = 0
        self.lines = []

    def compare(self, label, rust_text, py_text, justify=None):
        self.cases += 1
        if rust_text == py_text:
            self.lines.append(f"PASS {label}: byte-identical ({len(rust_text)} chars)")
            return True
        if justify:
            self.justified += 1
            self.lines.append(f"JUSTIFIED {label}: {justify}")
            return True
        self.diffs += 1
        diff = list(difflib.unified_diff(
            rust_text.splitlines(), py_text.splitlines(),
            fromfile=f"rust:{label}", tofile=f"python:{label}", lineterm=""))
        shown = "\n".join(diff[:80])
        self.lines.append(f"FAIL {label}: {len(diff)} diff lines\n{shown}")
        return False

    def summary(self):
        self.lines.append(
            f"\n=== {self.cases} cases, {self.diffs} diffs, "
            f"{self.justified} justified ===")
        return "\n".join(self.lines)


def run(args):
    report = Report()
    cfg = plugin_config()
    env = {"OMNI_DIR": os.environ.get("OMNI_DIR", PLUGIN_DIR)}
    rust = McpServer([args.rust_bin], "rust-builtin", env=env)
    py = McpServer([sys.executable, "server.py"], "python-remote", env=env)
    try:
        rust.initialize()
        py.initialize()
        rust.configure(cfg)
        py.configure(cfg)
        time.sleep(2.0)  # let the builtin's async DB pool connect

        # (1) Tool registry: same names, same order, same schemas.
        rust_tools = json.dumps(rust.tools_list(), indent=2, ensure_ascii=False, sort_keys=True)
        py_tools = json.dumps(py.tools_list(), indent=2, ensure_ascii=False, sort_keys=True)
        report.compare("tools/list", rust_tools, py_tools)

        # (2) prompt_compact-messages cases (no DB needed).
        cases = [
            ("compact-under-budget-null-contract",
             {"messages": [{"role": "user", "content": "hi"}],
              "hard_budget": 100000, "soft_budget": 50000}),
            ("compact-over-budget",
             {"messages": compact_corpus(), "hard_budget": 1000,
              "soft_budget": 500, "keep_recent": 2}),
            ("compact-descriptor-read-tool",
             {"messages": compact_corpus(read_tool="my_plugin__read_file"),
              "hard_budget": 1000, "soft_budget": 500, "keep_recent": 2,
              "read_only_tools": ["my_plugin__read_file"]}),
            ("compact-lexical-read-tool",
             {"messages": compact_corpus(read_tool="filesystem__read"),
              "hard_budget": 1000, "soft_budget": 500, "keep_recent": 2,
              "read_only_tools": ["filesystem__read", "search__database"]}),
            ("compact-force-override",
             {"messages": compact_corpus(), "hard_budget": 10_000_000,
              "soft_budget": 5_000_000, "keep_recent": 0, "force_compact": True}),
            ("compact-missing-hard-budget",
             {"messages": [{"role": "user", "content": "x"}], "soft_budget": 10}),
            ("compact-missing-soft-budget",
             {"messages": [{"role": "user", "content": "x"}], "hard_budget": 10}),
            ("compact-tokenizer-encoding",
             {"messages": compact_corpus(), "hard_budget": 1000,
              "soft_budget": 500, "keep_recent": 2}),
        ]
        for label, arguments in cases:
            rust_err, rust_text = rust.call_tool("prompt_compact-messages", arguments)
            py_err, py_text = py.call_tool("prompt_compact-messages", arguments)
            report.compare(f"{label}[isError={rust_err}]", str(rust_err), str(py_err))
            report.compare(label, rust_text, py_text)

        # (3) Durable dumps: thread_dir + current_iteration must produce the same
        # context-N.json + auto-notes.md on both sides.
        dumps = {}
        for server in (rust, py):
            tmp = tempfile.mkdtemp(prefix=f"prompt-dump-{server.label}-")
            dumps[server.label] = tmp
            _, text = server.call_tool("prompt_compact-messages", {
                "messages": compact_corpus(), "hard_budget": 1000,
                "soft_budget": 500, "keep_recent": 2, "thread_dir": tmp,
                "current_iteration": 7,
            })
            report.lines.append(f"NOTE {server.label} dump envelope: {text[:200]}")
        for name in ("context-7.json", "auto-notes.md"):
            rust_path = os.path.join(dumps["rust-builtin"], name)
            py_path = os.path.join(dumps["python-remote"], name)
            rust_body = open(rust_path).read() if os.path.exists(rust_path) else "<absent>"
            py_body = open(py_path).read() if os.path.exists(py_path) else "<absent>"
            report.compare(f"thread_dir/{name}", rust_body, py_body)
        for tmp in dumps.values():
            shutil.rmtree(tmp, ignore_errors=True)

        # (4) prompt_generate over the SAME live dev DB + omni dir.
        if args.thread_id:
            for label, arguments in [
                ("generate-default", {
                    "thread_id": args.thread_id,
                    "channel_id": args.channel_id,
                    "profile_name": args.profile,
                    "user_message": "parity probe",
                    "plan": None,
                    "tool_names": ["filesystem__read", "prompt__generate", "web__search"],
                    "platform": "mattermost",
                }),
                ("generate-no-plan", {
                    "thread_id": args.thread_id,
                    "channel_id": args.channel_id,
                    "profile_name": args.profile,
                    "user_message": "implement a multi-step redesign of the parity harness",
                    "plan": False,
                }),
                ("generate-plan-true", {
                    "thread_id": args.thread_id,
                    "channel_id": args.channel_id,
                    "profile_name": args.profile,
                    "user_message": "build a multi-step parity matrix for the prompt plugin",
                    "plan": True,
                    "tool_names": ["filesystem__read", "filesystem__write", "git__status",
                                   "prompt__generate"],
                    "platform": "mattermost",
                }),
            ]:
                rust_err, rust_text = rust.call_tool("prompt_generate", arguments)
                py_err, py_text = py.call_tool("prompt_generate", arguments)
                report.compare(f"{label}[isError={rust_err}]", str(rust_err), str(py_err))
                report.compare(label, rust_text, py_text)
        else:
            report.lines.append("SKIP prompt_generate: no --thread-id supplied")
    finally:
        rust.close()
        py.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-bin", default="/target/release/mcp-server-prompt",
                        help="compiled builtin prompt plugin binary")
    parser.add_argument("--thread-id", type=int, default=None)
    parser.add_argument("--channel-id", default="omnidev")
    parser.add_argument("--profile", default="omni")
    parser.add_argument("--report", default=None, help="write the full report to this file")
    parsed = parser.parse_args()

    if not os.path.exists(parsed.rust_bin):
        print(f"Rust builtin binary not found: {parsed.rust_bin}", file=sys.stderr)
        return 2
    report = run(parsed)
    text = report.summary()
    print(text)
    if parsed.report:
        with open(parsed.report, "w") as handle:
            handle.write(text + "\n")
    return 1 if report.diffs else 0


if __name__ == "__main__":
    sys.exit(main())
