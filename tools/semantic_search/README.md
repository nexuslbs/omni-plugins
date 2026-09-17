# semantic_search (MCP tool plugin)

Local **semantic search over the profile wiki AND the channel messages**,
backed by a Qdrant vector store. The plugin computes every embedding itself
with a **LOCAL vectorizer** - no LLM, no embedding API, no external/paid
service is ever contacted. The only network endpoints it talks to are the
configured Qdrant server and (for the messages tools only) the omniagent
Postgres database, which is **read with SELECT statements only - the plugin
never writes to it**.

This replaces the wiki vector search that used to live inside omniagent core
(audit item V-7). Core now has **zero** knowledge of semantic search or Qdrant
(`git grep -i qdrant` in the omniagent repo returns no matches); the capability
ships entirely as this plugin.

## Tools

| Tool | Purpose |
|------|---------|
| `semantic_search` | Ranked semantic hits for a natural-language `query` (optional `limit`, `path_filter`). Each hit carries the wiki path, markdown heading, line range, score and a text snippet. |
| `semantic_search_index` | Build/refresh the index. Idempotent: re-running over an unchanged corpus keeps the same point count; points of deleted files (or of chunks that no longer exist) are removed. Optional `paths`, `globs`, `rebuild`. |
| `semantic_search_stats` | Index status: Qdrant URL, collection, point count, vector size, active local backend, corpus roots/globs. |
| `semantic_search__messages` | Ranked semantic hits over channel messages for a natural-language `query`. Each hit carries the message id, channel, thread, role (author), timestamp and a content snippet. Optional filters: `channel_id`, `thread_id`, `since`/`until` (ISO-8601 or epoch seconds), `min_score`, `limit`. |
| `semantic_search_messages_index` | Build/refresh the messages index from the omniagent DB. Read-only SELECTs, idempotent upserts; a FULL run prunes stale points (deleted messages/threads), scoped runs (`channels`/`since`/`until`/`limit`) are upsert-only. Optional `rebuild`. |
| `semantic_search_messages_stats` | Messages index status: Qdrant URL, collection, point count, vector size, backend, excluded msg types, max chars per message. |

CLI entrypoints (dev/deploy/CI, non-interactive):

```bash
python3 server.py index [--rebuild]
python3 server.py search "<query>" [--limit 10] [--path-filter profiles/omni/wiki]
python3 server.py stats
python3 server.py messages-search "<query>" [--limit 10] [--channel omnidev] [--thread 1234] [--since 2026-09-01T00:00:00Z] [--until 2026-09-17T00:00:00Z]
python3 server.py messages-index [--channels omnidev,main] [--since ISO] [--until ISO] [--limit N] [--rebuild]
python3 server.py messages-stats
```

## Local vectorizer (no LLM calls)

`embedding_backend` selects the local backend:

| Backend | Dim | Size / CPU | Notes |
|---------|-----|-----------|-------|
| `hash` (default) | 384 | 0 bytes downloaded, ~1 ms/chunk | Deterministic feature hashing (word unigrams + character trigrams, blake2b, sublinear weighting, L2-normalized), Python stdlib only, fully offline, reproducible across processes/machines. |
| `fastembed` (optional) | 384 | ~50 MB onnxruntime + ~90 MB model, ~10-30 ms/chunk CPU | In-process ONNX sentence embeddings (`BAAI/bge-small-en-v1.5` by default). Fully local inference; requires `pip install fastembed` (see `requirements.txt`). |

Both backends are 384-dimensional, so both fit the same collection shape;
vectors from different backends are not comparable - re-run
`semantic_search_index` after switching. No backend ever calls an LLM or an
embedding provider: `semantic_search_index` reports
`LOCAL vectorizer: backend=... (no LLM/embedding API called)` and the test
suite asserts that every outbound HTTP request during index/search goes to the
configured Qdrant base URL.

## Configuration

Plugin config keys (injected by the framework as environment variables; the
`server.py` also accepts them from the process environment for standalone
runs):

| Key | Default | Meaning |
|-----|---------|---------|
| `qdrant_url` | `$env:QDRANT_URL` (fallback `http://qdrant:6333`) | Qdrant HTTP API base URL |
| `qdrant_api_key` | `$env:QDRANT_API_KEY` | optional `api-key` header |
| `collection` | `wiki_semantic` | Qdrant collection |
| `corpus_roots` | `profiles/{profile}/wiki` | comma-separated roots, absolute or relative to `OMNI_DIR`; `{profile}` resolves to the asking profile |
| `corpus_globs` | `**/*.md` | comma-separated globs inside every root |
| `embedding_backend` | `hash` | `hash` or `fastembed` |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | fastembed model (384 dims) |
| `chunk_size` / `chunk_overlap` | `1200` / `200` | markdown chunking (characters) |
| `batch_size` | `64` | points per upsert batch |
| `timeout_secs` | `20` | Qdrant HTTP timeout |
| `default_limit` | `10` | default `semantic_search` hit count |
| `database_url` | `$env:DATABASE_URL` | Postgres URL of the omniagent DB (messages tools; SELECT-only) |
| `messages_collection` | `messages_semantic` | Qdrant collection for the messages index |
| `exclude_msg_types` | `tool-result,multi-tool,tool,prompt,reasoning,plan` | `msg_type` values excluded from the messages index (agent-internal bookkeeping) |
| `messages_max_chars` | `2000` | messages longer than this are truncated before embedding |
| `messages_min_chars` | `8` | messages shorter than this are not indexed |

Credentials are referenced as `$secret:NAME` / `$env:VAR` only - never
hardcoded. Qdrant itself is **opt-in** via the compose profile `qdrant`.

## Running Qdrant (development)

The omnidev dev environment enables the `qdrant` profile (generated env, never
hand-edited), so the dev stack starts `omnidev-qdrant-1` next to the other
`omnidev-*` containers:

```bash
python3 /opt/workspace/omni-deployer/omnidev.py setup     # or: deploy.py dev
docker compose --profile qdrant ps                        # only omnidev-* services
```

A deployment without the profile behaves exactly as before: no Qdrant
container, and `semantic_search` degrades with an explicit message.

## Message semantic search (`semantic_search__messages`)

### Storage decision: Qdrant (same as the wiki)

Message embeddings are stored in a **dedicated Qdrant collection
(`messages_semantic`)** on the same Qdrant server and with the same LOCAL
vectorizer as the wiki corpus. Rationale:

* **Dev/prod parity and reuse**: the wiki plugin already ships a Qdrant
  profile in the omnidev dev stack, a battle-tested REST client
  (`se_qdrant.py`) and the local vectorizer. Adding a collection reuses all of
  it - zero new infrastructure, identical operational story.
* **No write access to the omniagent DB**: the plugin's contract is
  read-only access to the messages table (SELECT only). pgvector would force
  the plugin to write embeddings into the core database (and require a core
  schema migration + a core-side indexing hook), which is explicitly out of
  scope and contradicts the wiki plugin's clean separation (core has zero
  knowledge of semantic search).
* **In-memory alternatives** (plain dict / numpy arrays) do not survive
  restarts, do not scale to the ~150k-message corpus, and would need a
  hand-rolled ANN index; Qdrant gives filtered top-k search (channel/thread/
  time filters) out of the box.
* **Operational cost**: one extra collection on an already-required service;
  a full re-index is a single command and costs only local CPU (feature
  hashing, ~1 ms/message).

### Collection schema

| Field | Type | Purpose |
|-------|------|---------|
| `message_id` | integer (payload) | omniagent `messages.id` |
| `channel_id` | string (payload) | channel name (e.g. `omnidev`) |
| `thread_id` | integer or null (payload) | conversation thread id |
| `role` | string (payload) | message role = author (the messages table has no separate username column) |
| `created_at` | ISO-8601 string (payload) | human-readable timestamp |
| `created_ts` | float epoch (payload) | numeric timestamp for `since`/`until` range filters |
| `content` | string (payload) | message body, truncated to `messages_max_chars` (default 2000) with a `...[truncated]` marker |
| `backend` / `profile` | string (payload) | embedding backend and profile that produced the point |
| vector | 384-dim float vector | LOCAL vectorizer output (same as wiki) |

Point identity: `uuid5(NS_messages, "omni-semantic-search:messages:<collection>:<channel_id>:<thread_id>:<message_id>")`
with a dedicated namespace - deterministic, so re-indexing unchanged messages
upserts the same points (idempotent).

Messages are embedded per-message (no chunking): a message is one point.
Long messages are truncated (see `messages_max_chars`); agent-internal
bookkeeping rows (`tool-result`, `multi-tool`, `tool`, `prompt`, `reasoning`,
`plan`) are excluded by default via `exclude_msg_types` so the index holds
actual conversation content.

### Indexing lifecycle

* **Source**: the omniagent `messages` table (`content`, `channel_id`,
  `thread_id`, `role`, `created_at`) - read-only. `se_db.MessageSource`
  opens its own psycopg2 connection and issues SELECT statements only.
* **Backfill**: `semantic_search_messages_index` (or CLI
  `server.py messages-index`) embeds every eligible message and upserts it.
  A **full** run additionally scrolls the collection and deletes stale points
  whose message no longer exists in the DB (deletion/removal handling).
* **Incremental**: pass `channels` / `since` / `until` / `limit` to index
  only a window - upsert-only, never deletes, cheap enough to run on a
  schedule or after thread completion.
* **Never blocks the write path**: indexing is a separate process (MCP tool
  call / CLI) with its own DB connection; the omniagent message writer is
  untouched. The plugin never writes to the DB.
* **Idempotency**: re-running over the same data keeps the same point count.

## Degradation contract (never crash, never fabricate)

| Situation | Result |
|-----------|--------|
| Qdrant unreachable (down, refused, timeout, DNS) | `semantic search unavailable (qdrant unreachable at <url> ...) - no results returned`; index attempts report `... index NOT updated` |
| Collection missing / empty | `index empty, run semantic_search_index (collection '<name>' has no points)` |
| Malformed Qdrant response | `semantic search unavailable (qdrant error): malformed ...` |
| Unknown `embedding_backend` | explicit `unknown embedding_backend ...` error |
| Vector size mismatch with the existing collection | explicit error naming both sizes |

## Indexing model

* Provenance per point: `path` (relative to `OMNI_DIR` when inside it),
  `heading` (markdown breadcrumb), `line_start`/`line_end`, `hash`,
  `text`, `backend`, `profile`.
* Markdown-aware chunking: split at headings, oversized sections split on line
  boundaries with `chunk_overlap` carry-over.
* Point identity: `uuid5(collection, path#chunk_index)` - stable, so
  re-indexing unchanged content upserts the same points; stale points (removed
  files, shrunk files) are deleted.

## Tests

```bash
# integration (needs a reachable Qdrant):
QDRANT_URL=http://qdrant:6333 python3 tests/test_semantic_search.py
# messages suite (needs Qdrant + the omniagent DB; DB smoke skips when unreachable):
QDRANT_URL=http://qdrant:6333 DATABASE_URL=$DATABASE_URL python3 tests/test_semantic_search_messages.py
```

The suite covers: deterministic local embeddings, chunk provenance,
index + top-k search on a fixture corpus, idempotent re-index, stale-point
removal, Qdrant-down degradation, empty-index message, and the
no-external-network assertion (only the Qdrant URL is contacted). It creates a
unique throwaway collection and deletes it afterwards.

The messages suite (`tests/test_semantic_search_messages.py`) mirrors this:
fixture-driven index/search with a fake message source (deterministic point
ids, idempotent re-index, stale pruning, channel/thread/time filters,
attribution fields in the payload, truncation, excluded msg types), plus a
live-DB smoke test (index a few real rows, search them back) that skips when
`DATABASE_URL` is unreachable. All tests use a unique throwaway collection.
