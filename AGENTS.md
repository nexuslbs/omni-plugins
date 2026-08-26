# omni-plugins: AGENTS.md

Agent-facing conventions for working in this repository.

## What this repo is

Source for **remote plugins** (installed into a deployment via
`config/remote.yml` or the dashboard) and for the **plugin-less provider
definitions** in the root `models.yml`. It is NOT a deploy target: plugins
here are consumed by omni-stack / omni-root deployments at runtime.

## Layout conventions

- `tools/<name>/` - MCP tool plugin (Rust crate or Python server) with a
  `plugin.json` manifest at the root.
- `platforms/<name>/` - platform plugin (inbound/outbound messaging) with
  `plugin.json`.
- `providers/<name>/` - provider plugin (LLM provider adapter) with
  `plugin.json`.
- `models.yml` (root) - plugin-less provider definitions; keep provider
  entries here in sync with what deployments expect (`providers.<name>.plugin:
  false` for plugin-less, `true`/plugin name for plugin-backed).
- `remote.yml` / `remote.test.yml` - remote source lists used by the
  omni-deployer integration suite. Do not remove entries the tests rely on.

## Build / test conventions

- Rust tool plugins: `cargo check` (and `cargo fmt --check`) inside the
  plugin directory must pass; they compile standalone (no omniagent
  dependency - `mcp-server-util` is the shared runtime).
- Python plugins: `python3 -m py_compile` on every `.py` file.
- The omni-deployer `scripts/tests.py` installs plugins from this repo
  (`install-git`) and verifies MCP tools register; changes to plugin
  manifests/entrypoints may break those tests - run the plugin groups
  (install/enable/remove/update) before pushing.

## Commit conventions

- One logical change per commit; messages follow the repo history style
  (`feat(...)`, `fix(...)`, `chore(...)`, `docs(...)`).
- Never commit `target/`, `node_modules/`, `.git/`, `data/`, `.remote/`.
- Push to `main` only; never force-push.

## License

MIT - see [LICENSE](LICENSE).
