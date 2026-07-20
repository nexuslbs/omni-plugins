#!/usr/bin/env python3
"""prompt-python MCP server : Python equivalent of the Rust prompt plugin.

Tools:
  - generate: Build the complete LLM prompt (system prompt + thread context +
              summaries + skills + subtasks + planning). Same output as the
              Rust mcp-server-prompt except "You are OmniAgent (Python)" vs
              "You are OmniAgent" in the static identity line.
  - compact-messages: Compact old assistant/tool-call pairs in a message array.

MCP JSON-RPC over stdio. Requires DATABASE_URL and OMNI_DIR env vars.
"""

import json
import os
import sys
import logging
import hashlib
import psycopg2
import psycopg2.extras
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [prompt-python] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
initialized = False
conn = None

# ---------------------------------------------------------------------------
# MCP protocol helpers
# ---------------------------------------------------------------------------

def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}

def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

def make_tool_result(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }

# ---------------------------------------------------------------------------
# Memory / file reading helpers
# ---------------------------------------------------------------------------

def read_file(path):
    """Read a file, return content or empty string."""
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return ""

def load_memories(data_dir, profile_name):
    """Read MEMORY.md and USER.md for a profile."""
    base = Path(data_dir) / "profiles" / profile_name / "memories"
    memory_raw = read_file(base / "MEMORY.md")
    user_raw = read_file(base / "USER.md")
    return memory_raw, user_raw

def get_skills(data_dir, profile_name):
    """List skills from the skills directory."""
    skills_dir = Path(data_dir) / "profiles" / profile_name / "skills"
    skills = []
    if skills_dir.exists():
        for f in sorted(skills_dir.iterdir()):
            if f.suffix == ".md":
                content = read_file(f)
                first_line = content.strip().split("\n")[0] if content.strip() else ""
                desc = first_line.lstrip("#").strip() if first_line.startswith("#") else first_line
                skills.append(f"- {f.stem}: {desc}")
    return skills

def truncate_str(s, max_chars):
    """Truncate a string at a character boundary."""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "..."

# ---------------------------------------------------------------------------
# Prompt building : identical to Rust prompt_builder.rs
# ---------------------------------------------------------------------------

TOOL_GUIDANCE = (
    "TOOL USE RULES (fail the task if you violate these):\n"
    "1. CALL TOOLS DIRECTLY : Do NOT search the filesystem, read plugin configs, "
    "read mcp-config.json files, inspect server.py files, or look at docker-compose files "
    "to discover what tools exist or how to call them. The function-calling API already "
    "shows you every available tool with its name, description, and parameters. "
    "If you need information about available tools, use the list_tool_details tool. "
    "Reading config files to find tools is always wrong and wastes turns.\n"
    "2. SEARCH BEFORE QUERY : Use search (search_messages, search_wiki) before "
    "query_database for text/vector searches. Only use query_database for structured "
    "aggregations (counts, sums, averages, groupings).\n"
    "3. WRITE SINGLE-FIELD FILES : When using filesystem_write_tool, write complete "
    "single-field content files. Do NOT write partial files and append later. Do NOT "
    "write placeholder content expecting to \"fill in\" values afterward.\n"
    "4. RENAME INSTEAD OF RECREATE : When a file/directory already exists and you "
    "need to change its name, rename it (filesystem_move). Do NOT delete and recreate.\n"
    "5. NO POLLING : Do NOT repeatedly check the same condition. If you're waiting "
    "for something, use the appropriate tool once and wait for the result.\n"
    "6. TOGGLE INSTEAD OF CONDITIONAL : For boolean/config values, use the toggle "
    "endpoint. Do NOT read the current value, compute the negation, and write it back.\n"
    "7. COMPLETE WORK : Before presenting results, finish ALL steps. Do not interrupt "
    "your work to show intermediate progress unless asked.\n"
    "8. CONFIRM DESTRUCTIVE ACTIONS : Before delete/overwrite/stop operations, "
    "present what you will do and wait for confirmation.\n"
    "9. SKIP ON FAILURE : If an operation fails (network error, not found, bad request), "
    "try once more with a different approach, then move on. Do NOT retry the same "
    "failing call more than once. There is no hidden state that changes between retries."
)

PLATFORM_HINTS = {
    "telegram": (
        "You are on a text messaging communication platform, Telegram. "
        "Standard markdown is automatically converted to Telegram format. Supported: **bold**, "
        "*italic*, ~~strikethrough~~, ||spoiler||, `inline code`, ```code blocks```, [links](url), "
        "and ## headers. Telegram has NO table syntax : prefer bullet lists or labeled key: value "
        "pairs over pipe tables (any tables you do emit are auto-rewritten into row-group bullets, "
        "which you can produce directly for cleaner output). You can send media files natively: "
        "to deliver a file to the user, include MEDIA:/absolute/path/to/file in your response. "
        "Images (.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice bubbles, and "
        "videos (.mp4) play inline. You can also include image URLs in markdown format ![alt](url) "
        "and they will be sent as native photos."
    ),
    "mattermost": (
        "You are on a Mattermost messaging platform. Standard markdown formatting is supported: "
        "**bold**, *italic*, `code`, ```code blocks```, [links](url), headings, lists, tables, "
        "blockquotes. Mattermost supports most GFM (GitHub Flavored Markdown)."
    ),
}

def build_dynamic_identity(tool_names):
    """Build identity string : same as Rust but with '(Python)' marker."""
    tool_set = set(tool_names)

    has_fetch = "fetch" in tool_set
    has_search = any(n.startswith("search_") for n in tool_names)
    has_query = any(n.startswith("query_") for n in tool_names)
    has_kanban = any(n.startswith("kanban") for n in tool_names)
    has_cron = any(n.startswith("cron") for n in tool_names)
    has_git = any(n.startswith("commit") or n.startswith("create_github") or n.startswith("clone_repo") or n == "status" for n in tool_names)
    has_subtasks = any(n.startswith("manage_subtask") for n in tool_names)
    has_skills = any(n.startswith("create_skill") or n.startswith("list_skills") for n in tool_names)
    has_plugin = any(n == "plugin_manager" or n == "list_plugins" for n in tool_names)

    parts = ["filesystem (read/write/list)"]
    if has_fetch: parts.append("fetch (HTTP)")
    if has_search: parts.append("search (messages/wiki)")
    if has_query: parts.append("query_database (SQL)")
    if has_kanban: parts.append("kanban")
    if has_cron: parts.append("cron")
    if has_git: parts.append("git")
    if has_subtasks: parts.append("manage_subtasks")
    if has_skills: parts.append("skills")
    if has_plugin: parts.append("plugin_manager")

    CATEGORIZED = {
        "filesystem", "fetch", "search_", "query_", "kanban", "cron",
        "commit", "create_github", "clone_repo", "status", "manage_subtask",
        "create_skill", "list_skills", "plugin_manager", "list_plugins",
        "list_tool_details", "compose", "hindsight_", "docker_",
        "promote_to_memory", "list_memories", "review_memories", "manage_memory",
        "get_metrics", "setup_", "kanban_",
    }

    for n in tool_names:
        if not any(n.startswith(c) or n == c for c in CATEGORIZED):
            parts.append(n)

    tool_list = ", ".join(parts) if parts else ", ".join(tool_names)

    # KEY DIFFERENCE from Rust: "(Python)" marker in identity line
    return f"You are OmniAgent (Python) : precise, efficient, autonomous. Your tools: {tool_list}. Use minimum roundtrips. If a tool fails, move on : don't retry more than twice."

def build_system_prompt(data_dir, profile_name, platform, system_message, tool_names):
    """Build the three-tier system prompt : matches Rust build_system_prompt()."""
    parts = []

    # Tier 1 : Stable
    parts.append(build_dynamic_identity(tool_names))
    parts.append(TOOL_GUIDANCE)
    parts.append(f"Active Hermes profile: {profile_name}.")

    # Tier 2 : Context / optional system message
    if system_message:
        parts.append(system_message)

    # Tier 3 : Volatile
    hint = PLATFORM_HINTS.get(platform)
    if hint:
        parts.append(hint)

    memory_raw, user_raw = load_memories(data_dir, profile_name)

    if memory_raw:
        max_chars = int(os.environ.get("MEMORY_MAX_CHARS", "5000"))
        truncated = memory_raw[:max_chars]
        if len(memory_raw) > max_chars:
            truncated += f"\n\n[... truncated from {len(memory_raw)} to ~{max_chars} chars]"
        header = f"## MEMORY (your personal notes) [{100}% : {len(memory_raw)}/{len(memory_raw)} chars]"
        parts.append(f"{header}\n{truncated}")

    if user_raw:
        max_chars = int(os.environ.get("USER_MAX_CHARS", "1000"))
        truncated = user_raw[:max_chars]
        if len(user_raw) > max_chars:
            truncated += f"\n\n[... truncated from {len(user_raw)} to ~{max_chars} chars]"
        header = f"## USER PROFILE (who the user is) [{100}% : {len(user_raw)}/{len(user_raw)} chars]"
        parts.append(f"{header}\n{truncated}")

    return "\n\n".join(parts)

def build_planning_prompt(tool_names, plan_iteration, max_iterations, previous_plan, user_message):
    """Build planning prompt : matches Rust build_planning_prompt()."""
    tool_list = f"Your available tools: {', '.join(tool_names)}." if tool_names else ""

    if plan_iteration == 0:
        iter_note = f" (iteration {plan_iteration + 1}/{max_iterations})" if max_iterations > 1 else ""
        context = (
            f"## Plan{iter_note}\n"
            f"Before responding, create a high-level plan with numbered steps. "
            f"{tool_list}\n"
            f"Be specific about which tool to use and what parameters to pass. "
            f"Aim for the minimum number of steps to complete the task. "
            f"Wrap your plan in a <plan> block. After delivering the final answer, "
            f"evaluate: if the task was completed, call the completion tool."
        )
    else:
        context = (
            f"## Revised Plan (iteration {plan_iteration + 1}/{max_iterations})\n"
            f"Your previous plan did not fully complete the task. "
            f"Review what was done vs what remains. Identify the specific "
            f"blockage and create a revised plan. Each step must include "
            f"which tool to use and what parameters.\n\n"
            f"Previous plan:\n{previous_plan or '(none)'}"
        )

    memory_raw, user_raw = None, None  # Planning prompt doesn't need full memory
    parts = []
    if memory_raw: parts.append(f"MEMORY: {len(memory_raw)} chars")
    if user_raw: parts.append(f"USER PROFILE: {len(user_raw)} chars")
    memory_info = f"\nAvailable context:\n" + "\n".join(parts) if parts else ""

    user_msg = f"\n\nUser request:\n{user_message}" if user_message else ""

    return f"{context}{memory_info}{user_msg}"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    """Get or create a database connection."""
    global conn
    if conn is None or conn.closed:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL must be set")
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
    return conn

def get_thread_messages(cursor, thread_id, limit=10):
    cursor.execute(
        """SELECT id, thread_id, role, content, msg_type, msg_subtype,
                  TO_CHAR(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at
           FROM messages
           WHERE thread_id = %s
             AND role IN ('cause', 'agent')
             AND msg_type IN ('message', 'reasoning')
           ORDER BY created_at DESC
           LIMIT %s""",
        (thread_id, limit),
    )
    rows = cursor.fetchall()
    rows.reverse()  # oldest first
    return rows

def get_latest_summary(cursor, channel_id):
    cursor.execute(
        """SELECT id, channel_id, next_thread_id, content
           FROM summaries
           WHERE channel_id = %s
           ORDER BY id DESC
           LIMIT 1""",
        (channel_id,),
    )
    return cursor.fetchone()

def get_threads_since(cursor, channel_id, since_id, limit=5):
    cursor.execute(
        """SELECT id, status, cause
           FROM threads
           WHERE channel_id = %s
             AND status = 'completed'
             AND id > %s
           ORDER BY id ASC
           LIMIT %s""",
        (channel_id, since_id, limit),
    )
    return cursor.fetchall()

def get_subtasks(cursor, thread_id):
    cursor.execute(
        """SELECT id, description, status, thread_id
           FROM thread_subtasks
           WHERE thread_id = %s
           ORDER BY id ASC""",
        (thread_id,),
    )
    return cursor.fetchall()

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_generate(req_id, arguments, meta):
    """Generate the full LLM prompt as parts : matches Rust handle_generate_full()."""
    try:
        profile_name = (arguments or {}).get("profile_name", "default")
        platform = (arguments or {}).get("platform", "")
        system_message = (arguments or {}).get("system_message")
        user_message = (arguments or {}).get("user_message", "")
        tool_names = (arguments or {}).get("tool_names", [])
        thread_id = (arguments or {}).get("thread_id") or (meta or {}).get("thread_id")
        channel_id = (arguments or {}).get("channel_id") or (meta or {}).get("channel_id")

        data_dir = os.environ.get("OMNI_DIR", os.path.expanduser("~/.omniagent"))

        # 1. Build system prompt parts
        memory_raw, user_raw = load_memories(data_dir, profile_name)

        parts = []
        parts.append(build_dynamic_identity(tool_names))
        parts.append(TOOL_GUIDANCE)
        parts.append(f"Active Hermes profile: {profile_name}.")
        if system_message:
            parts.append(system_message)
        hint = PLATFORM_HINTS.get(platform)
        if hint:
            parts.append(hint)

        # Split into system (stable parts) vs memory vs soul
        system_parts_before_soul = [p for p in parts if p != system_message]
        soul = system_message if system_message else ""
        system = "\n\n".join(system_parts_before_soul)

        # Memory = MEMORY.md + USER.md content
        memory = ""
        if memory_raw:
            max_chars = int(os.environ.get("MEMORY_MAX_CHARS", "5000"))
            truncated = memory_raw[:max_chars]
            if len(memory_raw) > max_chars:
                truncated += f"\n\n[... truncated from {len(memory_raw)} to ~{max_chars} chars]"
            header = f"## MEMORY (your personal notes) [100% : {len(memory_raw)}/{len(memory_raw)} chars]"
            memory += f"{header}\n{truncated}\n"
        if user_raw:
            max_chars = int(os.environ.get("USER_MAX_CHARS", "1000"))
            truncated = user_raw[:max_chars]
            if len(user_raw) > max_chars:
                truncated += f"\n\n[... truncated from {len(user_raw)} to ~{max_chars} chars]"
            header = f"## USER PROFILE (who the user is) [100% : {len(user_raw)}/{len(user_raw)} chars]"
            memory += f"{header}\n{truncated}"
        memory = memory.strip()

        # 2. Build context blocks
        context_blocks = []
        db = get_db()
        cursor = db.cursor()

        if thread_id is not None:
            thread_id = int(thread_id)
            rows = get_thread_messages(cursor, thread_id, 10)
            if rows:
                formatted = [f"[{r[2]}]: {truncate_str(r[3], 500)}" for r in rows]
                context_blocks.append("Recent conversation history (current thread):\n" + "\n".join(formatted))

        if channel_id is not None:
            channel_id = int(channel_id)
            summary = get_latest_summary(cursor, channel_id)
            if summary:
                context_blocks.append(
                    f"Previous channel summary (covers threads up to id={summary[2]}):\n"
                    f"{truncate_str(summary[3], 4000)}"
                )
                threads = get_threads_since(cursor, channel_id, summary[2], 5)
                if threads:
                    thread_info = [f"[Thread #{t[0]} by {t[2]}]: completed" for t in threads]
                    context_blocks.append("Recent threads (after last summary):\n" + "\n---\n".join(thread_info))

        # Skills
        skills = get_skills(data_dir, profile_name)
        if skills:
            context_blocks.append("Available skills:\n" + "\n".join(skills))

        # Subtasks
        if thread_id is not None:
            subtask_rows = get_subtasks(cursor, thread_id)
            if subtask_rows:
                lines = [f"## Subtasks (Thread #{thread_id})"]
                for i, s in enumerate(subtask_rows):
                    icon = {"completed": "✅", "cancelled": "❌", "error": "⚠️"}.get(s[2], "⬜")
                    lines.append(f"{i + 1}. {icon} {s[1]}")
                context_blocks.append("\n".join(lines))

        cursor.close()

        context = "\n\n---\n\n".join(context_blocks)
        user = user_message

        result = json.dumps({
            "system": system,
            "memory": memory,
            "soul": soul,
            "context": context,
            "user": user,
        }, indent=2)

        send_json(make_success(req_id, make_tool_result(result)))

    except Exception as e:
        log.error("generate tool failed: %s", e, exc_info=True)
        send_json(make_success(req_id, make_tool_result(f"Error: {e}", True)))


def handle_compact_messages(req_id, arguments):
    """Compact old assistant messages : matches Rust handle_compact_messages()."""
    try:
        messages = (arguments or {}).get("messages", [])
        keep_recent = int((arguments or {}).get("keep_recent", 3))

        if not isinstance(messages, list):
            send_json(make_success(req_id, make_tool_result("Missing required argument: 'messages' (array of ChatMessage)", True)))
            return

        before = len(messages)

        # Find indices of assistant messages with tool_calls
        tool_indices = [i for i, m in enumerate(messages)
                        if m.get("role") == "assistant" and m.get("tool_calls")]

        while len(tool_indices) > keep_recent:
            compact_up_to = len(tool_indices) - keep_recent
            for idx in reversed(tool_indices[:compact_up_to]):
                calls = messages[idx].get("tool_calls", [])
                summary = [f"{tc['function']['name']}()" for tc in calls]

                # Find tool-role messages following this assistant message
                tool_end = idx + 1
                while tool_end < len(messages) and messages[tool_end].get("role") == "tool":
                    tool_end += 1

                tool_names_list = [messages[i].get("name", "") for i in range(idx + 1, tool_end) if messages[i].get("name")]
                tool_info = f". Results from: {', '.join(tool_names_list)}" if tool_names_list else ""

                condensed = f"[compact: {', '.join(summary)}{tool_info}]" if summary else "[compact]"
                messages[idx]["content"] = condensed
                messages[idx]["tool_calls"] = None
                del messages[idx + 1:tool_end]

            # Recalculate tool_indices after deletions
            tool_indices = [i for i, m in enumerate(messages)
                            if m.get("role") == "assistant" and m.get("tool_calls")]

        after = len(messages)
        result = json.dumps({
            "messages": messages,
            "was_compacted": before != after,
            "before_count": before,
            "after_count": after,
        }, indent=2)

        send_json(make_success(req_id, make_tool_result(result)))

    except Exception as e:
        log.error("compact-messages tool failed: %s", e, exc_info=True)
        send_json(make_success(req_id, make_tool_result(f"Error: {e}", True)))


# ---------------------------------------------------------------------------
# MCP lifecycle
# ---------------------------------------------------------------------------

def handle_initialize(req_id):
    result = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "prompt-python", "version": "0.1.0"},
    }
    send_json(make_success(req_id, result))
    log.info("Initialized: prompt-python v0.1.0")


def handle_tools_list(req_id):
    tools = [
        {
            "name": "prompt-build",
            "description": "[prompt-python] Generate the complete LLM prompt as 5 parts (system, memory, soul, context, user) for a conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "profile_name": {
                        "type": "string",
                        "description": "Profile name (default: default)"
                    },
                    "platform": {
                        "type": "string",
                        "description": "Platform identifier (e.g. 'telegram', 'mattermost')"
                    },
                    "system_message": {
                        "type": "string",
                        "description": "Optional system message override"
                    },
                    "user_message": {
                        "type": "string",
                        "description": "User's message to include in the prompt"
                    },
                    "tool_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of available tool names"
                    },
                    "thread_id": {
                        "type": "integer",
                        "description": "Thread ID for context assembly (recent messages, subtasks)"
                    },
                    "channel_id": {
                        "type": "integer",
                        "description": "Channel ID for context assembly (summaries)"
                    }
                },
                "required": []
            }
        },
        {
            "name": "prompt-compact",
            "description": "[prompt-python] Compact old assistant messages in a conversation to save tokens. "
                           "Removes redundant assistant tool-call pairs from the middle of the conversation "
                           "while preserving system messages and the most recent messages.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "description": "Array of ChatMessage objects to compact",
                    },
                    "keep_recent": {
                        "type": "integer",
                        "description": "Number of most recent messages to always keep (default: 3)",
                    },
                },
                "required": ["messages"],
            },
        },
    ]
    send_json(make_success(req_id, {"tools": tools}))
    log.info("tools/list returned 2 tools")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global initialized

    log.info("prompt-python MCP server starting (PID=%d)", os.getpid())
    log.info("OMNI_DIR=%s", os.environ.get("OMNI_DIR", "(not set)"))
    log.info("DATABASE_URL=%s", "set" if os.environ.get("DATABASE_URL") else "(not set)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            log.error("Failed to parse JSON-RPC: %s", e)
            continue

        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})
        meta = params.get("_meta") if isinstance(params, dict) else None

        if method == "initialize":
            if req_id is not None:
                handle_initialize(req_id)
                initialized = True

        elif method == "notifications/initialized":
            log.info("Client initialized notification received")

        elif method == "tools/list":
            if not initialized:
                if req_id is not None:
                    send_json(make_error(req_id, -32000, "Server not initialized"))
                continue
            if req_id is not None:
                handle_tools_list(req_id)

        elif method == "tools/call":
            if not initialized:
                if req_id is not None:
                    send_json(make_error(req_id, -32000, "Server not initialized"))
                continue
            if req_id is not None:
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {}) if isinstance(params, dict) else {}

                if tool_name == "prompt-build":
                    handle_generate(req_id, arguments, meta)
                elif tool_name == "prompt-compact":
                    handle_compact_messages(req_id, arguments)
                else:
                    if req_id is not None:
                        send_json(make_error(req_id, -32602, f"Unknown tool: {tool_name}"))

        else:
            log.warning("Unknown method: %s", method)
            if req_id is not None:
                send_json(make_error(req_id, -32601, f"Method not found: {method}"))

    log.info("prompt-python MCP server shutting down (stdin closed)")


if __name__ == "__main__":
    main()
