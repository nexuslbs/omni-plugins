#!/usr/bin/env python3
"""Integration tests for the semantic_search MCP plugin.

Runs against a REAL Qdrant (default http://qdrant:6333, override with
QDRANT_URL) and a throwaway collection. No LLM / embedding API is used or
required anywhere: every vector is computed by the plugin's LOCAL embedder.

    QDRANT_URL=http://qdrant:6333 python3 tests/test_semantic_search.py

Covers: local embedder determinism/discrimination, markdown chunk provenance,
index + top-k search on a fixture corpus, idempotent re-index, stale-point
removal, empty-index degradation, qdrant-down degradation (no fabricated hit),
the CLI entrypoints, and the no-external-network assertion (every outbound
HTTP request goes to the configured Qdrant base URL).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# Test defaults; only the Qdrant base URL is configurable.
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").strip()
os.environ["QDRANT_URL"] = QDRANT_URL
os.environ.setdefault("EMBEDDING_BACKEND", "hash")

import se_corpus  # noqa: E402
import se_embed  # noqa: E402
from se_qdrant import QdrantClient, QdrantError, QdrantUnreachable  # noqa: E402
import se_index  # noqa: E402
import server  # noqa: E402

RUN_ID = uuid.uuid4().hex[:8]
FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("[%s] %s%s" % (status, label, (" -- " + str(detail)) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)
    return bool(condition)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

DOCS = {
    "Reference/alpha.md": """# Alpha Platform

## Autoscaling

The kubernetes cluster autoscaler grows node pools when pods are pending and
the horizontal pod autoscaler cannot schedule replicas onto existing nodes.
Scale-down walks the utilization window before draining a node.

## Rollouts

Rolling updates replace pods gradually; readiness probes gate traffic.
""",
    "Reference/beta.md": """# Beta Search

## Qdrant

The qdrant vector database stores embeddings in collections and answers
similarity search queries with cosine distance over dense vectors. Payloads
carry provenance metadata next to each vector point.

## Indexing

Indexing batches upsert points and deletes stale points when a source file
disappears.
""",
    "Reference/gamma.md": """# Gamma Database

## Replication

PostgreSQL streaming replication ships write-ahead log segments to a standby
that replays them; failover promotes the standby when the primary is lost.

## Backups

Base backups plus WAL archiving allow point-in-time recovery.
""",
}


def make_corpus(root):
    corpus = Path(root) / "wiki"
    for rel, text in DOCS.items():
        path = corpus / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return corpus


def test_config(corpus, collection):
    return {
        "omni_dir": str(Path(corpus).parent),
        "profile": "test",
        "qdrant_url": QDRANT_URL,
        "qdrant_api_key": "",
        "collection": collection,
        "corpus_roots": [str(corpus)],
        "corpus_globs": ["**/*.md"],
        "embedding_backend": "hash",
        "embedding_model": se_embed.DEFAULT_FASTEMBED_MODEL,
        "chunk_size": 400,
        "chunk_overlap": 50,
        "batch_size": 2,
        "timeout_secs": 15,
        "default_limit": 5,
    }


def paths_of(hits):
    return [str((hit.get("payload") or {}).get("path") or "") for hit in hits]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_local_embedder():
    """LOCAL vectorizer: deterministic, 384-dim, unit norm, discriminative."""
    a1 = se_embed.hash_embed("qdrant vector similarity search")
    a2 = se_embed.hash_embed("qdrant vector similarity search")
    b = se_embed.hash_embed("postgres streaming replication failover")
    embedder = se_embed.make_embedder("hash")
    check("embedder dim == 384", embedder.dim == se_embed.EMBED_DIM, embedder.dim)
    check("embedder is deterministic", a1 == a2)
    check("embedder discriminates", a1 != b)
    norm = sum(v * v for v in a1) ** 0.5
    check("embedding is L2-normalized", abs(norm - 1.0) < 1e-6, norm)
    check("embedder backend name is local", embedder.backend == "hash")
    try:
        se_embed.make_embedder("totally-unknown-backend")
        check("unknown backend raises EmbedderError", False)
    except se_embed.EmbedderError as exc:
        check("unknown backend raises EmbedderError", "unknown embedding_backend" in str(exc), exc)


def test_chunking_provenance():
    """Chunks keep heading breadcrumb + 1-based line ranges; frontmatter is skipped."""
    text = "---\ntitle: x\n---\n# Top\n\nintro text\n\n## Sub\n\nbody text\n"
    chunks = se_corpus.chunk_markdown(text, max_chars=200, overlap=20, source_path="doc.md")
    headings = [c["heading"] for c in chunks]
    check("chunking produced chunks", len(chunks) >= 2, headings)
    intro = next((c for c in chunks if "intro text" in c["text"]), None)
    check("frontmatter not indexed", all("title: x" not in c["text"] for c in chunks), chunks)
    check("line numbers are file-relative (frontmatter offset)",
          intro is not None and intro["line_start"] <= 6 <= intro["line_end"], intro)
    check("heading breadcrumb kept", any("Sub" in h for h in headings), headings)
    check("chunk text present", all(c["text"].strip() for c in chunks))
    check("chunk ids stable", se_index.point_id("c", "doc.md", 0) == se_index.point_id("c", "doc.md", 0))


def test_index_and_search(client, config, collection):
    """Index the fixture corpus and assert expected documents rank in the top-k."""
    embedder = se_embed.make_embedder("hash")
    stats = se_index.index_corpus(client, embedder, config, "test")
    check("indexed 3 files", stats["files"] == 3, stats)
    check("created >= 3 points", stats["point_count"] >= 3, stats)

    expectations = {
        "kubernetes autoscaler horizontal pod autoscaler node pool": "Reference/alpha.md",
        "qdrant vector database embeddings similarity search": "Reference/beta.md",
        "postgresql streaming replication standby failover": "Reference/gamma.md",
    }
    for query, expected in expectations.items():
        vector = embedder.embed_query(query)
        hits = client.search(collection, vector, limit=3)
        got = paths_of(hits)
        check("top-3 contains %s" % expected, any(expected in p for p in got), got)
        check("hits carry provenance", all(
            (h.get("payload") or {}).get("heading") and (h.get("payload") or {}).get("text")
            for h in hits), hits[0] if hits else None)
    return stats


def test_idempotent_reindex(client, config, collection, first_count):
    """Re-indexing an unchanged corpus keeps the point count and adds nothing."""
    embedder = se_embed.make_embedder("hash")
    second = se_index.index_corpus(client, embedder, config, "test")
    check("re-index point count unchanged", second["point_count"] == first_count,
          "%s != %s" % (second["point_count"], first_count))
    check("re-index deletes nothing", second["deleted"] == 0, second)
    check("re-index files unchanged", second["files"] == 3, second)


def test_stale_removal(client, config, corpus):
    """Deleting a source file removes exactly its points on the next index."""
    embedder = se_embed.make_embedder("hash")
    before = client.point_count(config["collection"])
    gone = Path(corpus) / "Reference/gamma.md"
    text = gone.read_text()
    gone.unlink()
    try:
        stats = se_index.index_corpus(client, embedder, config, "test")
        check("stale points deleted", stats["deleted"] > 0, stats)
        check("point count dropped", stats["point_count"] < before,
              "%s !< %s" % (stats["point_count"], before))
        remaining = {str((p.get("payload") or {}).get("path")) for p in client.scroll(config["collection"])}
        check("deleted file no longer indexed",
              not any("Reference/gamma.md" in p for p in remaining), remaining)
    finally:
        gone.write_text(text)


def test_empty_index_degradation():
    """Missing/empty collection: explicit message, no crash, no invented hit."""
    empty = "semantic_search_empty_" + RUN_ID
    os.environ["COLLECTION"] = empty
    os.environ["QDRANT_URL"] = QDRANT_URL
    os.environ["CORPUS_ROOTS"] = "profiles/{profile}/wiki"
    result = server.handle_search({"query": "anything at all"}, {"profile_name": "test"})
    text = result["content"][0]["text"]
    check("empty index message", "index empty, run semantic_search_index" in text, text)
    check("empty index is not an error hit list", "score=" not in text, text)
    result = server.handle_stats({}, {"profile_name": "test"})
    check("stats reports empty index", "index empty" in result["content"][0]["text"],
          result["content"][0]["text"])


def test_qdrant_down_degradation():
    """Unreachable Qdrant: explicit degradation message, no fabricated hit, no crash."""
    client = QdrantClient("http://127.0.0.1:1", "", 2)
    try:
        client.health()
        check("unreachable qdrant raises QdrantUnreachable", False)
    except QdrantUnreachable as exc:
        check("unreachable qdrant raises QdrantUnreachable", "unreachable" in str(exc), exc)
    except QdrantError as exc:
        check("unreachable qdrant raises QdrantUnreachable", False, exc)

    os.environ["QDRANT_URL"] = "http://127.0.0.1:1"
    try:
        search = server.handle_search({"query": "qdrant vector search"}, {"profile_name": "test"})
        text = search["content"][0]["text"]
        check("search degrades when qdrant down",
              "semantic search unavailable (qdrant unreachable)" in text, text)
        check("degradation is flagged as error", search.get("isError") is True, search)
        check("no fabricated hit when qdrant down", "score=" not in text, text)
        index = server.handle_index({}, {"profile_name": "test"})
        itext = index["content"][0]["text"]
        check("index reports NOT updated when qdrant down",
              "index NOT updated" in itext, itext)
    finally:
        os.environ["QDRANT_URL"] = QDRANT_URL


def test_no_external_network(client, config, corpus):
    """Every outbound HTTP request during index+search goes to the Qdrant URL."""
    import urllib.request
    seen = []
    original = urllib.request.urlopen

    def spy(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        seen.append(url)
        return original(request, *args, **kwargs)

    urllib.request.urlopen = spy
    try:
        embedder = se_embed.make_embedder("hash")
        se_index.index_corpus(client, embedder, config, "test")
        client.search(config["collection"], embedder.embed_query("qdrant embeddings"), limit=3)
    finally:
        urllib.request.urlopen = original
    check("network calls were made", len(seen) > 0)
    external = [u for u in seen if not u.startswith(QDRANT_URL.rstrip("/"))]
    check("no non-qdrant network call (no LLM/embedding API)", not external, external)


def test_cli_entrypoints(corpus, collection):
    """The CLI entrypoints work non-interactively (dev/deploy/CI flows)."""
    env = dict(os.environ)
    env.update({
        "QDRANT_URL": QDRANT_URL,
        "COLLECTION": collection,
        "CORPUS_ROOTS": str(corpus),
        "CORPUS_GLOBS": "**/*.md",
        "OMNI_DIR": str(Path(corpus).parent),
        "EMBEDDING_BACKEND": "hash",
    })
    index = subprocess.run([sys.executable, "server.py", "index"], cwd=str(PLUGIN_DIR),
                           capture_output=True, text=True, env=env)
    check("cli index exit 0", index.returncode == 0, index.stderr[-400:])
    check("cli index reports local vectorizer",
          "LOCAL vectorizer: backend=hash" in index.stdout, index.stdout[-400:])
    search = subprocess.run([sys.executable, "server.py", "search", "qdrant vector embeddings",
                             "--limit", "3"], cwd=str(PLUGIN_DIR),
                            capture_output=True, text=True, env=env)
    check("cli search exit 0", search.returncode == 0, search.stderr[-400:])
    check("cli search returns provenance", "Reference/beta.md" in search.stdout, search.stdout[-400:])
    stats = subprocess.run([sys.executable, "server.py", "stats"], cwd=str(PLUGIN_DIR),
                           capture_output=True, text=True, env=env)
    check("cli stats reports points", '"points"' in stats.stdout, stats.stdout[-300:])


def test_tools_registered():
    """The MCP tool registry exposes the documented tool names."""
    names = {tool["name"] for tool in server.TOOLS}
    for expected in ("semantic_search", "semantic_search_index", "semantic_search_stats"):
        check("tool registered: %s" % expected, expected in names, names)
    for tool in server.TOOLS:
        check("tool has schema: %s" % tool["name"], isinstance(tool.get("inputSchema"), dict))


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main():
    print("=== semantic_search integration tests (Qdrant: %s) ===" % QDRANT_URL)
    collection = "semantic_search_test_" + RUN_ID
    client = QdrantClient(QDRANT_URL, "", 15)
    try:
        client.health()
    except QdrantError as exc:
        print("[SKIP] Qdrant is not reachable at %s: %s" % (QDRANT_URL, exc))
        return 2
    root = tempfile.mkdtemp(prefix="semantic-search-test-")
    try:
        corpus = make_corpus(root)
        config = test_config(corpus, collection)
        test_local_embedder()
        test_tools_registered()
        test_chunking_provenance()
        stats = test_index_and_search(client, config, collection)
        test_idempotent_reindex(client, config, collection, stats["point_count"])
        test_stale_removal(client, config, corpus)
        test_no_external_network(client, config, corpus)
        test_empty_index_degradation()
        test_qdrant_down_degradation()
        test_cli_entrypoints(corpus, collection)
    finally:
        try:
            client.delete_collection(collection)
        except QdrantError:
            pass
        shutil.rmtree(root, ignore_errors=True)
    if FAILURES:
        print("\n=== RESULT: FAIL (%d) -> %s ===" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("\n=== RESULT: PASS (all checks) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
