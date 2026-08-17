#!/usr/bin/env python3
"""hindsight MCP server : Python equivalent of the removed Rust hindsight plugin.

Tools:
  - hindsight_recall: search hindsight persistent memory for relevant memories
  - hindsight_retain: store a memory in hindsight persistent memory
  - hindsight_reflect: ask hindsight to synthesize an answer across memories

MCP JSON-RPC over stdio (mirrors tools/actions/server.py). Configuration is
received from the omniagent via the `configure` message at startup (the
omniagent passes the plugin config block from plugins.yml). The Rust original
read config only via configure; this python port accepts BOTH the configure
message and env-var fallbacks (HINDSIGHT_URL / HINDSIGHT_BANK / ...) so it
works standalone too. Requires only the python standard library (urllib).
"""

import json
import os
import sys
import logging
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hindsight-python] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hindsight-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"

# --------------------------------------------------------------------------
# Config — defaulted from env, overridable via the `configure` message.
# --------------------------------------------------------------------------

DEFAULTS = {
    "url": os.environ.get("HINDSIGHT_URL", "http://hindsight:8888"),
    "bank_id": os.environ.get("HINDSIGHT_BANK", "omniagent"),
    "limit": int(os.environ.get("HINDSIGHT_LIMIT", "5")),
    "budget": os.environ.get("HINDSIGHT_BUDGET", "low"),
    "tags": os.environ.get("HINDSIGHT_TAGS", "from_user"),
    "tags_match": os.environ.get("HINDSIGHT_TAGS_MATCH", "any"),
    "types": os.environ.get("HINDSIGHT_TYPES", "world"),
    "timeout_secs": int(os.environ.get("HINDSIGHT_TIMEOUT", "15")),
}

CONFIG = dict(DEFAULTS)


def apply_configure(params):
    """Apply a `configure` message payload to the running config."""
    if not isinstance(params, dict):
        return
    cfg = dict(CONFIG)
    cfg["url"] = str(params.get("hindsight_url", cfg["url"]))
    cfg["bank_id"] = str(params.get("hindsight_bank", cfg["bank_id"]))
    if params.get("hindsight_limit") is not None:
        cfg["limit"] = int(params["hindsight_limit"])
    cfg["budget"] = str(params.get("hindsight_budget", cfg["budget"]))
    cfg["tags"] = str(params.get("hindsight_tags", cfg["tags"]))
    cfg["tags_match"] = str(params.get("hindsight_tags_match", cfg["tags_match"]))
    cfg["types"] = str(params.get("hindsight_types", cfg["types"]))
    if params.get("hindsight_timeout") is not None:
        cfg["timeout_secs"] = int(params["hindsight_timeout"])
    CONFIG.update(cfg)


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
# HTTP helper (stdlib only)
# --------------------------------------------------------------------------

def _post(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body
    except urllib.error.URLError as e:
        return 0, f"hindsight request failed: {e.reason}"


def _recall_url(cfg):
    return f"{cfg['url'].rstrip('/')}/v1/default/banks/{cfg['bank_id']}/memories/recall"


def _retain_url(cfg):
    return f"{cfg['url'].rstrip('/')}/v1/default/banks/{cfg['bank_id']}/memories"


def _reflect_url(cfg):
    return f"{cfg['url'].rstrip('/')}/v1/default/banks/{cfg['bank_id']}/reflect"


def _parse_csv(s):
    if not s:
        return None
    items = [x.strip() for x in s.split(",") if x.strip()]
    return items or None


# --------------------------------------------------------------------------
# tool handlers
# --------------------------------------------------------------------------

def handle_recall(args, meta):
    query = args.get("query")
    if not query:
        return make_tool_result("Missing required argument: 'query'", True)
    cfg = dict(CONFIG)
    payload = {"query": query}
    payload["limit"] = args.get("limit", cfg["limit"])
    payload["budget"] = args.get("budget", cfg["budget"])
    tags = _parse_csv(str(args.get("tags", ""))) or _parse_csv(cfg["tags"])
    if tags:
        payload["tags"] = tags
        payload["tags_match"] = args.get("tags_match", cfg["tags_match"])
    types = _parse_csv(str(args.get("types", ""))) or _parse_csv(cfg["types"])
    if types:
        payload["types"] = types
    status, body = _post(_recall_url(cfg), payload, cfg["timeout_secs"])
    if status == 0:
        return make_tool_result(body, True)
    if status >= 400:
        return make_tool_result(f"Hindsight returned HTTP {status}: {body}", True)
    try:
        data = json.loads(body)
    except ValueError:
        return make_tool_result(f"Failed to parse hindsight response: {body}", True)
    memories = data.get("results") or []
    if not memories:
        return make_tool_result("No relevant memories found.")
    lines = []
    for m in memories:
        text = m.get("text", "")
        tags_str = ", ".join(str(t) for t in (m.get("tags") or []))
        lines.append(f"[{tags_str}] {text}")
    return make_tool_result(
        f"## Hindsight Memories ({len(memories)} results):\n\n" + "\n---\n".join(lines))


def handle_retain(args, meta):
    content = args.get("content")
    if not content:
        return make_tool_result("Missing required argument: 'content'", True)
    cfg = dict(CONFIG)
    context = args.get("context", "memory retention")
    item = {"content": content, "context": context, "strategy": "fast"}
    tags = _parse_csv(str(args.get("tags", "")))
    if tags:
        item["tags"] = tags
    if args.get("document_id"):
        item["document_id"] = args["document_id"]
    payload = {"items": [item], "async": False}
    status, body = _post(_retain_url(cfg), payload, cfg["timeout_secs"])
    if status == 0:
        return make_tool_result(body, True)
    if status >= 400:
        return make_tool_result(f"Retain returned HTTP {status}: {body}", True)
    return make_tool_result("Memory retained successfully.")


def handle_reflect(args, meta):
    query = args.get("query")
    if not query:
        return make_tool_result("Missing required argument: 'query'", True)
    cfg = dict(CONFIG)
    payload = {"query": query, "budget": cfg["budget"]}
    status, body = _post(_reflect_url(cfg), payload, cfg["timeout_secs"])
    if status == 0:
        return make_tool_result(body, True)
    if status >= 400:
        return make_tool_result(f"Reflect returned HTTP {status}: {body}", True)
    try:
        data = json.loads(body)
        text = data.get("text", "No reflection")
    except ValueError:
        text = body
    return make_tool_result(f"## Hindsight Reflection:\n\n{text}")


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "hindsight_recall",
        "description": "Search hindsight persistent memory for relevant past memories. "
                       "Returns text passages ranked by relevance to the query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query to find relevant memories"},
                "limit": {"type": "integer", "description": "Max results to return (default: from config)"},
                "budget": {"type": "string", "enum": ["low", "mid", "high"],
                           "description": "Recall budget: low is fastest, high is most thorough"},
                "tags": {"type": "string", "description": "Comma-separated tags to filter"},
                "tags_match": {"type": "string", "enum": ["any", "all", "any_strict", "all_strict"],
                               "description": "Tag matching mode"},
                "types": {"type": "string", "description": "Comma-separated fact types"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "hindsight_retain",
        "description": "Store a memory in hindsight persistent memory. Use for important facts, "
                       "decisions, and user preferences that should be remembered across sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory content to store"},
                "context": {"type": "string", "description": "Context label for the memory"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "document_id": {"type": "string", "description": "Optional document ID for deduplication"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "hindsight_reflect",
        "description": "Ask hindsight to synthesize an answer by reasoning across all stored "
                       "memories. Use when you need a synthesized answer rather than raw recall results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question or topic to reflect on"},
            },
            "required": ["query"],
        },
    },
]

HANDLERS = {
    "hindsight_recall": handle_recall,
    "hindsight_retain": handle_retain,
    "hindsight_reflect": handle_reflect,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "hindsight-python", "version": "0.1.0"},
    })


def handle_configure(msg):
    params = msg.get("params") or {}
    if isinstance(params, dict) and "arguments" in params:
        params = params["arguments"]
    apply_configure(params)
    return make_success(msg.get("id"), {"configured": True})


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
        elif method == "configure":
            send_json(handle_configure(msg))
        elif method == "ping":
            send_json(make_success(rid, {}))
        else:
            send_json(make_error(rid, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
