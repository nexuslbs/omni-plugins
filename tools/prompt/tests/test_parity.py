#!/usr/bin/env python3
"""Parity guards for the remote Python prompt plugin (tools/prompt/server.py).

These tests pin the parity surface against the builtin Rust prompt plugin
(omniagent plugins/tools/prompt). They need NO database:

  * the pure parity surface is asserted by importing server.py as a module
    (read-type detection, descriptor precedence, token-budget measurement,
    manifest schema, guidance text);
  * the tool registry and the compaction path are driven end to end over
    stdio JSON-RPC by spawning `python3 server.py`, exactly like the runtime
    does (same transport the builtin exposes through run_server_with_config).

Run (inside the omniagent runtime image / dev container, where psycopg2 is
installed, like the plugin itself):

    python3 tools/prompt/tests/test_parity.py

Regenerate the golden tool list after an intentional, Rust-verified change
(the differential harness must show the Rust side matching it):

    PROMPT_PARITY_UPDATE=1 python3 tools/prompt/tests/test_parity.py

Byte-identical OUTPUT parity (prompt_generate / prompt_compact-messages over
real DB state) is proven by tools/prompt/tests/differential_harness.py; the
procedure is documented in tools/prompt/PARITY.md.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
FIXTURE_DIR = HERE / "fixtures"
TOOLS_LIST_FIXTURE = FIXTURE_DIR / "tools_list.json"

sys.path.insert(0, str(PLUGIN_DIR))
import server  # noqa: E402  (the plugin module under test)

# Builtin Rust plugin manifest: compared when present (workspace checkouts /
# the dev container expose it at the default path).
RUST_PLUGIN_JSON = Path(os.environ.get(
    "PROMPT_RUST_PLUGIN_JSON",
    "/opt/workspace/omniagent/plugins/tools/prompt/plugin.json",
))

# Reference manifest: the builtin Rust plugin's config_schema, snapshotted at
# tests/fixtures/rust_plugin_config_schema.json and, when the omniagent
# checkout is present, compared live.
RUST_SCHEMA_FIXTURE = FIXTURE_DIR / "rust_plugin_config_schema.json"

# Exact-parity contract: NO label may differ. The omni_dir label was the
# last hold-out (omni-plugins manifest convention "OMNI_DIR"); it is now
# pinned to the builtin Rust value "Omni Dir" so gate 1 is a literal
# zero-diff manifest comparison.
ALLOWED_LABEL_DIFFS = {}

EXPECTED_TOOL_NAMES = ["prompt_generate", "prompt_compact-messages"]

# Tool-naming grammar c78e874: current `{plugin}__{tool}` names.
GRAMMAR_CURRENT = [
    "filesystem__write", "filesystem__read",
    "notes__note_write", "notes__note_append", "notes__note_read",
    "notes__note_list", "notes__note_rm",
    "docker__compose", "subtasks__manage_subtasks",
]
# Pre-flip (one-release alias window) names that must no longer appear in the
# user-visible guidance text.
GRAMMAR_LEGACY_IN_GUIDANCE = [
    "filesystem_write", "filesystem_read", "filesystem__exec",
    "notes_note-write", "notes_note-write", "notes_note_append",
    "docker_compose", "subtasks_manage-subtasks", "subtasks_manage_subtasks",
]

READ_TOOLS_LEGACY = [
    "filesystem__read", "filesystem__list", "filesystem__search",
    "filesystem__info", "search__database", "search__messages", "search__wiki",
    "skills__view", "git__status", "git__run_command",
    "filesystem_read", "filesystem_list", "filesystem_search",
    "filesystem_info", "search_database", "search_messages", "search_wiki",
    "skills_view", "git_status", "git_run-command",
]
# Tools the legacy prefix list does NOT match. NOTE: that list is
# deliberately coarse (`filesystem__` matches filesystem__write too) - exact
# Rust parity; only the descriptor set (1d29f3b) narrows writers out.
NON_READ_TOOLS = [
    "docker__compose", "subtasks__manage_subtasks", "prompt__generate",
]


class Session:
    """Line-delimited JSON-RPC session against `python3 server.py`."""

    def __init__(self, initialize=True, **env_overrides):
        env = dict(os.environ)
        env.setdefault("OMNI_DIR", str(PLUGIN_DIR))
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(PLUGIN_DIR),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._id = 0
        if initialize:
            self.rpc("initialize", {"protocolVersion": server.MCP_PROTOCOL_VERSION})

    def rpc(self, method, params=None):
        self._id += 1
        req_id = self._id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(
                f"server closed stdout while handling {method} (id={req_id})")
        return json.loads(line)

    def call_tool(self, name, arguments):
        resp = self.rpc("tools/call", {"name": name, "arguments": arguments})
        result = resp["result"]
        text = result["content"][0]["text"]
        return result.get("isError", False), text

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def load_tool_list():
    session = Session()
    try:
        return session.rpc("tools/list")["result"]["tools"]
    finally:
        session.close()


def compact_corpus(read_tool="filesystem__read", non_read_tool="docker__compose"):
    """Deterministic over-budget conversation with two tool-call turns."""
    messages = [{"role": "system", "content": "S" * 200}]
    for i in range(40):
        messages.append({"role": "user", "content": f"question {i} " + "u" * 200})
        messages.append({"role": "assistant", "content": f"answer {i} " + "a" * 200})
    # Read-type tool turn (generous excerpt) and a non-read turn (generic cap).
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


class ToolRegistryParity(unittest.TestCase):
    """Gate 2: same tools, same names, same input schemas."""

    @classmethod
    def setUpClass(cls):
        cls.tools = load_tool_list()

    def test_tool_names_and_order(self):
        self.assertEqual([t["name"] for t in self.tools], EXPECTED_TOOL_NAMES)

    def test_schemas_match_golden(self):
        """Golden snapshot of the registry (regenerate with PROMPT_PARITY_UPDATE=1)."""
        if os.environ.get("PROMPT_PARITY_UPDATE") == "1":
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            TOOLS_LIST_FIXTURE.write_text(
                json.dumps(self.tools, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            self.skipTest("fixture regenerated")
        self.assertTrue(TOOLS_LIST_FIXTURE.is_file(),
                        f"missing golden fixture {TOOLS_LIST_FIXTURE}")
        golden = json.loads(TOOLS_LIST_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.tools, golden)

    def test_uninitialized_session_is_rejected(self):
        session = Session(initialize=False)
        try:
            resp = session.rpc("tools/list")
            self.assertIn("error", resp)
        finally:
            session.close()

    def test_unknown_tool_is_rejected(self):
        session = Session()
        try:
            resp = session.rpc("tools/call", {"name": "prompt_nope", "arguments": {}})
            self.assertIn("error", resp)
            self.assertIn("Unknown tool", resp["error"]["message"])
        finally:
            session.close()


class ManifestParity(unittest.TestCase):
    """Gate 1: same plugin.json config schema (keys/types/defaults/labels)."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))["config_schema"]
        source = RUST_PLUGIN_JSON if RUST_PLUGIN_JSON.is_file() else RUST_SCHEMA_FIXTURE
        cls.reference_source = str(source)
        cls.rust = json.loads(source.read_text(encoding="utf-8"))["config_schema"]

    def test_entry_count(self):
        self.assertEqual(len(self.schema), 15)
        self.assertEqual(len(self.rust), 15)

    def test_schema_matches_builtin(self):
        rust = self.rust
        self.assertEqual(len(rust), len(self.schema))
        label_diffs = {}
        for mine, theirs in zip(self.schema, rust):
            self.assertEqual(mine["key"], theirs["key"])
            self.assertEqual(mine["type"], theirs["type"])
            self.assertEqual(mine["default"], theirs["default"])
            self.assertEqual(mine.get("description"), theirs.get("description"))
            if mine.get("label") != theirs.get("label"):
                label_diffs[mine["key"]] = (theirs["label"], mine["label"])
        self.assertEqual(label_diffs, ALLOWED_LABEL_DIFFS,
                         "labels must equal the builtin plugin.json exactly")


class GuidanceGrammarParity(unittest.TestCase):
    """c78e874: the guidance text must use the `{plugin}__{tool}` grammar."""

    def test_current_grammar_names_present(self):
        missing = [n for n in GRAMMAR_CURRENT if n not in server.TOOL_GUIDANCE]
        self.assertEqual(missing, [], "guidance lost current-grammar tool names")

    def test_legacy_names_absent(self):
        for name in GRAMMAR_LEGACY_IN_GUIDANCE:
            self.assertNotIn(name, server.TOOL_GUIDANCE,
                             f"legacy tool name {name!r} still in TOOL_GUIDANCE")


class ReadToolDetectionParity(unittest.TestCase):
    """c78e874 (prefix list) + 1d29f3b (descriptor-driven, fail-closed)."""

    def test_legacy_prefix_list_covers_both_grammars(self):
        for name in READ_TOOLS_LEGACY:
            self.assertTrue(server.legacy_read_type_tool(name),
                            f"{name} should be a read-type tool")
        for name in NON_READ_TOOLS:
            self.assertFalse(server.legacy_read_type_tool(name),
                             f"{name} should not be a read-type tool")

    def test_empty_descriptors_fall_back_to_prefixes(self):
        for settings in ({}, {"read_only_tools": None}, {"read_only_tools": []}):
            self.assertTrue(server.is_read_type_tool("filesystem__read", settings))
            self.assertFalse(server.is_read_type_tool("docker__compose", settings))

    def test_descriptors_take_precedence(self):
        settings = {"read_only_tools": ["my_plugin__read_thing"]}
        self.assertTrue(server.is_read_type_tool("my_plugin__read_thing", settings))
        # Declared set is authoritative: a prefix match outside it is NOT read.
        self.assertFalse(server.is_read_type_tool("filesystem__read", settings))

    def test_read_only_tool_names_parsing(self):
        self.assertEqual(server.read_only_tool_names({"read_only_tools": ["a", "b"]}),
                         ["a", "b"])
        self.assertEqual(server.read_only_tool_names({}), [])
        self.assertEqual(server.read_only_tool_names({"read_only_tools": "a"}), [])
        self.assertEqual(server.read_only_tool_names({"read_only_tools": [1, "a"]}),
                         ["a"])


class MeasureSizeParity(unittest.TestCase):
    """Rust measure_size: chars/4 fallback, BPE when a tokenizer is set."""

    def test_empty_encoding_uses_chars_over_four(self):
        messages = [{"role": "user", "content": "x" * 40}]
        self.assertEqual(server.measure_size(messages, ""), 10)

    def test_invalid_encoding_falls_back(self):
        messages = [{"role": "user", "content": "x" * 40}]

        class Boom:
            @staticmethod
            def encoding_for_model(_):
                raise ValueError("unknown model")

            @staticmethod
            def get_encoding(_):
                raise ValueError("unknown encoding")

        original = server.tiktoken
        server.tiktoken = Boom
        try:
            self.assertEqual(server.measure_size(messages, "not-a-real-encoding"), 10)
        finally:
            server.tiktoken = original

    def test_valid_encoding_counts_serialized_messages(self):
        messages = [{"role": "user", "content": "abcd"}]

        class Half:
            @staticmethod
            def encode(text, disallowed_special=()):
                return list(range(len(text) // 2))

        class FakeTiktoken:
            @staticmethod
            def encoding_for_model(_):
                return Half()

        original = server.tiktoken
        server.tiktoken = FakeTiktoken
        try:
            expected = len(server.serialize_messages_rust(messages)) // 2
            self.assertEqual(server.measure_size(messages, "gpt-4"), expected)
        finally:
            server.tiktoken = original


class CompactMessagesE2E(unittest.TestCase):
    """Compaction over stdio: budgets, excerpts, descriptor override, errors."""

    def setUp(self):
        self.session = Session()

    def tearDown(self):
        self.session.close()

    def test_over_budget_compaction_uses_budget_and_excerpts(self):
        messages = compact_corpus()
        is_error, text = self.session.call_tool("prompt_compact-messages", {
            "messages": messages,
            "hard_budget": 1000,
            "soft_budget": 500,
            "keep_recent": 2,
        })
        self.assertFalse(is_error, text)
        envelope = json.loads(text)
        # Rust main.rs L1845-1862: the tool ALWAYS returns the envelope; the
        # messages array is present only when something was compacted.
        self.assertTrue(envelope["was_compacted"], text)
        self.assertIsInstance(envelope["messages"], list)
        self.assertLess(len(envelope["messages"]), len(messages))
        joined = json.dumps(envelope, ensure_ascii=False)
        self.assertIn(server.COMPACTION_SUMMARY_MARKER, joined)
        read_run = max((len(m.group(0)) for m in re.finditer(r"R+", joined)), default=0)
        generic_run = max((len(m.group(0)) for m in re.finditer(r"D+", joined)), default=0)
        self.assertEqual(read_run, 2000, "read-type tool keeps read_excerpt_chars")
        self.assertEqual(generic_run, 800, "other tools keep tool_excerpt_chars")

    def test_descriptor_supplied_read_tool_gets_generous_excerpt(self):
        messages = compact_corpus(read_tool="my_plugin__read_file")
        is_error, text = self.session.call_tool("prompt_compact-messages", {
            "messages": messages,
            "hard_budget": 1000,
            "soft_budget": 500,
            "keep_recent": 2,
            "read_only_tools": ["my_plugin__read_file"],
        })
        self.assertFalse(is_error, text)
        joined = json.dumps(json.loads(text), ensure_ascii=False)
        run = max((len(m.group(0)) for m in re.finditer(r"R+", joined)), default=0)
        self.assertEqual(run, 2000, "descriptor-declared read tool keeps the generous excerpt")

    def test_force_compact_bypasses_gate(self):
        """Rust main.rs L1785-1800: the core re-invokes with force_compact=true
        when provider usage shows the context is still over the hard budget."""
        messages = compact_corpus()
        # Negative control (Rust main.rs L2875-2885): budgets above the local
        # estimate, NO force -> null-contract, no compaction.
        is_error, text = self.session.call_tool("prompt_compact-messages", {
            "messages": messages,
            "hard_budget": 10_000_000,
            "soft_budget": 5_000_000,
            "keep_recent": 0,
        })
        self.assertFalse(is_error, text)
        control = json.loads(text)
        self.assertFalse(control["was_compacted"], text)
        self.assertIsNone(control["messages"])
        # Same corpus WITH force_compact=true must compact (engine override).
        is_error, text = self.session.call_tool("prompt_compact-messages", {
            "messages": messages,
            "hard_budget": 10_000_000,
            "soft_budget": 5_000_000,
            "keep_recent": 0,
            "force_compact": True,
        })
        self.assertFalse(is_error, text)
        envelope = json.loads(text)
        self.assertTrue(envelope["was_compacted"], text)
        self.assertIsInstance(envelope["messages"], list)

    def test_within_budget_null_contract(self):
        """Rust main.rs L1845-1862: always the envelope; `messages` is null and
        `was_compacted` false when nothing was compacted."""
        is_error, text = self.session.call_tool("prompt_compact-messages", {
            "messages": [{"role": "user", "content": "hi"}],
            "hard_budget": 100000,
            "soft_budget": 50000,
        })
        self.assertFalse(is_error, text)
        envelope = json.loads(text)
        self.assertIsNone(envelope["messages"])
        self.assertFalse(envelope["was_compacted"])
        self.assertEqual(envelope["before_count"], envelope["after_count"])
        self.assertEqual(
            sorted(envelope), ["after_count", "before_count", "dump_file",
                               "entries", "iteration", "messages", "was_compacted"])

    def test_legacy_prefix_list_is_coarse_like_rust(self):
        # `filesystem__` matches writers too; only the declared descriptor set
        # narrows them out - exact Rust legacy_read_type_tool semantics.
        self.assertTrue(server.legacy_read_type_tool("filesystem__write"))
        self.assertFalse(server.is_read_type_tool(
            "filesystem__write", {"read_only_tools": ["filesystem__read"]}))

    def test_missing_arguments_error_messages(self):
        for arguments, needle in (
            ({}, "Missing required argument: 'messages'"),
            ({"messages": []}, "Missing required argument: 'hard_budget'"),
            ({"messages": [], "hard_budget": 10}, "Missing required argument: 'soft_budget'"),
        ):
            with self.subTest(needle=needle):
                is_error, text = self.session.call_tool(
                    "prompt_compact-messages", arguments)
                self.assertTrue(is_error, text)
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
