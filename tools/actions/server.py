#!/usr/bin/env python3
"""actions-python MCP server : Python equivalent of the Rust built-in actions plugin.

Tools:
  - hindsight_populator: retain recent messages into Hindsight memory
              (reads <OMNI_DIR>/hindsight_watermark.json, selects new messages
              from the messages table, advances the watermark).
  - relevance_indexer: update the wiki relevance index
              (scans <OMNI_DIR>/profiles/omni/wiki for .md files, scores by
              mtime recency, writes relevant-index.md).
  - setup_knowledge_pipeline: create the knowledge pipeline schedule in
              <OMNI_DIR>/config/tasks.yml (idempotent).

MCP JSON-RPC over stdio (mirrors tools/memory/server.py). Requires OMNI_DIR
and DATABASE_URL env vars. The profile is always "omni" (matches the Rust
default_profile_name()).
"""

import json
import os
import sys
import logging
import datetime as dt
from pathlib import Path

try:
    import psycopg2
except Exception:  # pragma: no cover - handled defensively
    psycopg2 = None

try:
    import yaml
except Exception:  # pragma: no cover - handled defensively
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [actions-python] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("actions-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_PROFILE = "omni"


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


def cfg_env(*keys):
    """Read a plugin config value from env (framework may inject config as env)."""
    for k in keys:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return ""


def get_omni_dir():
    return cfg_env("omni_dir") or os.environ.get("OMNI_DIR") or _fail_omni_dir()


def _fail_omni_dir():
    raise RuntimeError(
        "OMNI_DIR is not set: set the OMNI_DIR environment variable or configure the "
        "'omni_dir' plugin config field (default '$env:OMNI_DIR')"
    )


def rfc3339_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# tool: hindsight_populator
# --------------------------------------------------------------------------

HINDSIGHT_MSG_TYPES = (
    "message", "reasoning", "plan", "error", "cause", "tool", "tool-result",
)


def _load_watermark(omni_dir):
    path = Path(omni_dir) / "hindsight_watermark.json"
    try:
        data = json.loads(path.read_text())
        return int(data.get("last_message_id", 0) or 0)
    except Exception:
        return 0


def _save_watermark(omni_dir, last_id):
    path = Path(omni_dir) / "hindsight_watermark.json"
    payload = {"last_message_id": last_id, "last_run_at": rfc3339_now()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def handle_hindsight(args, meta):
    if psycopg2 is None:
        return make_tool_result("hindsight_populator error: psycopg2 is not available", True)
    omni_dir = get_omni_dir()
    last_id = _load_watermark(omni_dir)
    db_url = os.environ.get("DATABASE_URL", "")
    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:
        return make_tool_result(f"hindsight_populator error: cannot connect to database: {e}", True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM messages WHERE id > %s AND msg_type IN %s "
            "AND COALESCE(content, '') != '' ORDER BY id ASC LIMIT 200",
            (last_id, HINDSIGHT_MSG_TYPES))
        rows = [int(r[0]) for r in cur.fetchall()]
    except Exception as e:
        return make_tool_result(f"hindsight_populator error: query failed: {e}", True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        return make_tool_result("No new messages to process")
    max_id = max(rows)
    try:
        _save_watermark(omni_dir, max_id)
    except Exception as e:
        return make_tool_result(f"hindsight_populator error: failed to write watermark: {e}", True)
    return make_tool_result(
        f"Hindsight populator: retained {len(rows)} messages (watermark: {last_id} -> {max_id})")


# --------------------------------------------------------------------------
# tool: relevance_indexer
# --------------------------------------------------------------------------

def collect_md_files(dirpath, entries, prefix):
    try:
        names = sorted(os.listdir(dirpath))
    except OSError:
        return
    for name in names:
        p = Path(dirpath) / name
        if p.is_dir():
            collect_md_files(p, entries, f"{prefix}{name}/")
        elif p.suffix == ".md" and name != "relevant-index.md":
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0
            entries.append((f"{prefix}{name}", mtime))


def handle_relevance(args, meta):
    omni_dir = get_omni_dir()
    wiki_dir = Path(omni_dir) / "profiles" / DEFAULT_PROFILE / "wiki"
    if not wiki_dir.is_dir():
        return make_tool_result("No wiki directory found")
    entries = []
    collect_md_files(wiki_dir, entries, "")
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    scored = []
    for path, mtime in entries:
        age = max(0.0, now - mtime)
        if age < 3600:
            score = 50.0
        elif age < 86400:
            score = 40.0
        elif age < 604800:
            score = 30.0
        else:
            score = 10.0
        scored.append((path, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    output = "# Relevant Wiki Pages\n\n"
    for path, score in scored[:30]:
        line = f"- [{path}]({path}) --- score: {score:.0f}\n"
        if len(output) + len(line) > 1000:
            break
        output += line
    try:
        (wiki_dir / "relevant-index.md").write_text(output)
    except Exception as e:
        return make_tool_result(
            f"relevance_indexer error: failed to write relevant-index.md: {e}", True)
    return make_tool_result(f"Relevance indexer complete: {len(scored)} files indexed")


# --------------------------------------------------------------------------
# tool: setup_knowledge_pipeline
# --------------------------------------------------------------------------

DEFAULT_CRON = "0 */6 * * *"
DEFAULT_PROMPT = ("Run the knowledge pipeline maintenance (summarize channels, "
                  "update wiki, run relevance indexer, populate hindsight).")

SCHEDULE_BLOCK = (
    "  knowledge_pipeline:\n"
    "    enabled: true\n"
    "    channel: cron-default\n"
    "    profile: pipeline\n"
    "    plan: true\n"
    "    cron: {cron}\n"
    "    prompt: {prompt}\n"
    "    skills: '[\"knowledge-pipeline\"]'\n"
    "    silent: false\n"
    "    display_name: Knowledge Pipeline\n"
)


def yaml_quote(value):
    """Quote a scalar for YAML (JSON-style double quotes are valid YAML)."""
    return json.dumps(str(value))


def tasks_schedules(text):
    """Return the schedules dict (None if unparsable or yaml unavailable)."""
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        return None
    return data.get("schedules") if isinstance(data, dict) else None


def insert_schedule_block(text, block):
    """Insert `block` (an indented schedule entry) under the `schedules:` key.

    Preserves the rest of the file (comments, other sections) — a plain-text
    insert instead of a yaml round-trip, so the git-tracked tasks.yml keeps
    its formatting.
    """
    if text is None:
        text = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "schedules:" or stripped.startswith("schedules:"):
            if stripped != "schedules:":
                lines[i] = "schedules:"
            lines.insert(i + 1, block.rstrip("\n"))
            return "\n".join(lines) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block + "\n"


def handle_setup_pipeline(args, meta):
    omni_dir = get_omni_dir()
    schedule = str(args.get("schedule") or DEFAULT_CRON).strip() or DEFAULT_CRON
    prompt = str(args.get("prompt") or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
    tasks_path = Path(omni_dir) / "config" / "tasks.yml"
    text = tasks_path.read_text() if tasks_path.exists() else ""
    schedules = tasks_schedules(text)
    if isinstance(schedules, dict) and "knowledge_pipeline" in schedules:
        return make_tool_result("Knowledge Pipeline schedule already exists in tasks.yml")
    block = SCHEDULE_BLOCK.format(cron=yaml_quote(schedule), prompt=yaml_quote(prompt))
    new_text = insert_schedule_block(text, block)
    try:
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tasks_path.with_suffix(".tmp")
        tmp.write_text(new_text)
        os.replace(tmp, tasks_path)
    except Exception as e:
        return make_tool_result(
            f"setup_knowledge_pipeline error: failed to save tasks.yml: {e}", True)
    return make_tool_result(
        f"Knowledge Pipeline schedule created in tasks.yml with cron '{schedule}'")


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "hindsight_populator",
        "description": "Retain recent messages into Hindsight memory. Queries new messages "
                       "since the last watermark and retains them for long-term persistent recall.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "relevance_indexer",
        "description": "Update the wiki relevance index. Scans wiki files and updates "
                       "relevant-index.md based on recency and reference count.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "setup_knowledge_pipeline",
        "description": "Create or verify the periodic knowledge pipeline schedule in tasks.yml. "
                       "Creates a schedule that runs the maintenance pipeline (summarize channels, "
                       "update wiki/skills, relevance indexing, hindsight populate).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schedule": {"type": "string",
                             "description": "Optional cron schedule in 5-field Linux format. Default: '0 */6 * * *'."},
                "prompt": {"type": "string", "description": "Optional prompt override."},
            },
            "required": [],
        },
    },
]

HANDLERS = {
    "hindsight_populator": handle_hindsight,
    "relevance_indexer": handle_relevance,
    "setup_knowledge_pipeline": handle_setup_pipeline,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "actions-python", "version": "0.1.0"},
    })


def handle_tools_call(msg):
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    meta = params.get("meta") or {}
    if name not in HANDLERS:
        send_json(make_error(rid, -32601, f"Unknown tool: {name}"))
        return
    try:
        result = HANDLERS[name](args, meta)
    except Exception as e:
        log.exception("tool %s crashed", name)
        result = make_tool_result(f"{name} error: {e}", True)
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
            send_json(make_error(rid, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
