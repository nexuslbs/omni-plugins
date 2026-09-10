# engram (remote BINARY plugin)

[Engram](https://github.com/Gentleman-Programming/engram) is a single
self-contained Go binary (SQLite + FTS5) that exposes an MCP stdio server.
There is **no build step**: the deliverable is a prebuilt archive published on
GitHub Releases. This directory therefore contains only the plugin manifest -
`git clone` (the `install-git` endpoint) stays clone-only, and the plugin
INSTALL action downloads the artifact declared in `plugin.json`.

## Manifest

`plugin.json` declares the artifact in the `binary` field:

```json
"binary": {
  "url": "https://github.com/Gentleman-Programming/engram/releases/download/v{version}/engram_{version}_{os}_{goarch}.tar.gz",
  "version": "1.20.0",
  "file": "engram",
  "member": "engram",
  "format": "tar.gz",
  "checksums": { "linux-x86_64": "sha256:<hex>" },
  "assets": { "windows-x86_64": "https://.../engram_1.20.0_windows_amd64.zip" }
}
```

- `url` is a template: `{version}`, `{os}` (linux/darwin/windows),
  `{arch}` (x86_64/aarch64) and `{goarch}` (amd64/arm64) are substituted from
  the running platform.
- `assets` maps `<os>-<arch>` to an explicit URL and takes precedence over
  `url` (for releases that do not follow one naming scheme).
- `checksums` maps `<os>-<arch>` to the expected SHA-256 (`sha256:<hex>` or
  bare hex); `checksum` applies to every platform. A mismatch fails the
  install and leaves no file behind.
- `auth` (`$secret:NAME`) resolves a credential from the secrets store for
  private artifacts. Never put a literal token here.
- `entrypoint` is the MCP start command: `engram mcp` over stdio. Omniagent
  resolves it inside the plugin directory first, so the installed binary is
  used as soon as it exists, and a missing binary produces an explicit
  "run install" message instead of a silent no-tools plugin.

## Installing (dev)

```sh
# clone only (install-git semantics, unchanged)
curl -sX POST localhost:8080/api/plugins/tools/remote/engram/download
# download + verify + place the artifact, then hot-reload the MCP server
curl -sX POST localhost:8080/api/plugins/tools/remote/engram/install
```

The artifact lands as `engram` (mode 0755) next to this `plugin.json`; a
re-install re-fetches and replaces it (idempotent). Install status (declared /
installed / version / expected checksum / size) is part of the plugin detail
API response (`binary` field) and of the plugin page.
