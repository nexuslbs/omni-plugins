#!/usr/bin/env python3
"""Idempotent corpus indexing for the semantic_search plugin.

Index identity is (collection, path, chunk_index) via a deterministic UUIDv5,
so re-running an index over an unchanged corpus upserts the same points and
the point count stays stable. Points whose source file disappeared, or whose
chunk no longer exists (file shrank), are deleted.
"""

import uuid

from se_corpus import chunk_markdown, discover_files, display_path, resolve_roots
from se_qdrant import QdrantError, QdrantCollectionMissing

_NAMESPACE = uuid.UUID("6f2b2c34-1a1e-4d0e-9f7c-0b1a2c3d4e5f")


def point_id(collection, rel_path, index):
    return str(uuid.uuid5(_NAMESPACE, "omni-semantic-search:%s:%s#%s" % (collection, rel_path, index)))


def build_chunks(config, profile):
    """Return (files, chunks) for the configured corpus and profile."""
    roots = resolve_roots(config["corpus_roots"], profile, config["omni_dir"])
    files = discover_files(roots, config["corpus_globs"], config["omni_dir"])
    collection = config["collection"]
    chunks = []
    for path in files:
        text = path.read_text(errors="replace")
        rel = display_path(path, config["omni_dir"])
        file_chunks = chunk_markdown(text, config["chunk_size"], config["chunk_overlap"], rel)
        for index, chunk in enumerate(file_chunks):
            chunk["index"] = index
            chunk["id"] = point_id(collection, rel, index)
            chunks.append(chunk)
    return files, chunks


def index_corpus(client, embedder, config, profile, log=None):
    """Build/refresh the Qdrant index. Returns a stats dict."""
    log = log or (lambda _msg: None)
    collection = config["collection"]
    batch_size = max(1, int(config["batch_size"]))
    files, chunks = build_chunks(config, profile)
    log("corpus: %d file(s), %d chunk(s) from %s"
        % (len(files), len(chunks), ", ".join(config["corpus_roots"])))

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

    existing = {}
    for point in client.scroll(collection):
        payload = point.get("payload") or {}
        path = payload.get("path")
        if path:
            existing.setdefault(str(path), set()).add(str(point.get("id")))

    upserted = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        vectors = embedder.embed_documents([c["text"] for c in batch])
        points = []
        for chunk, vector in zip(batch, vectors):
            points.append({
                "id": chunk["id"],
                "vector": vector,
                "payload": {
                    "path": chunk["path"],
                    "heading": chunk["heading"],
                    "line_start": chunk["line_start"],
                    "line_end": chunk["line_end"],
                    "hash": chunk["hash"],
                    "text": chunk["text"],
                    "backend": embedder.backend,
                    "profile": profile,
                },
            })
        client.upsert(collection, points)
        upserted += len(points)
        log("upserted %d/%d chunk(s)" % (upserted, len(chunks)))

    desired_ids = {chunk["id"] for chunk in chunks}
    stale = []
    for path, ids in existing.items():
        if path not in {chunk["path"] for chunk in chunks}:
            stale.extend(ids)
        else:
            stale.extend(i for i in ids if i not in desired_ids)
    if stale:
        client.delete_ids(collection, stale)
        log("deleted %d stale point(s)" % len(stale))

    stats = {
        "files": len(files),
        "chunks": len(chunks),
        "upserted": upserted,
        "deleted": len(stale),
        "backend": embedder.backend,
        "collection": collection,
        "point_count": client.point_count(collection),
    }
    log("index complete: %s" % stats)
    return stats
