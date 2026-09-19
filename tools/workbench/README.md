# workbench (MCP tool plugin)

A **thin fetch wrapper** over the [workbench](https://github.com/nexuslbs/workbench)
HTTP API. It exposes exactly **one** MCP tool, `tool` - exposed to the agent as
**`workbench__tool`** - and forwards every call as a single HTTP request. There
is no business logic, no SDK, no state and no retry/backoff: the plugin never
inspects or reshapes the workbench payload.

## Tool

`workbench__tool`

| arg | type | required | description |
| --- | ---- | -------- | ----------- |
| `tool` | string | yes | workbench tool/command name, e.g. `hello_world` |
| `params` | object | no (default `{}`) | arguments passed to the workbench tool |

Agent call example:

```json
{"name": "workbench__tool", "arguments": {"tool": "hello_world", "params": {}}}
```

## HTTP contract

```
POST {base_url}{tool_path}
Content-Type: application/json
Accept: application/json

{"tool": "<tool>", "params": {<params>}}
```

Defaults: `base_url = http://workbench:8080` (the workbench compose service DNS
name inside the omni docker network; the dev overlay publishes host port
`12347`), `tool_path = /api/tool/call`.

> **Workbench-side endpoint status (2026-09-19).** The workbench core web seam
> currently registers only `GET /api/web/pages`
> (`src/web/providers/http.ts`) plus the shell assets and the `/health` status
> endpoint, and the workbench UI plugins register their own UI routes
> (`settings`, `plugin-inventory`, `plugin-manager`, `cordis-ui`). There is
> **no by-name tool/command invocation endpoint** in workbench today: commands
> are invoked through the CLI registry (`workbench <command> [args...]`).
> This wrapper therefore implements the agreed contract above with
> `tool_path = /api/tool/call`; if/when workbench exposes a real invocation
> route, only `tool_path` changes (a config value, no code change). The gap is
> tracked as a companion change on the `workbench` board.

## Response handling

- 2xx: the response body is returned as MCP text content, pretty-printed when
  it is JSON.
- Non-2xx: `isError: true` with the status code, status text and body.
- Connection refused / DNS failure / timeout (`AbortController`,
  `timeout_secs`) / unreadable body: a clear `isError` message. Never a crash,
  never a hang.
- Missing/invalid `tool` argument: JSON-RPC error `-32602`.

Calls are **concurrent**: the stdio readline loop never awaits a call, so 150
parallel `workbench__tool` calls run in flight together.

## Config (`plugin.json` `config_schema`)

| key | type | default | description |
| --- | ---- | ------- | ----------- |
| `base_url` | string | `http://workbench:8080` | workbench HTTP API base URL |
| `tool_path` | string | `/api/tool/call` | path appended to `base_url` |
| `timeout_secs` | integer | `60` | HTTP timeout per call |
| `auth_header` | string | `""` | optional `Authorization` header value (e.g. `Bearer $secret:WORKBENCH_TOKEN`) |

Values reach the plugin through the `configure` JSON-RPC request and/or as
environment variables (`base_url` / `BASE_URL` / `WORKBENCH_BASE_URL`). Secret
references use `$env:VAR` / `$secret:NAME` (never a literal `${VAR}`).

## Runtime

Node >= 18 (global `fetch` + `AbortController`), stdio JSON-RPC like
`tools/test-js-tool/server.js`. **No npm dependencies.**

## Local smoke test

```bash
node --check server.js
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | node server.js
```
