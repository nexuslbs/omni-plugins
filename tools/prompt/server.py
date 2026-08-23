#!/usr/bin/env python3
"""prompt MCP server : Python equivalent of the builtin Rust prompt plugin.

Tools:
  - prompt_generate: Build the complete LLM prompt (system prompt + thread
              context + summaries + skills + subtasks + planning). Same
              output as the builtin Rust mcp-server-prompt.
  - prompt_compact-messages: Compact a conversation to stay within token
              budgets (frozen '=== Compaction Summary ===' block, progressive
              passes, durable context dumps + auto-notes). Same output as the
              builtin Rust mcp-server-prompt.

Exact-parity port of /opt/workspace/omniagent/plugins/tools/prompt/
(plugin.json + src/main.rs + src/compact.rs + src/prompt_builder.rs +
src/memory_store.rs + src/chat_message.rs + src/notes.rs + src/dump.rs).
The ONLY intended difference is the implementation language (Python vs Rust).

MCP JSON-RPC over stdio. Requires DATABASE_URL and OMNI_DIR env vars
(plugin config may inject them; $env: placeholders resolved by the
framework).
"""

import json
import os
import re
import sys
import logging
import hashlib
import psycopg2
import psycopg2.extras
from pathlib import Path

# Optional real-token measurement (parity with tiktoken_rs). When absent the
# chars/4 fallback applies, exactly like the Rust fallback on load failure.
try:
    import tiktoken  # type: ignore
except ImportError:
    tiktoken = None

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
# Plugin config — mirrors the builtin Rust PluginConfig (plugin.json
# config_schema, 15 keys). Values arrive as env vars injected by the
# framework (key name or UPPER_SNAKE variant); defaults match the Rust
# PluginConfig::default() exactly.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "planning_complexity_max_chars": 60,
    "planning_complexity_keywords": (
        "implement,refactor,redesign,architecture,create,build,design,develop,"
        "migrate,restructure,overhaul,rewrite,configure,set up,deploy,integrate,"
        "add feature,fix bug,resolve issue,multi-step,complex"
    ),
    "prompt_plan_max_tokens": 2048,
    "memory_max_chars": 5000,
    "tokenizer_encoding": "",
    "condense_keep_turns": 4,
    "database_url": "",
    "omni_dir": "",
    "tool_excerpt_chars": 800,
    "total_excerpt_cap": 4000,
    "read_excerpt_chars": 2000,
    "compact_keep_recent": 3,
    "compact_max_passes": 3,
    "compact_keep_step": 1,
    "max_summary_chars": 50000,
}

# Values sent via an MCP "configure" message (the builtin receives config
# through run_server_with_config's configure callback; the framework may also
# do this for python servers). Takes precedence over env vars.
CONFIG_OVERRIDES = {}

INT_KEYS = {
    "planning_complexity_max_chars",
    "prompt_plan_max_tokens",
    "memory_max_chars",
    "condense_keep_turns",
    "tool_excerpt_chars",
    "total_excerpt_cap",
    "read_excerpt_chars",
    "compact_keep_recent",
    "compact_max_passes",
    "compact_keep_step",
    "max_summary_chars",
}
STR_KEYS = {
    "planning_complexity_keywords",
    "tokenizer_encoding",
    "database_url",
    "omni_dir",
}

# Clamps mirroring PluginConfig::from_json (max(1) guards).
CLAMP_MIN_ONE = {"compact_max_passes", "compact_keep_step", "max_summary_chars", "condense_keep_turns"}


def _env_names(key):
    """Candidate env var names for a config key (framework injects config as
    env; try the key as-is and the UPPER_SNAKE convention)."""
    upper = key.upper()
    names = [key, upper]
    if "-" in key:
        names.append(key.replace("-", "_").upper())
    return names


def get_config():
    """Resolve the effective plugin config: configure-message overrides, then
    env vars (empty string = unset), then builtin defaults."""
    cfg = dict(DEFAULT_CONFIG)
    for key, default in DEFAULT_CONFIG.items():
        value = None
        for name in _env_names(key):
            if name in CONFIG_OVERRIDES:
                value = CONFIG_OVERRIDES[name]
                break
        if value is None:
            for name in _env_names(key):
                v = os.environ.get(name)
                if v is not None and v.strip() != "":
                    value = v.strip()
                    break
        if value is None:
            continue
        if key in INT_KEYS:
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                continue
            if key in CLAMP_MIN_ONE:
                cfg[key] = max(1, cfg[key])
        else:
            cfg[key] = str(value)
    return cfg


def cfg_env(key):
    """Read a single resolved config value (string form)."""
    cfg = get_config()
    v = cfg.get(key, "")
    return "" if v is None else str(v)


def _fail_omni_dir():
    raise RuntimeError(
        "OMNI_DIR is not set: set the OMNI_DIR environment variable or configure the "
        "'omni_dir' plugin config field (default '$env:OMNI_DIR')"
    )


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
# File / memory helpers (mirror Rust memory_store.rs + helpers)
# ---------------------------------------------------------------------------

def read_file(path):
    """Read a file, return content or empty string."""
    try:
        with open(path, "r") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return ""


def truncate_str(s, max_chars):
    """Rust truncate_str: chars-count based, appends '...' (no note)."""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "..."


def load_memory_raw(data_dir, profile_name):
    """Memory = {data_dir}/profiles/{profile}/MEMORY.md (profile ROOT).
    USER.md is never read (SOUL.md/USER.md support removed upstream)."""
    base = Path(data_dir) / "profiles" / profile_name
    return read_file(base / "MEMORY.md")


def build_memory_section(memory_raw, memory_max_chars):
    """Rust read_memory_section + truncate_content, exact header/format."""
    if not memory_raw:
        return ""
    raw_len = len(memory_raw)
    if raw_len > memory_max_chars:
        header = (
            "## MEMORY (your personal notes) "
            f"[TRUNCATED: showing first {memory_max_chars} of {raw_len} chars]"
        )
        body = memory_raw[:memory_max_chars] + "..." + \
            f"\n\n[... truncated from {raw_len} to ~{memory_max_chars} chars]"
    else:
        header = f"## MEMORY (your personal notes) [{raw_len} chars]"
        body = memory_raw
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Stable identity / guidance texts (exact copies of prompt_builder.rs)
# ---------------------------------------------------------------------------

def build_dynamic_identity(tool_names):
    """Rust build_dynamic_identity: categorized groups + uncategorized extras
    in input order."""
    has_fetch = any(n == "fetch" for n in tool_names)
    has_search = any(n.startswith("search_") for n in tool_names)
    has_query = any(n.startswith("query_") for n in tool_names)
    has_kanban = any(n.startswith("kanban") for n in tool_names)
    has_cron = any(n.startswith("cron") for n in tool_names)
    has_git = any(
        n.startswith("commit") or n.startswith("create_github")
        or n.startswith("clone_repo") or n == "status"
        for n in tool_names
    )
    has_subtasks = any(
        n.startswith("subtasks_") or n.startswith("manage_subtask")
        for n in tool_names
    )
    has_skills = any(
        n.startswith("create_skill") or n.startswith("list_skills")
        for n in tool_names
    )
    has_plugin = any(n == "plugin_manager" or n == "list_plugins" for n in tool_names)

    parts = ["filesystem (read/write/list)"]
    if has_fetch:
        parts.append("fetch (HTTP)")
    if has_search:
        parts.append("search (messages/wiki)")
    if has_query:
        parts.append("search_database (SQL)")
    if has_kanban:
        parts.append("kanban")
    if has_cron:
        parts.append("cron")
    if has_git:
        parts.append("git")
    if has_subtasks:
        parts.append("manage_subtasks")
    if has_skills:
        parts.append("skills")
    if has_plugin:
        parts.append("plugin_manager")

    def is_categorized(name):
        return (
            name.startswith("filesystem")
            or name == "fetch"
            or name.startswith("search_")
            or name.startswith("query_")
            or name.startswith("kanban")
            or name.startswith("cron")
            or name.startswith("commit")
            or name.startswith("create_github")
            or name.startswith("clone_repo")
            or name == "status"
            or name.startswith("manage_subtask")
            or name.startswith("subtasks_")
            or name.startswith("create_skill")
            or name.startswith("list_skills")
            or name == "plugin_manager"
            or name == "list_tools_details"
            or name == "list_tool_details"
            or name == "compose"
            or name.startswith("hindsight_")
            or name.startswith("docker_")
            or name == "promote_to_memory"
            or name == "list_memories"
            or name == "review_memories"
            or name == "manage_memory"
            or name == "search_metrics"
            or name.startswith("setup_")
            or name.startswith("kanban_")
        )

    extra = [n for n in tool_names if not is_categorized(n)]
    parts.extend(extra)

    tool_list = ", ".join(parts) if parts else ", ".join(tool_names)

    return (
        "You are OmniAgent: precise, efficient, autonomous. "
        f"Your tools: {tool_list}. Use minimum roundtrips. If a tool fails, move on: "
        "don't retry more than twice. HONESTY RULE: if you cannot complete the task, "
        "your final summary MUST clearly state that you gave up and why, and what "
        "remains undone — NEVER claim the task was completed unless every requested "
        "step was actually done and verified. NEVER end a turn with only thinking and "
        "no action: a response with no tool call is treated as the end of the task, "
        "so every turn MUST end with either tool calls or a final answer. If you have "
        "finished thinking, immediately emit your next tool call or your final answer "
        "— never stop after reasoning alone."
    )


TOOL_GUIDANCE = (
    "TOOL USE RULES (fail the task if you violate these):\n"
    "1. CALL TOOLS DIRECTLY: Do NOT search the filesystem, read plugin config files, "
    "or inspect server configuration to discover what tools exist or how to call them. "
    "Available tools are listed with their name, description, and parameters in the "
    "function-calling API. Reading config files to find tools is always wrong and wastes turns.\n"
    "2. SEARCH BEFORE QUERY: Use search tools before querying databases for text or "
    "vector searches. Only use direct data queries for structured aggregations "
    "(counts, sums, averages, groupings).\n"
    "3. WRITE COMPLETE FILES: When writing a file, write the entire content in a single "
    "operation. Do NOT write placeholder content expecting to fill in values afterward. "
    "EXCEPTION — LARGE OUTPUTS: if the file content is too large to fit in a single "
    "response (approaching your output token limit), split it across multiple "
    "filesystem_write calls: first with append=false, then append=true for each "
    "subsequent chunk. Never abandon a large write — chunk it. Never let an output "
    "length limit cause task failure.\n"
    "4. RENAME INSTEAD OF RECREATE: When a file or directory already exists and you "
    "need to change its name, use the rename tool. Do NOT delete and recreate.\n"
    "5. NO POLLING: Do NOT repeatedly check the same condition. If you're waiting "
    "for something, make a single request and wait for the result.\n"
    "6. SET DIRECTLY: For configuration values, set the new value directly. Do NOT "
    "read the current value, flip it, and write it back.\n"
    "7. COMPLETE WORK: Before presenting results, finish ALL steps. Do not interrupt "
    "your work to show intermediate progress unless asked.\n"
    "8. CONFIRM DESTRUCTIVE ACTIONS: Before delete, overwrite, or stop operations, "
    "present what you will do and wait for confirmation.\n"
    "9. SKIP ON FAILURE: If an operation fails (network error, not found, bad request), "
    "try once more with a different approach, then move on. Do NOT retry the same "
    "failing call more than once. There is no hidden state that changes between retries.\n"
    "10. TAKE NOTES: maintain a durable working memory with the note_* tools "
    "(notes_note-write/notes_note-append/notes_note-read/notes_note-list/notes_note-rm) after every non-trivial "
    "discovery (paths, line numbers, commands, root causes, decisions). Notes "
    "survive compaction and thread death — the retry thread starts with them.\n"
    "11. VERIFY-ONCE: read a file ONCE with `filesystem_read` (offset/limit paging — ONE\n"
    "call per page) and write the facts you need into your working notes; never re-read the\n"
    "same file or line range. NEVER use `docker_compose exec ... sed -n` / `grep -n` to read\n"
    "file contents: docker_compose is for RUNNING commands/builds, not reading files.\n"
    "Re-reading overlapping line ranges of the same file is the #1 budget killer (threads have\n"
    "died at 120/120 after 100+ sed windows with zero commits). Consult your notes, not the\n"
    "disk, when you need content again.\n"
    "12. NEVER RE-READ CONTEXT DUMPS: a context-*.json dump is read ONCE per thread — a second read returns a '[duplicate read ...]' marker, not content. Trust the injected '=== Context Compacted ===' summary and your notes instead; re-reading dumps is a forbidden anti-loop that wastes iterations.\n"
    "13. SUBTASKS: after planning a multi-step task, create one subtask per plan step with the subtasks tool (subtasks_manage-subtasks, action=\"add\"); as you finish each step mark its subtask completed (action=\"update\", subtask_id=N, status=\"completed\"); cancel any subtask that is no longer needed (status=\"cancelled\"); before your final answer, complete or cancel ALL subtasks so none remain pending."
)


PLATFORM_HINTS = {
    "telegram": (
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
    ),
    "mattermost": (
        "You are on a Mattermost messaging platform. Standard markdown formatting is supported: "
        "**bold**, *italic*, `code`, ```code blocks```, [links](url), headings, lists, tables, "
        "blockquotes. Mattermost supports most GFM (GitHub Flavored Markdown)."
    ),
}


# ---------------------------------------------------------------------------
# Skills (Rust get_skills: frontmatter-aware, flat + Hermes layouts)
# ---------------------------------------------------------------------------

def extract_frontmatter_field(content, field):
    """Rust extract_frontmatter_field: YAML frontmatter between --- markers."""
    content = content.strip()
    if not content.startswith("---"):
        return None
    after_first = content[len("---"):].lstrip()
    end = after_first.find("\n---")
    if end == -1:
        return None
    frontmatter = after_first[:end]
    prefix = field + ":"
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                return value[1:-1]
            if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                return value[1:-1]
            return value
    return None


def extract_skill_description(content):
    """Rust extract_skill_description: frontmatter description:, else first
    meaningful line with leading '#' stripped."""
    desc = extract_frontmatter_field(content, "description")
    if desc is not None and desc.strip():
        return desc
    for line in content.splitlines():
        line = line.strip()
        if line == "" or line == "---":
            continue
        return line.lstrip("#").strip()
    return "No description"


def skill_display_name(content, fallback):
    """Rust skill_display_name: frontmatter name:, else fallback."""
    name = extract_frontmatter_field(content, "name")
    if name is not None and name.strip():
        return name
    return fallback


def get_skills(data_dir, profile_name):
    """Rust get_skills: profile-scoped skills root; flat <name>.md and
    Hermes-style <category>/<name>/SKILL.md; sorted."""
    skills_dir = Path(data_dir) / "profiles" / profile_name / "skills"
    skills = []
    if skills_dir.exists():
        for entry in sorted(skills_dir.iterdir()):
            path = entry
            if path.is_file() and path.suffix == ".md":
                content = read_file(path)
                if content:
                    stem = path.stem
                    name = skill_display_name(content, stem)
                    desc = extract_skill_description(content)
                    skills.append(f"- {name}: {desc}")
            if path.is_dir():
                try:
                    cat_entries = sorted(path.iterdir())
                except OSError:
                    cat_entries = []
                for cat_entry in cat_entries:
                    skill_file = cat_entry / "SKILL.md"
                    if not skill_file.is_file():
                        continue
                    content = read_file(skill_file)
                    if content:
                        dir_name = cat_entry.name
                        name = skill_display_name(content, dir_name)
                        desc = extract_skill_description(content)
                        skills.append(f"- {name}: {desc}")
    skills.sort()
    return skills

# ---------------------------------------------------------------------------
# DB helpers (mirror main.rs queries)
# ---------------------------------------------------------------------------

def get_db():
    """Get or create a database connection (configure-message overrides first)."""
    global conn
    if conn is None or conn.closed:
        cfg = get_config()
        database_url = cfg.get("database_url") or os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("DATABASE_URL must be set")
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
    return conn


def get_thread_messages(cursor, thread_id, limit=10):
    cursor.execute(
        """SELECT id, thread_id, role, content, msg_type, msg_subtype,
                  COALESCE(TO_CHAR(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), '') AS created_at
           FROM messages
           WHERE thread_id = %s
             AND (role = 'cause' OR msg_type IN ('message', 'reasoning'))
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


def get_thread_task_ref(cursor, thread_id):
    cursor.execute(
        "SELECT task_id, schedule_task_id FROM threads WHERE id = %s",
        (thread_id,),
    )
    return cursor.fetchone()


# ---------------------------------------------------------------------------
# R8-J: prior attempts of the SAME task
# ---------------------------------------------------------------------------

PRIOR_ATTEMPTS_MAX_ENTRIES = 5
PRIOR_ATTEMPTS_MAX_SUMMARY_CHARS = 800


def get_prior_threads_by_task(cursor, task_id, current_thread_id, limit):
    cursor.execute(
        """SELECT id, status, iterations,
                  TO_CHAR(ended_at, 'YYYY-MM-DD HH24:MI') AS ended_at
           FROM threads
           WHERE task_id = %s
             AND (task_type = 'kanban' OR task_type IS NULL)
             AND id < %s
           ORDER BY id DESC
           LIMIT %s""",
        (task_id, current_thread_id, limit),
    )
    return cursor.fetchall()


def get_thread_summary(cursor, thread_id):
    cursor.execute(
        "SELECT content FROM messages WHERE thread_id = %s AND msg_type = 'summary' ORDER BY id DESC LIMIT 1",
        (thread_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def render_prior_attempts_block(rows, summaries):
    """Rust render_prior_attempts_block: newest first, capped at 5."""
    rows = sorted(rows, key=lambda r: r[0], reverse=True)[:PRIOR_ATTEMPTS_MAX_ENTRIES]
    if not rows:
        return None
    lines = ["=== Previous attempts of this task (READ — do NOT repeat what they did) ==="]
    for r in rows:
        t_id, status, iterations, ended_at = r
        iter_s = str(iterations) if iterations is not None else "?"
        ended_s = ended_at or "?"
        summary = summaries.get(t_id)
        if summary is not None:
            lines.append(
                f"- thread {t_id} | status {status} | iterations {iter_s} | "
                f"ended_at {ended_s} | summary: {truncate_str(summary, PRIOR_ATTEMPTS_MAX_SUMMARY_CHARS)}"
            )
        else:
            lines.append(
                f"- thread {t_id} | status {status} | iterations {iter_s} | "
                f"ended_at {ended_s} | (no summary message)"
            )
    return "\n".join(lines)


def build_prior_attempts_block(cursor, thread_id):
    """Rust build_prior_attempts_block: earlier threads of the SAME kanban
    task (all statuses) with their final summaries. Plain threads get none."""
    row = get_thread_task_ref(cursor, thread_id)
    if not row or not row[0]:
        return None
    task_id = row[0]
    rows = get_prior_threads_by_task(cursor, task_id, thread_id, PRIOR_ATTEMPTS_MAX_ENTRIES)
    if not rows:
        return None
    summaries = {}
    for r in rows:
        try:
            summaries[r[0]] = get_thread_summary(cursor, r[0])
        except Exception:
            summaries[r[0]] = None
    return render_prior_attempts_block(rows, summaries)


# ---------------------------------------------------------------------------
# R8-K: learned knowledge (promoted memories read-back)
# ---------------------------------------------------------------------------

LEARNED_KNOWLEDGE_MAX_ENTRY_CHARS = 600
LEARNED_KNOWLEDGE_MAX_TOTAL_CHARS = 3000


def strip_frontmatter(content):
    """Rust strip_frontmatter: drop leading ---...--- YAML block."""
    trimmed = content.lstrip()
    if trimmed.startswith("---"):
        after_open = trimmed[len("---"):]
        idx = after_open.find("\n---")
        if idx != -1:
            return after_open[idx + 4:].lstrip()
        return trimmed  # unterminated fence - treat whole doc as body
    return trimmed


def load_promoted_memories(data_dir, profile_name):
    """Rust load_promoted_memories: wiki/Memory/Promoted/*.md, newest first."""
    base = Path(data_dir) / "profiles" / profile_name / "wiki" / "Memory" / "Promoted"
    memories = []
    if base.is_dir():
        for path in base.glob("*.md"):
            content = read_file(path)
            if not content:
                continue
            title = path.stem
            body = truncate_str(strip_frontmatter(content).strip(), LEARNED_KNOWLEDGE_MAX_ENTRY_CHARS)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            memories.append({"title": title, "body": body, "mtime": mtime})
    memories.sort(key=lambda m: m["mtime"], reverse=True)
    return memories


def render_learned_knowledge_block(memories):
    header = ("=== Learned Knowledge (promoted memories from prior threads - READ before acting; "
              "these are validated facts, do not re-derive them) ===")
    parts = [header]
    total = 0
    for m in memories:
        entry = f"- **{m['title']}**: {m['body']}"
        if total + len(entry) > LEARNED_KNOWLEDGE_MAX_TOTAL_CHARS and total > 0:
            break
        total += len(entry)
        parts.append(entry)
    return "\n".join(parts)


def build_learned_knowledge_block(data_dir, profile_name):
    """Rust build_learned_knowledge_block: never fails the prompt."""
    memories = load_promoted_memories(data_dir, profile_name)
    if not memories:
        return ("=== Learned Knowledge === (none yet - after completing this task, promote what "
                "you learned via memory_promote-to-memory so future threads benefit)")
    return render_learned_knowledge_block(memories)


# ---------------------------------------------------------------------------
# R8-K: interrupted-attempt warning
# ---------------------------------------------------------------------------

def count_interrupted_attempts(cursor, thread_id):
    row = get_thread_task_ref(cursor, thread_id)
    if not row or not row[0]:
        return None
    cursor.execute(
        "SELECT COUNT(*) FROM threads WHERE task_id = %s AND status = 'interrupted'",
        (row[0],),
    )
    return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Continuation self-orientation (Phase 3b): role block, prior step-threads,
# kanban history, resume ledger
# ---------------------------------------------------------------------------

def step_to_role(workflow_step):
    if workflow_step == "running":
        return "executor"
    if workflow_step == "testing":
        return "tester"
    if workflow_step == "review":
        return "reviewer"
    return None


def build_role_block(workflow_step):
    """Rust build_role_block: exact role instructions."""
    if workflow_step == "running":
        return ("You are the EXECUTOR of this workflow step: implement/execute the task described "
                "in the task description. Before acting, read the prior step-threads of this task "
                "listed below - resume from where the previous attempt ended, avoid repeating work "
                "that already succeeded, and fix the work if the last testing/review step failed or "
                "requested changes.")
    if workflow_step == "testing":
        return ("You are the TESTER of this workflow step: run the tests for the executed task (you "
                "may create automated tests), but you must NOT implement the task itself. Read the "
                "executor's thread and all recent threads of this task before testing.")
    if workflow_step == "review":
        return ("You are the REVIEWER of this workflow step: perform a comprehensive review of the "
                "execution AND the tests. You must NOT implement the task. Read the executor and "
                "tester threads plus all recent threads of this task. If everything passes: report "
                "a successful status with a normal summary. If you find issues: call the fail tool "
                "with workflow_step 'running', 'testing', or 'blocked' (never 'review') so the "
                "right role re-runs.")
    return None


def _mini_yaml_parse(text):
    """Minimal indentation-based YAML-subset parser for workflows.yml
    (nested maps of scalar values). Falls back to this when pyyaml is
    unavailable; the expected shape is
    workflows: {id}: roles: {role}: template: <name>."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        content = line.strip()
        key = None
        value = None
        if ":" in content:
            k, _, v = content.partition(":")
            key = k.strip().strip("\"'")
            value = v.strip().strip("\"'") if v.strip() else None
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value is None:
            nxt = {}
            parent[key] = nxt
            stack.append((indent, nxt))
        else:
            parent[key] = value
    return root


def load_template(data_dir, profile_name, template_name):
    """Rust memory_store::load_template: profiles/<p>/templates/<name>(.md)."""
    if not template_name:
        return None
    path = Path(data_dir) / "profiles" / profile_name / "templates" / template_name
    if path.suffix == "":
        path = path.with_suffix(".md")
    if not path.exists():
        return None
    content = read_file(path).strip()
    if not content:
        return None
    return content


def load_role_template(data_dir, profile_name, workflow_id, role):
    """Rust load_role_template: workflows.yml role template FILE NAME ->
    content from profiles/<p>/templates/<name>.md."""
    try:
        text = read_file(Path(data_dir) / "workflows.yml")
        if not text:
            return None
        parsed = _mini_yaml_parse(text)
        workflows = parsed.get("workflows", {}) if isinstance(parsed, dict) else {}
        wf = workflows.get(workflow_id, {}) if isinstance(workflows, dict) else {}
        roles = wf.get("roles", {}) if isinstance(wf, dict) else {}
        role_entry = roles.get(role, {}) if isinstance(roles, dict) else {}
        template_name = role_entry.get("template") if isinstance(role_entry, dict) else None
        if not template_name or not str(template_name).strip():
            return None
        return load_template(data_dir, profile_name, str(template_name).strip())
    except Exception as e:
        log.warning("load_role_template failed: %s", e)
        return None


def apply_workflow_mapping(system, user, user_message, workflow_step, template):
    """Rust apply_workflow_mapping: executor keeps task as USER + template as
    SYSTEM; tester/reviewer are INVERSE."""
    role = step_to_role(workflow_step)
    if role is None:
        return system, user
    if template is None:
        if workflow_step in ("testing", "review"):
            log.warning("workflow template required for %s step but missing in workflows.yml", workflow_step)
        return system, user
    if workflow_step == "running":
        system = system + f"\n\n## Workflow instructions ({role})\n{template}"
    elif workflow_step in ("testing", "review"):
        user = template
        description = user_message.strip()
        if description:
            system = system + f"\n\n## Task under {role} - context only, do not implement\n{description}"
    return system, user


def extract_tracking_path(body):
    """Rust extract_tracking_path: pull a data/tasks/<name> path out of a task body."""
    pos = body.find("data/tasks/")
    if pos == -1:
        return None
    start = pos
    head = body[:pos]
    for i in range(len(head) - 1, -1, -1):
        c = head[i]
        if c.isspace() or c in '`"\'([' ',':
            start = i + 1
            break
        if i == 0:
            start = 0
    after = body[pos + len("data/tasks/"):]
    end = len(after)
    for i, c in enumerate(after):
        if c.isspace() or c in '`"\'):],':
            end = i
            break
    if end == 0:
        return None
    return body[start:pos + len("data/tasks/")] + after[:end]


def last_message_info(cursor, thread_id):
    cursor.execute(
        "SELECT content, msg_type FROM messages WHERE thread_id = %s ORDER BY id DESC LIMIT 1",
        (thread_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return "<no messages>", None
    return row[0], row[1]


def build_continuation_block(cursor, thread_id):
    """Rust build_continuation_block: role block + prior step-threads + kanban
    history + resume ledger. None for plain threads; never fails the prompt."""
    row = get_thread_task_ref(cursor, thread_id)
    if not row:
        return None
    task_id, schedule_task_id = row
    blocks = []

    # Role instructions for the current workflow step.
    cursor.execute(
        "SELECT workflow_id, workflow_step FROM threads WHERE id = %s",
        (thread_id,),
    )
    step_row = cursor.fetchone()
    if step_row:
        wf_id, step = step_row
        if step:
            role_block = build_role_block(step)
            if role_block:
                blocks.append(role_block)

    # Prior step-threads of this task (chronological).
    prior = []
    if task_id:
        cursor.execute(
            "SELECT id, status, workflow_step FROM threads "
            "WHERE task_id = %s AND (task_type = 'kanban' OR task_type IS NULL) "
            "AND id != %s ORDER BY id DESC LIMIT 8",
            (task_id, thread_id),
        )
        prior.extend(cursor.fetchall())
    if schedule_task_id:
        cursor.execute(
            "SELECT id, status, workflow_step FROM threads "
            "WHERE schedule_task_id = %s AND id != %s ORDER BY id DESC LIMIT 8",
            (schedule_task_id, thread_id),
        )
        prior.extend(cursor.fetchall())
        cursor.execute(
            "SELECT id, status, workflow_step FROM threads "
            "WHERE task_id = %s AND task_type = 'cron' AND id != %s ORDER BY id DESC LIMIT 8",
            (schedule_task_id, thread_id),
        )
        prior.extend(cursor.fetchall())
    prior = sorted(prior, key=lambda t: t[0])
    if prior:
        parts = [
            "Prior step-threads of this task (thread, step, terminal status, last message) - "
            "resume from where the previous attempt ended; do not re-do completed work or "
            "repeat its mistakes:"
        ]
        for t in prior:
            t_id, status, step = t
            step_s = step or "-"
            content, msg_type = last_message_info(cursor, t_id)
            parts.append(
                f"thread {t_id} [step {step_s}] status {status} | "
                f"last message ({msg_type or 'text'}): {truncate_str(content, 180)}"
            )
        blocks.append("\n".join(parts))

    # Recent kanban history.
    if task_id:
        cursor.execute(
            """SELECT action, initial_board, final_board, comment,
                      TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI') AS created_at
               FROM kanban_history
               WHERE kanban_task_id = %s
               ORDER BY id DESC
               LIMIT 5""",
            (task_id,),
        )
        history = cursor.fetchall()
        if history:
            parts = ["Recent kanban history (why this task is being run again):"]
            for h in history:
                action, initial_board, final_board, comment, created_at = h
                suffix = ""
                if comment:
                    suffix = f' - "{truncate_str(comment, 120)}"'
                parts.append(
                    f"{initial_board or '?'} -> {final_board or '?'}: {action} ({created_at or '?'}){suffix}"
                )
            blocks.append("\n".join(parts))

    # Resume ledger referenced by the task body.
    if task_id:
        cursor.execute("SELECT body FROM kanban_tasks WHERE id = %s", (task_id,))
        body_row = cursor.fetchone()
        if body_row and body_row[0]:
            ledger = extract_tracking_path(body_row[0])
            if ledger:
                blocks.append(
                    "Task tracking file (resume ledger): read " + ledger + " first - it records "
                    "what has been done, verified, or failed across attempts of this task."
                )

    if not blocks:
        return None
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Cross-task channel context (Phase 3b-ext): other tasks' terminal threads
# ---------------------------------------------------------------------------

CROSS_TASK_MAX_ENTRIES = 3
CROSS_TASK_MAX_MESSAGE_CHARS = 400
CROSS_TASK_MAX_TITLE_CHARS = 60


def render_cross_task_block(own_task_id, own_schedule_task_id, rows):
    """Rust render_cross_task_block: drop own-task threads, newest first,
    capped at 3."""
    def excludes(other_task_id):
        return other_task_id == own_task_id or other_task_id == own_schedule_task_id

    entries = [r for r in rows if r[1] is not None and not excludes(r[1])]
    entries.sort(key=lambda r: r[0], reverse=True)
    entries = entries[:CROSS_TASK_MAX_ENTRIES]
    if not entries:
        return None
    lines = [
        "Recent threads from other tasks on this channel (background context from sibling tasks, "
        "not your own history): a prior phase on this channel often already solved the exact "
        "problem you are investigating - trust its final result instead of re-deriving it."
    ]
    for r in entries:
        t_id, t_task_id, t_title, t_step, t_status, t_content, t_msg_type = r
        if t_task_id and t_title:
            task_label = f'task {t_task_id} "{truncate_str(t_title, CROSS_TASK_MAX_TITLE_CHARS)}"'
        elif t_task_id:
            task_label = f"task {t_task_id}"
        else:
            task_label = "plain thread"
        step = t_step or "-"
        msg_type = t_msg_type or "text"
        content = t_content or "<no messages>"
        lines.append(
            f"- thread {t_id} | {task_label} | step {step} | status {t_status} | "
            f"last message ({msg_type}): {truncate_str(content, CROSS_TASK_MAX_MESSAGE_CHARS)}"
        )
    return "\n".join(lines)


def build_cross_task_block(cursor, thread_id):
    """Rust build_cross_task_block: same channel, terminal threads of OTHER
    tasks; None for plain threads / no matches."""
    row = get_thread_task_ref(cursor, thread_id)
    if not row:
        return None
    task_id, schedule_task_id = row
    if not task_id and not schedule_task_id:
        return None
    cursor.execute(
        "SELECT channel_id FROM threads WHERE id = %s",
        (thread_id,),
    )
    cid_row = cursor.fetchone()
    if not cid_row or not cid_row[0]:
        return None
    channel_id = cid_row[0]

    cursor.execute(
        """
        SELECT t.id,
               t.task_id,
               k.title        AS task_title,
               t.workflow_step,
               t.status,
               m.content      AS last_content,
               m.msg_type     AS last_msg_type
        FROM threads t
        LEFT JOIN kanban_tasks k ON k.id = t.task_id
        LEFT JOIN LATERAL (
            SELECT content, msg_type
            FROM messages
            WHERE thread_id = t.id
            ORDER BY thread_sequence DESC NULLS LAST,
                     iteration_number DESC NULLS LAST,
                     id DESC
            LIMIT 1
        ) m ON true
        WHERE t.channel_id = %s
          AND t.id != %s
          AND t.task_id IS NOT NULL
          AND (NULLIF(%s::text, '') IS NULL OR t.task_id != NULLIF(%s::text, '')::text)
          AND (NULLIF(%s::text, '') IS NULL OR t.task_id != NULLIF(%s::text, '')::text)
          AND t.status IN ('completed', 'review', 'failed', 'interrupted', 'skipped')
        ORDER BY t.id DESC
        LIMIT %s
        """,
        (channel_id, thread_id,
         task_id or "", task_id or "",
         schedule_task_id or "", schedule_task_id or "",
         CROSS_TASK_MAX_ENTRIES),
    )
    rows = cursor.fetchall()
    return render_cross_task_block(task_id, schedule_task_id, rows)

# ---------------------------------------------------------------------------
# Tool: prompt_generate (mirror Rust handle_generate_full)
# ---------------------------------------------------------------------------

def handle_generate(req_id, arguments, meta):
    """Generate the full LLM prompt as JSON: system/memory/context/user/plan."""
    try:
        args = arguments or {}
        profile_name = args.get("profile_name") or (meta or {}).get("profile_name") or "omni"
        platform = args.get("platform") or (meta or {}).get("platform") or ""
        system_message = args.get("system_message")
        user_message = args.get("user_message") or ""
        tool_names = args.get("tool_names") or []
        if not isinstance(tool_names, list):
            tool_names = []
        tool_names = [str(t) for t in tool_names]
        thread_id = args.get("thread_id")
        if thread_id is None and meta:
            thread_id = meta.get("thread_id")
        channel_id = args.get("channel_id")
        if channel_id is None and meta:
            channel_id = meta.get("channel_name") or meta.get("channel_id")

        cfg = get_config()
        data_dir = cfg_env("omni_dir") or os.environ.get("OMNI_DIR") or _fail_omni_dir()

        # 1. System prompt parts (identity + guidance + profile + optional
        # system message + platform hint) and memory section (MEMORY.md).
        memory_raw = load_memory_raw(data_dir, profile_name)
        parts = []
        parts.append(build_dynamic_identity(tool_names))
        parts.append(TOOL_GUIDANCE)
        parts.append(f"Active Hermes profile: {profile_name}.")
        if system_message:
            parts.append(system_message)
        hint = PLATFORM_HINTS.get(platform)
        if hint:
            parts.append(hint)
        memory_section = build_memory_section(memory_raw, cfg.get("memory_max_chars", 5000))
        if memory_section:
            parts.append(memory_section)

        # Split into system (non-memory) vs memory (parts starting with
        # "## MEMORY"), exactly like the Rust split loop.
        system_parts = []
        memory_text = ""
        for part in parts:
            if part.startswith("## MEMORY"):
                memory_text += part + "\n"
            else:
                system_parts.append(part)
        system = "\n\n".join(system_parts)
        memory = memory_text.strip()

        # 2. Context blocks.
        context_blocks = []
        db = get_db()
        cursor = db.cursor()

        # 2a. Recent thread messages
        if thread_id is not None:
            try:
                tid = int(thread_id)
                msgs = get_thread_messages(cursor, tid, 10)
                if msgs:
                    formatted = [f"[{m[2]}]: {truncate_str(m[3], 500)}" for m in msgs]
                    context_blocks.append(
                        "Recent conversation history (current thread):\n" + "\n".join(formatted)
                    )
            except Exception as e:
                log.warning("Failed to get thread messages: %s", e)

        # 2b. Latest summary and threads since
        if channel_id is not None:
            try:
                summary = get_latest_summary(cursor, str(channel_id))
                if summary:
                    context_blocks.append(
                        f"Previous channel summary (covers threads up to id={summary[2]}):\n"
                        f"{truncate_str(summary[3], 4000)}"
                    )
                    threads = get_threads_since(cursor, str(channel_id), summary[2], 5)
                    if threads:
                        thread_info = [f"[Thread #{t[0]} by {t[2]}]: completed" for t in threads]
                        context_blocks.append(
                            "Recent threads (after last summary):\n" + "\n---\n".join(thread_info)
                        )
            except Exception as e:
                log.warning("Failed to get summary: %s", e)

        # 2c. Skills
        skills = get_skills(data_dir, profile_name)
        if skills:
            context_blocks.append(
                "Available skills (read one with view_skill before acting when it matches the task):\n"
                + "\n".join(skills)
                + "\n\nAfter solving a non-trivial, repeatable task (3+ tool calls, reusable "
                  "procedure), create a skill with create_skill so future threads reuse it."
            )

        # 2c-ext2. Previous attempts of the SAME task (R8-J)
        if thread_id is not None:
            try:
                prior = build_prior_attempts_block(cursor, int(thread_id))
                if prior:
                    context_blocks.append(prior)
            except Exception as e:
                log.warning("Prior attempts context unavailable: %s", e)

        # 2c-ext3. Learned Knowledge (R8-K)
        try:
            learned = build_learned_knowledge_block(data_dir, profile_name)
            if learned:
                context_blocks.append(learned)
        except Exception as e:
            log.warning("Learned knowledge context unavailable: %s", e)

        # 2d. Subtasks
        if thread_id is not None:
            try:
                subtask_rows = get_subtasks(cursor, int(thread_id))
                if subtask_rows:
                    lines = [f"## Subtasks (Thread #{thread_id})"]
                    for i, s in enumerate(subtask_rows):
                        icon = {"completed": "✅", "cancelled": "❌", "error": "⚠️"}.get(s[2], "⬜")
                        lines.append(f"{i + 1}. {icon} {s[1]}")
                    context_blocks.append("\n".join(lines))
            except Exception as e:
                log.warning("Failed to get subtasks: %s", e)

        # 2e. Continuation self-orientation
        if thread_id is not None:
            try:
                cont = build_continuation_block(cursor, int(thread_id))
                if cont:
                    context_blocks.append(cont)
            except Exception as e:
                log.warning("continuation context unavailable: %s", e)

        # 2e-ext0. Interrupted-attempt warning
        if thread_id is not None:
            try:
                n = count_interrupted_attempts(cursor, int(thread_id))
                if n is not None and n > 0:
                    context_blocks.append(
                        f"WARNING: {n} prior attempt(s) of this task were INTERRUPTED (iteration "
                        "limit). Read their summaries above. If you are about to do what they "
                        "did, you are repeating a mistake."
                    )
            except Exception as e:
                log.warning("interrupted-attempt count unavailable: %s", e)

        # 2e-ext. Cross-task channel context
        if thread_id is not None:
            try:
                cross = build_cross_task_block(cursor, int(thread_id))
                if cross:
                    context_blocks.append(cross)
            except Exception as e:
                log.warning("cross-task context unavailable: %s", e)

        context = "\n\n---\n\n".join(context_blocks)
        user = user_message

        # 2f. Workflow step prompt mapping (inverse for tester/reviewer)
        if thread_id is not None:
            try:
                cursor.execute(
                    "SELECT workflow_id, workflow_step FROM threads WHERE id = %s",
                    (int(thread_id),),
                )
                wf = cursor.fetchone()
                if wf and wf[0] and wf[1]:
                    role = step_to_role(wf[1])
                    template = None
                    if role:
                        template = load_role_template(data_dir, profile_name, wf[0], role)
                    system, user = apply_workflow_mapping(
                        system, user, user_message, wf[1], template
                    )
            except Exception as e:
                log.warning("workflow step lookup unavailable: %s", e)

        cursor.close()

        # Plan resolution (mirrors Rust): true=plan, false=no plan,
        # null/absent = let plugin-level config decide.
        plan_input = args.get("plan")
        if plan_input is not None:
            plan = bool(plan_input)
        else:
            max_chars = cfg.get("planning_complexity_max_chars", 60)
            keywords_str = cfg.get("planning_complexity_keywords", "")
            keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
            lower_user = user_message.lower()
            has_keyword = False
            if keywords:
                has_keyword = any(k in lower_user for k in keywords)
            plan = len(user_message) > max_chars or has_keyword

        result = json.dumps({
            "system": system,
            "memory": memory,
            "context": context,
            "user": user,
            "plan": plan,
        }, indent=2, ensure_ascii=False)

        send_json(make_success(req_id, make_tool_result(result)))

    except Exception as e:
        log.error("generate tool failed: %s", e, exc_info=True)
        send_json(make_success(req_id, make_tool_result(f"Error: {e}", True)))


# ---------------------------------------------------------------------------
# Token budget measurement (mirror Rust measure_size: chars/4 fallback, real
# BPE when tokenizer_encoding configured and tiktoken available)
# ---------------------------------------------------------------------------

def serialize_messages_rust(items):
    """Serialize ChatMessage dicts exactly like Rust serde_json::to_string:
    field order role, content, tool_call_id?, tool_calls?, name?; compact,
    unicode raw (ensure_ascii=False)."""
    out = []
    for m in items:
        d = {"role": m.get("role", ""), "content": m.get("content", "")}
        if m.get("tool_call_id") is not None:
            d["tool_call_id"] = m["tool_call_id"]
        if m.get("tool_calls") is not None:
            calls = []
            for tc in m["tool_calls"]:
                calls.append({
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    },
                })
            d["tool_calls"] = calls
        if m.get("name") is not None:
            d["name"] = m["name"]
        out.append(d)
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def measure_size(messages, tokenizer_encoding):
    """Rust measure_size: chars/4 fallback, tiktoken BPE when configured."""
    chars = 0
    for m in messages:
        chars += len(m.get("content", ""))
        calls = m.get("tool_calls") or []
        for tc in calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    if not tokenizer_encoding:
        return chars // 4
    if tiktoken is None:
        return chars // 4
    try:
        text = serialize_messages_rust(messages)
        enc = None
        try:
            enc = tiktoken.encoding_for_model(tokenizer_encoding)
        except Exception:
            enc = tiktoken.get_encoding(tokenizer_encoding)
        return len(enc.encode(text, disallowed_special=()))
    except Exception as e:
        log.warning("[prompt] Failed to load BPE encoding '%s': %s: falling back to chars/4",
                    tokenizer_encoding, e)
        return chars // 4


# ---------------------------------------------------------------------------
# Compaction (mirror compact.rs: frozen summary block + dump.rs + notes.rs)
# ---------------------------------------------------------------------------

COMPACTION_SUMMARY_MARKER = "=== Compaction Summary ==="

READ_TOOL_PREFIXES = (
    "filesystem_read",
    "filesystem_list",
    "filesystem_search",
    "filesystem_info",
    "search_database",
    "search_messages",
    "search_wiki",
    "skills_view",
    "git_status",
    "git_run-command",
)


def is_read_type_tool(name):
    return any(name.startswith(p) for p in READ_TOOL_PREFIXES)


# --- dump.rs: context-<iter>.json digests ---

DUMP_MAX_BYTES = 200 * 1024
DUMP_MAX_FILES = 3
DIGEST_HEAD_CHARS = 400
# Process-lifetime dedupe set (mirrors Rust static Mutex<HashSet>).
_APPENDED = set()


def _stable_hash(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def append_dump(dir_path, iter_num, tool, args, content):
    """Rust append_dump: JSON-lines digest, deduped by (file, tool+args)."""
    if tool == "" and content == "":
        return False
    try:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    file_path = str(Path(dir_path) / f"context-{iter_num}.json")
    dedupe_key = (file_path, _stable_hash(f"{tool}\u0000{args}"))
    if dedupe_key in _APPENDED:
        return False

    chars = len(content)
    head = content[:DIGEST_HEAD_CHARS]
    tail = content[-DIGEST_HEAD_CHARS:] if chars > DIGEST_HEAD_CHARS else content
    args_s = args if args.strip() else f"[content:{_stable_hash(content)}]"
    line = json.dumps({
        "tool": tool,
        "args": args_s,
        "chars": chars,
        "head": head,
        "tail": tail,
    }, ensure_ascii=False)

    try:
        with open(file_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        return False
    _APPENDED.add(dedupe_key)
    enforce_caps(dir_path, iter_num)
    return True


def enforce_caps(dir_path, iter_num):
    """Rust enforce_caps: per-file 200KB (keep newest lines) + keep last 3
    context-*.json files."""
    file_path = Path(dir_path) / f"context-{iter_num}.json"
    try:
        if file_path.stat().st_size > DUMP_MAX_BYTES:
            content = read_file(file_path)
            lines = content.splitlines()
            kept = []
            bytes_used = 0
            for l in reversed(lines):
                lb = len(l.encode("utf-8")) + 1
                if bytes_used + lb > DUMP_MAX_BYTES:
                    break
                bytes_used += lb
                kept.append(l)
            kept.reverse()
            with open(file_path, "w") as f:
                f.write("\n".join(kept) + "\n")
    except OSError:
        pass

    files = []
    try:
        for e in Path(dir_path).iterdir():
            name = e.name
            if name.startswith("context-") and name.endswith(".json"):
                rest = name[len("context-"):-len(".json")]
                if rest.isdigit():
                    files.append((name, int(rest)))
    except OSError:
        return
    if len(files) > DUMP_MAX_FILES:
        files.sort(key=lambda x: x[1])
        for name, _ in files[:len(files) - DUMP_MAX_FILES]:
            try:
                Path(dir_path, name).unlink()
            except OSError:
                pass


def note_append(dir_path, name, content):
    """Rust notes::note_append: append content.trim_end() + '\\n'."""
    try:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        with open(Path(dir_path) / name, "a") as f:
            if content.strip():
                f.write(content.rstrip() + "\n")
    except OSError:
        pass


def prune_summary_block(content, max_chars):
    """Rust prune_summary_block: bound the frozen block; newest entries kept,
    oldest dropped; header preserved byte-for-byte."""
    if len(content) <= max_chars:
        return content
    idx = content.find("\n- [")
    header_end = idx + 1 if idx != -1 else 0
    header = content[:header_end]
    entries_text = content[header_end:]
    budget = max(0, max_chars - len(header) - 64)

    entry_list = []
    cur = ""
    for line in entries_text.splitlines():
        if line.startswith("- [") and cur:
            entry_list.append(cur)
            cur = line
        elif cur:
            cur = cur + "\n" + line
        else:
            cur = line
    if cur:
        entry_list.append(cur)

    kept = []
    used = 0
    for entry in reversed(entry_list):
        cost = len(entry) + 1
        if used + cost > budget:
            break
        used += cost
        kept.append(entry)
    kept.reverse()
    if not kept:
        kept = [entry_list[-1]] if entry_list else []
    return header + "\n".join(kept) + "\n[older entries pruned — see context-*.json dumps]"


def compact_old_assistant_messages(messages, keep_recent, thread_dir, current_iteration, settings):
    """Rust compact::compact_old_assistant_messages: drain the oldest
    tool-call turns into ONE frozen summary block. Returns
    (removed, dump_file, dump_entries). Null-contract: no drain when
    tool-call turns <= keep_recent (returns zeroes, list untouched)."""
    removed = 0
    dump_file = None
    dump_entries = 0

    summary_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "system" and m.get("content", "").startswith(COMPACTION_SUMMARY_MARKER):
            summary_idx = i
            break

    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("tool_calls") is not None
    ]

    if len(tool_indices) <= keep_recent:
        return removed, dump_file, dump_entries

    n_drain = len(tool_indices) - keep_recent
    drain_start = tool_indices[0]
    drain_end = tool_indices[n_drain - 1] + 1
    while drain_end < len(messages) and messages[drain_end].get("role") == "tool":
        drain_end += 1

    entries = []
    i = drain_start
    while i < drain_end:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls") is not None:
            calls = m.get("tool_calls") or []
            names = [tc.get("function", {}).get("name", "") for tc in calls]

            tool_end = i + 1
            while tool_end < drain_end and messages[tool_end].get("role") == "tool":
                tool_end += 1
            tool_count = tool_end - i - 1

            # WS-2: durable dump + auto-notes of the tool results about to
            # be drained.
            if thread_dir:
                for tm in messages[i + 1:tool_end]:
                    if tm.get("role") == "tool" and tm.get("content"):
                        tool_name = tm.get("name") or ""
                        args = ""
                        for tc in calls:
                            if tc.get("function", {}).get("name") == tool_name:
                                args = tc.get("function", {}).get("arguments", "")
                                break
                        if append_dump(thread_dir, current_iteration, tool_name, args, tm.get("content", "")):
                            dump_entries += 1
                        if is_read_type_tool(tool_name):
                            note_append(
                                thread_dir,
                                "auto-notes.md",
                                f"## [engine:auto-note {tool_name}]\n"
                                f"{tm['content'][:settings['read_excerpt_chars']]}\n",
                            )
                if tool_count > 0:
                    dump_file = f"context-{current_iteration}.json"

            # Excerpt for the summary entry (same caps).
            excerpt = ""
            total_excerpt = 0
            for tm in messages[i + 1:tool_end]:
                tool_name = tm.get("name") or ""
                is_read = is_read_type_tool(tool_name)
                excerpt_chars = settings["read_excerpt_chars"] if is_read else settings["tool_excerpt_chars"]
                content_preview = tm.get("content", "")[:excerpt_chars]
                chunk_len = len(content_preview)
                if total_excerpt + chunk_len > settings["total_excerpt_cap"]:
                    break
                total_excerpt += chunk_len
                excerpt += content_preview + "\n"

            if excerpt.strip() == "":
                entries.append(
                    f"- [iter {current_iteration}] {', '.join(names)} → (results drained)"
                )
            else:
                entries.append(
                    f"- [iter {current_iteration}] {', '.join(names)} →\n{excerpt.rstrip()}"
                )
            removed += tool_count
            i = tool_end
        elif m.get("role") == "system" and m.get("content", "").startswith(COMPACTION_SUMMARY_MARKER):
            # Defensive: never fold the summary block into itself.
            i += 1
        else:
            # Non-tool-call message inside the drained span: preserve a
            # preview so the information is not silently lost.
            total = len(m.get("content", ""))
            preview = m.get("content", "")[:800]
            suffix = "…" if total > 800 else ""
            entries.append(f"- [{m.get('role', '')}] {preview}{suffix}")
            i += 1

    joined_entries = "\n".join(entries)
    if summary_idx is not None:
        messages[summary_idx]["content"] = messages[summary_idx]["content"] + "\n" + joined_entries
        messages[summary_idx]["content"] = prune_summary_block(
            messages[summary_idx]["content"], settings["max_summary_chars"]
        )
        del messages[drain_start:drain_end]
    else:
        block_content = (
            f"{COMPACTION_SUMMARY_MARKER}\n"
            "Frozen prefix block: older conversation turns were compacted into this summary, "
            "oldest first. Everything before this block is the fixed preamble; everything "
            "after is the live conversation. Recover destroyed read results from auto-notes.md "
            "/ context-*.json dumps.\n"
            + joined_entries
        )
        block_content = prune_summary_block(block_content, settings["max_summary_chars"])
        block = {"role": "system", "content": block_content}
        messages.insert(drain_start, block)
        del messages[drain_start + 1:drain_end + 1]

    return removed, dump_file, dump_entries


def handle_compact_messages(req_id, arguments):
    """Rust handle_compact_messages: budget-gated, progressive passes, frozen
    summary block; returns the compacted array or null."""
    try:
        args = arguments or {}
        messages_arr = args.get("messages")
        if not isinstance(messages_arr, list):
            send_json(make_success(req_id, make_tool_result(
                "Missing required argument: 'messages' (array of ChatMessage)", True)))
            return

        cfg = get_config()
        keep_recent = args.get("keep_recent")
        keep_recent = int(keep_recent) if keep_recent is not None else cfg.get("compact_keep_recent", 3)

        # Parse into ChatMessage dicts (mirror serde parse; missing role or
        # content is a parse error).
        messages = []
        try:
            for m in messages_arr:
                if not isinstance(m, dict):
                    raise ValueError("message is not an object")
                if "role" not in m or "content" not in m:
                    raise ValueError("missing field `role` or `content`")
                parsed = {"role": str(m["role"]), "content": str(m["content"])}
                for key in ("tool_call_id", "name"):
                    if m.get(key) is not None:
                        parsed[key] = m[key]
                if m.get("tool_calls") is not None:
                    calls = []
                    for tc in m["tool_calls"]:
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        calls.append({
                            "id": tc.get("id", "") if isinstance(tc, dict) else "",
                            "type": tc.get("type", "function") if isinstance(tc, dict) else "function",
                            "function": {
                                "name": fn.get("name", "") if isinstance(fn, dict) else "",
                                "arguments": fn.get("arguments", "") if isinstance(fn, dict) else "",
                            },
                        })
                    parsed["tool_calls"] = calls
                messages.append(parsed)
        except Exception as e:
            send_json(make_success(req_id, make_tool_result(f"Failed to parse messages: {e}", True)))
            return

        hard_budget = args.get("hard_budget")
        if hard_budget is None:
            send_json(make_success(req_id, make_tool_result(
                "Missing required argument: 'hard_budget' (token budget; the omniagent passes the "
                "resolved per-thread hard token budget)", True)))
            return
        hard_budget = int(hard_budget)
        soft_budget = args.get("soft_budget")
        if soft_budget is None:
            send_json(make_success(req_id, make_tool_result(
                "Missing required argument: 'soft_budget' (token budget; the omniagent passes the "
                "resolved per-thread soft token budget)", True)))
            return
        soft_budget = int(soft_budget)

        thread_dir = args.get("thread_dir")
        if thread_dir is not None:
            thread_dir = str(thread_dir)
        current_iteration = int(args.get("current_iteration") or 0)

        tokenizer_encoding = cfg.get("tokenizer_encoding", "")
        before = len(messages)
        dump_file = None
        entries = 0

        current_size = measure_size(messages, tokenizer_encoding)
        if current_size > hard_budget:
            settings = {
                "tool_excerpt_chars": cfg.get("tool_excerpt_chars", 800),
                "total_excerpt_cap": cfg.get("total_excerpt_cap", 4000),
                "read_excerpt_chars": cfg.get("read_excerpt_chars", 2000),
                "max_summary_chars": cfg.get("max_summary_chars", 50000),
            }
            compact_max_passes = cfg.get("compact_max_passes", 3)
            compact_keep_step = cfg.get("compact_keep_step", 1)
            keep = keep_recent
            for pass_num in range(compact_max_passes):
                removed, df, de = compact_old_assistant_messages(
                    messages, keep, thread_dir, current_iteration, settings
                )
                if df:
                    dump_file = df
                entries += de
                after_size = measure_size(messages, tokenizer_encoding)
                if after_size <= soft_budget or keep == 0:
                    break
                if pass_num + 1 == compact_max_passes:
                    break
                keep = max(0, keep - compact_keep_step)

        after = len(messages)

        result = {
            "messages": [
                {
                    "role": m.get("role", ""),
                    "content": m.get("content", ""),
                    **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") is not None else {}),
                    **({"tool_calls": m["tool_calls"]} if m.get("tool_calls") is not None else {}),
                    **({"name": m["name"]} if m.get("name") is not None else {}),
                }
                for m in messages
            ] if before != after else None,
            "was_compacted": before != after,
            "iteration": current_iteration,
            "dump_file": dump_file,
            "entries": entries,
            "before_count": before,
            "after_count": after,
        }
        text = json.dumps(result, indent=2, ensure_ascii=False)
        send_json(make_success(req_id, make_tool_result(text)))

    except Exception as e:
        log.error("compact-messages tool failed: %s", e, exc_info=True)
        send_json(make_success(req_id, make_tool_result(f"Error: {e}", True)))

# ---------------------------------------------------------------------------
# MCP lifecycle (mirror the Rust server: same tool names, descriptions,
# input schemas, and serverInfo)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "prompt_generate",
        "description": (
            "Generate the complete LLM prompt for a conversation, including system prompt "
            "(identity, tool guidance, memory, user profile), thread context (recent messages, "
            "summaries, skills, subtasks), and optional planning instructions. Returns the full "
            "prompt as a JSON string. This is the single source of truth for prompt building: "
            "no other prompt assembly is needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile_name": {
                    "type": "string",
                    "description": "Profile name (default: omni)"
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
                    "type": "string",
                    "description": "Channel name for context assembly (summaries)"
                },
                "plan": {
                    "type": "boolean",
                    "description": "Plan mode suggestion: true=plan, false=no plan, null=let plugin decide based on config"
                }
            },
            "required": []
        }
    },
    {
        "name": "prompt_compact-messages",
        "description": (
            "Compact and prune a conversation to stay within token budgets. REQUIRES "
            "'soft_budget'/'hard_budget' token params (resolved per-thread by the "
            "omniagent; chars/4 fallback when no tokenizer). Compacts ONLY when the "
            "hard budget is exceeded (null-contract otherwise): drains the oldest "
            "tool-call turns into ONE frozen '=== Compaction Summary ===' block right "
            "after the system prompt, keeps recent turns verbatim, excerpts older "
            "read-type results and auto-notes them (auto-notes.md in thread_dir). The "
            "returned messages array must keep the prefix byte-stable; only the tail "
            "may change. Returns the compacted message array or null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "Array of ChatMessage objects to compact"
                },
                "keep_recent": {
                    "type": "integer",
                    "description": "Number of most recent messages to always keep (default: 3)"
                },
                "soft_budget": {
                    "type": "integer",
                    "description": "Required soft token budget: the reduction target when the hard budget is exceeded"
                },
                "hard_budget": {
                    "type": "integer",
                    "description": "Required hard token budget: compaction/pruning triggers when the context exceeds it"
                }
            },
            "required": ["messages", "soft_budget", "hard_budget"]
        }
    },
]


def handle_initialize(req_id):
    result = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "mcp-server-prompt", "version": "0.1.0"},
    }
    send_json(make_success(req_id, result))
    log.info("Initialized: mcp-server-prompt (python) v0.1.0")


def handle_tools_list(req_id):
    send_json(make_success(req_id, {"tools": TOOLS}))
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

        elif method == "configure":
            # The builtin receives plugin config via run_server_with_config's
            # configure callback; accept the same message when the framework
            # sends it to python servers too.
            if isinstance(params, dict):
                for k, v in params.items():
                    if v is None:
                        continue
                    if isinstance(v, (int, float, bool)):
                        CONFIG_OVERRIDES[k] = str(v)
                    elif isinstance(v, str):
                        CONFIG_OVERRIDES[k] = v
                log.info("configure received: %d keys", len(CONFIG_OVERRIDES))
            if req_id is not None:
                send_json(make_success(req_id, {"configured": True}))

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

                if tool_name == "prompt_generate":
                    handle_generate(req_id, arguments, meta)
                elif tool_name == "prompt_compact-messages":
                    handle_compact_messages(req_id, arguments)
                else:
                    send_json(make_error(req_id, -32602, f"Unknown tool: {tool_name}"))

        else:
            log.warning("Unknown method: %s", method)
            if req_id is not None:
                send_json(make_error(req_id, -32601, f"Method not found: {method}"))

    log.info("prompt-python MCP server shutting down (stdin closed)")


if __name__ == "__main__":
    main()
