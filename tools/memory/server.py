#!/usr/bin/env python3
"""memory-python MCP server : Python equivalent of the Rust memory plugin.

Tools:
  - promote_to_memory: promote a validated fact to long-term memory by
             writing <OMNI_DIR>/profiles/<profile>/wiki/Memory/Promoted/<name>.md
             with frontmatter (type, confidence, source_message_ids,
             source_tool_outputs, last_verified_at, created_at, expires_at).
  - list_memories: list promoted memories (filenames, titles, confidence,
             expiry dates), optionally including expired ones.
  - review_memories: expiry report for promoted memories (expired / expiring
             soon / valid).
  - manage_memory: add/remove/clean entries in a profile's MEMORY.md / USER.md.
  - save_summary: persist a channel summary as a NEW row in the summaries
             table (channel_id = channel NAME, next_thread_id = watermark,
             content = the summary markdown). The caller produces the text
             (the agent/LLM); this tool only stores it.

MCP JSON-RPC over stdio (mirrors tools/prompt/server.py). Requires OMNI_DIR
and DATABASE_URL env vars. The profile comes from meta.profile_name; when it is
missing/empty the plugin falls back ONLY to its explicitly configured
`default_profile` (plugin config / env) and otherwise REFUSES the call - it
never invents a profile name. The old literal "default" fallback silently
created <OMNI_DIR>/profiles/default, a directory no profile declares and no
prompt ever reads (recurring incident, telegram thread 2719).
"""

import json
import os
import re
import sys
import logging
import datetime as dt
from pathlib import Path

try:
    import psycopg2
except Exception:  # pragma: no cover - handled defensively in save_summary
    psycopg2 = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [memory-python] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("memory-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
CONFIDENCE_VALUES = ("high", "medium", "low")


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
# helpers
# --------------------------------------------------------------------------

def iso_timestamp(d=None):
    d = d or dt.datetime.now(dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def sanitize_filename(name):
    """Keep alphanumeric/hyphen/underscore, replace everything else with '_'."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name)) or "memory"


def get_omni_dir():
    return cfg_env("omni_dir") or os.environ.get("OMNI_DIR") or _fail_omni_dir()


def _fail_omni_dir():
    raise RuntimeError(
        "OMNI_DIR is not set: set the OMNI_DIR environment variable or configure the "
        "'omni_dir' plugin config field (default '$env:OMNI_DIR')"
    )


class ProfileNameMissing(RuntimeError):
    """Raised when a tool call carries no usable (declared) profile name."""


def configured_default_profile():
    """The EXPLICITLY configured default profile for this plugin, or "".

    Resolved from the plugin config / env (`default_profile`). There is
    deliberately NO hardcoded literal fallback: inventing a name made the
    plugin write into <OMNI_DIR>/profiles/default (telegram thread 2719).
    """
    return cfg_env("default_profile", "DEFAULT_PROFILE", "OMNI_DEFAULT_PROFILE")


def declared_profiles(omni_dir):
    """Profile names declared in <OMNI_DIR>/config/profiles.yml.

    Best-effort minimal YAML read (keys directly under the top-level
    `profiles:` mapping). Returns a set of names, or None when the file is
    absent/unreadable/empty - then the declaration guard is skipped instead of
    blocking legitimate work.
    """
    try:
        text = (Path(omni_dir) / "config" / "profiles.yml").read_text()
    except (OSError, TypeError):
        return None
    names = set()
    in_profiles = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_profiles:
            if re.match(r"^profiles\s*:", line):
                in_profiles = True
            continue
        if len(line) - len(line.lstrip(" ")) == 0:
            break  # end of the top-level profiles mapping
        m = re.match(r"^  ([A-Za-z0-9_.-]+)\s*:", line)
        if m:
            names.add(m.group(1))
    return names or None


def get_profile(meta):
    """Resolve the profile that owns the path this call touches.

    NEVER invents a name: meta.profile_name wins when non-empty, otherwise the
    explicitly configured `default_profile` is used, otherwise the call fails
    with ProfileNameMissing (surfaced as a tool error). A name that no declared
    profile owns is refused too - that is what used to create
    <OMNI_DIR>/profiles/default.
    """
    name = ""
    if isinstance(meta, dict):
        raw = meta.get("profile_name")
        if raw is not None and str(raw).strip():
            name = str(raw).strip()
    if name:
        source = "_meta.profile_name"
    else:
        name = configured_default_profile()
        source = "configured default_profile"
        if name:
            log.warning(
                "tool context carried no profile_name; using the configured "
                "default_profile %r", name)
    if not name:
        raise ProfileNameMissing(
            "profile_name missing in tool context; refusing to write. Fix the "
            "caller to send _meta.profile_name (or configure the memory "
            "plugin's 'default_profile'): this plugin never invents a profile "
            "name such as 'default'."
        )
    declared = declared_profiles(get_omni_dir())
    if declared is not None and name not in declared:
        raise ProfileNameMissing(
            f"profile '{name}' (from {source}) is not declared in "
            "<OMNI_DIR>/config/profiles.yml; refusing to create or touch "
            f"profiles/{name}/"
        )
    return name


def promoted_dir(profile):
    return Path(get_omni_dir()) / "profiles" / profile / "wiki" / "Memory" / "Promoted"


def memories_file(profile, target):
    """Profile-root MEMORY.md / USER.md - the file the prompt loader reads."""
    fname = "MEMORY.md" if target == "memory" else "USER.md"
    return Path(get_omni_dir()) / "profiles" / profile / fname


def legacy_memories_file(profile, target):
    """Legacy <profile>/memories/<file> path (read-only back-compat)."""
    fname = "MEMORY.md" if target == "memory" else "USER.md"
    return Path(get_omni_dir()) / "profiles" / profile / "memories" / fname


def read_memory_text(profile, target):
    """Current MEMORY.md/USER.md text, migrating a legacy memories/ file.

    The legacy `profiles/<profile>/memories/` file is read once for
    back-compat and becomes the base for the profile-root file; this plugin
    never writes to the legacy path again.
    """
    path = memories_file(profile, target)
    if path.exists():
        return path.read_text()
    legacy = legacy_memories_file(profile, target)
    if legacy.exists():
        log.warning("migrating legacy memory file %s to %s", legacy, path)
        return legacy.read_text()
    return ""


def parse_frontmatter(text):
    """Parse YAML-ish frontmatter between leading '---' markers."""
    meta = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm, body = parts[1], parts[2]
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta, body


def cfg_env(*keys):
    """Read a plugin config value from env (framework may inject config as env)."""
    for k in keys:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return ""


# --------------------------------------------------------------------------
# tool handlers
# --------------------------------------------------------------------------

def handle_promote(args, meta):
    name = str(args.get("name", "")).strip()
    content = str(args.get("content", "")).strip()
    confidence = str(args.get("confidence", "")).strip()
    if not name:
        return make_tool_result("promote_to_memory requires 'name'", True)
    if not content:
        return make_tool_result("promote_to_memory requires 'content'", True)
    if confidence not in CONFIDENCE_VALUES:
        return make_tool_result(
            f"invalid confidence '{confidence}': must be one of {list(CONFIDENCE_VALUES)}", True)
    profile = get_profile(meta)
    d = promoted_dir(profile)
    d.mkdir(parents=True, exist_ok=True)
    filepath = d / f"{sanitize_filename(name)}.md"
    created = iso_timestamp()
    try:
        expires_in_days = int(args.get("expires_in_days", 30) or 30)
    except (TypeError, ValueError):
        expires_in_days = 30
    expires = iso_timestamp(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_in_days))
    try:
        msg_ids = [int(x) for x in (args.get("source_message_ids") or [])]
    except (TypeError, ValueError):
        msg_ids = []
    tool_outputs = [str(x) for x in (args.get("source_tool_outputs") or [])]
    frontmatter = (
        "---\n"
        f"type: memory\n"
        f"confidence: {confidence}\n"
        f"source_message_ids: {json.dumps(msg_ids)}\n"
        f"source_tool_outputs: {json.dumps(tool_outputs)}\n"
        f"last_verified_at: {created}\n"
        f"created_at: {created}\n"
        f"expires_at: {expires}\n"
        "---\n\n"
        f"{content}\n"
    )
    filepath.write_text(frontmatter)
    return make_tool_result(f"Memory promoted to {filepath} (expires: {expires})")


def handle_list(args, meta):
    include_expired = bool(args.get("include_expired", False))
    profile = get_profile(meta)
    d = promoted_dir(profile)
    if not d.exists():
        return make_tool_result("[]")
    now = dt.datetime.now(dt.timezone.utc)
    entries = []
    for f in sorted(d.glob("*.md")):
        fm, _ = parse_frontmatter(f.read_text(errors="replace"))
        title = fm.get("title") or f.stem
        confidence = fm.get("confidence", "")
        expires_at = fm.get("expires_at", "")
        exp_dt = parse_datetime(expires_at)
        if exp_dt is not None and exp_dt < now and not include_expired:
            continue
        entries.append({"filename": f.name, "title": title,
                        "confidence": confidence, "expires_at": expires_at})
    return make_tool_result(json.dumps(entries, indent=2))


def handle_review(args, meta):
    try:
        expiring_days = int(args.get("expiring_soon_days", 7) or 7)
    except (TypeError, ValueError):
        expiring_days = 7
    profile = get_profile(meta)
    d = promoted_dir(profile)
    if not d.exists():
        return make_tool_result(f"No memories found for profile '{profile}'.")
    now = dt.datetime.now(dt.timezone.utc)
    expired, expiring, valid = [], [], []
    for f in sorted(d.glob("*.md")):
        fm, _ = parse_frontmatter(f.read_text(errors="replace"))
        exp_dt = parse_datetime(fm.get("expires_at", ""))
        if exp_dt is None:
            valid.append(f.stem)
        elif exp_dt < now:
            expired.append((f.stem, fm.get("expires_at", "")))
        elif (exp_dt - now).days <= expiring_days:
            expiring.append((f.stem, fm.get("expires_at", "")))
        else:
            valid.append(f.stem)
    lines = [
        f"Memory review for profile '{profile}' (expiring-soon threshold: {expiring_days} days):",
        f"- Total: {len(expired) + len(expiring) + len(valid)}",
        f"- Expired: {len(expired)}",
        f"- Expiring soon: {len(expiring)}",
        f"- Valid: {len(valid)}",
    ]
    if expired:
        lines.append("Expired:")
        lines += [f"  - {n} (expired {e})" for n, e in expired]
    if expiring:
        lines.append("Expiring soon:")
        lines += [f"  - {n} (expires {e})" for n, e in expiring]
    if valid:
        lines.append("Valid:")
        lines += [f"  - {n}" for n in valid]
    return make_tool_result("\n".join(lines))


def handle_manage(args, meta):
    target = str(args.get("target", "")).strip()
    action = str(args.get("action", "")).strip()
    if target not in ("memory", "user"):
        return make_tool_result("manage_memory requires 'target' of 'memory' or 'user'", True)
    if action not in ("add", "remove", "clean"):
        return make_tool_result("manage_memory requires 'action' of 'add', 'remove' or 'clean'", True)
    profile = get_profile(meta)
    # Profile ROOT (profiles/<profile>/MEMORY.md), the file the prompt loader
    # reads - never the legacy profiles/<profile>/memories/ subdirectory.
    filepath = memories_file(profile, target)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if action == "add":
        content = str(args.get("content", "")).strip()
        if not content:
            return make_tool_result("manage_memory 'add' requires 'content'", True)
        existing = read_memory_text(profile, target)
        filepath.write_text(f"{content}\n§\n{existing}")
        return make_tool_result(f"Memory added to {filepath} (profile: {profile}).")
    if action == "remove":
        search = str(args.get("content", "")).strip()
        text = read_memory_text(profile, target)
        if not search or not text:
            return make_tool_result(f"No matching entries removed from {filepath} (profile: {profile}).")
        filepath.write_text(text.replace(search, ""))
        return make_tool_result(f"Removed entries containing '{search}' from {filepath} (profile: {profile}).")
    # clean: delete the file (root and legacy) so it cannot resurrect itself
    for p in (filepath, legacy_memories_file(profile, target)):
        if p.exists():
            p.unlink()
    return make_tool_result(f"MEMORY file cleared: all entries removed from {filepath} (profile: {profile}).")


# --------------------------------------------------------------------------
# save_summary (defensive: must never crash a thread)
# --------------------------------------------------------------------------

def db_connect():
    """Open a psycopg2 connection from DATABASE_URL.

    Returns (conn, None) on success, or (None, error_message) on failure.
    """
    if psycopg2 is None:
        return None, "psycopg2 is not available"
    db_url = os.environ.get("DATABASE_URL") or cfg_env("database_url")
    if not db_url:
        return None, "DATABASE_URL is not set"
    try:
        return psycopg2.connect(db_url), None
    except Exception as e:
        return None, f"cannot connect to database: {e}"


def _save_summary_impl(args, meta):
    # channel_id is the channel NAME (text), exactly what summaries.channel_id
    # holds - NOT a numeric id and NOT a platform channel id.
    raw_cid = args.get("channel_id")
    if raw_cid is None or not str(raw_cid).strip():
        return make_tool_result(
            "save_summary requires 'channel_id': the channel NAME (e.g. 'main'), not a numeric id", True)
    channel_id = str(raw_cid).strip()

    # next_thread_id is the highest thread id covered by this summary; the
    # next summary run continues after it (same semantics as the hook
    # watermark and queries::create_summary in the omniagent runtime).
    raw_next = args.get("next_thread_id")
    if raw_next is None or (isinstance(raw_next, str) and not raw_next.strip()):
        return make_tool_result(
            "save_summary requires 'next_thread_id': the highest thread id covered by the summary", True)
    try:
        next_thread_id = int(raw_next)
    except (TypeError, ValueError):
        return make_tool_result(f"invalid next_thread_id '{raw_next}': must be an integer", True)

    raw_content = args.get("content")
    if raw_content is None or not str(raw_content).strip():
        return make_tool_result("save_summary requires non-empty 'content' (the summary markdown)", True)
    content = str(raw_content)

    conn, err = db_connect()
    if conn is None:
        return make_tool_result(f"save_summary error: {err}", True)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO summaries (channel_id, next_thread_id, content) VALUES (%s,%s,%s) "
            "RETURNING id, channel_id, next_thread_id, created_at",
            (channel_id, next_thread_id, content))
        row = cur.fetchone()
        conn.commit()
        summary_id, cid, nti, created = row
        created_s = created.isoformat() if hasattr(created, "isoformat") else str(created)
        return make_tool_result(
            f"Summary saved: id={summary_id}, channel_id={cid}, next_thread_id={nti}, "
            f"created_at={created_s} ({len(content)} chars)")
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.exception("save_summary insert failed")
        return make_tool_result(f"save_summary error: {e}", True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handle_save_summary(args, meta):
    """Top-level defensive wrapper: never lets an exception escape."""
    try:
        return _save_summary_impl(args, meta)
    except Exception as e:  # last line of defense
        log.exception("save_summary crashed")
        return make_tool_result(f"save_summary error: {e}", True)


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "promote_to_memory",
        "description": "Promote a validated fact to long-term memory by writing it to the wiki. "
                       "Memories are stored as markdown files under Memory/Promoted/ with frontmatter "
                       "containing provenance, confidence, and expiry information.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short, descriptive name for the memory (used as filename)"},
                "content": {"type": "string", "description": "The validated fact(s) to store as memory. Be precise and concise."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"],
                               "description": "Confidence in the fact's accuracy"},
                "source_message_ids": {"type": "array", "items": {"type": "integer"},
                                       "description": "Message IDs that support this fact from the conversation"},
                "source_tool_outputs": {"type": "array", "items": {"type": "string"},
                                        "description": "Tool call IDs whose outputs provide evidence"},
                "expires_in_days": {"type": "integer",
                                    "description": "Days until this memory expires and needs review (default: 30)"},
            },
            "required": ["name", "content", "confidence"],
        },
    },
    {
        "name": "list_memories",
        "description": "List all promoted memory entries in the wiki. Returns filenames, titles, confidence levels, and expiry dates for each memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_expired": {"type": "boolean",
                                    "description": "Whether to include expired memories (default: false)"},
            },
        },
    },
    {
        "name": "review_memories",
        "description": "Review promoted memory entries for expiry, verifying factual accuracy. Returns a report of expired or soon-to-expire memories that need re-validation or renewal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expiring_soon_days": {"type": "integer",
                                       "description": "Days threshold for 'expiring soon' warning (default: 7)"},
            },
        },
    },
    {
        "name": "manage_memory",
        "description": "Manage profile memory files (MEMORY.md and USER.md). Supports add, remove, and clean operations on the agent's persistent memory entries. Use on explicit user request only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["memory", "user"],
                           "description": "Which file: 'memory' for MEMORY.md, 'user' for USER.md"},
                "action": {"type": "string", "enum": ["add", "remove", "clean"],
                           "description": "Operation: 'add' prepends a new entry, 'remove' deletes entries matching substring, 'clean' clears all entries"},
                "content": {"type": "string",
                            "description": "Content for 'add' action. For 'remove', a substring to match against entries."},
            },
            "required": ["target", "action"],
        },
    },
    {
        "name": "save_summary",
        "description": "Save a channel summary as a NEW row in the summaries table. This tool only PERSISTS text the caller has already produced (the agent/LLM writes the summary); it does not generate it. "
                       "channel_id is the channel NAME (e.g. 'main'), stored verbatim in summaries.channel_id: NOT a numeric channel id and NOT the platform channel id. "
                       "next_thread_id is the highest thread id covered by this summary (the next summary run continues after it), stored verbatim in summaries.next_thread_id. "
                       "Returns the new summary id. All three arguments are required.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string",
                               "description": "Channel NAME whose history this summary covers (e.g. 'main'). Stored verbatim in summaries.channel_id. NOT a numeric id."},
                "next_thread_id": {"type": "integer",
                                   "description": "Highest thread id covered by this summary; the next summary run starts after it. Stored verbatim in summaries.next_thread_id."},
                "content": {"type": "string",
                            "description": "The summary markdown to store in summaries.content. Must be non-empty."},
            },
            "required": ["channel_id", "next_thread_id", "content"],
        },
    },
]

HANDLERS = {
    "promote_to_memory": handle_promote,
    "list_memories": handle_list,
    "review_memories": handle_review,
    "manage_memory": handle_manage,
    "save_summary": handle_save_summary,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "memory-python", "version": "0.1.0"},
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
    except ProfileNameMissing as e:
        # Guard rail (telegram thread 2719): a call with no usable profile
        # must fail loudly, NOT create <OMNI_DIR>/profiles/default.
        log.error("tool %s refused: %s", name, e)
        result = make_tool_result(f"{name} refused: {e}", True)
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
