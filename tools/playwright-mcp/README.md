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
| `transport` | `stdio` | The MCP client spawns a child process; the JSON-RPC stream is its stdio. |
| `command` / `args` | `sh -c '<wrapper>'` | The wrapper (2026-09-10) resolves the **host** path of the omni dir, prepares `data/playwright/`, then `exec docker run -i --rm --init ...`. The env-isolated MCP client passes no ambient variables, so the container mount path and the secret plumbing must be computed by the child itself. |
| `-v <omni>/data/playwright:/pw` | omni data dir | Storage state, `--secrets` dotenv and MCP output live in the omni DATA dir (gitignored, not the repo). |
| `--add-host=host.docker.internal:host-gateway` | on | Lets a page on the host (demo/test site, local dashboard) be reached from inside the browser container. |
| `--storage-state=/pw/state/<file>` | per site, env `PW_STATE_FILE`, default `sessions.json` | Session persistence: cookies + localStorage are loaded at startup and reused, so a task logs in ONCE instead of in a loop (X5). |
| `--secrets=/pw/state/secrets.env` | only when `PW_SECRET_*` env vars are configured | Redaction: playwright-mcp replaces every occurrence of a secret VALUE in tool output with `<secret>NAME</secret>`. |
| `--output-dir=/pw/output` | omni data dir | Session logs / snapshot files stay out of the chat and out of the repo. |
| `--headless` | on | omnidev/CI have no display; headless Chromium only (the image supports only headless Chromium). |
| `--isolated` | on | In-memory browser profile: no `.cache/ms-playwright` profile collisions between concurrent threads. Storage state is the only cross-run persistence by design. |
| `--no-sandbox` | on | Chromium sandbox is not usable in the container without extra capabilities (as in the upstream docker recipe). |
| `--timeout-action=10000` | 10 s | Bounds a single click/type/fill so a stuck element cannot hold a tool call. |
| `--timeout-navigation=30000` | 30 s (upstream default 60 s) | Bounds `browser_navigate`; a hanging site fails fast instead of stalling the thread. |
| `timeout_secs` | `120` | Second layer: the omniagent MCP client aborts the whole call after 120 s. |
| `max_retries` | `1` | No retry storm on a broken site. |
| `pool_size` | `1` | One browser session per agent session (a persistent profile cannot be shared). |
| `allowed_tools` | `["*"]` | All core Playwright tools (navigate/snapshot/find/click/fill_form/...). |

## Sessions, storage state and secrets (X5 recipe)

The verified recipe for authenticated web tasks (full flow, evidence and failure modes:
`profiles/omni/skills/web-interaction/SKILL.md` and
`profiles/omni/wiki/Reference/Omniagent/Playwright-MCP.md`).

**1. One storage-state file per site.** The wrapper reads `PW_STATE_FILE` and uses
`/pw/state/<file>`; the same variable names the file the agent must write after login. Use one file
per service (`PW_STATE_FILE` from the plugin `env:` block, or a per-site plugin entry when the
profile needs several at once). The default `sessions.json` is a single shared jar and is only
appropriate when the agent talks to one site.

**2. Secrets never enter the chat, the config or the repo.** Real values live in the omniagent
secrets store only. A site credential is wired by NAME:

```jsonc
// mcp-config.json, servers[].env
"env": { "PW_SECRET_GITHUB_PASSWORD": "$secret:GITHUB_PASSWORD" }
```

The wrapper copies every `PW_SECRET_*` variable into `/pw/state/secrets.env` (dotenv,
`chmod 600`, uid 1000) and passes `--secrets=/pw/state/secrets.env`. playwright-mcp then renders
the value as `<secret>GITHUB_PASSWORD</secret>` in EVERY tool output, so typed credentials and
page-echoed tokens are redacted before they reach the model context (verified: the demo page that
echoes its own token still shows `<secret>DEMO_PASSWORD</secret>` in snapshots and `browser_find`
results).

**3. Cookie hygiene.** Cookies and localStorage stay in the storage-state file under the omni data
dir (`data/playwright`, `chmod 700`, uid 1000, gitignored). Rules for the agent: never paste a
cookie value into chat or notes, never copy the state file into the repo, never read the file back
into the prompt (it is a credential blob), and never "clean up" by deleting it mid-task: the file IS
the session.

**4. Login once, then reuse.** Run 1 navigates to the login page, types user + `password`
(redacted), clicks submit, verifies the logged-in page with `browser_find`, then persists the
session with `browser_run_code_unsafe`:

```js
await (async (page) => { await page.context().storageState({ path: '/pw/state/<site>.json' }); return 'storage state saved'; })(page);
```

Run 2 (any later thread) starts the browser with `--storage-state=/pw/state/<site>.json` and
navigates straight to the authenticated page: no login form, no credentials needed.

**5. Verify after every action, no blind retries.** After each click/type/navigation, confirm the
expected state (URL + the specific text) with a targeted `browser_find`; on a mismatch stop and
report, do not loop. The bounded timeouts above turn a hung page into a bounded error.

**Failure mode seen while building this (fixed):** the browser container runs as uid 1000 while the
wrapper runs as root, so a root-owned `600` `secrets.env` produced
`EACCES: permission denied, open '/pw/state/secrets.env'` and the MCP failed to initialize. The
wrapper therefore `chown`s `data/playwright` to uid 1000 (falling back to world-readable modes only
when the chown is not possible).

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

Verify the session plumbing from the agent container (dev example):

```sh
python3 - <<'PY'
import json; p='/opt/omni/plugins/tools/.remote/mcp-playwright/tools/playwright-mcp/mcp-config.json'
s=json.load(open(p))['servers'][0]; print(s['command'], '--storage-state' in s['args'][1], s['env'])
PY
```
