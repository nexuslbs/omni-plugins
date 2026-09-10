#!/usr/bin/env python3
"""Self-test for the V-5 platform-declared prompt hints (Python copy).

Audit V-5: the prompt tool must NOT map platform NAMES to formatting hints
(the old ``PLATFORM_HINTS = {"telegram": ..., "mattermost": ...}`` table whose
catch-all silently dropped all guidance for every other platform). Instead the
platform plugin OWNS its hint and advertises it as
``capabilities.prompt_hint`` in its ``initialize`` result; the core forwards it
to this tool as the ``platform_hint`` argument.

Cases (mirroring the Rust unit tests in
``omniagent/plugins/tools/prompt/src/prompt_builder.rs`` ``platform_hint_tests``):

  (a) the Telegram string arrives through the DESCRIPTOR path (not a name map)
  (b) a platform this tool never heard of is rendered from its declared hint
  (c) a named platform with NO declared hint gets the generic markdown fallback
  (d) an unnamed platform keeps the historical output (no platform section)
  (e) Mattermost's declared hint is byte-identical to the old hardcoded string
  (f) Rust/Python parity: the generic fallback string and the plugin-declared
      Telegram/Mattermost strings are identical in both copies (when the
      omniagent checkout is available, e.g. inside the dev container)

Run from the repo root:  python3 scripts/test_prompt_hints.py
Exit code 0 = all cases pass, 1 = at least one case failed.

Task: task_omnidev_prompt_tool_get_platform_formatting (audit V-5).
"""
import importlib.util
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "tools", "prompt", "server.py")
TELEGRAM_PLATFORM = os.path.join(REPO, "platforms", "telegram", "platform.py")
# Other repo of the twin implementation (may be absent outside the dev box).
OMNIAGENT = os.environ.get("OMNIAGENT_REPO", "/opt/workspace/omniagent")
RUST_PROMPT_BUILDER = os.path.join(
    OMNIAGENT, "plugins", "tools", "prompt", "src", "prompt_builder.rs")
RUST_MATTERMOST = os.path.join(
    OMNIAGENT, "plugins", "platforms", "mattermost", "src", "main.rs")

RUN = 0
FAIL = 0
SKIP = 0


def check(name, cond, detail=""):
    global RUN, FAIL
    RUN += 1
    if cond:
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s%s" % (name, ("  -> " + detail) if detail else ""))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("SKIP  %s (%s)" % (name, why))


# ── load the tool module without running its MCP main loop ──────────────────
# server.py imports psycopg2 at module level; stub it when absent so this is a
# pure unit test that needs neither a database nor the plugin runtime.
try:
    import psycopg2  # noqa: F401
except Exception:  # pragma: no cover - depends on the host image
    stub = types.ModuleType("psycopg2")
    stub.extras = types.ModuleType("psycopg2.extras")
    sys.modules["psycopg2"] = stub
    sys.modules["psycopg2.extras"] = stub.extras

spec = importlib.util.spec_from_file_location("prompt_server_under_test", SERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

with open(SERVER, "r", encoding="utf-8") as fh:
    SERVER_SRC = fh.read()


def rust_const(path, name):
    """Extract a single-line `const NAME: &str = "...";` from Rust source."""
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r'const\s+%s\s*:\s*&str\s*=\s*"((?:[^"\\]|\\.)*)"\s*;' % re.escape(name), src)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


# ── (a)+(e) the two legacy strings still render, via the DESCRIPTOR path ────
TELEGRAM_HINT = (
    "You are on a text messaging communication platform, Telegram. "
    "Standard markdown is automatically converted to Telegram format. Supported: **bold**, "
    "*italic*, ~~strikethrough~~, ||spoiler||, `inline code`, ```code blocks```, [links](url), "
    "and ## headers. Telegram has NO table syntax: prefer bullet lists or labeled key: value "
    "pairs over pipe tables (any tables you do emit are auto-rewritten into row-group bullets, "
    "which you can produce directly for cleaner output). You can send media files natively: "
    "to deliver a file to the user, include MEDIA:/absolute/path/to/file in your response. "
    "Images (.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice bubbles, and "
    "videos (.mp4) play inline. You can also include image URLs in markdown format ![alt](url) "
    "and they will be sent as native photos."
)
MATTERMOST_HINT = (
    "You are on a Mattermost messaging platform. Standard markdown formatting is supported: "
    "**bold**, *italic*, `code`, ```code blocks```, [links](url), headings, lists, tables, "
    "blockquotes. Mattermost supports most GFM (GitHub Flavored Markdown)."
)

check("(a) telegram declared hint renders verbatim via descriptor path",
      mod.build_platform_hint("telegram", TELEGRAM_HINT) == TELEGRAM_HINT)
check("(a) telegram with NO declared hint is NOT the plugin text (name map is gone)",
      mod.build_platform_hint("telegram", None) == mod.GENERIC_PLATFORM_HINT)
check("(e) mattermost declared hint renders verbatim (byte-identical)",
      mod.build_platform_hint("mattermost", MATTERMOST_HINT) == MATTERMOST_HINT)

# ── (b) an unknown platform renders its OWN declared hint ──────────────────
IRC_HINT = "Use IRC colors and keep lines under 400 chars."
check("(b) unknown platform renders its declared hint",
      mod.build_platform_hint("irc-fake", IRC_HINT) == IRC_HINT)
check("(b) whitespace-only declaration counts as 'nothing declared'",
      mod.build_platform_hint("irc-fake", "   ") == mod.GENERIC_PLATFORM_HINT)

# ── (c) named platform, nothing declared -> generic markdown fallback ──────
check("(c) named platform without hint gets the generic fallback",
      mod.build_platform_hint("signal", None) == mod.GENERIC_PLATFORM_HINT)
check("(c) generic fallback is a real non-empty markdown note",
      isinstance(mod.GENERIC_PLATFORM_HINT, str)
      and "**bold**" in mod.GENERIC_PLATFORM_HINT
      and "```code blocks```" in mod.GENERIC_PLATFORM_HINT)

# ── (d) unnamed platform keeps the historical output ──────────────────────
check("(d) empty platform -> no platform section", mod.build_platform_hint("", None) is None)
check("(d) empty platform + declared hint -> declared hint wins",
      mod.build_platform_hint("", "declared rules") == "declared rules")
check("(d) None platform -> no platform section", mod.build_platform_hint(None, None) is None)

# ── the NAME -> hint table is gone, and handle_generate wires the arg ─────
check("no PLATFORM_HINTS name->hint table remains in server.py",
      "PLATFORM_HINTS" not in SERVER_SRC)
check("handle_generate reads the platform_hint argument",
      'platform_hint = args.get("platform_hint")' in SERVER_SRC)
check("handle_generate forwards it to build_platform_hint",
      "build_platform_hint(platform, platform_hint)" in SERVER_SRC)
check("the tool schema exposes the optional platform_hint argument",
      re.search(r'"platform_hint"\s*:\s*\{', SERVER_SRC) is not None)

# ── (f) Rust/Python parity of the strings owned by the plugins ────────────
if os.path.isfile(RUST_PROMPT_BUILDER) and os.path.isfile(RUST_MATTERMOST) \
        and os.path.isfile(TELEGRAM_PLATFORM):
    rust_generic = rust_const(RUST_PROMPT_BUILDER, "GENERIC_PLATFORM_HINT")
    rust_telegram = rust_const(RUST_PROMPT_BUILDER, "TELEGRAM_DECLARED_HINT")
    rust_mattermost_test = rust_const(RUST_PROMPT_BUILDER, "MATTERMOST_DECLARED_HINT")
    rust_mattermost_plugin = rust_const(RUST_MATTERMOST, "MATTERMOST_PROMPT_HINT")

    with open(TELEGRAM_PLATFORM, "r", encoding="utf-8") as fh:
        py_telegram = re.search(r'"prompt_hint"\s*:\s*"((?:[^"\\]|\\.)*)"', fh.read())
    py_telegram = py_telegram.group(1).replace('\\"', '"') if py_telegram else None

    check("(f) GENERIC_PLATFORM_HINT identical in Rust and Python",
          rust_generic == mod.GENERIC_PLATFORM_HINT,
          "rust=%r python=%r" % (rust_generic, mod.GENERIC_PLATFORM_HINT))
    check("(f) Python telegram hint == Rust test's declared telegram hint",
          py_telegram == TELEGRAM_HINT and py_telegram == rust_telegram,
          "py=%r rust=%r" % (py_telegram, rust_telegram))
    check("(f) Mattermost hint identical in the plugin and the Rust test",
          rust_mattermost_plugin == MATTERMOST_HINT
          and rust_mattermost_test == MATTERMOST_HINT,
          "plugin=%r test=%r" % (rust_mattermost_plugin, rust_mattermost_test))
    check("(f) Rust prompt_builder keeps no platform-name -> hint match",
          re.search(r'match\s+platform\s*\{', open(RUST_PROMPT_BUILDER).read()) is None)
else:
    skip("(f) Rust/Python string parity", "omniagent checkout not found at %s" % OMNIAGENT)

print("\n%d checks, %d failed, %d skipped" % (RUN, FAIL, SKIP))
sys.exit(1 if FAIL else 0)
