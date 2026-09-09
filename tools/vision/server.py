#!/usr/bin/env python3
"""vision MCP server : read_image (R7), a model-gated vision tool.

Tool:
  - read_image : read an image from a local file path or an http(s) URL and
    return its metadata (format, mime, pixel dimensions, byte size, sha256
    prefix) together with a base64 data URI the caller can hand to a
    vision-capable model. Images are never decoded or re-encoded: the tool
    only probes container headers (pure stdlib, no Pillow dependency) and
    passes the original bytes through.

Model gate (fail closed): the tool is ONLY usable when the deployment
operator has declared the active model vision-capable. The core does not
send the per-thread model name to MCP plugins, so the gate is configuration
driven:

  - VISION_MODELS (comma-separated) names the models that support
    image/vision input.
  - VISION_ACTIVE_MODEL names the deployment's active model when pinned.
  - Both empty  => plugin inert: every call returns a guard refusal.
  - ACTIVE set and NOT in VISION_MODELS => guard refusal naming the model.
  - ACTIVE set and IN VISION_MODELS    => allowed.
  - ACTIVE empty and VISION_MODELS non-empty => allowed (operator declared
    the deployment vision-capable; any vision model in VISION_MODELS may
    consume the output).

With no vision-capable model configured the plugin is inert and refuses
everything, mirroring the exec plugin's fail-closed posture.

Output discipline (cap + spill): the data URI is returned inline only while
the whole result stays under VISION_INLINE_MAX_CHARS; larger URIs are
spilled to VISION_SPILL_DIR/read_image_<sha10>_<epoch>.uri and the file path
is reported FIRST so it can never be cut by the inline cap. Files are
bounded by VISION_MAX_BYTES (default 10 MiB).

MCP JSON-RPC over stdio. Python stdlib only (no pip dependencies),
mirroring tools/exec/server.py and tools/web/server.py.
"""

import base64
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

import image_probe  # sibling module: pure-stdlib image probing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vision] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("vision-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"

VERSION = "0.1.0"


# --------------------------------------------------------------------------
# config (env vars resolved by the core from config_schema keys)
# --------------------------------------------------------------------------

def cfg(key, default=""):
    val = (os.environ.get(key) or "").strip()
    return val if val else default


def cfg_int(key, default, lo, hi):
    try:
        value = int(cfg(key, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


VISION_MODELS = [m.strip() for m in cfg("VISION_MODELS").split(",") if m.strip()]
ACTIVE_MODEL = cfg("VISION_ACTIVE_MODEL")
MAX_BYTES = cfg_int("VISION_MAX_BYTES", 10485760, 65536, 67108864)
INLINE_MAX_CHARS = cfg_int("VISION_INLINE_MAX_CHARS", 8000, 1000, 50000)
SPILL_DIR = cfg("VISION_SPILL_DIR")
if not SPILL_DIR:
    SPILL_DIR = os.path.join(tempfile.gettempdir(), "omni-vision")

_URL_TIMEOUT_SECS = 20
_URL_MAX_REDIRECTS = 5


# --------------------------------------------------------------------------
# JSON-RPC / MCP protocol helpers
# --------------------------------------------------------------------------

def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def make_tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": str(text)}],
            "isError": bool(is_error)}


# --------------------------------------------------------------------------
# model gate
# --------------------------------------------------------------------------

def gate_open():
    """True when the deployment is configured vision-capable."""
    if not VISION_MODELS:
        return False
    if not ACTIVE_MODEL:
        return True
    return ACTIVE_MODEL in VISION_MODELS


def gate_refusal():
    if not VISION_MODELS:
        return ("[vision] read_image is disabled: no vision-capable model is "
                "configured (VISION_MODELS is empty). An operator must list "
                "the models that support image input, e.g. VISION_MODELS="
                "deepseek-vl2. Nothing was read.")
    if ACTIVE_MODEL and ACTIVE_MODEL not in VISION_MODELS:
        return ("[vision] read_image is disabled: the active model %r is not "
                "declared vision-capable (VISION_MODELS=%s). Reading images "
                "for a text-only model would only waste context; nothing was "
                "read. An operator must add the active model to VISION_MODELS "
                "if it supports image input."
                % (ACTIVE_MODEL, ",".join(VISION_MODELS)))
    return ("[vision] read_image is disabled in this deployment. Nothing was "
            "read.")


# --------------------------------------------------------------------------
# read_image implementation
# --------------------------------------------------------------------------

def _read_local(path):
    try:
        st = os.stat(path)
    except OSError as e:
        raise image_probe.ImageProbeError("cannot stat %r: %s" % (path, e))
    if not os.path.isfile(path):
        raise image_probe.ImageProbeError("%r is not a regular file" % path)
    if st.st_size > MAX_BYTES:
        raise image_probe.ImageProbeError(
            "image at %r is %d bytes; VISION_MAX_BYTES is %d"
            % (path, st.st_size, MAX_BYTES))
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as e:
        raise image_probe.ImageProbeError("cannot read %r: %s" % (path, e))


class _BoundedReader:
    """Streams an http response, refusing to buffer past MAX_BYTES."""

    def __init__(self, resp, cap):
        self._resp = resp
        self._cap = cap

    def read(self):
        chunks = []
        total = 0
        while True:
            chunk = self._resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self._cap:
                raise image_probe.ImageProbeError(
                    "image from URL exceeds VISION_MAX_BYTES (%d bytes)" %
                    self._cap)
            chunks.append(chunk)
        return b"".join(chunks)


def _fetch_url(url):
    if not url.lower().startswith(("http://", "https://")):
        raise image_probe.ImageProbeError(
            "read_image url must be http(s); got %r (file paths go in the "
            "'path' argument)" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "omni-vision/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=_URL_TIMEOUT_SECS) as resp:
            if resp.status >= 400:
                raise image_probe.ImageProbeError(
                    "URL returned HTTP %d" % resp.status)
            reader = _BoundedReader(resp, MAX_BYTES)
            return reader.read()
    except urllib.error.HTTPError as e:
        raise image_probe.ImageProbeError("URL returned HTTP %d" % e.code)
    except urllib.error.URLError as e:
        raise image_probe.ImageProbeError("URL could not be fetched: %s" % e.reason)
    except OSError as e:
        raise image_probe.ImageProbeError("URL could not be fetched: %s" % e)


def _spill_uri(sha10, uri_text):
    os.makedirs(SPILL_DIR, exist_ok=True)
    path = os.path.join(SPILL_DIR, "read_image_%s_%d.uri"
                        % (sha10, int(time.time())))
    with open(path, "w", encoding="ascii") as fh:
        fh.write(uri_text)
    return path


def _format_result(source_desc, meta, raw):
    fmt = meta["format"]
    mime = meta["mime"]
    width = meta["width"]
    height = meta["height"]
    size = len(raw)
    sha10 = meta["sha256"][:10]
    b64 = base64.b64encode(raw).decode("ascii")
    uri = "data:%s;base64,%s" % (mime, b64)

    header = [
        "# read_image result",
        "source: %s" % source_desc,
        "format: %s (%s)" % (fmt, mime),
        "dimensions: %dx%d px" % (width, height),
        "bytes: %d" % size,
        "sha256 (prefix): %s" % sha10,
        "",
    ]
    header_len = sum(len(s) + 1 for s in header)
    data_uri_len = len(uri) + 1  # trailing newline

    if header_len + data_uri_len <= INLINE_MAX_CHARS:
        return make_tool_result("\n".join(header) + "\n" + uri + "\n")

    try:
        spill_path = _spill_uri(sha10, uri)
    except Exception as e:
        log.exception("vision spill failed")
        return make_tool_result(
            "read_image error: data URI is %d chars (inline cap %d) and the "
            "spill file could not be written: %s"
            % (len(uri), INLINE_MAX_CHARS, e), True)

    note = ("[data URI is %d chars; inline capped at %d. FULL data URI "
            "spilled to file: %s (read it with filesystem_read, then pass "
            "its content to the vision-capable model)]"
            % (len(uri), INLINE_MAX_CHARS, spill_path))
    budget = INLINE_MAX_CHARS - header_len - len(note) - 1
    if budget > 8:
        return make_tool_result(
            "\n".join(header) + "\n" + note + "\n\n" + uri[:budget] +
            "\n\n[... data URI truncated in the preview; the spilled file "
            "holds the complete data URI]")
    return make_tool_result("\n".join(header) + "\n" + note)


def handle_read_image(args):
    path = args.get("path")
    url = args.get("url")
    if path is not None and url is not None:
        return make_tool_result(
            "read_image error: provide exactly one of 'path' or 'url', not "
            "both", True)
    if path is None and url is None:
        return make_tool_result(
            "read_image error: 'path' (local image file) or 'url' (http(s) "
            "image URL) is required", True)

    if not gate_open():
        return make_tool_result(gate_refusal())

    try:
        if url is not None:
            url = str(url).strip()
            if not url:
                return make_tool_result(
                    "read_image error: 'url' must not be empty", True)
            raw = _fetch_url(url)
            source_desc = url
        else:
            path = str(path).strip()
            if not path:
                return make_tool_result(
                    "read_image error: 'path' must not be empty", True)
            raw = _read_local(path)
            source_desc = path
    except image_probe.ImageProbeError as e:
        return make_tool_result("read_image error: %s" % e, True)

    try:
        meta = image_probe.probe(raw)
    except image_probe.ImageProbeError as e:
        return make_tool_result("read_image error: %s" % e, True)

    log.info("read_image %s format=%s %dx%d bytes=%d", source_desc,
             meta["format"], meta["width"], meta["height"], len(raw))
    return _format_result(source_desc, meta, raw)


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "read_image",
        "description": (
            "Read an image from a local file path or an http(s) URL and "
            "return its metadata (format, mime, pixel dimensions, byte size, "
            "sha256 prefix) plus a base64 data URI (data:<mime>;base64,...) "
            "ready to hand to a vision-capable model. MODEL-GATED: this tool "
            "refuses to run when the deployment's active model is not "
            "declared vision-capable (see VISION_MODELS / VISION_ACTIVE_MODEL "
            "plugin config); with no vision-capable model configured the tool "
            "is inert and returns a guard refusal. Large data URIs are "
            "spilled to a file whose path is reported first (cap + spill). "
            "Provide exactly one of 'path' (local image file, PNG/JPEG/GIF/"
            "WebP/BMP) or 'url' (http/https image URL)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Local path of the image file to read (PNG/JPEG/GIF/WebP/BMP). Mutually exclusive with 'url'."},
                "url": {"type": "string",
                        "description": "http(s) URL of the image to fetch and read. Mutually exclusive with 'path'."},
            },
        },
    },
]

HANDLERS = {
    "read_image": handle_read_image,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "vision", "version": VERSION},
    })


def handle_tools_call(msg):
    rid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in HANDLERS:
        send_json(make_error(rid, -32601, "Unknown tool: %s" % name))
        return
    try:
        result = HANDLERS[name](args)
    except Exception as e:
        log.exception("tool %s crashed", name)
        result = make_tool_result("%s error: %s" % (name, e), True)
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


if __name__ == "__main__":
    main()
