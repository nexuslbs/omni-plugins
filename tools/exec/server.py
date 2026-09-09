#!/usr/bin/env python3
"""exec MCP server : sandboxed code/shell execution (R3).

Tools:
  - exec_run    : propose a shell/code command for sandboxed execution. NEVER
                  executes: creates a pending approval request and returns its
                  id. Nothing runs until an operator approves the request.
  - exec_approve: approve a pending request (request_id + operator-held
                  approval key) and execute the STORED command in the sandbox,
                  returning its output (cap + spill discipline).

Sandbox (see docs/r3-sandboxed-exec-design.md and sandbox.py): the approved
command runs in a THROWAWAY docker container with --network none, as
uid/gid 65534 with all capabilities dropped, read-only rootfs, cpu/mem/pids
bounds and a minimal explicit environment. It cannot reach production
containers, the production DB or any network by construction, and its
environment never contains the approval key or any other secret.

Approval gate:
  - Default configuration is INERT: without EXEC_APPROVAL_KEY (or with
    EXEC_APPROVAL_MODE=deny) exec_run refuses and nothing can be approved.
  - Each execution is approved exactly once: exec_run stores the exact
    command; exec_approve validates a constant-time key match against the
    configured key, then executes the STORED command. Requests expire after
    EXEC_APPROVAL_TTL_SECS (default 600). Audit events (request created /
    approved / denied / executed) are appended to <state>/audit.jsonl and
    logged to stderr; only sha256 digests of commands and keys are stored.

Configuration env vars (plugin.json config_schema keys; $secret:NAME refs are
resolved by the core before the server starts):

  EXEC_APPROVAL_MODE      require (default) | deny
  EXEC_APPROVAL_KEY       operator secret (empty => plugin inert)
  EXEC_APPROVAL_TTL_SECS  request validity window (default 600, clamp 60..3600)
  EXEC_IMAGE              pinned sandbox image (empty => execution denied)
  EXEC_TIMEOUT_SECS       default run timeout (default 30, clamp 1..300)
  EXEC_INLINE_MAX_CHARS   inline cap (default 8000, clamp 1000..50000)
  EXEC_OUTPUT_HARD_CAP    captured-bytes cap before kill (default 1048576)
  EXEC_SPILL_DIR          spill + request-state dir (default: system tmp)
  EXEC_CPUS / EXEC_MEM / EXEC_PIDS   container resource bounds

MCP JSON-RPC over stdio. Python stdlib only (no pip dependencies),
mirroring tools/web/server.py.
"""

import json
import logging
import os
import sys
import tempfile
import time

import sandbox  # sibling module: RequestStore, DockerRunner, helpers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [exec] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("exec-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"


# --------------------------------------------------------------------------
# config (env vars resolved by the core from config_schema keys)
# --------------------------------------------------------------------------

def cfg(key, default=""):
    val = (os.environ.get(key) or "").strip()
    return val if val else default


def cfg_int(key, default, lo, hi):
    try:
        value = int(cfg(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


MODE = cfg("EXEC_APPROVAL_MODE", "require")
if MODE not in ("require", "deny"):
    MODE = "require"
APPROVAL_KEY = cfg("EXEC_APPROVAL_KEY")           # empty => inert
APPROVAL_TTL_SECS = cfg_int("EXEC_APPROVAL_TTL_SECS", 600, 60, 3600)
IMAGE = cfg("EXEC_IMAGE")                          # empty => execution denied
DEFAULT_TIMEOUT_SECS = cfg_int("EXEC_TIMEOUT_SECS", 30, 1, 300)
INLINE_MAX_CHARS = cfg_int("EXEC_INLINE_MAX_CHARS", 8000, 1000, 50000)
OUTPUT_HARD_CAP = cfg_int("EXEC_OUTPUT_HARD_CAP", 1048576, 65536, 16777216)
SPILL_DIR = cfg("EXEC_SPILL_DIR")
CPUS = cfg("EXEC_CPUS", "1")
MEM = cfg("EXEC_MEM", "256m")
PIDS = cfg("EXEC_PIDS", "64")

if not SPILL_DIR:
    SPILL_DIR = os.path.join(tempfile.gettempdir(), "omni-exec")
STATE_DIR = SPILL_DIR

_store = None
_runner = None


def store():
    global _store
    if _store is None:
        _store = sandbox.RequestStore(STATE_DIR)
    return _store


def runner():
    global _runner
    if _runner is None:
        _runner = sandbox.DockerRunner(image=IMAGE, cpus=CPUS, mem=MEM,
                                       pids=PIDS)
    return _runner


# --------------------------------------------------------------------------
# JSON-RPC / MCP protocol helpers
# --------------------------------------------------------------------------

def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": str(text)}], "isError": bool(is_error)}


# --------------------------------------------------------------------------
# tool handlers
# --------------------------------------------------------------------------

def _deny_text():
    return sandbox.deny_reason(MODE, bool(APPROVAL_KEY)) or \
        "execution is disabled in this deployment"


def handle_exec_run(args):
    command = args.get("command")
    if command is None:
        return make_tool_result(
            "exec_run error: 'command' is required (a shell command string "
            "to run inside the sandbox)", True)
    command = str(command).strip()
    if not command:
        return make_tool_result("exec_run error: 'command' must not be empty", True)

    timeout_secs = DEFAULT_TIMEOUT_SECS
    raw_timeout = args.get("timeout_secs")
    if raw_timeout not in (None, ""):
        try:
            timeout_secs = int(raw_timeout)
        except (TypeError, ValueError):
            timeout_secs = DEFAULT_TIMEOUT_SECS
    timeout_secs = max(1, min(timeout_secs, 300))

    if MODE != "require" or not APPROVAL_KEY:
        return make_tool_result(
            "[exec] %s NOTHING was executed. An operator must configure "
            "EXEC_APPROVAL_MODE=require and EXEC_APPROVAL_KEY (and, to run, "
            "EXEC_IMAGE) for this plugin to execute anything." % _deny_text())

    try:
        record = store().create(command, timeout_secs, APPROVAL_TTL_SECS)
    except sandbox.ExecError as e:
        return make_tool_result("exec_run error: %s" % e, True)

    sha = record["sha256"][:16]
    rid = record["id"]
    log.info("exec_run request %s sha256=%s ttl=%ss", rid, sha,
             record["ttl_secs"])
    return make_tool_result(
        "[exec] approval required: request id=%s\n"
        "  command sha256 (prefix) = %s\n"
        "  timeout = %ss, request expires in %ss\n"
        "NOTHING was executed. The exact command above is stored under this "
        "request id. An operator must approve THIS request before it runs:\n"
        "  exec_approve(request_id=%r, approval_key=<the configured key>)\n"
        "Pending requests expire automatically; denied or expired requests "
        "never execute." % (rid, sha, record["timeout_secs"],
                            record["ttl_secs"], rid))


def _spill_output(command, text):
    digest = sandbox.sha256_hex(command)[:10]
    path = os.path.join(SPILL_DIR, "exec_%s_%s.out" % (digest, int(time.time())))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _format_run_result(record, result):
    sha = record["sha256"][:16]
    rid = record["id"]
    header = ["# exec sandbox run",
              "request id: %s" % rid,
              "command sha256 (prefix): %s" % sha,
              "exit code: %s" % (result.exit_code if result.exit_code is not None
                                 else "n/a"),
              "duration: %.2fs" % result.duration_secs,
              "container: %s" % result.container,
              ""]
    if result.error:
        header.append("run error: %s" % result.error)
        header.append("")
    header_len = sum(len(s) + 1 for s in header)
    output = result.output or ""

    if header_len + len(output) <= INLINE_MAX_CHARS:
        return make_tool_result("\n".join(header) + "\n" + output)

    # Output discipline: cap + spill. Full output goes to a file whose path
    # is reported first so it can never be cut by the inline cap.
    total = len(output)
    try:
        spill_path = _spill_output(record["command"], output)
    except Exception as e:
        log.exception("exec spill failed")
        return make_tool_result(
            "exec error: output is %d chars (inline cap %d) and the spill "
            "file could not be written: %s" % (total, INLINE_MAX_CHARS, e), True)
    note = ("[truncated: output is %d chars; inline capped at %d. FULL "
            "output spilled to file: %s (read it with filesystem_read)]"
            % (total, INLINE_MAX_CHARS, spill_path))
    budget = INLINE_MAX_CHARS - header_len - len(note) - 1
    if budget > 0:
        return make_tool_result(
            "\n".join(header) + "\n" + note + "\n\n" + output[:budget] +
            "\n\n[... preview truncated; see the spilled file for the full "
            "output]")
    return make_tool_result("\n".join(header) + "\n" + note)


def handle_exec_approve(args):
    request_id = str(args.get("request_id") or "").strip()
    approval_key = args.get("approval_key")
    if not request_id or approval_key is None:
        return make_tool_result(
            "exec_approve error: 'request_id' and 'approval_key' are both "
            "required", True)
    approval_key = str(approval_key)

    if MODE != "require" or not APPROVAL_KEY:
        return make_tool_result(
            "[exec] %s NOTHING was executed. An operator must configure "
            "EXEC_APPROVAL_MODE=require and EXEC_APPROVAL_KEY for approvals "
            "to work." % _deny_text())

    st = store()
    record = st.get(request_id)
    if record is None:
        st.audit({"event": "denied", "request_id": request_id,
                  "reason": "unknown_request"})
        return make_tool_result(
            "[exec] approval denied: unknown request id %r. NOTHING was "
            "executed." % request_id)
    if record.get("status") != sandbox.STATUS_PENDING:
        return make_tool_result(
            "[exec] request %s is already %s (each request is single-use). "
            "NOTHING was executed." % (request_id, record.get("status")))
    if st.expired(record):
        st.audit({"event": "denied", "request_id": request_id,
                  "reason": "expired", "sha256": record["sha256"]})
        return make_tool_result(
            "[exec] approval denied: request %s has expired. Create a new "
            "request with exec_run. NOTHING was executed." % request_id)

    if not sandbox.approval_key_matches(APPROVAL_KEY, approval_key):
        st.audit({"event": "denied", "request_id": request_id,
                  "reason": "bad_key", "sha256": record["sha256"],
                  "key_sha256": sandbox.sha256_hex(approval_key)})
        log.warning("exec_approve DENIED for %s (bad key)", request_id)
        return make_tool_result(
            "[exec] approval denied: invalid approval key for request %s. "
            "NOTHING was executed." % request_id)

    st.mark_approved(request_id, sandbox.sha256_hex(APPROVAL_KEY))
    st.audit({"event": "approved", "request_id": request_id,
              "sha256": record["sha256"],
              "key_sha256": sandbox.sha256_hex(APPROVAL_KEY)})

    timeout_secs = int(record.get("timeout_secs") or DEFAULT_TIMEOUT_SECS)
    try:
        result = runner().run(record["command"], timeout_secs, OUTPUT_HARD_CAP)
    except sandbox.ExecError as e:
        outcome = {"error": str(e)}
        st.mark_executed(request_id, outcome)
        st.audit({"event": "executed", "request_id": request_id,
                  "sha256": record["sha256"], "error": str(e)})
        return make_tool_result("[exec] sandbox failed to start: %s. "
                                "NOTHING was executed." % e, True)

    outcome = result.to_dict()
    st.mark_executed(request_id, outcome)
    st.audit({"event": "executed", "request_id": request_id,
              "sha256": record["sha256"],
              "exit_code": result.exit_code,
              "timed_out": result.timed_out,
              "overflow": result.overflow,
              "duration_secs": round(result.duration_secs, 2),
              "output_bytes": len(result.output.encode("utf-8"))})
    log.info("exec_approve %s exit=%s dur=%.2fs", request_id,
             result.exit_code, result.duration_secs)
    return _format_run_result(record, result)


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "exec_run",
        "description": (
            "Propose a shell/code command for sandboxed execution. This "
            "NEVER executes: it records the exact command as a pending "
            "approval request and returns the request id. An operator must "
            "then approve it with exec_approve (request_id + the configured "
            "approval key); only the exact stored command runs, inside a "
            "throwaway, network-isolated sandbox container (uid nobody, all "
            "capabilities dropped, read-only rootfs, cpu/mem/pids bounds). "
            "Requests expire automatically (TTL). Without a configured "
            "approval key this tool is inert and refuses. Use for bounded "
            "code/shell snippets that must not touch any network or "
            "production resource."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "Shell command to run inside the sandbox (executed with /bin/sh -c). Max 4000 chars."},
                "timeout_secs": {"type": "integer",
                                 "description": "Run timeout in seconds (1-300, default configured EXEC_TIMEOUT_SECS)."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "exec_approve",
        "description": (
            "Approve a pending exec_run request and execute the STORED "
            "command in the sandbox. Requires the request_id returned by "
            "exec_run and the operator-held approval key (EXEC_APPROVAL_KEY) "
            "configured for this plugin. The approval is single-use and "
            "bound to the exact stored command; requests expire after their "
            "TTL. Returns the sandboxed run output (exit code, duration), "
            "capped inline with full output spilled to a file when large."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string",
                               "description": "Request id returned by exec_run (e.g. R-123-abc123)."},
                "approval_key": {"type": "string",
                                 "description": "The operator-held approval key configured as EXEC_APPROVAL_KEY. Never guess: ask an operator."},
            },
            "required": ["request_id", "approval_key"],
        },
    },
]

HANDLERS = {
    "exec_run": handle_exec_run,
    "exec_approve": handle_exec_approve,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "exec", "version": "0.1.0"},
    })


def handle_tools_call(msg):
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in HANDLERS:
        send_json(make_error(rid, -32601, "Unknown tool: %s" % name))
        return
    try:
        result = HANDLERS[name](args)
    except Exception as e:
        log.exception("tool %s crashed", name)
        result = make_tool_result("%s error: %s" % (name, e), True)
    send_json(make_success(rid, result))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            send_json(handle_initialize(msg))
        elif method == "notifications/initialized" or (method or "").startswith("notifications/"):
            continue
        elif method == "tools/list":
            send_json(make_success(rid, {"tools": TOOLS}))
        elif method == "tools/call":
            handle_tools_call(msg)
        elif method == "ping":
            send_json(make_success(rid, {}))
        else:
            send_json(make_error(rid, -32601, "Method not found: %s" % method))


if __name__ == "__main__":
    main()
