#!/usr/bin/env python3
"""web MCP server : web_search (provider abstraction) + web_extract (R2).

Tools:
  - web_search : search the web through a swappable provider engine.
  - web_extract: readable HTML -> Markdown extraction from a URL (cap + spill).

web_search providers (provider-abstraction, mirroring OpenClaw/Hermes):
  - tavily  - POST JSON, api_key in body (https://tavily.com)
  - brave   - GET, X-Subscription-Token header (https://brave.com/search/api/)
  - exa     - POST JSON, x-api-key header (https://exa.ai)
  - ddg     - keyless DuckDuckGo HTML endpoint, parsed with stdlib html.parser

web_extract (code-improvement plan R2): fetches a page (static HTML only,
bounded 2 MB download), converts the readable content to Markdown via
tools/web/extract.py (stdlib html.parser; drops scripts/nav/ads/forms), and
returns title + final URL + markdown. Output discipline: the inline result is
capped (default 8000 chars, max 50000); when the extraction exceeds the cap
the FULL markdown is spilled to a file whose path is reported, so a huge page
can never flood the agent context.

Configuration is read from env vars. A deployment sets these through the
plugin config (config_schema keys). API keys are stored in the secrets store
and referenced by NAME ($secret:NAME, resolved by the core at configure time)
so that no key ever appears in a repo or config file:

  SEARCH_PROVIDER        default provider when the call omits 'provider' (tavily)
  TAVILY_API_KEY         Tavily API key            (secret, referenced by name)
  BRAVE_API_KEY          Brave Search API key      (secret, referenced by name)
  EXA_API_KEY            Exa API key               (secret, referenced by name)
  WEB_EXTRACT_SPILL_DIR  dir for web_extract spill files (default: system tmp)
  TAVILY_API_URL / BRAVE_API_URL / EXA_API_URL / DDG_URL
                         endpoint overrides (tests/mirrors; official defaults)

Output discipline (code-improvement plan R1): web_search results are capped
(max_results clamped to 1..10), every snippet is truncated, and the whole
inline result is capped with an explicit "[truncated ...]" note when the cap
trips, so a huge provider response can never flood the agent context.

MCP JSON-RPC over stdio. Python stdlib only (no pip dependencies),
mirroring tools/actions/server.py.
"""

import hashlib
import html as html_mod
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import extract  # sibling module (tools/web/extract.py): fetch_page + html_to_markdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [web] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("web-mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"

PROVIDERS = ("tavily", "brave", "ddg", "exa")

DEFAULT_URLS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "exa": "https://api.exa.ai/search",
    "ddg": "https://html.duckduckgo.com/html/",
}

KEY_ENV = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "exa": "EXA_API_KEY",
    "ddg": None,  # keyless
}

URL_ENV = {
    "tavily": "TAVILY_API_URL",
    "brave": "BRAVE_API_URL",
    "exa": "EXA_API_URL",
    "ddg": "DDG_URL",
}

MAX_RESULTS_DEFAULT = 5
MAX_RESULTS_HARD = 10
SNIPPET_MAX_CHARS = 300
INLINE_MAX_CHARS = 3500  # items are snippet-capped (~300 chars each, max 10),
                         # so 3500 keeps the explicit truncation note reachable
HTTP_TIMEOUT_SECS = 20
USER_AGENT = "Mozilla/5.0 (compatible; omniagent-web-search/0.1; +https://github.com/nexuslbs/omni-plugins)"

# web_extract output discipline (R2): inline cap + spill-to-file.
EXTRACT_INLINE_MAX_CHARS = 8000    # default inline cap for the markdown
EXTRACT_MAX_CHARS_MIN = 1000       # user-provided max_chars clamp
EXTRACT_MAX_CHARS_MAX = 50000      # hard ceiling for the inline cap


# --------------------------------------------------------------------------
# JSON-RPC / MCP protocol helpers
# --------------------------------------------------------------------------

def send_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def make_success(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": str(text)}], "isError": bool(is_error)}


# --------------------------------------------------------------------------
# config helpers
# --------------------------------------------------------------------------

def cfg(key, default=""):
    val = (os.environ.get(key) or "").strip()
    return val if val else default


def provider_endpoint(provider):
    return cfg(URL_ENV[provider], DEFAULT_URLS[provider])


def provider_key(provider):
    env = KEY_ENV[provider]
    if env is None:
        return None  # ddg is keyless
    value = cfg(env)
    return value or None  # missing/empty key means "not configured"


# --------------------------------------------------------------------------
# HTTP helpers (stdlib urllib; never log credentials)
# --------------------------------------------------------------------------

def _request(url, method="GET", headers=None, payload=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    req.add_header("User-Agent", USER_AGENT)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=HTTP_TIMEOUT_SECS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return e.code, "", "HTTP %s: %s%s" % (e.code, e.reason, (" " + detail if detail else ""))
    except urllib.error.URLError as e:
        return 0, "", "network error: %s" % (e.reason or e)
    except Exception as e:  # pragma: no cover - defensive
        return 0, "", "request failed: %s" % e


def _clean_text(value, limit=SNIPPET_MAX_CHARS):
    if value is None:
        return ""
    text = str(value)
    text = html_mod.unescape(text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


# --------------------------------------------------------------------------
# provider searches (each returns list of {title, url, snippet})
# --------------------------------------------------------------------------

def _search_tavily(query, max_results, key):
    url = provider_endpoint("tavily")
    status, body, err = _request(
        url, method="POST",
        headers={"Content-Type": "application/json"},
        payload={"api_key": key, "query": query, "max_results": max_results},
    )
    if err:
        raise RuntimeError("tavily request failed: %s" % err)
    try:
        data = json.loads(body)
    except ValueError as e:
        raise RuntimeError("tavily returned invalid JSON (HTTP %s): %s" % (status, e))
    results = data.get("results") or []
    out = []
    for item in results:
        out.append({
            "title": _clean_text(item.get("title", ""), 200),
            "url": str(item.get("url", "")),
            "snippet": _clean_text(item.get("content", item.get("snippet", ""))),
        })
    return out


def _search_brave(query, max_results, key):
    params = urllib.parse.urlencode({"q": query, "count": max_results})
    url = "%s?%s" % (provider_endpoint("brave"), params)
    status, body, err = _request(
        url, method="GET",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    if err:
        raise RuntimeError("brave request failed: %s" % err)
    try:
        data = json.loads(body)
    except ValueError as e:
        raise RuntimeError("brave returned invalid JSON (HTTP %s): %s" % (status, e))
    results = ((data.get("web") or {}).get("results")) or []
    out = []
    for item in results:
        out.append({
            "title": _clean_text(item.get("title", ""), 200),
            "url": str(item.get("url", "")),
            "snippet": _clean_text(item.get("description", item.get("snippet", ""))),
        })
    return out


def _search_exa(query, max_results, key):
    url = provider_endpoint("exa")
    status, body, err = _request(
        url, method="POST",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        payload={"query": query, "numResults": max_results},
    )
    if err:
        raise RuntimeError("exa request failed: %s" % err)
    try:
        data = json.loads(body)
    except ValueError as e:
        raise RuntimeError("exa returned invalid JSON (HTTP %s): %s" % (status, e))
    results = data.get("results") or []
    out = []
    for item in results:
        out.append({
            "title": _clean_text(item.get("title", ""), 200),
            "url": str(item.get("url", "")),
            "snippet": _clean_text(item.get("text", item.get("snippet", ""))),
        })
    return out


class _DdgParser(HTMLParser):
    """Collect DuckDuckGo HTML results: result__a anchors + result__snippet."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_a = False
        self._in_snippet = False
        self._cls = ""
        self._buf = []
        self._href = ""
        self.anchors = []    # (title, url)
        self.snippets = []   # snippet text

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        cls = dict(attrs).get("class", "") or ""
        if "result__a" in cls.split():
            self._in_a = True
            self._cls = "a"
            self._buf = []
            self._href = dict(attrs).get("href", "")
        elif "result__snippet" in cls.split():
            self._in_a = True
            self._cls = "snippet"
            self._buf = []

    def handle_data(self, data):
        if self._in_a:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._in_a:
            return
        text = "".join(self._buf).strip()
        if self._cls == "a":
            self.anchors.append((text, self._href))
        elif self._cls == "snippet":
            self.snippets.append(text)
        self._in_a = False
        self._cls = ""
        self._buf = []


def _ddg_real_url(href):
    """DuckDuckGo wraps result URLs in /l/?uddg=<encoded>&rut=... ; unwrap."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "/l/?uddg=" in href:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return href


def _search_ddg(query, max_results, key):
    del key  # ddg is keyless
    params = urllib.parse.urlencode({"q": query})
    url = "%s?%s" % (provider_endpoint("ddg"), params)
    status, body, err = _request(url, method="GET", headers={"Accept": "text/html"})
    if err:
        raise RuntimeError("ddg request failed: %s" % err)
    parser = _DdgParser()
    try:
        parser.feed(body)
    except Exception as e:  # pragma: no cover - defensive
        raise RuntimeError("ddg HTML parse failed: %s" % e)
    out = []
    for i, (title, href) in enumerate(parser.anchors[:max_results]):
        snippet = parser.snippets[i] if i < len(parser.snippets) else ""
        out.append({
            "title": _clean_text(title, 200),
            "url": _ddg_real_url(href),
            "snippet": _clean_text(snippet),
        })
    return out


SEARCHERS = {
    "tavily": _search_tavily,
    "brave": _search_brave,
    "exa": _search_exa,
    "ddg": _search_ddg,
}


# --------------------------------------------------------------------------
# tool: web_search
# --------------------------------------------------------------------------

def handle_web_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        return make_tool_result(
            "web_search error: 'query' is required (string, the search keywords)", True)
    provider = str(args.get("provider") or cfg("SEARCH_PROVIDER", "tavily")).strip().lower()
    if provider not in PROVIDERS:
        return make_tool_result(
            "web_search error: unknown provider %r (choose from %s)"
            % (provider, ", ".join(PROVIDERS)), True)
    try:
        max_results = int(args.get("max_results") or MAX_RESULTS_DEFAULT)
    except (TypeError, ValueError):
        max_results = MAX_RESULTS_DEFAULT
    if max_results <= 0:
        max_results = MAX_RESULTS_DEFAULT
    max_results = max(1, min(max_results, MAX_RESULTS_HARD))

    key = provider_key(provider)
    if key is None and provider != "ddg":
        return make_tool_result(
            "web_search error: no %s API key configured for provider '%s'. "
            "Store the key in the secrets store and reference it by name in the "
            "plugin config, e.g. config %s = $secret:%s "
            "(provider 'ddg' needs no key)."
            % (KEY_ENV[provider], provider, KEY_ENV[provider], KEY_ENV[provider]), True)

    try:
        results = SEARCHERS[provider](query, max_results, key or "")
    except RuntimeError as e:
        return make_tool_result("web_search error: %s" % e, True)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("web_search %s crashed", provider)
        return make_tool_result("web_search error: unexpected failure: %s" % e, True)

    if not results:
        return make_tool_result(
            "web_search: no results for %r (provider: %s)" % (query, provider))

    # Output discipline: cap + explicit truncation note.
    lines = ["# web_search results (provider: %s, query: %s)" % (provider, query)]
    rendered = 0
    for i, r in enumerate(results, start=1):
        block = "\n\n### %d. %s\nurl: %s\n%s" % (
            i, r["title"] or "(no title)", r["url"] or "(no url)", r["snippet"] or "")
        if sum(len(l) for l in lines) + len(block) > INLINE_MAX_CHARS:
            remaining = len(results) - rendered
            lines.append(
                "\n\n[truncated: output capped at %d chars; %d more result(s) omitted]"
                % (INLINE_MAX_CHARS, remaining))
            break
        lines.append(block)
        rendered += 1
    return make_tool_result("".join(lines))


# --------------------------------------------------------------------------
# tool: web_extract
# --------------------------------------------------------------------------

def _spill_web_extract(url, markdown):
    """Write the full extraction to a spill file.

    Uses WEB_EXTRACT_SPILL_DIR when configured (a host-visible path under the
    deployment data dir) and falls back to the system temp dir. Returns
    (path, byte_size) or raises on failure.
    """
    spill_dir = cfg("WEB_EXTRACT_SPILL_DIR") or tempfile.gettempdir()
    try:
        os.makedirs(spill_dir, exist_ok=True)
    except Exception:
        spill_dir = tempfile.gettempdir()
        try:
            os.makedirs(spill_dir, exist_ok=True)
        except Exception:
            pass
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    path = os.path.join(spill_dir, "web_extract_%s_%s.md" % (digest, int(time.time())))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    return path, len(markdown.encode("utf-8"))


def handle_web_extract(args):
    url = str(args.get("url") or "").strip()
    if not url:
        return make_tool_result(
            "web_extract error: 'url' is required (string, the http(s) URL to extract)", True)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return make_tool_result(
            "web_extract error: unsupported URL %r: only http(s) URLs are "
            "supported (use the raw fetch tool for other schemes)" % url, True)
    # Inline cap (user-adjustable, hard-clamped).
    max_chars = EXTRACT_INLINE_MAX_CHARS
    raw_max = args.get("max_chars")
    if raw_max not in (None, ""):
        try:
            max_chars = int(raw_max)
        except (TypeError, ValueError):
            max_chars = EXTRACT_INLINE_MAX_CHARS
    max_chars = max(EXTRACT_MAX_CHARS_MIN, min(max_chars, EXTRACT_MAX_CHARS_MAX))

    try:
        page = extract.fetch_page(url)
    except RuntimeError as e:
        return make_tool_result("web_extract error: %s" % e, True)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("web_extract fetch crashed")
        return make_tool_result("web_extract error: unexpected failure: %s" % e, True)

    final_url = page.get("url") or url
    try:
        markdown, meta = extract.html_to_markdown(page.get("html") or "", base_url=final_url)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("web_extract parse crashed")
        return make_tool_result("web_extract error: %s" % e, True)

    if not markdown:
        return make_tool_result(
            "web_extract: no readable text content found at %s (the page may be "
            "JavaScript-rendered; this tool extracts static HTML only)" % final_url)

    title = (meta.get("title") or meta.get("first_h1") or "").strip()
    if page.get("body_truncated"):
        markdown += ("\n\n[note: the page body exceeded the download cap; the "
                     "extraction may be incomplete]")
    header = ["# web_extract: %s" % (title or final_url),
              "source url: %s" % final_url, ""]
    header_len = sum(len(s) + 1 for s in header)

    if len(markdown) + header_len <= max_chars:
        return make_tool_result("\n".join(header) + "\n" + markdown)

    # Output discipline: cap + spill. The full extraction goes to a file whose
    # path is always reported first so it can never be cut by the cap.
    total = len(markdown)
    try:
        spill_path, _size = _spill_web_extract(final_url, markdown)
    except Exception as e:
        log.exception("web_extract spill failed")
        return make_tool_result(
            "web_extract error: extraction is %d chars (inline cap %d) and the "
            "spill file could not be written: %s" % (total, max_chars, e), True)
    note = ("[truncated: extraction is %d chars; inline capped at %d. FULL "
            "extraction spilled to file: %s (read it with filesystem_read)]"
            % (total, max_chars, spill_path))
    budget = max_chars - header_len - len(note) - 1
    if budget > 0:
        return make_tool_result(
            "\n".join(header) + "\n" + note + "\n\n" + markdown[:budget] +
            "\n\n[... preview truncated; see the spilled file for the full text]")
    return make_tool_result("\n".join(header) + "\n" + note)


TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web and return ranked results (title, url, snippet). "
            "Provider abstraction over Tavily, Brave, DuckDuckGo ('ddg', keyless) "
            "and Exa; the engine is selected per call via 'provider' and defaults to "
            "the configured SEARCH_PROVIDER. Results are capped (default 5, max 10) "
            "and truncated with an explicit note when large. Use this to discover "
            "URLs and current facts the agent does not already know."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search keywords (required)."},
                "provider": {"type": "string",
                             "enum": list(PROVIDERS),
                             "description": "Search engine: tavily, brave, ddg (keyless), exa. Default: configured SEARCH_PROVIDER."},
                "max_results": {"type": "integer",
                                "description": "Max results to return (1-10, default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_extract",
        "description": (
            "Extract the readable content of a web page as Markdown: fetch the "
            "URL (static HTML only, bounded 2 MB download), drop scripts, nav, "
            "ads, forms and boilerplate, and convert headings, paragraphs, "
            "links, images, lists, quotes, code and simple tables. Returns the "
            "page title, the final URL after redirects, and the markdown, "
            "capped inline (default 8000 chars; raise with 'max_chars' up to "
            "50000). When the extraction exceeds the cap the FULL text is "
            "spilled to a file whose path is reported first, so a huge page "
            "can never flood the agent context. Use after web_search to read "
            "a result page in depth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "http(s) URL of the page to extract (required)."},
                "max_chars": {"type": "integer",
                              "description": "Inline character cap (1000-50000, default 8000). Over-cap extractions are spilled to a file."},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {
    "web_search": handle_web_search,
    "web_extract": handle_web_extract,
}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def handle_initialize(req):
    return make_success(req.get("id"), {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "web", "version": "0.2.0"},
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
