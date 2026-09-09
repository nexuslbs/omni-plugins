#!/usr/bin/env python3
"""Readable HTML -> Markdown extraction backend for the web_extract tool (R2).

Pure-Python stdlib implementation (no pip dependencies, mirroring the rest of
the tools/web plugin). Two public functions are consumed by server.py:

  fetch_page(url) -> dict
      GET the URL (bounded download), return
      {"url": final_url, "html": decoded text, "body_truncated": bool}.

  html_to_markdown(html, base_url="") -> (markdown_text, meta)
      Parse HTML into readable markdown (best-effort readability: junk
      elements such as scripts, styles, navigation, footers, cookie banners,
      ads and forms are dropped; headings, paragraphs, links, images, lists,
      blockquotes, code blocks and simple tables are converted). meta is a
      dict with the page "title" and the first "first_h1" heading.

Design notes (code-improvement plan R2): the markdown is capped INLINE by
server.py, and the FULL extraction is spilled to a file when it exceeds the
cap (DeepSeek caps+spill pattern), so a large page can never flood the agent
context while the complete text stays one filesystem read away.
"""

import gzip
import html as html_mod
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

HTTP_TIMEOUT_SECS = 25
MAX_BODY_BYTES = 2 * 1024 * 1024  # download cap (2 MB) per page
MIN_TEXT_CHARS = 80  # below this the page produced no real readable content
USER_AGENT = (
    "Mozilla/5.0 (compatible; omniagent-web-extract/0.2; "
    "+https://github.com/nexuslbs/omni-plugins)"
)

# Elements whose whole subtree is dropped (scripts, chrome, interactive junk).
SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "iframe", "svg", "canvas",
    "video", "audio", "embed", "object", "applet", "picture", "source",
    "track", "nav", "footer", "aside", "form", "button", "select",
    "option", "optgroup", "textarea", "datalist", "map", "math",
})

# class/id substrings that mark boilerplate/chrome containers to drop.
DISCARD_HINTS = (
    "nav", "footer", "sidebar", "menu", "cookie", "consent", "banner",
    "advert", "sponsor", "promo", "social", "share", "related", "comment",
    "popup", "modal", "breadcrumb", "pagination", "widget", "toolbar",
    "newsletter", "subscribe", "hidden",
)

BLOCK_TEXT_TAGS = frozenset({
    "p", "div", "article", "section", "main", "header", "figure",
    "figcaption", "address", "details", "summary",
})

INLINE_STRONG = frozenset({"strong", "b"})
INLINE_EM = frozenset({"em", "i"})
INLINE_DEL = frozenset({"del", "s", "strike"})

HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _collapse(text):
    """Collapse any run of whitespace to a single space, preserving at most
    one leading and one trailing space. Keeping the edge spaces lets inline
    elements that are separated in the HTML source keep that separation in
    the markdown ("Hello <b>x</b>" -> "Hello **x**", not "Hello**x**")."""
    parts = text.split()
    if not parts:
        return ""
    collapsed = " ".join(parts)
    if text[:1].isspace():
        collapsed = " " + collapsed
    if text[-1:].isspace():
        collapsed += " "
    return collapsed


def _http_error_detail(e):
    try:
        return e.read(200).decode("utf-8", errors="replace")[:200]
    except Exception:
        return ""


def fetch_page(url, timeout=HTTP_TIMEOUT_SECS, max_bytes=MAX_BODY_BYTES):
    """GET url, following redirects, with a bounded body download.

    Returns {"url", "html", "body_truncated"}; raises RuntimeError on HTTP
    error, network failure or non-HTML content type (callers turn that into a
    controlled tool error).
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or url
            ctype = (resp.headers.get("Content-Type") or "").lower()
            main = ctype.split(";", 1)[0].strip()
            if main and main not in ("text/html", "application/xhtml+xml",
                                     "text/plain"):
                raise RuntimeError(
                    "URL returned content-type %r (not HTML); use the raw "
                    "fetch tool for APIs and non-HTML data" % (ctype or "unknown"))
            raw = bytearray()
            truncated = False
            while len(raw) < max_bytes:
                chunk = resp.read(min(65536, max_bytes - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            else:
                truncated = True
            body = bytes(raw)
            if body[:2] == b"\x1f\x8b":  # server gzipped despite our headers
                try:
                    body = gzip.decompress(body)
                    truncated = len(body) > max_bytes
                    body = body[:max_bytes]
                except OSError:
                    pass
            charset = resp.headers.get_content_charset() or "utf-8"
            text = body.decode(charset, errors="replace")
            return {"url": final_url, "html": text, "body_truncated": truncated}
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s%s" % (
            e.code, e.reason, (" " + _http_error_detail(e) if _http_error_detail(e) else "")))
    except urllib.error.URLError as e:
        raise RuntimeError("network error: %s" % (e.reason or e))
    except RuntimeError:
        raise
    except Exception as e:  # pragma: no cover - defensive
        raise RuntimeError("request failed: %s" % e)


def _normalize_ref(value, base_url, what):
    """Resolve and safety-check an href/src. Returns None when unusable."""
    if not value:
        return None
    raw = html_mod.unescape(str(value)).strip()
    if not raw or raw.startswith("#"):
        return None
    if base_url:
        try:
            raw = urllib.parse.urljoin(base_url, raw)
        except ValueError:
            return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return raw


class _HtmlToMarkdown(HTMLParser):
    """Best-effort readable HTML -> Markdown converter (streaming parser).

    Fidelity notes: nested lists/blockquotes and inline markdown inside table
    cells are handled to a pragmatic degree; exotic layouts degrade to plain
    text rather than crashing.
    """

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url or ""
        self.chunks = []            # output pieces (text + structural control)
        self._skip_stack = []       # tags currently dropped (subtree)
        self._in_pre = False
        self._in_a = False
        self._a_buf = []
        self._a_href = None
        self._q = 0                 # blockquote nesting depth
        self._q_line = 0            # pending "> " prefix owed at next content
        self._in_li = 0
        self._lists = []            # open list containers: {"kind","n"}
        self._in_table = False
        self._tbl_row = None
        self._tbl_rows = []
        self._in_cell = False
        self._cell = None
        self._cell_is_head = False
        self._in_title = False
        self._title_buf = []
        self.title = ""
        self._cap_h1 = None
        self._h1_done = False
        self.first_h1 = ""

    # ---- low-level output helpers --------------------------------------

    def _trim_tail(self):
        while self.chunks:
            last = self.chunks[-1]
            stripped = last.rstrip(" \t\r\n")
            if stripped == "":
                self.chunks.pop()
                continue
            if len(stripped) != len(last):
                self.chunks[-1] = stripped
            return

    def _block_start(self):
        """Separate blocks with a blank line; remember the blockquote prefix
        owed on the line that follows (it is emitted lazily with real
        content, so empty quotes never leave a stray bare '>' line)."""
        self._trim_tail()
        self._q_line = self._q
        if not self.chunks:
            return
        self.chunks.append("\n\n")

    def _flush_quote_prefix(self):
        """Emit the pending blockquote prefix ("> " per nesting level) before
        content that opens a new line inside a quote. Lazy: emits nothing
        unless real content follows."""
        if self._q_line:
            self.chunks.append("> " * self._q_line)
            self._q_line = 0

    def _soft_newline(self):
        self._trim_tail()
        if self.chunks:
            self.chunks.append("\n")

    def _emit_inline(self, text):
        if self._in_a:
            self._a_buf.append(text)
        elif self._in_cell:
            self._cell.append(text)
        else:
            self._flush_quote_prefix()
            self.chunks.append(text)

    # ---- parser entry points -------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_stack:
            return
        if self._in_title:
            return
        if self._in_pre:
            return
        if tag in SKIP_TAGS or self._is_discard(attrs):
            self._skip_stack.append(tag)
            return
        if tag in HEADING_LEVELS:
            self._block_start()
            self._flush_quote_prefix()
            self.chunks.append("#" * HEADING_LEVELS[tag] + " ")
            if tag == "h1" and not self._h1_done and self._cap_h1 is None:
                self._cap_h1 = []
            return
        if tag == "p":
            self._start_text_block()
            return
        if tag in BLOCK_TEXT_TAGS:
            self._start_text_block()
            return
        if tag == "blockquote":
            self._q += 1
            self._block_start()
            return
        if tag == "pre":
            self._block_start()
            self.chunks.append("```\n")
            self._in_pre = True
            return
        if tag == "table":
            self._block_start()
            self._in_table = True
            self._tbl_rows = []
            self._tbl_row = None
            return
        if tag in ("ul", "ol"):
            if self._in_li:
                self._soft_newline()
            else:
                self._block_start()
            self._lists.append({"kind": tag, "n": 0})
            return
        if tag == "li":
            self._start_list_item()
            return
        if tag == "hr":
            self._block_start()
            self._flush_quote_prefix()
            self.chunks.append("---")
            return
        if tag == "br":
            self._soft_newline()
            return
        if tag == "img":
            self._emit_img(attrs)
            return
        if tag == "a":
            self._in_a = True
            self._a_buf = []
            href = dict(attrs).get("href", "")
            self._a_href = _normalize_ref(href, self.base_url, "link")
            return
        if tag == "title":
            self._in_title = True
            self._title_buf = []
            return
        if self._in_table and tag == "tr":
            self._tbl_row = []
            return
        if self._in_table and tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
            self._cell_is_head = (tag == "th")
            return
        if tag in INLINE_STRONG:
            self._emit_inline("**")
            return
        if tag in INLINE_EM:
            self._emit_inline("*")
            return
        if tag in INLINE_DEL:
            self._emit_inline("~~")
            return
        if tag == "code":
            self._emit_inline("`")
            return
        # everything else (span, small, abbr, ...): inline text passes through

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if self._in_pre:
            if tag == "pre":
                self._in_pre = False
                if self.chunks and not self.chunks[-1].endswith("\n"):
                    self.chunks.append("\n")
                self.chunks.append("```")
                self._block_start()
            return
        if self._in_title:
            if tag == "title":
                self._in_title = False
                self.title = "".join(self._title_buf).strip()
                self._title_buf = []
            return
        if self._in_table:
            if tag in ("td", "th"):
                text = "".join(self._cell or []).strip()
                if self._tbl_row is not None:
                    self._tbl_row.append((self._cell_is_head, text))
                self._in_cell = False
                self._cell = None
            elif tag == "tr":
                if self._tbl_row is not None:
                    self._tbl_rows.append(self._tbl_row)
                self._tbl_row = None
            elif tag == "table":
                self._in_table = False
                rendered = self._render_table()
                if rendered:
                    self._flush_quote_prefix()
                    self.chunks.append(rendered)
                    self._block_start()
            return
        if self._in_a and tag == "a":
            self._in_a = False
            text = "".join(self._a_buf).strip()
            href = self._a_href
            if text and href:
                self._emit_inline("[%s](%s)" % (text, href))
            elif text:
                self._emit_inline(text)
            self._a_buf = []
            self._a_href = None
            return
        if tag in HEADING_LEVELS:
            if tag == "h1" and self._cap_h1 is not None:
                self.first_h1 = "".join(self._cap_h1).strip()
                self._h1_done = True
                self._cap_h1 = None
            return
        if tag == "blockquote":
            if self._q > 0:
                self._q -= 1
                if self._q == 0:
                    self._q_line = 0
            return
        if tag == "li":
            if self._in_li > 0:
                self._in_li -= 1
            return
        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            if self._in_li == 0:
                self._block_start()
            return
        if tag in INLINE_STRONG:
            self._emit_inline("**")
            return
        if tag in INLINE_EM:
            self._emit_inline("*")
            return
        if tag in INLINE_DEL:
            self._emit_inline("~~")
            return
        if tag == "code":
            self._emit_inline("`")
            return
        # block text tags and anything else: no end action needed

    def handle_data(self, data):
        if self._skip_stack or self._in_title:
            if self._in_title and not self._skip_stack:
                self._title_buf.append(data)
            return
        if self._in_pre:
            self.chunks.append(data)
            return
        text = _collapse(data)
        if not text:
            return
        if self._in_a:
            self._a_buf.append(text)
            return
        if self._in_cell:
            self._cell.append(text)
            return
        if text[:1] == " ":
            # Fresh block start: a leading space would turn the line into a
            # markdown code block, so drop it when we are at a line boundary
            # (nothing yet, after a block separator, or after a quote prefix).
            prev = self.chunks[-1] if self.chunks else ""
            if not prev or prev[-1:] in ("\n", " ", ">") or not prev.rstrip(" \t\r\n"):
                text = text[1:]
        if self._cap_h1 is not None:
            self._cap_h1.append(text)
        self._flush_quote_prefix()
        if text:
            self.chunks.append(text)

    # ---- helpers --------------------------------------------------------

    def _is_discard(self, attrs):
        d = dict(attrs)
        cls = (d.get("class") or "").lower()
        idv = (d.get("id") or "").lower()
        for hint in DISCARD_HINTS:
            if hint in cls or hint in idv:
                return True
        return False

    def _start_text_block(self):
        if self._in_li:
            self._soft_newline()
            self.chunks.append("  " * len(self._lists))
            return
        self._block_start()

    def _start_list_item(self):
        if self.chunks and not self.chunks[-1].endswith("\n"):
            self.chunks.append("\n")
            self._q_line = self._q
        if not self._lists:
            self._lists.append({"kind": "ul", "n": 0})
        top = self._lists[-1]
        indent = "  " * (len(self._lists) - 1)
        if top["kind"] == "ol":
            top["n"] += 1
            marker = "%d. " % top["n"]
        else:
            marker = "- "
        self._flush_quote_prefix()
        self.chunks.append(indent + marker)
        self._in_li += 1

    def _emit_img(self, attrs):
        d = dict(attrs)
        src = _normalize_ref(d.get("src", ""), self.base_url, "image")
        if not src:
            return
        alt = html_mod.unescape(str(d.get("alt", "") or "")).strip()
        syntax = "![%s](%s)" % (alt, src)
        if self._in_a:
            self._a_buf.append(syntax)
        elif self._in_cell:
            self._cell.append(syntax)
        else:
            self._flush_quote_prefix()
            self.chunks.append(syntax)

    def _render_table(self):
        rows = self._tbl_rows
        self._tbl_rows = []
        if not rows:
            return ""
        lines = []
        first = rows[0]
        has_head = bool(first) and any(is_head for is_head, _ in first)
        start = 0
        if has_head:
            lines.append("| " + " | ".join(
                c.replace("|", "\\|") for _, c in first) + " |")
            lines.append("| " + " | ".join("---" for _ in first) + " |")
            start = 1
        for row in rows[start:]:
            if not row:
                continue
            lines.append("| " + " | ".join(
                c.replace("|", "\\|") for _, c in row) + " |")
        return "\n".join(lines)


def _postprocess(text):
    """Normalize the raw markdown: max one blank line between blocks, drop
    empty quote lines, keep code fences verbatim."""
    out = []
    fence = False
    blank = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("```"):
            fence = not fence
        if not fence:
            if line.strip() == "":
                if out and blank == 0:
                    out.append("")
                blank = 1
                continue
            blank = 0
            if re.match(r"^(> )+$", line):  # bare quote prefix, no content
                continue
            out.append(line)
        else:
            blank = 0
            out.append(raw_line)
    return "\n".join(out).strip()


def html_to_markdown(html_text, base_url=""):
    """Parse HTML text into readable markdown.

    Returns (markdown, meta) where meta = {"title": str, "first_h1": str}.
    """
    parser = _HtmlToMarkdown(base_url=base_url)
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception as e:  # pragma: no cover - defensive
        raise RuntimeError("HTML parse failed: %s" % e)
    markdown = _postprocess("".join(parser.chunks))
    meta = {
        "title": parser.title,
        "first_h1": parser.first_h1,
    }
    return markdown, meta
