# exec — sandboxed code/shell execution tool (R3)

MCP tool plugin providing two tools with an explicit approval gate:

- **exec_run**: propose a shell/code command for sandboxed execution. NEVER
  executes: records the exact command as a pending approval request and
  returns a request id.
- **exec_approve**: approve a pending request (`request_id` + the
  operator-held `EXEC_APPROVAL_KEY`) and run the exact stored command in a
  throwaway, network-isolated sandbox container. Single-use per request,
  TTL-bounded (default 600 s), fully audited.

## No production access, by design

The approved command runs in a disposable docker container with:

- `--network none` — no network devices at all: cannot reach production
  containers, the production DB, host ports, or the internet;
- `--user 65534:65534` + `--cap-drop ALL` + `--security-opt
  no-new-privileges` — nobody uid, zero capabilities, no privilege
  escalation;
- `--read-only` rootfs with a 16 MB tmpfs `/tmp` — nothing else writable;
- a minimal explicit environment (`PATH`, `HOME=/tmp`, `LANG`, `TMPDIR`,
  `PYTHONDONTWRITEBYTECODE`) — the plugin process env (including the
  approval key) is NEVER forwarded;
- cpu/mem/pids resource bounds and a hard timeout with forced kill;
- no volume mounts, element-wise argv construction (no shell), operator
  pinned + validated image.

Fail closed: missing docker CLI, missing/invalid `EXEC_IMAGE` or resource
flags => the run errors out; nothing ever falls back to an un-sandboxed
execution.

## Approval gate

- Default configuration is **inert**: without `EXEC_APPROVAL_KEY` (or with
  `EXEC_APPROVAL_MODE=deny`) `exec_run` refuses and nothing can be
  approved.
- The approval key is referenced from the secrets store by name
  (`$secret:EXEC_APPROVAL_KEY`) and is never committed; only sha256 digests
  of keys/commands are stored in the audit log.
- A request can be approved exactly once; denied/expired/already-used
  requests never execute.

## Configuration (config_schema keys -> env vars)

| Key | Default | Meaning |
|-----|---------|---------|
| EXEC_APPROVAL_MODE | require | require = every run needs approval; deny = inert |
| EXEC_APPROVAL_KEY | (empty) | operator secret for exec_approve (empty => inert) |
| EXEC_APPROVAL_TTL_SECS | 600 | approval request validity (60-3600) |
| EXEC_IMAGE | (empty) | pinned sandbox image, must exist locally (empty => execution denied) |
| EXEC_TIMEOUT_SECS | 30 | default run timeout (1-300) |
| EXEC_INLINE_MAX_CHARS | 8000 | inline result cap (1000-50000) |
| EXEC_OUTPUT_HARD_CAP | 1048576 | captured bytes before kill (65536-16777216) |
| EXEC_SPILL_DIR | system tmp | spill/state dir (requests + audit.jsonl + .out spills) |
| EXEC_CPUS / EXEC_MEM / EXEC_PIDS | 1 / 256m / 64 | container resource bounds |

## Output discipline

Merged stdout+stderr; capture hard-capped (default 1 MiB, then kill). The
inline result is capped (default 8000 chars); when output exceeds the cap
the FULL output is spilled to `EXEC_SPILL_DIR/exec_<sha10>_<epoch>.out`
whose path is reported first, so a huge run can never flood the agent
context.

## Structure

- `server.py` — MCP stdio server (exec_run / exec_approve), python stdlib only
- `sandbox.py` — RequestStore (approval requests + audit) and DockerRunner
  (throwaway-container backend)
- `plugin.json` / `mcp-config.json` — plugin manifests and config schema
- design + approval-gate spec: `docs/r3-sandboxed-exec-design.md`
