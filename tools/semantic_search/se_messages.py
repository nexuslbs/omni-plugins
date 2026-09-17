#!/usr/bin/env python3
"""Message-level semantic indexing for the semantic_search plugin.

Bridges the read-only message window (se_db.MessageSource) and the Qdrant
vector store (se_qdrant.QdrantClient): messages are embedded with the same
LOCAL vectorizer as the wiki corpus and upserted into a dedicated Qdrant
collection (`messages_semantic` by default). The plugin never writes into the
omniagent database - the DB is only ever SELECTed.

Point identity is a deterministic UUID (uuid5 over channel/thread/message id),
so re-indexing is idempotent: re-running over the same messages keeps the same
points. A FULL index run (no channel/time/limit filters) additionally prunes
stale points whose message no longer exists in the DB (deletion handling);
scoped/incremental runs are upsert-only and never delete.
"""

import hashlib
import uuid

from se_db import MessageSourceError
from se_embed import EmbedderError
from se_qdrant import QdrantError

# Dedicated namespace for message points (distinct from the wiki namespace).
NS_MESSAGES = uuid.UUID("7e3c4d5e-2b2f-4e1f-8a0d-1c2d3e4f5a6b")


def point_id(collection, channel_id, thread_id, message_id):
    """Deterministic point id: same message -> same point, across runs/hosts."""
    key = "omni-semantic-search:messages:%s:%s:%s:%s" % (
        collection, channel_id, thread_id if thread_id is not None else "-", message_id)
    return str(uuid.uuid5(NS_MESSAGES, key))


def _truncate(content, max_chars):
    content = (content or "").strip()
    if max_chars and len(content) > max_chars:
        return content[:max_chars] + "...[truncated]"
    return content


def build_points(messages, embedder, config, profile):
    """Embed messages and build Qdrant points.

    Returns (points, skipped): points is the list of point dicts ready for
    upsert; skipped counts messages that produced no embedding (empty content
    after trimming).
    """
    collection = config["messages_collection"]
    max_chars = int(config["messages_max_chars"] or 0)
    points = []
    skipped = 0
    for message in messages:
        text = _truncate(message["content"], max_chars)
        if not text:
            skipped += 1
            continue
        vector = embedder.embed_query(text)
        points.append({
            "id": point_id(collection, message["channel_id"], message["thread_id"],
                           message["message_id"]),
            "vector": list(vector),
            "payload": {
                "message_id": message["message_id"],
                "channel_id": message["channel_id"],
                "thread_id": message["thread_id"],
                "role": message["role"],
                "created_at": message["created_at"],
                "created_ts": message["created_ts"],
                "content": text,
                "backend": embedder.backend,
                "profile": profile,
            },
        })
    return points, skipped


def index_messages(source, embedder, client, config, profile, log=None):
    """Index messages from the DB into Qdrant (idempotent upserts + stale prune).

    `source` is a se_db.MessageSource; `client` a se_qdrant.QdrantClient.
    Returns a list of human-readable progress lines (appends to `log` when
    given). Filters (channels/since/until/limit) are passed through to the DB
    query; a FULL run (no filters) prunes points whose messages no longer
    exist. Never writes to the omniagent DB.
    """
    log = log if callable(log) else (lambda line: None)
    collection = config["messages_collection"]
    channels = config.get("_index_channels")
    since = config.get("_index_since")
    until = config.get("_index_until")
    limit = config.get("_index_limit")
    scoped = bool(channels or since or until or limit)

    messages = source.fetch_messages(
        exclude_msg_types=config.get("exclude_msg_types"),
        channels=channels, since=since, until=until, limit=limit,
        min_chars=config.get("messages_min_chars", 8))
    log("DB: %d message(s) read (read-only SELECT; filters=%s)"
        % (len(messages), "scoped" if scoped else "full"))

    points, skipped = build_points(messages, embedder, config, profile)
    if skipped:
        log("%d message(s) skipped (empty after trimming)" % skipped)

    if not client.collection_exists(collection):
        client.create_collection(collection, embedder.dim)
        log("created collection '%s' (dim=%d, cosine)" % (collection, embedder.dim))
    else:
        size = client.vector_size(collection)
        if size is not None and int(size) != int(embedder.dim):
            raise QdrantError(
                "collection '%s' has vector size %s but the configured backend '%s' "
                "produces %d dims - use a different collection name or rebuild it"
                % (collection, size, embedder.backend, embedder.dim))

    batch = int(config["batch_size"])
    for start in range(0, len(points), batch):
        client.upsert(collection, points[start:start + batch])
    log("Qdrant: upserted %d point(s) into '%s'" % (len(points), collection))

    if not scoped:
        current_ids = {p["id"] for p in points}
        stale = []
        try:
            for point in client.scroll(collection, limit=512):
                pid = str(point.get("id"))
                if pid not in current_ids:
                    stale.append(pid)
        except QdrantError as exc:
            raise QdrantError("stale-point scan failed for '%s': %s" % (collection, exc))
        if stale:
            client.delete_ids(collection, stale)
        log("Qdrant: pruned %d stale point(s) (messages no longer in the DB window)"
            % len(stale))
    return messages


def search_messages(client, embedder, config, query, limit=10, score_threshold=None,
                    channel_id=None, thread_id=None, since=None, until=None):
    """Semantic search over the messages collection with optional filters.

    Returns the raw hit list from Qdrant ({id, score, payload}). Filters are
    translated to Qdrant conditions on the payload metadata fields
    (channel_id / thread_id / created_ts).
    """
    vector = embedder.embed_query(query)
    filters = []
    if channel_id:
        filters.append({"key": "channel_id", "match": {"value": str(channel_id)}})
    if thread_id is not None:
        filters.append({"key": "thread_id", "match": {"value": int(thread_id)}})
    if since is not None or until is not None:
        rng = {}
        if since is not None:
            rng["gte"] = float(since)
        if until is not None:
            rng["lte"] = float(until)
        filters.append({"key": "created_ts", "range": rng})
    return client.search(config["messages_collection"], vector, limit=limit,
                         score_threshold=score_threshold, filters=filters)


def parse_iso_ts(value):
    """Accept an ISO-8601 string or epoch float and return an epoch float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit() and not text.startswith("+"):
        return float(text)
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise ValueError("invalid timestamp '%s' (use ISO-8601 or epoch seconds)" % value)


def content_hash(text):
    """Short stable hash of a message body (for display/debug)."""
    return hashlib.blake2b((text or "").encode("utf-8"), digest_size=4).hexdigest()