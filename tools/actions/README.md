# actions (python MCP plugin)

Python port of the omniagent built-in `plugins/tools/actions` Rust plugin.
Provides the 3 action tools (the `kanban_dispatcher` tool moved into the
omniagent core, see the core-dispatcher task):

| Registered tool name (core)   | MCP tool name         | Purpose |
|-------------------------------|-----------------------|---------|
| `actions_hindsight-populator` | `hindsight_populator` | Retain recent messages into Hindsight memory (advances `{OMNI_DIR}/hindsight_watermark.json`) |
| `actions_relevance-indexer`   | `relevance_indexer`   | Update `{OMNI_DIR}/profiles/omni/wiki/relevant-index.md` by mtime recency |
| `actions_setup-knowledge-pipeline` | `setup_knowledge_pipeline` | Create the `knowledge_pipeline` schedule in `{OMNI_DIR}/config/tasks.yml` (idempotent) |

The core registers tools as `<plugin-name>_<tool-name>` with underscores
converted to dashes, so the tool names above match the `tool_name` values in
omni-stack `config/actions.yml` without any changes.

## Structure

- `plugin.json` - plugin metadata + config schema (`database_url`,
  `omni_dir`, `$env:` defaults resolved at install).
- `mcp-config.json` - stdio server config (command `python3 server.py`,
  env `OMNI_DIR`/`DATABASE_URL`).
- `server.py` - MCP JSON-RPC over stdio, mirroring `tools/memory/server.py`.

## Behavior parity with the Rust plugin

- `hindsight_populator`: SELECT `id FROM messages WHERE id > <watermark> AND
  msg_type IN ('message','reasoning','plan','error','cause','tool',
  'tool-result') AND COALESCE(content,'') != '' ORDER BY id ASC LIMIT 200`;
  writes back `{"last_message_id": <max id>, "last_run_at": <rfc3339>}`.
- `relevance_indexer`: recursive `.md` scan of
  `{OMNI_DIR}/profiles/omni/wiki` (excluding `relevant-index.md`), recency
  buckets 50/40/30/10 (<1h / <24h / <7d / else), top 30 entries, ≤1000 chars.
- `setup_knowledge_pipeline`: idempotent insert of the `knowledge_pipeline`
  schedule (`enabled: true, channel: cron-default, profile: pipeline,
  plan: true, cron: <schedule|0 */6 * * *>, prompt: <prompt>, skills:
  '["knowledge-pipeline"]', silent: false, display_name: Knowledge Pipeline`)
  under the top-level `schedules:` key of `{OMNI_DIR}/config/tasks.yml`;
  preserves the rest of the file (text insert + atomic replace).

Requires `psycopg2` (DB access) and optionally `yaml` (idempotency check);
both are available in the omniagent runtime container.
