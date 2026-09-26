#!/usr/bin/env python3
"""Regression tests for the memory plugin's profile guard.

Telegram thread 2719 (recurring bug): the plugin used to fall back to the
literal profile name "default" whenever `_meta.profile_name` was absent or
empty. Because every path is built as `<OMNI_DIR>/profiles/<profile>/...`,
that silently created `<OMNI_DIR>/profiles/default` - a directory no
`config/profiles.yml` declares and no prompt ever reads. Real memories were
written there and lost.

The plugin must now:
  * use `_meta.profile_name` when non-empty (the REAL wire key: the host
    renames `CallToolParams.meta` to `_meta`, see
    src/mcp/external/protocol.rs:142; a legacy `meta` key is only tolerated),
  * else fall back ONLY to its explicitly configured `default_profile`,
  * else refuse the call (ProfileNameMissing -> tool error),
  * never touch a profile name that `config/profiles.yml` does not declare,
  * write MEMORY.md / USER.md at the profile ROOT (the file the prompt loader
    reads), never the legacy `profiles/<profile>/memories/` subdirectory.

The last group of tests drives `handle_tools_call` - the plugin's real
JSON-RPC entry point - with the host's actual wire shape, so the profile
guard cannot silently regress into reading the wrong key again.

Run: python3 -m pytest tools/memory/tests/  (or python3 -m unittest)
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server.py"


def load_server():
    """Load server.py as a module (it must not run the stdio loop on import)."""
    spec = importlib.util.spec_from_file_location("memory_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ProfileGuardTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.omni = Path(self._tmp.name)
        (self.omni / "config").mkdir(parents=True, exist_ok=True)
        (self.omni / "config" / "profiles.yml").write_text(
            "profiles:\n"
            "  omni:\n"
            "    tools: []\n"
            "  test-profile:\n"
            "    tools: []\n",
            encoding="utf-8",
        )
        os.environ["OMNI_DIR"] = str(self.omni)
        for key in ("DEFAULT_PROFILE", "OMNI_DEFAULT_PROFILE"):
            os.environ.pop(key, None)
        self.srv = load_server()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    # ── the profile name itself ─────────────────────────────────────────────

    def test_declared_profile_name_is_used_verbatim(self):
        self.assertEqual(self.srv.get_profile({"profile_name": "omni"}), "omni")
        self.assertEqual(
            self.srv.get_profile({"profile_name": " test-profile "}), "test-profile"
        )

    def test_missing_or_empty_profile_name_is_refused(self):
        for meta in (None, {}, {"profile_name": ""}, {"profile_name": "   "}):
            with self.assertRaises(self.srv.ProfileNameMissing):
                self.srv.get_profile(meta)

    def test_undeclared_profile_is_refused(self):
        # "default" is the exact literal that used to be invented.
        with self.assertRaises(self.srv.ProfileNameMissing):
            self.srv.get_profile({"profile_name": "default"})

    def test_no_profiles_default_directory_is_ever_created(self):
        for meta in (None, {}, {"profile_name": "default"}):
            with self.assertRaises(self.srv.ProfileNameMissing):
                self.srv.get_profile(meta)
        self.assertFalse((self.omni / "profiles" / "default").exists())

    def test_configured_default_profile_is_an_explicit_opt_in(self):
        os.environ["DEFAULT_PROFILE"] = "omni"
        self.assertEqual(self.srv.get_profile({}), "omni")
        # A configured default that is NOT declared is still refused.
        os.environ["DEFAULT_PROFILE"] = "default"
        with self.assertRaises(self.srv.ProfileNameMissing):
            self.srv.get_profile({})

    # ── tool calls surface the refusal as a tool error ──────────────────────

    # ── tool calls on the REAL wire shape (params._meta) ───────────────────

    def _call_wire(self, params):
        """Drive the plugin through its real JSON-RPC entry point."""
        captured = []
        self.srv.send_json = lambda obj: captured.append(obj)
        self.srv.handle_tools_call(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}
        )
        self.assertEqual(len(captured), 1, "exactly one JSON-RPC reply expected")
        return captured[0]["result"]

    def test_extract_meta_prefers_the_wire_key(self):
        # the host sends the tool context as `params._meta`
        self.assertEqual(
            self.srv.extract_meta({"_meta": {"profile_name": "omni"}}),
            {"profile_name": "omni"},
        )
        # legacy `meta` is tolerated (older callers)
        self.assertEqual(
            self.srv.extract_meta({"meta": {"profile_name": "omni"}}),
            {"profile_name": "omni"},
        )
        # `_meta` WINS when both are present
        self.assertEqual(
            self.srv.extract_meta(
                {"_meta": {"profile_name": "omni"},
                 "meta": {"profile_name": "default"}}
            ),
            {"profile_name": "omni"},
        )
        for params in (None, {}, {"_meta": None}, {"meta": "oops"}):
            self.assertEqual(self.srv.extract_meta(params), {})

    def test_wire_call_without_profile_refuses_and_creates_no_default(self):
        # `_meta` empty, and no meta key at all: both must be refused loudly.
        for params in (
            {"name": "promote_to_memory",
             "arguments": {"name": "x", "content": "y", "confidence": "high"},
             "_meta": {}},
            {"name": "promote_to_memory",
             "arguments": {"name": "x", "content": "y", "confidence": "high"}},
        ):
            result = self._call_wire(params)
            self.assertTrue(result["isError"], result)
            self.assertIn("refusing", result["content"][0]["text"])
        self.assertFalse((self.omni / "profiles" / "default").exists())

    def test_wire_call_writes_to_the_profile_from_meta(self):
        result = self._call_wire({
            "name": "promote_to_memory",
            "arguments": {"name": "wire-mem", "content": "wire body",
                          "confidence": "high"},
            "_meta": {"profile_name": "omni"},
        })
        self.assertFalse(result["isError"], result)
        promoted = (self.omni / "wiki" / "Memory" / "Promoted"
                    / "wire-mem.md")
        self.assertTrue(promoted.exists(), f"expected {promoted}")
        self.assertIn("wire body", promoted.read_text())
        self.assertFalse((self.omni / "profiles" / "default").exists())

    def test_wire_call_with_legacy_meta_key_still_works(self):
        result = self._call_wire({
            "name": "promote_to_memory",
            "arguments": {"name": "legacy-mem", "content": "legacy body",
                          "confidence": "high"},
            "meta": {"profile_name": "omni"},
        })
        self.assertFalse(result["isError"], result)
        promoted = (self.omni / "wiki" / "Memory" / "Promoted"
                    / "legacy-mem.md")
        self.assertTrue(promoted.exists(), f"expected {promoted}")
        self.assertFalse((self.omni / "profiles" / "default").exists())

    def test_wire_manage_memory_writes_profile_root(self):
        result = self._call_wire({
            "name": "manage_memory",
            "arguments": {"action": "add", "content": "wire-root",
                          "target": "memory"},
            "_meta": {"profile_name": "omni"},
        })
        self.assertFalse(result["isError"], result)
        root = self.omni / "profiles" / "omni" / "MEMORY.md"
        self.assertTrue(root.exists())
        self.assertIn("wire-root", root.read_text())
        self.assertFalse((self.omni / "profiles" / "omni" / "memories").exists())

    def test_wire_list_memories_without_profile_refuses(self):
        result = self._call_wire({"name": "list_memories", "arguments": {}, "_meta": {}})
        self.assertTrue(result["isError"], result)
        self.assertFalse((self.omni / "profiles" / "default").exists())

    # ── manage_memory writes the profile ROOT ───────────────────────────────

    def test_manage_memory_writes_profile_root_not_legacy_memories_dir(self):
        res = self.srv.handle_manage(
            {"action": "add", "content": "hello-root", "target": "memory"},
            {"profile_name": "omni"},
        )
        self.assertFalse(res["isError"])
        root = self.omni / "profiles" / "omni" / "MEMORY.md"
        legacy = self.omni / "profiles" / "omni" / "memories" / "MEMORY.md"
        self.assertTrue(root.exists())
        self.assertIn("hello-root", root.read_text())
        self.assertFalse(legacy.exists(), "legacy profiles/<p>/memories/ must not be written")

    def test_manage_memory_migrates_a_legacy_file_to_the_root(self):
        legacy_dir = self.omni / "profiles" / "omni" / "memories"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "MEMORY.md").write_text("legacy-entry\n", encoding="utf-8")
        self.srv.handle_manage(
            {"action": "add", "content": "new-entry", "target": "memory"},
            {"profile_name": "omni"},
        )
        root = self.omni / "profiles" / "omni" / "MEMORY.md"
        text = root.read_text()
        self.assertIn("new-entry", text)
        self.assertIn("legacy-entry", text, "legacy content must be carried over once")

    # ── promote_to_memory writes the SHARED wiki promoted dir ────────

    def test_promote_to_memory_writes_declared_profile_promoted_dir(self):
        res = self.srv.handle_promote(
            {"name": "my-mem", "content": "fact body", "confidence": "high"},
            {"profile_name": "omni"},
        )
        self.assertFalse(res["isError"])
        promoted = (
            self.omni / "wiki" / "Memory" / "Promoted" / "my-mem.md"
        )
        self.assertTrue(promoted.exists(), f"expected {promoted}")
        self.assertIn("fact body", promoted.read_text())
        self.assertIn("my-mem.md", res["content"][0]["text"])
        # no orphan profile directory was created anywhere
        self.assertFalse((self.omni / "profiles" / "default").exists())

    def test_promote_to_memory_without_profile_name_is_refused(self):
        # The internal handler raises; handle_tools_call turns it into a tool
        # error (covered by test_tool_call_without_profile_returns_tool_error).
        with self.assertRaises(self.srv.ProfileNameMissing):
            self.srv.handle_promote(
                {"name": "orphan", "content": "fact body", "confidence": "high"}, {}
            )
        self.assertFalse((self.omni / "profiles" / "default").exists())


if __name__ == "__main__":
    unittest.main()
