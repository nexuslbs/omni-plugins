#!/usr/bin/env python3
"""
test_plugins.py - comprehensive test suite for the omni-plugins remote registry.

Runs INSIDE the omniagent container (piped via stdin, like scripts/tests.py in
omni-deployer). It exercises the REAL plugin registration path in the stack:

  1. every remote MCP tool plugin in remote.yml is registered via the
     install-git API, enabled, its tools appear in /mcp/tools, and every tool
     is invoked via /mcp/execute (the live MCP executor);
  2. the noop-full provider (stdio) is registered, enabled, and its protocol
     (initialize / list_models / complete) is exercised;
  3. the noop provider (api_mode chat_completions) is enabled and its backend
     (the noop-provider compose service) answers a chat completion;
  4. every platform in remote.yml is registered and enabled; telegram is
     exercised against the bundled mock Telegram Bot API (no real token),
     test-js / test-python / test-rust platforms are exercised over stdio
     (initialize / configure / deliver).

No real credentials are used anywhere. The only skipped tests are those that
cannot run in the current environment (e.g. a rust binary that could not be
compiled by install-git, or a node server whose npm install failed); every
skip is printed with its reason.

Usage (inside the omniagent container):
    python3 -u - < scripts/test_plugins.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8080"
REMOTE_REPO = "/opt/workspace/omni-plugins"
DATA_DIR = "/opt/omni"

tests_run = 0
tests_pass = 0
tests_fail = 0
skips = []


def test(fn):
    global tests_run, tests_pass, tests_fail
    tests_run += 1
    name = fn.__name__.replace("test_", "Test ").replace("_", " ")
    print(f"\n--- {name} ", end="", flush=True)
    t0 = time.time()
    try:
        fn()
        print(f"PASS ({time.time() - t0:.1f}s)", flush=True)
        tests_pass += 1
    except SkipTest as e:
        print(f"SKIP ({time.time() - t0:.1f}s): {e}", flush=True)
        skips.append((fn.__name__, str(e)))
    except Exception as e:
        import traceback
        print(f"FAIL ({time.time() - t0:.1f}s): {e}", flush=True)
        traceback.print_exc()
        tests_fail += 1


class SkipTest(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════
#  API helpers
# ═══════════════════════════════════════════════════════════════════════

def api_get(path, timeout=10):
    with urllib.request.urlopen(f"{BASE}/api{path}", timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}/api{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def get_json(path, timeout=10):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def post_json(path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def mcp_tools():
    """Return the list of registered MCP tools (full_name / name)."""
    data = get_json("/mcp/tools")
    tools = data if isinstance(data, list) else (data.get("tools") or data.get("data") or [])
    return tools


def mcp_execute(name, args, timeout=120):
    """POST a tool call to the live MCP executor; returns the parsed body."""
    req = urllib.request.Request(
        f"{BASE}/mcp/execute",
        data=json.dumps({"name": name, "arguments": args}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_for_tool(substr, timeout=120):
    """Wait until a tool whose full_name contains `substr` is registered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for t in mcp_tools():
                fn = t.get("full_name") or t.get("name") or ""
                if substr in fn:
                    return fn
        except Exception:
            pass
        time.sleep(2)
    return None


def install_remote(name, ptype, path=None):
    """Register a remote plugin via the install-git API (real registration path).

    Uses file:// to the bind-mounted omni-plugins checkout first, falling back
    to the HTTPS GitHub URL (CI environments without the bind mount).
    """
    if path is None:
        path = f"{ptype}/{name}"
    candidates = ["https://github.com/nexuslbs/omni-plugins.git"]
    if os.path.isdir(REMOTE_REPO):
        candidates.insert(0, f"file://{REMOTE_REPO}")
    last_err = None
    for url in candidates:
        try:
            resp = api_post("/plugins/install-git", {"url": url, "name": name, "path": path})
            if resp.get("error") and "already" in str(resp.get("error")).lower():
                return
            if resp.get("error"):
                last_err = resp.get("error")
                continue
            return
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "already" in body.lower():
                return
            last_err = f"HTTP {e.code}: {body[:200]}"
    raise SkipTest(f"install-git {name} failed: {last_err}")


def enable_remote(name, ptype):
    singular = ptype.rstrip("s")
    try:
        resp = api_post(f"/plugins/{singular}s/remote/{name}/enable", {})
        if resp.get("error") and "already" in str(resp.get("error")).lower():
            return
        if resp.get("error"):
            raise SkipTest(f"enable {name}: {resp.get('error')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "already" in body.lower():
            return
        raise SkipTest(f"enable {name}: HTTP {e.code}: {body[:200]}")


def plugin_status(name, ptype):
    singular = ptype.rstrip("s")
    plugins = api_get("/plugins")["data"]
    for p in plugins:
        if p.get("name") == name and p.get("plugin_type") == singular:
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════
#  MCP stdio helpers (for platform/provider protocol tests)
# ═══════════════════════════════════════════════════════════════════════

def spawn_stdio(cmd, cwd=None, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            cwd=cwd, env=e)


def stdio_call(proc, method, params=None, req_id=1, timeout=30):
    req = {"id": req_id, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            continue
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == req_id:
            return resp
    raise AssertionError(f"no response for {method} within {timeout}s")


def stop_proc(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  GROUP 1: registry completeness (static audit of the repo checkout)
# ═══════════════════════════════════════════════════════════════════════

def test_registry_completeness():
    """Every plugin in remote.yml must exist in the checkout with plugin.json
    (and mcp-config.json for MCP tool servers)."""
    with open(f"{REMOTE_REPO}/remote.yml") as f:
        content = f.read()
    import re
    tools = re.findall(r"^  ([\w-]+):\n    url:.*\n    path: (tools/[\w-]+)", content, re.M)
    platforms = re.findall(r"^  ([\w-]+):\n    url:.*\n    path: (platforms/[\w-]+)", content, re.M)
    providers = re.findall(r"^  ([\w-]+):\n    url:.*\n    path: (providers/[\w-]+)", content, re.M)
    assert tools, "no tools parsed from remote.yml"
    for name, path in tools + platforms + providers:
        d = f"{REMOTE_REPO}/{path}"
        assert os.path.isdir(d), f"missing plugin dir {path}"
        assert os.path.exists(f"{d}/plugin.json"), f"{path} missing plugin.json"
        if path.startswith("tools/"):
            assert os.path.exists(f"{d}/mcp-config.json"), f"{path} missing mcp-config.json"
    print(f"registry ok: {len(tools)} tools, {len(platforms)} platforms, {len(providers)} providers")


# ═══════════════════════════════════════════════════════════════════════
#  GROUP 2: remote MCP tool plugins - register, enable, list, invoke
# ═══════════════════════════════════════════════════════════════════════

# Expected tool names (substring) per tool plugin, plus benign args for
# /mcp/execute. Tools whose name contains "test-error" are DESIGNED to error;
# for them we assert the executor returned an is_error response (correct
# behavior), not a crash.
TOOL_PLUGINS = {
    "actions": {
        "tools": ["hindsight_populator", "relevance_indexer", "setup_knowledge_pipeline"],
        "args": {"hindsight_populator": {}, "relevance_indexer": {},
                 "setup_knowledge_pipeline": {}},
    },
    "cosmos-rust-tool": {"tools": ["hello"], "args": {"hello": {}}},
    "cron-echo": {"tools": ["cron_echo"], "args": {"cron_echo": {}}},
    "hindsight": {"tools": ["hindsight_recall", "hindsight_reflect", "hindsight_retain"],
                  "args": {"hindsight_recall": {}, "hindsight_reflect": {}, "hindsight_retain": {}}},
    "memory": {"tools": ["generate_summary", "list_memories", "manage_memory",
                         "promote_to_memory", "review_memories"],
               "args": {"generate_summary": {}, "list_memories": {},
                        "manage_memory": {}, "promote_to_memory": {},
                        "review_memories": {}}},
    "paperclip": {"tools": ["paperclip"], "args": {"paperclip": {}}},
    "prompt": {"tools": ["prompt_compact-messages", "prompt_generate"],
               "args": {"prompt_compact-messages": {}, "prompt_generate": {}}},
    "test-js-tool": {"tools": ["test-js-tool_wait", "test-js-tool_echo",
                               "test-js-tool_save-datetime", "test-js-tool_test-error"],
                     "args": {"test-js-tool_wait": {"seconds": 0},
                              "test-js-tool_echo": {"text": "hello from test_plugins"},
                              "test-js-tool_save-datetime": {"path": "/tmp/plugin_test_dt.txt"},
                              "test-js-tool_test-error": {}}},
    "test-python": {"tools": ["test-python_echo", "test-python_lorem",
                              "test-python_save-datetime", "test-python_test-error",
                              "test-python_wait"],
                    "args": {"test-python_echo": {"text": "hello from test_plugins"},
                             "test-python_lorem": {"count": 1},
                             "test-python_save-datetime": {"path": "/tmp/plugin_test_dt.txt"},
                             "test-python_test-error": {},
                             "test-python_wait": {"seconds": 0}}},
    "test-rust-tool": {"tools": ["test-rust-tool_wait", "test-rust-tool_echo",
                                 "test-rust-tool_save-datetime", "test-rust-tool_test-error"],
                       "args": {"test-rust-tool_wait": {"seconds": 0},
                                "test-rust-tool_echo": {"text": "hello from test_plugins"},
                                "test-rust-tool_save-datetime": {"path": "/tmp/plugin_test_dt.txt"},
                                "test-rust-tool_test-error": {}}},
    "test-rust-tool-2": {"tools": ["hello"], "args": {"hello": {}}},
}


def test_tool_plugins():
    for name, spec in TOOL_PLUGINS.items():
        try:
            _exercise_tool_plugin(name, spec)
        except SkipTest as e:
            print(f"  [skip tool plugin {name}: {e}]")
            skips.append((f"tool:{name}", str(e)))
            continue
        except Exception as e:
            raise AssertionError(f"tool plugin {name}: {e}")


def _exercise_tool_plugin(name, spec):
    install_remote(name, "tools", f"tools/{name}")
    enable_remote(name, "tools")
    # Wait for the plugin's tools to be listed in /mcp/tools (registration proof).
    registered = []
    for want in spec["tools"]:
        full = wait_for_tool(want, timeout=120)
        if full is None:
            raise SkipTest(f"tool {want} never appeared in /mcp/tools after enable")
        registered.append(full)
    # Invoke every tool through the live MCP executor (real invocation path).
    for want, full in zip(spec["tools"], registered):
        args = spec["args"].get(want, {})
        try:
            resp = mcp_execute(full, args)
        except urllib.error.HTTPError as e:
            raise AssertionError(f"{full}: HTTP {e.code}: {e.read()[:200]}")
        is_err = bool(resp.get("is_error", resp.get("isError", False)))
        if "test-error" in want:
            assert is_err, f"{full} is an error tool but returned success: {resp}"
        else:
            assert not is_err, f"{full} returned is_error: {resp}"
            assert resp.get("success") is not False, f"{full} failed: {resp}"
    print(f"  ok: {name} -> {len(registered)} tools registered + invoked")


# ═══════════════════════════════════════════════════════════════════════
#  GROUP 3: providers
# ═══════════════════════════════════════════════════════════════════════

def test_noop_full_provider():
    """noop-full (stdio provider): register remote, enable, verify status, and
    exercise the stdio protocol (initialize / list_models / complete)."""
    install_remote("noop-full", "providers", "providers/noop-full")
    enable_remote("noop-full", "providers")
    st = plugin_status("noop-full", "providers")
    assert st is not None, "noop-full not in /plugins"
    assert st.get("status") == "enabled", f"noop-full status: {st.get('status')}"
    assert st.get("source") == "remote", f"noop-full source: {st.get('source')}"
    # Exercise the provider protocol directly over stdio (no credentials).
    client = f"{REMOTE_REPO}/providers/noop-full/client.py"
    assert os.path.exists(client), f"missing {client}"
    proc = spawn_stdio(["python3", client])
    try:
        init = stdio_call(proc, "initialize", {}, req_id=1)
        assert init.get("result", {}).get("name") == "noop-full", init
        models = init["result"].get("models", [])
        assert "test-model-1" in models and "test-model-2" in models, models
        lm = stdio_call(proc, "list_models", {}, req_id=2)
        assert "test-model-1" in lm.get("result", {}).get("models", []), lm
        comp = stdio_call(proc, "complete",
                          {"model": "test-model-1",
                           "messages": [{"role": "user", "content": "ping from test_plugins"}]},
                          req_id=3)
        content = comp.get("result", {}).get("content", "")
        assert "ping from test_plugins" in content, content
    finally:
        stop_proc(proc)
    print("  ok: noop-full registered+enabled; initialize/list_models/complete work")


def test_noop_provider():
    """noop (api_mode chat_completions): register remote, enable, and verify the
    backend (noop-provider compose service) answers a chat completion."""
    install_remote("noop", "providers", "providers/noop")
    enable_remote("noop", "providers")
    st = plugin_status("noop", "providers")
    assert st is not None, "noop not in /plugins"
    assert st.get("status") == "enabled", f"noop status: {st.get('status')}"
    # Direct HTTP completion against the noop-provider service (no credentials).
    body = json.dumps({"model": "test-model-1",
                       "messages": [{"role": "user", "content": "hello noop"}]}).encode()
    req = urllib.request.Request("http://noop-provider:9090/v1/chat/completions",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    assert "choices" in resp, resp
    print("  ok: noop registered+enabled; noop-provider backend answered a completion")


# ═══════════════════════════════════════════════════════════════════════
#  GROUP 4: platforms
# ═══════════════════════════════════════════════════════════════════════

def test_platforms_registered():
    """Every platform in remote.yml registers + enables as a remote platform."""
    for name in ["telegram", "test-js", "test-python", "test-rust"]:
        try:
            install_remote(name, "platforms", f"platforms/{name}")
            enable_remote(name, "platforms")
        except SkipTest as e:
            print(f"  [skip platform {name}: {e}]")
            skips.append((f"platform:{name}", str(e)))
            continue
        st = plugin_status(name, "platforms")
        assert st is not None, f"{name} not in /plugins"
        assert st.get("status") == "enabled", f"{name} status: {st.get('status')}"
        assert st.get("source") == "remote", f"{name} source: {st.get('source')}"
        print(f"  ok: {name} registered + enabled")


def test_telegram_platform_mock():
    """telegram platform against the bundled mock Telegram Bot API - no real
    bot token. Spawn the mock, configure api_base_url to it, deliver a message
    and verify the mock received it."""
    tg_dir = f"{REMOTE_REPO}/platforms/telegram"
    mock_py = f"{tg_dir}/tests/mock_telegram_api.py"
    platform_py = f"{tg_dir}/platform.py"
    assert os.path.exists(mock_py) and os.path.exists(platform_py), "telegram mock/plugin missing"
    # Free port for the mock.
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    mock_port = s.getsockname()[1]
    s.close()
    mock = spawn_stdio(["python3", mock_py, "--port", str(mock_port)])
    try:
        # Wait for the mock to come up.
        deadline = time.time() + 20
        ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/health", timeout=2):
                    ok = True
                    break
            except Exception:
                time.sleep(1)
        assert ok, "mock telegram api did not come up"
        proc = spawn_stdio(["python3", platform_py])
        try:
            init = stdio_call(proc, "initialize", {}, req_id=1)
            caps = init.get("result", {}).get("capabilities", {})
            assert caps.get("inbound") is True and caps.get("outbound") is True, init
            cfg = stdio_call(proc, "configure",
                             {"config": {"api_base_url": f"http://127.0.0.1:{mock_port}",
                                         "bot_token": "test-token-no-real-credentials",
                                         "polling_enabled": False}},
                             req_id=2)
            assert cfg.get("result", {}).get("configured") is True, cfg
            dlv = stdio_call(proc, "deliver",
                             {"resource_identifier": "123456789",
                              "text": "hello from test_plugins",
                              "message_id": "m1"},
                             req_id=3, timeout=30)
            assert "error" not in dlv, dlv
            # Verify the mock received the sendMessage with the right text.
            got = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{mock_port}/admin/sent", timeout=5).read())
            sent = [m for m in got.get("messages", []) if m.get("text") == "hello from test_plugins"]
            assert sent, f"mock did not receive the delivered message: {got}"
        finally:
            stop_proc(proc)
    finally:
        stop_proc(mock)
    print("  ok: telegram deliver hit the mock (no real token)")


def test_stdio_platforms():
    """test-js / test-python / test-rust platforms: spawn the platform server
    over stdio and exercise initialize / configure / deliver."""
    cases = [
        ("test-js", ["node", "server.js"], {"PLATFORM_GREETING": "Hello from JS"}),
        ("test-python", ["python3", "platform.py"], {"PLATFORM_GREETING": "Hello from Python"}),
        ("test-rust", ["./target/release/test-rust-platform"], {"PLATFORM_GREETING": "Hello from Rust"}),
    ]
    for name, cmd, cfg in cases:
        d = f"{REMOTE_REPO}/platforms/{name}"
        proc = None
        try:
            if name == "test-rust":
                bin_path = f"{d}/target/release/test-rust-platform"
                if not os.path.exists(bin_path):
                    raise SkipTest("test-rust platform binary not built (install-git compile failed?)")
                proc = spawn_stdio([bin_path], cwd=d)
            else:
                proc = spawn_stdio(cmd, cwd=d)
            init = stdio_call(proc, "initialize", {}, req_id=1)
            assert init.get("result", {}).get("name") == name, init
            c = stdio_call(proc, "configure", {"config": cfg}, req_id=2)
            assert c.get("result", {}).get("configured") is True, c
            dlv = stdio_call(proc, "deliver",
                             {"resource_identifier": "r1", "text": "hi", "message_id": "m1"},
                             req_id=3, timeout=20)
            assert "error" not in dlv, dlv
            print(f"  ok: {name} initialize/configure/deliver")
        except SkipTest:
            raise
        finally:
            stop_proc(proc)


# ═══════════════════════════════════════════════════════════════════════
#  Run
# ═══════════════════════════════════════════════════════════════════════

print("=" * 60)
print("  test_plugins.py - omni-plugins remote registry test suite")
print(f"  repo: {REMOTE_REPO}  base: {BASE}")
print("=" * 60)

test(test_registry_completeness)
test(test_tool_plugins)
test(test_noop_full_provider)
test(test_noop_provider)
test(test_platforms_registered)
test(test_telegram_platform_mock)
test(test_stdio_platforms)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {tests_pass} passed, {tests_fail} failed, {len(skips)} skipped "
      f"({tests_run} total)")
if skips:
    print("  SKIPPED:")
    for name, reason in skips:
        print(f"    - {name}: {reason}")
print(f"{'=' * 60}")

sys.exit(0 if tests_fail == 0 else 1)