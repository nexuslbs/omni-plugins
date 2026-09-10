#!/usr/bin/env python3
"""Minimal Qdrant REST client (standard library only) for the semantic_search plugin.

Every failure mode is explicit:
  - QdrantUnreachable  : network/socket/connection error (server down)
  - QdrantError        : HTTP error, malformed JSON response, or malformed
                         response shape
The client NEVER invents results: a failed search raises and the tool layer
turns it into an explicit degradation message.
"""

import json
import urllib.error
import urllib.request

# Endpoints tried for a similarity search, in order. Qdrant >= 1.10 exposes the
# universal query API (/points/query); the legacy /points/search is kept as a
# fallback so older servers keep working.
_QUERY_PATHS = ("/points/query", "/points/search")


class QdrantError(Exception):
    """Qdrant answered, but with an HTTP error or an unusable response."""


class QdrantUnreachable(QdrantError):
    """Qdrant could not be reached at all (connection refused, timeout, DNS)."""


class QdrantCollectionMissing(QdrantError):
    """The configured collection does not exist yet."""


class QdrantClient:
    def __init__(self, url, api_key="", timeout=20.0):
        if not url or not str(url).strip():
            raise QdrantError("qdrant_url is not configured (set the plugin config or QDRANT_URL)")
        self.url = str(url).strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = float(timeout or 20)

    # -- transport ---------------------------------------------------------
    def request(self, method, path, body=None, allow_404=False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if allow_404 and exc.code == 404:
                return None
            raise QdrantError(
                "qdrant HTTP %s on %s %s%s" % (exc.code, method, path,
                                               (": " + detail) if detail else "")
            ) from exc
        except Exception as exc:  # URLError, socket.timeout, OSError, ...
            raise QdrantUnreachable(
                "qdrant unreachable at %s (%s %s): %s" % (self.url, method, path, exc)
            ) from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise QdrantError(
                "malformed qdrant response on %s %s (invalid JSON): %s" % (method, path, exc)
            ) from exc
        if not isinstance(parsed, dict):
            raise QdrantError(
                "malformed qdrant response on %s %s: expected a JSON object, got %s"
                % (method, path, type(parsed).__name__)
            )
        return parsed

    # -- collections -------------------------------------------------------
    def health(self):
        """Return True when the server answers, raise QdrantUnreachable otherwise."""
        self.request("GET", "/collections")
        return True

    def collection_exists(self, name):
        return self.request("GET", "/collections/%s" % name, allow_404=True) is not None

    def create_collection(self, name, dim, distance="Cosine"):
        return self.request("PUT", "/collections/%s" % name,
                            {"vectors": {"size": int(dim), "distance": distance}})

    def collection_info(self, name):
        resp = self.request("GET", "/collections/%s" % name, allow_404=True)
        if resp is None:
            raise QdrantCollectionMissing(
                "collection '%s' does not exist yet - run semantic_search_index" % name)
        return resp.get("result") or {}

    def point_count(self, name):
        return int(self.collection_info(name).get("points_count") or 0)

    def vector_size(self, name):
        vectors = (self.collection_info(name).get("config") or {}).get("params", {}).get("vectors")
        if isinstance(vectors, dict):
            if "size" in vectors:
                return int(vectors["size"])
            # named vectors: take the first entry
            for value in vectors.values():
                if isinstance(value, dict) and "size" in value:
                    return int(value["size"])
        return None

    def delete_collection(self, name):
        return self.request("DELETE", "/collections/%s" % name)

    # -- points ------------------------------------------------------------
    def upsert(self, name, points, wait=True):
        if not points:
            return {}
        return self.request("PUT", "/collections/%s/points?wait=%s" % (name, str(bool(wait)).lower()),
                            {"points": points})

    def delete_ids(self, name, ids, wait=True):
        if not ids:
            return {}
        return self.request("POST", "/collections/%s/points/delete?wait=%s" % (name, str(bool(wait)).lower()),
                            {"points": list(ids)})

    def scroll(self, name, limit=256, with_payload=True, with_vector=False):
        """Yield every point (id + payload) of the collection."""
        offset = None
        while True:
            body = {"limit": int(limit), "with_payload": bool(with_payload),
                    "with_vector": bool(with_vector)}
            if offset is not None:
                body["offset"] = offset
            resp = self.request("POST", "/collections/%s/points/scroll" % name, body)
            result = resp.get("result")
            if not isinstance(result, dict):
                raise QdrantError("malformed scroll response for collection '%s'" % name)
            points = result.get("points")
            if points is None:
                raise QdrantError("malformed scroll response for collection '%s': missing points" % name)
            if not isinstance(points, list):
                raise QdrantError("malformed scroll response for collection '%s': points is not a list" % name)
            for point in points:
                yield point
            offset = result.get("next_page_offset")
            if offset is None or not points:
                return

    def search(self, name, vector, limit=10, score_threshold=None, path_prefix=None):
        """Similarity search. Returns a list of hit dicts {id, score, payload}."""
        base = {"limit": int(limit), "with_payload": True}
        if score_threshold is not None:
            base["score_threshold"] = float(score_threshold)
        if path_prefix:
            base["filter"] = {"must": [{"key": "path", "match": {"text": path_prefix}}]}

        last_error = None
        for suffix in _QUERY_PATHS:
            # /points/query (Qdrant >= 1.10) takes `query`; the legacy
            # /points/search takes `vector`. The key must follow the endpoint:
            # sending the wrong one makes Qdrant answer HTTP 400 (JSON body
            # error), which would mask a missing collection (404).
            body = dict(base)
            body["query" if suffix.endswith("/query") else "vector"] = list(vector)
            path = "/collections/%s%s" % (name, suffix)
            try:
                resp = self.request("POST", path, body, allow_404=True)
            except QdrantCollectionMissing:
                raise
            except QdrantError as exc:
                last_error = exc
                if "HTTP 404" in str(exc):
                    continue  # endpoint not supported by this server
                raise
            if resp is None:
                last_error = QdrantCollectionMissing(
                    "collection '%s' does not exist yet - run semantic_search_index" % name)
                continue
            result = resp.get("result")
            if isinstance(result, dict):
                result = result.get("points")
            if result is None:
                raise QdrantError("malformed search response from '%s': missing result" % name)
            if not isinstance(result, list):
                raise QdrantError("malformed search response from '%s': result is not a list" % name)
            hits = []
            for item in result:
                if not isinstance(item, dict) or "id" not in item:
                    raise QdrantError("malformed search hit from '%s': %r" % (name, item))
                hits.append({"id": item.get("id"), "score": float(item.get("score") or 0.0),
                             "payload": item.get("payload") or {}})
            return hits
        raise last_error or QdrantError("qdrant search failed for collection '%s'" % name)
