# playwright-mcp (omni-plugins tool)

External MCP server plugin that exposes [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)
to omniagent as a stdio MCP server running the **official Playwright MCP container**
(`mcr.microsoft.com/playwright/mcp`) through the docker CLI that is already available inside the
omniagent container.

## Why a container (and not `npx @playwright/mcp`)

* No browser binary and no system libraries are needed in the omniagent image: the official image
  ships headless Chromium plus its dependencies.
* Upgrades are a single image tag bump; nothing is installed inside the agent container.
* The repository `microsoft/playwright-mcp` has **no `plugin.json` / `mcp-config.json`**, so it
  cannot be included directly through the omniagent remote-plugin (git clone) mechanism. This plugin
  is therefore the sanctioned fallback: the MCP is packaged here in `omni-plugins` and referenced
  from `config/remote.yml` exactly like the other `tools/*` plugins.

## Configuration (mcp-config.json)

| Field | Value | Why |
|---|---|---|
| `transport` | `stdio` | The MCP client spawns `docker run -i ...`; the JSON-RPC stream is the container's stdio. |
| `command` / `args` | `docker run -i --rm --init mcr.microsoft.com/playwright/mcp ...` | Official README recipe ("Docker", stdio variant). `--rm` guarantees no container lingers after a thread dies. |
| `--headless` | on | omnidev/CI have no display; headless Chromium only (the image supports only headless Chromium). |
| `--isolated` | on | In-memory profile: no `.cache/ms-playwright` profile collisions between concurrent threads. |
| `--no-sandbox` | on | Chromium sandbox is not usable in the container without extra capabilities (as in the upstream docker recipe). |
| `--timeout-action=10000` | 10 s (default 5 s in 0.x, 5 s upstream) | Bounds a single click/type/fill so a stuck element cannot hold a tool call. |
| `--timeout-navigation=30000` | 30 s (upstream default 60 s) | Bounds `browser_navigate`; a hanging site fails fast instead of stalling the thread. |
| `timeout_secs` | `120` | Second layer: the omniagent MCP client aborts the whole call after 120 s. |
| `max_retries` | `1` | No retry storm on a broken site. |
| `pool_size` | `1` | One browser session per agent session (a persistent profile cannot be shared). |
| `allowed_tools` | `["*"]` | All core Playwright tools (navigate/snapshot/find/click/fill_form/...). |

## Token cost (measured - see the wiki reference page)

Tool schemas plus accessibility snapshots are the expensive part, not the browser. Rules of thumb
(full numbers: `profiles/omni/wiki/Reference/Omniagent/Playwright-MCP.md`):

1. `fetch` / `web_extract` first - a plain HTTP GET is ~100x cheaper than a browser round trip.
2. Use the browser only when the page needs JavaScript, forms, auth or a session.
3. Prefer `browser_find` (a few matching nodes) over `browser_snapshot` (the whole tree), and pass
   `depth` to cap the tree when a full snapshot is unavoidable.
4. Accessibility tree only - never screenshot-driven flows (`browser_take_screenshot` costs image
   tokens and cannot be acted on).

## Install (dev / prod)

`config/plugins.yml`: `mcp-playwright: {enabled: true, source: remote, config: {}}`
`config/remote.yml`: `mcp-playwright: {url: <omni-plugins repo URL>, path: tools/playwright-mcp}`

Then `POST /api/plugins/install-git {"url": "...", "path": "tools/playwright-mcp", "name": "mcp-playwright"}`
and reload. The runtime loads `<data_dir>/plugins/tools/.remote/mcp-playwright/tools/playwright-mcp/mcp-config.json`.
