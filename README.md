# omni-plugins

Test, reference, and remote-installable plugins for [OmniAgent](https://github.com/nexuslbs/omniagent).

This repository is the source for **remote plugins** (installed via
`config/remote.yml` / the dashboard "Install from git" flow) and for the
**plugin-less provider definitions** used by deployments.

## What lives here

| Path | Purpose |
|------|---------|
| `models.yml` | Plugin-less provider definitions (DeepSeek, OpenAI, OpenCode Go, Noop, ...): base URLs, model lists, API modes, token budgets. Deployments merge this into their `config/models.yml`. |
| `providers/` | Provider plugins (e.g. `noop`, `noop-full` test providers) |
| `platforms/` | Platform plugins (e.g. `telegram`, plus `test-*` reference implementations) |
| `tools/` | MCP tool plugins (e.g. `prompt`, `hindsight`, `memory`, `actions`, `paperclip`, plus `test-*`/`cron-*`/`cosmos-*` reference tools) |
| `remote.yml` / `remote.test.yml` | Remote plugin source lists used by the omni-deployer integration tests |

## Structure of a plugin

Every plugin is a self-contained directory with a `plugin.json` manifest at its
root (MCP tools/platforms) or a provider manifest:

```json
{
  "name": "my-tool",
  "type": "tool",
  "description": "What the tool does",
  "entrypoint": "server.py",
  "input_schema": { ... }
}
```

Plugins are installed into a deployment's `plugins/{type}/.remote/{name}/`
directory via the remote-install flow; they are never committed to the
omniagent image.

## Related repositories

| Repository | Description |
|-----------|-------------|
| [nexuslbs/omniagent](https://github.com/nexuslbs/omniagent) | Core agent (Rust API, MCP framework, LLM execution) |
| [nexuslbs/omni-dashboard](https://github.com/nexuslbs/omni-dashboard) | Web dashboard (Vite + TypeScript SPA) |
| [nexuslbs/omni-stack](https://github.com/nexuslbs/omni-stack) | Docker Compose stack + OMNI_DIR config |
| [nexuslbs/omni-root](https://github.com/nexuslbs/omni-root) | Runtime config/wiki/state mirror fork of omni-stack |
| [nexuslbs/omni-deployer](https://github.com/nexuslbs/omni-deployer) | deploy.py + integration test suite |

## License

MIT - see [LICENSE](LICENSE).
