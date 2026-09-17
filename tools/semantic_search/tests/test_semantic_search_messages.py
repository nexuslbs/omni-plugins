#!/usr/bin/env python3
"""Integration tests for the semantic_search messages tools.

Runs against a REAL Qdrant (default http://qdrant:6333, override with
QDRANT_URL) and a throwaway collection. The fixture part of the suite uses a
fake in-memory message source; the live-DB smoke test uses the real
se_db.MessageSource against DATABASE_URL and SKIPS when that DB is
unreachable (it never writes to it - SELECT only).

    QDRANT_URL=http://qdrant:6333 DATABASE_URL=$DATABASE_URL \
        python3 tests/test_semantic_search_messages.py

Covers: deterministic point ids (idempotent re-index), top-k search with
channel/thread/time filters, payload attribution (channel/thread/role/date),
truncation, excluded msg types, stale-point pruning (deletion handling),
empty-index degradation, and a live-DB index+search smoke test.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").strip()
os.environ["QDRANT_URL"] = QDRANT_URL
os.environ.setdefault("EMBEDDING_BACKEND", "hash")

import se_embed  # noqa: E402
import se_messages  # noqa: E402
from se_qdrant import QdrantClient, QdrantError, QdrantUnreachable  # noqa: E402

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

def msg(mid, channel, thread, role, text, ts="2026-09-10T12:00:00+00:00", msg_type="message"):
    return {
        "message_id": mid,
        "channel_id": channel,
        "thread_id": thread,
        "role": role,
        "created_at": ts,
        "created_ts": datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(),
        "content": text,
        "msg_type": msg_type,
    }


FIXTURE_MESSAGES = [
    msg(1, "omnidev", 100, "user", "The kubernetes autoscaler grows node pools when pods are pending."),
    msg(2, "omnidev", 100, "agent", "The qdrant vector database stores embeddings in collections and answers similarity queries."),
    msg(3, "main", 101, "user", "How do I configure the telegram platform token?"),
    msg(4, "main", 101, "agent", "Put the token in the secrets table, never in a config file."),
    msg(5, "omnidev", 102, "user", "Rolling updates replace pods gradually; readiness probes gate traffic."),
    msg(6, "omnidev", 102, "agent", "A very long message " + "x" * 3000 + " trailing words about autoscaling."),
]


class FakeSource:
    """In-memory stand-in for se_db.MessageSource (same fetch_messages contract)."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.last_kwargs = None

    def fetch_messages(self, exclude_msg_types=None, channels=None,
                       since=None, until=None, limit=None, min_chars=8):
        self.last_kwargs = dict(exclude_msg_types=exclude_msg_types, channels=channels,
                                since=since, until=until, limit=limit, min_chars=min_chars)
        excluded = set(exclude_msg_types or ())
        out = [m for m in self.messages
               if len(m["content"].strip()) >= int(min_chars or 0)
               and m.get("msg_type", "message") not in excluded]
        if channels:
            out = [m for m in out if m["channel_id"] in channels]
        if since is not None:
            out = [m for m in out if m["created_ts"] >= since]
        if until is not None:
            out = [m for m in out if m["created_ts"] <= until]
        if limit is not None:
            out = out[:int(limit)]
        return out


def test_config(collection, **overrides):
    config = {
        "omni_dir": "",
        "profile": "test",
        "qdrant_url": QDRANT_URL,
        "qdrant_api_key": "",
        "collection": "wiki_semantic_test",
        "messages_collection": collection,
        "corpus_roots": [],
        "corpus_globs": [],
        "embedding_backend": "hash",
        "embedding_model": se_embed.DEFAULT_FASTEMBED_MODEL,
        "chunk_size": 1200,
        "chunk_overlap": 200,
        "batch_size": 2,
        "timeout_secs": 15,
        "default_limit": 5,
        "database_url": "",
        "exclude_msg_types": ["tool-result", "multi-tool", "tool", "prompt", "reasoning", "plan"],
        "messages_max_chars": 2000,
        "messages_min_chars": 8,
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_point_id_deterministic():
    a = se_messages.point_id("messages_semantic", "omnidev", 100, 42)
    b = se_messages.point_id("messages_semantic", "omnidev", 100, 42)
    c = se_messages.point_id("messages_semantic", "omnidev", 100, 43)
    d = se_messages.point_id("messages_semantic", "main", 100, 42)
    check("point_id deterministic for same message", a == b, (a, b))
    check("point_id differs across message ids", a != c, (a, c))
    check("point_id differs across channels", a != d, (a, d))


def test_index_and_search_filters():
    collection = "msgs_test_%s" % RUN_ID
    client = QdrantClient(QDRANT_URL, "", 15)
    embedder = se_embed.make_embedder("hash", se_embed.DEFAULT_FASTEMBED_MODEL)
    config = test_config(collection)
    source = FakeSource(FIXTURE_MESSAGES)

    try:
        se_messages.index_messages(source, embedder, client, config, "test")
        count = client.point_count(collection)
        check("indexed all 6 fixture messages", count == 6, count)

        # top-k search: query about qdrant should surface message 2
        hits = se_messages.search_messages(client, embedder, config, "vector database embeddings", limit=3)
        check("search returns hits", len(hits) >= 1, len(hits))
        top = hits[0]
        payload = top["payload"]
        check("top hit attribution: channel", payload.get("channel_id") == "omnidev", payload)
        check("top hit attribution: thread", payload.get("thread_id") == 100, payload)
        check("top hit attribution: role", payload.get("role") == "agent", payload)
        check("top hit attribution: date", payload.get("created_at") is not None, payload)
        check("top hit has snippet content", len(payload.get("content") or "") > 0, payload)
        check("top hit has message_id", payload.get("message_id") == 2, payload)

        # channel filter
        hits = se_messages.search_messages(client, embedder, config, "token",
                                           limit=5, channel_id="main")
        check("channel filter keeps only main", hits and all(h["payload"]["channel_id"] == "main" for h in hits),
              [h["payload"]["channel_id"] for h in hits])

        # thread filter
        hits = se_messages.search_messages(client, embedder, config, "token", limit=5, thread_id=101)
        check("thread filter keeps only thread 101",
              hits and all(h["payload"]["thread_id"] == 101 for h in hits),
              [h["payload"]["thread_id"] for h in hits])

        # time filter: everything in fixtures is 2026-09-10; until earlier => no hits
        earlier = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        hits = se_messages.search_messages(client, embedder, config, "token", limit=5, until=earlier)
        check("time filter until=2026-01-01 yields no hits", not hits, hits)

        # idempotent re-index keeps point count
        se_messages.index_messages(source, embedder, client, config, "test")
        check("re-index keeps point count (idempotent)", client.point_count(collection) == 6,
              client.point_count(collection))

        # deletion handling: drop message 2 from the source, full re-index prunes it
        source.messages = [m for m in FIXTURE_MESSAGES if m["message_id"] != 2]
        se_messages.index_messages(source, embedder, client, config, "test")
        count = client.point_count(collection)
        check("stale point pruned after deletion", count == 5, count)
        hits = se_messages.search_messages(client, embedder, config, "vector database embeddings", limit=3)
        check("pruned message no longer searchable", not hits or hits[0]["payload"]["message_id"] != 2,
              [h["payload"]["message_id"] for h in hits])

        # excluded msg types: a tool-result row must not be indexed
        source.messages = list(FIXTURE_MESSAGES) + [
            msg(99, "omnidev", 100, "agent", "tool internals about vector math", msg_type="tool-result")]
        se_messages.index_messages(source, embedder, client, config, "test")
        payloads = [p["payload"] for p in client.scroll(collection, limit=100)]
        check("excluded msg_type not indexed",
              all(p.get("message_id") != 99 for p in payloads), len(payloads))

        # truncation: message 6 (3000 chars) stored at max 2000 + marker
        se_messages.index_messages(source, embedder, client, config, "test")
        payloads = [p["payload"] for p in client.scroll(collection, limit=100)]
        long_one = next((p for p in payloads if p.get("message_id") == 6), None)
        check("long message truncated with marker",
              long_one and len(long_one["content"]) <= 2000 + len("...[truncated]")
              and long_one["content"].endswith("...[truncated]"),
              (long_one or {}).get("content", "")[-40:])
    finally:
        try:
            client.delete_collection(collection)
        except QdrantError:
            pass


def test_empty_index_degradation():
    collection = "msgs_empty_%s" % RUN_ID
    client = QdrantClient(QDRANT_URL, "", 15)
    embedder = se_embed.make_embedder("hash", se_embed.DEFAULT_FASTEMBED_MODEL)
    config = test_config(collection)
    try:
        hits = se_messages.search_messages(client, embedder, config, "anything", limit=3)
        check("empty collection raises QdrantCollectionMissing", False, hits)
    except Exception as exc:
        check("empty collection raises QdrantCollectionMissing",
              "does not exist" in str(exc) or "empty" in str(exc).lower(), str(exc))
    finally:
        try:
            client.delete_collection(collection)
        except QdrantError:
            pass


def test_live_db_smoke():
    """Index a handful of real messages from DATABASE_URL and search them back.

    Skips (with a note) when the DB is unreachable; never writes to it.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[SKIP] live-DB smoke: DATABASE_URL not set")
        return
    try:
        import se_db  # noqa: F401
        source = se_db.MessageSource(database_url, timeout=5)
        sample = source.fetch_messages(limit=5)
        if not sample:
            print("[SKIP] live-DB smoke: no messages returned")
            return
    except Exception as exc:
        print("[SKIP] live-DB smoke: DB unreachable (%s)" % exc)
        return

    collection = "msgs_live_%s" % RUN_ID
    client = QdrantClient(QDRANT_URL, "", 15)
    embedder = se_embed.make_embedder("hash", se_embed.DEFAULT_FASTEMBED_MODEL)
    config = test_config(collection, database_url=database_url,
                         _index_limit=len(sample))
    try:
        se_messages.index_messages(source, embedder, client, config, "test")
        count = client.point_count(collection)
        check("live-DB smoke: indexed %d real messages" % len(sample), count == len(sample), count)
        if count:
            probe = sample[0]["content"][:60]
            hits = se_messages.search_messages(client, embedder, config, probe, limit=3)
            check("live-DB smoke: search returns hits", len(hits) >= 1, len(hits))
            if hits:
                p = hits[0]["payload"]
                check("live-DB smoke: hit has channel/thread/role/date",
                      p.get("channel_id") is not None and p.get("created_at") is not None
                      and p.get("role") is not None and (p.get("content") or "").strip() != "",
                      p)
    finally:
        try:
            client.delete_collection(collection)
        except QdrantError:
            pass


def test_server_tool_registration():
    """The MCP server advertises the three messages tools."""
    import server  # noqa: E402
    names = {t["name"] for t in server.TOOLS}
    check("semantic_search__messages registered",
          "semantic_search__messages" in names, sorted(names))
    check("semantic_search_messages_index registered",
          "semantic_search_messages_index" in names)
    check("semantic_search_messages_stats registered",
          "semantic_search_messages_stats" in names)
    check("wiki tools still registered",
          {"semantic_search", "semantic_search_index", "semantic_search_stats"} <= names)


def main():
    print("semantic_search messages integration suite (QDRANT_URL=%s, run=%s)"
          % (QDRANT_URL, RUN_ID))
    test_point_id_deterministic()
    test_index_and_search_filters()
    test_empty_index_degradation()
    test_live_db_smoke()
    test_server_tool_registration()

    print("\n%d check(s) failed: %s" % (len(FAILURES), FAILURES or "none"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())