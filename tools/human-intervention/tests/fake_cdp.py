#!/usr/bin/env python3
"""Fake browser for the generic page-state probe.

Serves the two CDP surfaces the plugin uses, with a mutable snapshot so a test
can flip the page from BLOCKED to RESOLVED:

  * HTTP  GET /json/list                -> one `page` target with a ws URL
  * WS    Runtime.evaluate              -> {"result": {"result": {"value": <snapshot>}}}

No detection logic lives here: the fake merely returns whatever structural
snapshot the test currently holds.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeBrowser:
    def __init__(self, snapshot: dict | None = None):
        self.snapshot = dict(snapshot or {
            "url": "https://example.test/target", "title": "target",
            "ready": "complete", "overlay_count": 1, "large_frame_count": 0,
            "form_count": 0, "text_len": 12,
        })
        self.http = None
        self.http_thread = None
        self.ws_sock = None
        self.ws_thread = None
        self.ws_port = 0
        self.evaluations = 0
        self.url = ""

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        self._start_ws()
        handler = self._handler_class()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        return self.url

    def stop(self):
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.ws_sock is not None:
            try:
                self.ws_sock.close()
            finally:
                self.ws_sock = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()

    def resolve(self):
        """Flip the page to the RESOLVED structural state."""
        self.snapshot.update({"overlay_count": 0, "large_frame_count": 0,
                              "title": "target", "ready": "complete",
                              "form_count": 1, "text_len": 420})

    # ── websocket server ───────────────────────────────────────────────────
    def _start_ws(self):
        self.ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ws_sock.bind(("127.0.0.1", 0))
        self.ws_sock.listen(4)
        self.ws_port = self.ws_sock.getsockname()[1]
        self.ws_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.ws_thread.start()

    def _accept_loop(self):
        while self.ws_sock is not None:
            try:
                conn, _ = self.ws_sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_ws, args=(conn,), daemon=True).start()

    def _serve_ws(self, conn: socket.socket):
        try:
            conn.settimeout(5)
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                handshake += chunk
            head = handshake.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            key = ""
            for line in head.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
            while True:
                opcode, payload = _read_frame(conn)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    _send_frame(conn, 0xA, payload)
                    continue
                if opcode not in (0x1, 0x0):
                    continue
                msg = json.loads(payload.decode("utf-8"))
                if msg.get("method") == "Runtime.evaluate":
                    self.evaluations += 1
                    reply = {"id": msg.get("id"), "result": {"result": {
                        "type": "object", "value": dict(self.snapshot)}}}
                else:
                    reply = {"id": msg.get("id"), "result": {}}
                _send_frame(conn, 0x1, json.dumps(reply).encode("utf-8"))
        except (OSError, ValueError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ── HTTP /json/list ────────────────────────────────────────────────────
    def _handler_class(self):
        browser = self
        ws_url = f"ws://127.0.0.1:{self.ws_port}/devtools/page/target-1"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/json/list":
                    raw = json.dumps([{
                        "id": "target-1", "type": "page",
                        "url": browser.snapshot.get("url"), "title": browser.snapshot.get("title"),
                        "webSocketDebuggerUrl": ws_url,
                    }]).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        return Handler


def _read_frame(sock: socket.socket):
    def recv_exactly(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf.extend(chunk)
        return bytes(buf)

    b1, b2 = recv_exactly(2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exactly(8))[0]
    mask = recv_exactly(4) if masked else b""
    payload = recv_exactly(length) if length else b""
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def _send_frame(sock: socket.socket, opcode: int, payload: bytes = b""):
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    sock.sendall(bytes(header) + payload)
