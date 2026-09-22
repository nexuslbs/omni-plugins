#!/usr/bin/env python3
"""Minimal, dependency-free Chrome DevTools Protocol client (stdlib only).

Only what the generic page-state probe needs:

  * list_page_targets(base_url)  -> GET {base}/json/list   (HTTP, urllib)
  * evaluate(base_url, js)       -> ONE Runtime.evaluate    (WebSocket, RFC 6455)

The WebSocket client is deliberately tiny (text frames only, client-masked,
ping/pong/close handled, CDP events skipped while waiting for the response id)
so the plugin keeps ZERO third-party dependencies: omniagent spawns plugin
children with an EMPTY environment and no package manager, so `websocket-client`
or `requests` may not exist.

NOTE (site-knowledge-free): this module transports a caller-supplied JS
expression; it contains NO page classification, no vendor vocabulary and no
challenge detection logic (that is `server.py`'s structural predicate).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import urllib.parse
import urllib.request

CDP_HTTP_TIMEOUT = float(os.environ.get("HI_CDP_HTTP_TIMEOUT", "10"))


class CdpError(RuntimeError):
    """Any transport/protocol failure while talking CDP."""


def base_url(url: str | None = None) -> str:
    url = (url or os.environ.get("HI_CDP_URL") or "http://browser:9222").strip()
    return url.rstrip("/")


def http_get_json(url: str, timeout: float = CDP_HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (ops-provided URL)
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def list_page_targets(url: str | None = None, timeout: float = CDP_HTTP_TIMEOUT):
    """Return the page targets exposed by the browser's /json/list endpoint."""
    targets = http_get_json(f"{base_url(url)}/json/list", timeout=timeout)
    pages = [t for t in targets if (t.get("type") == "page")]
    return pages


def pick_page_target(url: str | None = None, target_url: str | None = None,
                     timeout: float = CDP_HTTP_TIMEOUT):
    """Pick ONE page target: exact `target_url` match when given, else the first
    page target. Raises CdpError when the browser exposes no page target."""
    pages = list_page_targets(url, timeout=timeout)
    if not pages:
        raise CdpError("no page target exposed by the browser")
    if target_url:
        for page in pages:
            if page.get("url") == target_url:
                return page
    return pages[0]


# ── RFC 6455 client ─────────────────────────────────────────────────────────


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise CdpError("websocket closed while reading")
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(sock: socket.socket):
    """Read ONE websocket frame -> (opcode, payload bytes)."""
    b1, b2 = _recv_exactly(sock, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exactly(sock, 8))[0]
    mask = _recv_exactly(sock, 4) if masked else b""
    payload = _recv_exactly(sock, length) if length else b""
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def _send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])  # FIN + text
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < (1 << 16):
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    mask = os.urandom(4)  # client frames MUST be masked
    header.extend(mask)
    masked_payload = bytes(payload[i] ^ mask[i % 4] for i in range(n))
    sock.sendall(bytes(header) + masked_payload)


def _send_control(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode, 0x80 | len(payload)])
    header.extend(mask)
    header.extend(bytes(payload[i] ^ mask[i % 4] for i in range(len(payload))))
    sock.sendall(bytes(header))


class CdpSession:
    """ONE websocket to a page target; `call()` sends a CDP command and returns
    the `result` object of the matching response (CDP events are skipped)."""

    def __init__(self, ws_url: str, timeout: float = CDP_HTTP_TIMEOUT):
        self.ws_url = ws_url
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "CdpSession":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def connect(self) -> None:
        parts = urllib.parse.urlsplit(self.ws_url)
        if parts.scheme not in ("ws", "wss"):
            raise CdpError(f"not a websocket url: {self.ws_url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if parts.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        raw.sendall(handshake.encode("ascii"))
        buffered = b""
        while b"\r\n\r\n" not in buffered:
            chunk = raw.recv(4096)
            if not chunk:
                raise CdpError("websocket handshake failed: connection closed")
            buffered += chunk
        head, _, rest = buffered.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raw.close()
            raise CdpError(f"websocket handshake failed: {status_line}")
        self._sock = raw
        if rest:  # cannot happen with a spec-compliant peer (no frames pre-101)
            raise CdpError("unexpected bytes after handshake")

    def close(self) -> None:
        if self._sock is not None:
            try:
                _send_control(self._sock, 0x8)
            except OSError:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- protocol ----------------------------------------------------------
    def call(self, method: str, params: dict | None = None) -> dict:
        if self._sock is None:
            raise CdpError("session is not connected")
        self._next_id += 1
        msg_id = self._next_id
        _send_text(self._sock, json.dumps(
            {"id": msg_id, "method": method, "params": params or {}}))
        while True:
            opcode, payload = _read_frame(self._sock)
            if opcode == 0x8:
                raise CdpError("websocket closed by peer")
            if opcode == 0x9:  # ping -> pong
                _send_control(self._sock, 0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode not in (0x0, 0x1):
                continue
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if msg.get("id") != msg_id:
                continue  # CDP event (or a stray response): skip
            if "error" in msg:
                raise CdpError(f"CDP error on {method}: {msg['error']}")
            return msg.get("result") or {}


def evaluate(expression: str, url: str | None = None, target_url: str | None = None,
             timeout: float = CDP_HTTP_TIMEOUT, by_value: bool = True):
    """Run `expression` in the page target and return the value (`by_value`) or
    the raw RemoteObject."""
    page = pick_page_target(url, target_url, timeout=timeout)
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CdpError("page target exposes no webSocketDebuggerUrl "
                       "(the browser must be started with --remote-debugging-port)")
    with CdpSession(ws_url, timeout=timeout) as session:
        result = session.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": by_value,
            "awaitPromise": False,
        })
    remote = result.get("result") or {}
    if result.get("exceptionDetails"):
        raise CdpError(f"page exception: {result['exceptionDetails']}")
    return remote.get("value")
