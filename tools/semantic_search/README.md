# semantic_search (MCP tool plugin)

Local **semantic search over the profile wiki**, backed by a Qdrant vector
store. The plugin computes every embedding itself with a **LOCAL vectorizer** -
no LLM, no embedding API, no external/paid service is ever contacted. The only
network endpoint it talks to is the configured Qdrant server.

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

CLI entrypoints (dev/deploy/CI, non-interactive):

```bash
python3 server.py index [--rebuild]
python3 server.py search "<query>" [--limit 10] [--path-filter profiles/omni/wiki]
python3 server.py stats
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
```

The suite covers: deterministic local embeddings, chunk provenance,
index + top-k search on a fixture corpus, idempotent re-index, stale-point
removal, Qdrant-down degradation, empty-index message, and the
no-external-network assertion (only the Qdrant URL is contacted). It creates a
unique throwaway collection and deletes it afterwards.
