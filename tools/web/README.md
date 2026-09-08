# web : web_search tool plugin (R1, provider abstraction)

Python MCP tool server exposing **`web_search`** - ranked web search with a
swappable provider engine, plus room for a future `web_extract` (R2) in the
same server.

- Repo: `tools/web` (remote plugin; consumed by omni-stack deployments via
  `config/remote.yml` / the dashboard install-git flow).
- Server name `web`, MCP stdio, Python stdlib only (no pip/npm/cargo deps;
  gate: `python3 -m py_compile server.py`).

## Providers (provider-abstraction)

| provider | engine | auth |
|---|---|---|
| `tavily` (default) | https://tavily.com | `TAVILY_API_KEY` (POST body) |
| `brave` | https://brave.com/search/api/ | `BRAVE_API_KEY` (X-Subscription-Token) |
| `exa` | https://exa.ai | `EXA_API_KEY` (x-api-key) |
| `ddg` | DuckDuckGo HTML | keyless |

Pick an engine per call with the `provider` argument (`tavily | brave | ddg |
exa`); the configured `SEARCH_PROVIDER` is the default when omitted. Engines
are swappable without touching the agent: configure a different key/provider.

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

- `max_results` default 5, hard-clamped to 1..10.
- Every snippet is normalized and truncated (~300 chars).
- The whole inline result is capped (3500 chars); when the cap trips an
  explicit `[truncated ...]` note reports how many results were omitted, so a
  large provider response can never flood the agent context.

## Local checks

```sh
python3 -m py_compile server.py
# functional smoke (mocked provider endpoints, no network):
python3 /opt/workspace/tmp/webpatch/verify_web.py   # optional dev scratch
```

Registration/integration is covered by `scripts/test_plugins.py`
(EXPECTED_TOOLS entry `web: ["web_search"]`); without a configured key the
suite records the invoke as a documented skip (external dep unavailable),
matching the existing external-dependency skip pattern.
