#!/usr/bin/env python3
"""semantic_search MCP server - LOCAL semantic search over the profile wiki.

Tools:
  - semantic_search: ranked semantic hits (path, heading, line range, score,
    snippet) for a natural-language query. Empty index and unreachable Qdrant
    are reported explicitly; no hit is ever fabricated.
  - semantic_search_index: (re)build the Qdrant index of the wiki corpus with
    the LOCAL vectorizer (idempotent, stale points removed).

Embeddings are computed in-process by a local backend (see se_embed.py): no
LLM, no embedding API, no external service is contacted - the only network
endpoint used is the configured Qdrant server.

MCP JSON-RPC over stdio. Config comes from plugin config (injected as env
vars) or, when running standalone, from the environment.
"""

import json
import logging
import os
import sys

from se_corpus import discover_files, display_path, resolve_roots
from se_embed import EMBED_DIM, EmbedderError, make_embedder
from se_index import build_chunks, index_corpus
from se_qdrant import QdrantClient, QdrantCollectionMissing, QdrantError, QdrantUnreachable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [semantic_search] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("semantic_search")

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_QDRANT_URL = "http://qdrant:6333"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def cfg(*names, default=""):
    """Read a plugin config value from the environment (framework injects config as env)."""
    for name in names:
        for candidate in (name, name.upper(), "SEMANTIC_SEARCH_" + name.upper()):
            value = os.environ.get(candidate)
            if value is not None and value.strip():
                return value.strip()
    return default


def cfg_int(name, default, minimum=None):
    try:
        value = int(float(cfg(name, default=str(default)) or default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    return value


def split_list(raw, fallback):
    raw = (raw or "").strip()
    if not raw:
        return list(fallback)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except ValueError:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()]


def get_profile(meta):
    if isinstance(meta, dict) and meta.get("profile_name"):
        return str(meta["profile_name"])
    return "default"


def load_config(meta=None, args=None):
    args = args or {}
    omni_dir = cfg("omni_dir") or os.environ.get("OMNI_DIR") or ""
    if args.get("omni_dir"):
        omni_dir = str(args["omni_dir"])
    profile = get_profile(meta)
    roots = args.get("paths") or split_list(cfg("corpus_roots", default="profiles/{profile}/wiki"),
                                            ["profiles/{profile}/wiki"])
    globs = args.get("globs") or split_list(cfg("corpus_globs", default="**/*.md"), ["**/*.md"])
    return {
        "omni_dir": omni_dir,
        "profile": profile,
        "qdrant_url": cfg("qdrant_url", "QDRANT_URL", default=DEFAULT_QDRANT_URL),
        "qdrant_api_key": cfg("qdrant_api_key", "QDRANT_API_KEY"),
        "collection": cfg("collection", default="wiki_semantic"),
        "corpus_roots": [str(r) for r in roots],
        "corpus_globs": [str(g) for g in globs],
        "embedding_backend": cfg("embedding_backend", default="hash"),
        "embedding_model": cfg("embedding_model", default="BAAI/bge-small-en-v1.5"),
        "chunk_size": cfg_int("chunk_size", 1200, minimum=200),
        "chunk_overlap": cfg_int("chunk_overlap", 200, minimum=0),
        "batch_size": cfg_int("batch_size", 64, minimum=1),
        "timeout_secs": cfg_int("timeout_secs", 20, minimum=1),
        "default_limit": cfg_int("default_limit", 10, minimum=1),
    }


def make_client(config):
    return QdrantClient(config["qdrant_url"], config["qdrant_api_key"], config["timeout_secs"])


def get_embedder(config):
    return make_embedder(config["embedding_backend"], config["embedding_model"])


def make_tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": str(text)}], "isError": bool(is_error)}


# --------------------------------------------------------------------------
# tool handlers
# --------------------------------------------------------------------------

def _semantic_search_impl(args, meta):
    query = str(args.get("query", "") or "").strip()
    if not query:
        return make_tool_result("semantic_search requires a non-empty 'query'", True)
    config = load_config(meta, args)
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else config["default_limit"]
    except (TypeError, ValueError):
        limit = config["default_limit"]
    limit = max(1, min(limit, 100))
    embedder = get_embedder(config)
    vector = embedder.embed_query(query)
    client = make_client(config)
    try:
        hits = client.search(config["collection"], vector, limit=limit,
                             score_threshold=args.get("min_score"),
                             path_prefix=args.get("path_filter"))
    except QdrantUnreachable as exc:
        return make_tool_result(
            "semantic search unavailable (qdrant unreachable): %s - no results returned" % exc, True)
    except QdrantCollectionMissing as exc:
        return make_tool_result("index empty, run semantic_search_index (%s)" % exc)
    except QdrantError as exc:
        return make_tool_result("semantic search unavailable (qdrant error): %s" % exc, True)

    if not hits:
        count = 0
        try:
            count = client.point_count(config["collection"])
        except QdrantError:
            pass
        if count == 0:
            return make_tool_result(
                "index empty, run semantic_search_index (collection '%s' has no points)"
                % config["collection"])
        return make_tool_result(
            "No semantic hits for '%s' in collection '%s' (%d point(s) indexed, backend=%s)."
            % (query, config["collection"], count, embedder.backend))

    lines = ["%d semantic hit(s) for '%s' [collection=%s backend=%s dim=%d]"
             % (len(hits), query, config["collection"], embedder.backend, embedder.dim)]
    for rank, hit in enumerate(hits, start=1):
        payload = hit.get("payload") or {}
        heading = payload.get("heading") or "(no heading)"
        lines.append("%d. score=%.4f  %s  [%s]  lines %s-%s"
                     % (rank, hit["score"], payload.get("path", "?"), heading,
                        payload.get("line_start", "?"), payload.get("line_end", "?")))
        snippet = " ".join((payload.get("text") or "").split())
        lines.append("   %s" % (snippet[:400] + ("..." if len(snippet) > 400 else "")))
    return make_tool_result("\n".join(lines))


def _semantic_search_index_impl(args, meta):
    config = load_config(meta, args)
    collection = config["collection"]
    progress = []
    try:
        embedder = get_embedder(config)
    except EmbedderError as exc:
        return make_tool_result("semantic_search_index error: %s" % exc, True)
    client = make_client(config)
    try:
        client.health()
    except QdrantUnreachable as exc:
        return make_tool_result(
            "semantic search unavailable (qdrant unreachable): %s - index NOT updated" % exc, True)
    except QdrantError as exc:
        return make_tool_result("semantic_search_index error: %s" % exc, True)

    if args.get("rebuild") and client.collection_exists(collection):
        client.delete_collection(collection)
        progress.append("dropped collection '%s' (rebuild requested)" % collection)

    try:
        stats = index_corpus(client, embedder, config, config["profile"], log=progress.append)
    except QdrantUnreachable as exc:
        return make_tool_result(
            "semantic search unavailable (qdrant unreachable): %s - index NOT updated" % exc, True)
    except (QdrantError, EmbedderError) as exc:
        return make_tool_result("semantic_search_index error: %s" % exc, True)
    progress.append("LOCAL vectorizer: backend=%s dim=%d (no LLM/embedding API called)"
                    % (embedder.backend, embedder.dim))
    return make_tool_result("\n".join(progress))


def _semantic_search_stats_impl(args, meta):
    config = load_config(meta, args)
    client = make_client(config)
    try:
        info = client.collection_info(config["collection"])
    except QdrantUnreachable as exc:
        return make_tool_result(
            "semantic search unavailable (qdrant unreachable): %s" % exc, True)
    except QdrantCollectionMissing as exc:
        return make_tool_result("index empty, run semantic_search_index (%s)" % exc)
    except QdrantError as exc:
        return make_tool_result("semantic_search_index error: %s" % exc, True)
    embedder = get_embedder(config)
    return make_tool_result(json.dumps({
        "collection": config["collection"],
        "points": info.get("points_count"),
        "vector_size": client.vector_size(config["collection"]),
        "backend": embedder.backend,
        "backend_dim": embedder.dim,
        "qdrant_url": config["qdrant_url"],
        "corpus_roots": config["corpus_roots"],
        "corpus_globs": config["corpus_globs"],
    }, indent=2))


def handle_search(args, meta):
    try:
        return _semantic_search_impl(args, meta)
    except Exception as exc:  # last line of defense
        log.exception("semantic_search crashed")
        return make_tool_result("semantic_search error: %s" % exc, True)


def handle_index(args, meta):
    try:
        return _semantic_search_index_impl(args, meta)
    except Exception as exc:
        log.exception("semantic_search_index crashed")
        return make_tool_result("semantic_search_index error: %s" % exc, True)


def handle_stats(args, meta):
    try:
        return _semantic_search_stats_impl(args, meta)
    except Exception as exc:
        log.exception("semantic_search_stats crashed")
        return make_tool_result("semantic_search_stats error: %s" % exc, True)


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "semantic_search",
        "description": "Semantic search over the profile wiki (Qdrant + LOCAL vectorizer; no LLM "
                       "or embedding API call). Returns ranked hits with wiki path, heading, line "
                       "range and score. Degrades explicitly: 'unavailable (qdrant unreachable)' "
                       "or 'index empty, run semantic_search_index' - never a fabricated hit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query"},
                "limit": {"type": "integer", "description": "Max hits (default 10, max 100)"},
                "path_filter": {"type": "string",
                                "description": "Optional substring filter on the indexed path"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "semantic_search_index",
        "description": "Build or refresh the semantic-search index for the wiki corpus. Chunks "
                       "markdown by heading, embeds each chunk with the LOCAL vectorizer "
                       "(feature-hashing by default; no LLM/embedding API), upserts to Qdrant. "
                       "Idempotent: re-running over an unchanged corpus keeps the point count and "
                       "deletes points of removed files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Override corpus roots (absolute or relative to OMNI_DIR)"},
                "globs": {"type": "array", "items": {"type": "string"},
                          "description": "Override corpus globs (default ['**/*.md'])"},
                "rebuild": {"type": "boolean",
                            "description": "Drop the collection first for a clean rebuild"},
            },
        },
    },
    {
        "name": "semantic_search_stats",
        "description": "Report the semantic-search index status: Qdrant URL, collection, point "
                       "count, vector size, active local embedding backend and corpus roots.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "semantic_search": handle_search,
    "semantic_search_index": handle_index,
    "semantic_search_stats": handle_stats,
}


# --------------------------------------------------------------------------
# MCP stdio loop
# --------------------------------------------------------------------------

def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "semantic-search-python", "version": "0.1.0"},
    })


def handle_tools_call(msg):
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    meta = params.get("meta") or {}
    if name not in HANDLERS:
        send_json(make_error(rid, -32601, "Unknown tool: %s" % name))
        return
    try:
        result = HANDLERS[name](args, meta)
    except Exception as exc:
        log.exception("tool %s crashed", name)
        result = make_tool_result("%s error: %s" % (name, exc), True)
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


# --------------------------------------------------------------------------
# CLI (deploy/dev/CI friendly, non-interactive)
# --------------------------------------------------------------------------

def cli(argv):
    meta = {}
    if len(argv) >= 2 and argv[1] == "index":
        args = {}
        if "--rebuild" in argv:
            args["rebuild"] = True
        result = _semantic_search_index_impl(args, meta)
    elif len(argv) >= 3 and argv[1] == "search":
        args = {"query": argv[2]}
        for i, token in enumerate(argv):
            if token == "--limit" and i + 1 < len(argv):
                args["limit"] = argv[i + 1]
            if token == "--path-filter" and i + 1 < len(argv):
                args["path_filter"] = argv[i + 1]
        result = _semantic_search_impl(args, meta)
    elif len(argv) >= 2 and argv[1] in ("stats", "status"):
        result = _semantic_search_stats_impl({}, meta)
    else:
        print("usage: server.py index [--rebuild] | search <query> [--limit N] | stats")
        return 2
    print(result["content"][0]["text"])
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv))
    main()
