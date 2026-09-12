# prompt (Python, remote) - exact-parity port of the builtin Rust prompt plugin

`tools/prompt/server.py` is a **remote** (stdio MCP, `python3 server.py`) Python
implementation of the builtin Rust prompt plugin
(`omniagent/plugins/tools/prompt/`, binary `mcp-server-prompt`).

The contract is **exact behavioural parity**: for identical inputs and identical
plugin config the Python server must return **byte-identical** payloads. The ONLY
allowed difference is the implementation language (plus the unavoidable
remote-vs-builtin packaging difference: the builtin uses `entrypoint` in
`plugin.json`, the remote uses `mcp-config.json`).

## Tools

| tool | parity source |
| --- | --- |
| `prompt_generate` | `omniagent/plugins/tools/prompt/src/main.rs` (+ `prompt_builder.rs`, `memory_store.rs`, `chat_message.rs`) |
| `prompt_compact-messages` | `omniagent/plugins/tools/prompt/src/main.rs` (+ `compact.rs`, `notes.rs`, `dump.rs`) |

Both tools keep the core `{plugin}__{tool}` naming grammar and the schemas in
`main.rs` (`prompt_generate`, `prompt_compact-messages`).

## Config

`plugin.json` `config_schema` is a literal mirror of the builtin manifest: same
keys, types, defaults, labels and descriptions (all 15 keys). The manifest is
asserted against `tests/fixtures/rust_plugin_config_schema.json` (and against the
live `omniagent` checkout when it is present) by
`tests/test_parity.py::ManifestParity`.

Runtime env (injected by the omniagent plugin config): `OMNI_DIR` (omni root) and
`DATABASE_URL`. `requirements.txt` is installed by the plugin installer
(`install_python_deps`) into `{plugin_dir}/.venv` or `{plugin_dir}/pylib`;
`server.py` puts that site-packages directory on `sys.path` itself, because the
runtime spawns the plugin with the system `python3`.

## Parity procedure (how to verify and how to re-sync)

Always run against the **omnidev dev stack** (containers `omnidev-*`, env
`/opt/workspace/omni-deployer/omnidev.env`). Never against production.

### 1. Unit suite (schemas, grammar, budget math, tool registry)

```bash
docker exec -w /opt/workspace/omni-plugins/tools/prompt omnidev-omniagent-1 \
    python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Covers: manifest parity, tool names/order/schemas, `{plugin}__{tool}` grammar in
the guidance + read-tool recognition, hard/soft budget resolution, tokenizer
chars/4 fallback, compaction null-contract and frozen-summary semantics.

### 2. Differential harness (the acceptance gate)

Drives BOTH servers - the compiled builtin binary and `python3 server.py` - over
the SAME inputs / DB state with the same config, and compares the returned
payloads byte-for-byte. Exit code 0 = zero unexplained diffs.

```bash
# builtin binary (built from source in omnidev)
docker exec -w /opt/workspace/omni-plugins/tools/prompt omnidev-omniagent-1 \
    python3 tests/diff_rust_python.py \
        --rust-bin /target/release/mcp-server-prompt \
        --thread-id <real-dev-thread-id> --channel-id omnidev --profile omni
```

Case families: `tools/list`, `prompt_generate` (plain, platform hint,
tool-name list, tool descriptors, complex message, plan/no-plan), and
`prompt_compact-messages` (under/over budget, descriptor-driven read tools,
lexical read tools, force override, missing budgets, tokenizer encoding,
dump/auto-notes artifacts).

Per-field diffing of `prompt_generate` (used while porting, handy for
regressions) lives in `tests/diff_fields.py`.

### 3. Re-sync after a Rust change

1. `git -C /opt/workspace/omniagent log --oneline -- plugins/tools/prompt/` and
   diff the range since the last Python sync (the header of `server.py` names the
   Rust files it mirrors).
2. Re-port every behaviour-affecting change (prompt text, section order, caps,
   budget math, read-tool recognition, error strings).
3. Re-run steps 1 + 2 above; a non-zero diff count is a failure, not a warning.
4. If `plugin.json` changed in Rust, update `tests/fixtures/rust_plugin_config_schema.json`
   (regenerate with `PROMPT_PARITY_UPDATE=1`).

### 4. Dev-stack smoke (remote install)

Install this plugin from GitHub in the dev stack (plugin-manager `install-git`,
`https://github.com/nexuslbs/omni-plugins`, subdir `tools/prompt`), confirm
`prompt_generate` + `prompt_compact-messages` register, and issue one real call
of each. Switching the PRODUCTION `plugins.yml` prompt entry from
`source: built-in` to this remote plugin is out of scope.

## Documented differences (all non-behavioural)

* Language: Python 3 vs Rust.
* Packaging: `mcp-config.json` (`python3 server.py`, stdio) vs the builtin
  `entrypoint`; `id`/`name` follow the omni-plugins manifest convention.
* `server.py` bootstraps its own dependency path (see above); the builtin links
  `tiktoken_rs` at build time. Token counts and the chars/4 fallback are
  identical.
