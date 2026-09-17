#!/usr/bin/env python3
"""Read-only access to the omniagent messages table for the semantic_search
plugin (the `semantic_search__messages` family of tools).

The plugin NEVER writes into the omniagent database: every statement here is a
plain SELECT. Connection details come from the plugin config / environment
(`database_url`, default `$env:DATABASE_URL`), which the omniagent runtime
already has - the plugin just opens its own read-only connection.

psycopg2 is required for the messages tools (it is present in the omniagent
runtime image). Imports are lazy so the wiki-only tools keep working on a
deployment without psycopg2.
"""

import os
import re
from datetime import datetime, timezone

# Message types that are agent-internal bookkeeping (tool calls, tool output,
# system prompt dumps, reasoning traces, plans) and would pollute a semantic
# search of the actual conversation. Everything else (user prompts, causes,
# agent replies, summaries, errors, ...) is indexed by default.
DEFAULT_EXCLUDE_MSG_TYPES = (
    "tool-result", "multi-tool", "tool", "prompt", "reasoning", "plan",
)

_SELECT = (
    "SELECT id, channel_id, thread_id, role, created_at, content "
    "FROM messages "
    "WHERE content IS NOT NULL AND length(btrim(content)) >= %(min_chars)s "
    "AND msg_type NOT IN %(excluded)s"
)
_ORDER = " ORDER BY id"


class MessageSourceError(Exception):
    """Raised when the messages source (DB) cannot be read."""


def parse_database_url(url):
    """Split a postgres:// URL into (host, port, dbname, user, password) kwargs.

    Accepts postgres:// and postgresql:// schemes; tolerates missing password
    and query-string parameters (e.g. ?sslmode=...). Returns an empty dict when
    the URL is unusable so the caller can report a precise error.
    """
    if not url or not str(url).strip():
        return {}
    match = re.match(r"^(?:postgres|postgresql)://([^:/?#]+)(?::([^@/?#]*))?@([^:/?#]+)(?::(\d+))?/([^?#]*)", str(url).strip())
    if not match:
        return {}
    user, password, host, port, dbname = match.groups()
    kwargs = {"host": host, "dbname": dbname or "postgres"}
    if user:
        kwargs["user"] = user
    if password:
        kwargs["password"] = password
    if port:
        kwargs["port"] = int(port)
    return kwargs


def _utc_iso(value):
    """Normalize a datetime/timestamp to a UTC ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class MessageSource:
    """Read-only window over the omniagent messages table (psycopg2)."""

    def __init__(self, database_url="", timeout=10):
        self.database_url = (database_url or os.environ.get("DATABASE_URL") or "").strip()
        self.timeout = float(timeout or 10)

    def _connect(self):
        try:
            import psycopg2  # lazy: wiki-only deployments need no driver
        except Exception as exc:
            raise MessageSourceError(
                "messages indexing requires the psycopg2 package (not installed): %s" % exc
            ) from exc
        kwargs = parse_database_url(self.database_url)
        if not kwargs:
            raise MessageSourceError(
                "database_url is not configured or not a postgres URL "
                "(set the plugin config 'database_url' or DATABASE_URL)")
        try:
            return psycopg2.connect(connect_timeout=int(self.timeout), **kwargs)
        except Exception as exc:
            raise MessageSourceError(
                "cannot connect to the omniagent database (%s): %s"
                % (kwargs.get("host", "?"), exc)
            ) from exc

    def fetch_messages(self, exclude_msg_types=None, channels=None,
                       since=None, until=None, limit=None, min_chars=8):
        """Return a list of message dicts, oldest first.

        Each dict: {message_id, channel_id, thread_id, role, created_at,
        created_ts, content}. `exclude_msg_types` filters out agent-internal
        bookkeeping rows; `channels` / `since` / `until` / `limit` narrow the
        window. Read-only: only SELECT statements are ever issued.
        """
        excluded = tuple(sorted(set(exclude_msg_types or DEFAULT_EXCLUDE_MSG_TYPES)))
        clauses = [_SELECT]
        params = {"excluded": excluded, "min_chars": int(min_chars or 0)}
        if channels:
            clauses.append("AND channel_id = ANY(%(channels)s)")
            params["channels"] = list(channels)
        if since is not None:
            clauses.append("AND created_at >= %(since)s")
            params["since"] = since
        if until is not None:
            clauses.append("AND created_at <= %(until)s")
            params["until"] = until
        sql = " ".join(clauses) + _ORDER
        if limit is not None:
            sql += " LIMIT %(limit)s"
            params["limit"] = int(limit)

        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except MessageSourceError:
            raise
        except Exception as exc:
            raise MessageSourceError("messages query failed: %s" % exc) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        messages = []
        for row in rows:
            message_id, channel_id, thread_id, role, created_at, content = row
            created_ts = None
            iso = None
            if created_at is not None:
                created_ts = float(created_at.timestamp())
                iso = _utc_iso(created_at)
            messages.append({
                "message_id": int(message_id),
                "channel_id": str(channel_id or ""),
                "thread_id": int(thread_id) if thread_id is not None else None,
                "role": str(role or ""),
                "created_at": iso,
                "created_ts": created_ts,
                "content": str(content or ""),
            })
        return messages


def source_from_config(config):
    """Build a MessageSource from a load_config() dict."""
    return MessageSource(config.get("database_url", ""), config.get("timeout_secs", 10))