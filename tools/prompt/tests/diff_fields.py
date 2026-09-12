#!/usr/bin/env python3
"""Field-level parity probe.

Runs prompt_generate on BOTH servers (builtin Rust + Python remote) with the
same inputs and prints ONLY the fields that differ, with the first divergent
character and a small context window.  This keeps the actionable signal small
when the payloads are tens of kilobytes.

    python3 tests/diff_fields.py --rust-bin /target/release/mcp-server-prompt \
        --thread-id 2402 --channel-id omnidev --profile omni
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_rust_python import McpServer, plugin_config  # noqa: E402


def first_div(a, b, ctx=160):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    lo = max(0, i - ctx)
    return i, a[lo:i + ctx], b[lo:i + ctx]


def walk(rust, py, path=""):
    out = []
    if type(rust) is not type(py):
        out.append((path, "TYPE", repr(rust)[:160], repr(py)[:160]))
        return out
    if isinstance(rust, dict):
        for key in sorted(set(rust) | set(py)):
            sub = f"{path}.{key}"
            if key not in rust:
                out.append((sub, "MISSING-IN-RUST", "", str(py[key])[:160]))
            elif key not in py:
                out.append((sub, "MISSING-IN-PY", str(rust[key])[:160], ""))
            else:
                out += walk(rust[key], py[key], sub)
    elif isinstance(rust, list):
        if len(rust) != len(py):
            out.append((f"{path}.len", "LEN", str(len(rust)), str(len(py))))
        for index, (left, right) in enumerate(zip(rust, py)):
            out += walk(left, right, f"{path}[{index}]")
    elif isinstance(rust, str):
        if rust != py:
            index, rctx, pctx = first_div(rust, py)
            out.append((path, f"STR chars {len(rust)}/{len(py)} first diff @{index}", rctx, pctx))
    else:
        if rust != py:
            out.append((path, "VAL", repr(rust), repr(py)))
    return out


def generate_cases(args):
    """Same arguments are sent to both servers; only the shape varies."""
    base = {
        "thread_id": args.thread_id,
        "channel_id": args.channel_id,
        "profile_name": args.profile,
        "user_message": "parity probe",
    }
    descriptors = [
        {"name": "filesystem", "tools": ["filesystem__read", "filesystem__write",
                                         "filesystem__list"], "read_only": ["filesystem__read"]},
        {"name": "prompt", "tools": ["prompt__generate"]},
        {"name": "web", "tools": ["web__search", "web__extract"]},
    ]
    return [
        ("plain", dict(base, plan=False)),
        ("platform-mattermost", dict(base, plan=False, platform="mattermost",
                                     platform_hint="Markdown supported here.")),
        ("tool-names", dict(base, plan=False,
                            tool_names=["filesystem__read", "prompt__generate", "web__search"])),
        ("tool-descriptors", dict(base, plan=False, tool_descriptors=descriptors)),
        ("complex-message", dict(base, plan=False,
                                 user_message="implement a multi-step redesign of the "
                                              "parity harness with migrations")),
        ("plan-null", dict(base, plan=None)),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust-bin", default="/target/release/mcp-server-prompt")
    parser.add_argument("--thread-id", type=int, required=True)
    parser.add_argument("--channel-id", default="omnidev")
    parser.add_argument("--profile", default="omni")
    args = parser.parse_args()

    cfg = plugin_config()
    env = {"OMNI_DIR": os.environ.get("OMNI_DIR", os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))}
    rust = McpServer([args.rust_bin], "rust-builtin", env=env)
    py = McpServer([sys.executable, "server.py"], "python-remote", env=env)
    total = 0
    try:
        rust.initialize()
        py.initialize()
        rust.configure(cfg)
        py.configure(cfg)
        time.sleep(2.0)
        for label, arguments in generate_cases(args):
            _, rust_text = rust.call_tool("prompt_generate", arguments)
            _, py_text = py.call_tool("prompt_generate", arguments)
            diffs = walk(json.loads(rust_text), json.loads(py_text))
            total += len(diffs)
            print(f"=== {label}: {len(diffs)} differing field(s)")
            for path, kind, left, right in diffs:
                print(f"  - {path} [{kind}]")
                print(f"      rust : {left!r}")
                print(f"      python: {right!r}")
        print(f"=== TOTAL {total} differing field(s)")
    finally:
        rust.close()
        py.close()
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
