# web : web_search + web_extract tool plugin (R1 + R2)

Python MCP tool server exposing **`web_search`** (ranked web search with a
swappable provider engine) and **`web_extract`** (readable HTML-to-Markdown
extraction from a URL, with cap + spill output discipline).

- Repo: `tools/web` (remote plugin; consumed by omni-stack deployments via
  `config/remote.yml` / the dashboard install-git flow).
- Server name `web`, MCP stdio, Python stdlib only (no pip/npm/cargo deps;
  gate: `python3 -m py_compile server.py extract.py`).

## web_search: providers (provider-abstraction)

| provider | engine | auth |
|---|---|---|
| `tavily` (default) | https://tavily.com | `TAVILY_API_KEY` (POST body) |
| `brave` | https://brave.com/search/api/ | `BRAVE_API_KEY` (X-Subscription-Token) |
| `exa` | https://exa.ai | `EXA_API_KEY` (x-api-key) |
| `ddg` | DuckDuckGo HTML | keyless |

Pick an engine per call with the `provider` argument (`tavily | brave | ddg |
exa`); the configured `SEARCH_PROVIDER` is the default when omitted. Engines
are swappable without touching the agent: configure a different key/provider.

## web_extract: readable extraction (R2)

`web_extract(url, max_chars=8000)` fetches a page and returns its readable
content as Markdown:

- Static HTML only (bounded 2 MB download, ~25s timeout, redirects followed).
- Best-effort readability: scripts, styles, nav, footers, cookie banners,
  ads, forms and social/share widgets are dropped; headings, paragraphs,
  links, images, lists, blockquotes, code blocks and simple tables are
  converted (`extract.py`, stdlib `html.parser`, no pip deps).
- Returns the page title, the final URL after redirects, and the markdown.
- JavaScript-rendered pages and non-HTML content types produce a controlled,
  actionable error (use the raw fetch tool for APIs / non-HTML data).

### Output discipline (cap + spill)

- Inline result capped at 8000 chars by default (raise per call with
  `max_chars`, hard clamp 1000-50000).
- When the extraction exceeds the cap, the FULL markdown is written to a
  spill file and the tool result reports the spill path first (plus an
  inline preview), so a huge page can never flood the agent context. The
  full text is one `filesystem_read` away.
- Spill location: `WEB_EXTRACT_SPILL_DIR` config key when set (point it at a
  host-visible path under the deployment data dir, e.g.
  `/opt/omni/data/web_extract`), else the system temp dir. File names:
  `web_extract_<sha1(url)10>_<epoch>.md`.

## Configuration and secrets (never commit keys)

API keys are stored in the **secrets store** and referenced **by name**; the
core resolves `$secret:NAME` from the secrets table when the plugin is
configured, so no key ever appears in a repo, config file or log.

Example `plugins.yml` wiring for a deployment:

```yaml
tools:
  web:
    enabled: true
    config:
      SEARCH_PROVIDER: tavily
      TAVILY_API_KEY: $secret:TAVILY_API_KEY
      WEB_EXTRACT_SPILL_DIR: /opt/omni/data/web_extract
```

Create the secret first (secrets API / dashboard / `secrets.env`):
`TAVILY_API_KEY` holds the Tavily key value. Same pattern for
`BRAVE_API_KEY` / `EXA_API_KEY` (`ddg` needs none). If a referenced secret is
missing the plugin reports an actionable error at call time (it never runs
with a literal `$secret:` string).

Optional endpoint overrides (tests/mirrors, not secrets): `TAVILY_API_URL`,
`BRAVE_API_URL`, `EXA_API_URL`, `DDG_URL` - they default to the official
endpoints.

## Output discipline (code-improvement plan R1)

- `web_search` `max_results` default 5, hard-clamped to 1..10.
- Every snippet is normalized and truncated (~300 chars).
- The whole inline result is capped (3500 chars); when the cap trips an
  explicit `[truncated ...]` note reports how many results were omitted.

## Local checks

```sh
python3 -m py_compile server.py extract.py
# functional smoke (local HTTP fixture, no network):
python3 /opt/workspace/tmp/webpatch/verify_web_r2.py   # optional dev scratch
```

Registration/integration is covered by `scripts/test_plugins.py`
(EXPECTED_TOOLS entry `web: ["web_search", "web_extract"]`); invocations that
need an external dependency (a search API key, a reachable web URL) are
recorded as documented skips, matching the existing external-dependency skip
pattern.
