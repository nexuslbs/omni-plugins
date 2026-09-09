# R3 design: `exec` - sandboxed code/shell execution tool plugin

Status: DESIGN (deliverable of the R3 task; implementation follows this spec).
Repo: `nexuslbs/omni-plugins`, plugin at `tools/exec`.
Date: 2026-09-09. Task: `task_omnidev_r3_sandboxed_code_shell_exec_p0_p1` (thread 1537).
Chain: R1 web_search (done), R2 web_extract (done, approved), R3 = this plugin; R4 depends on R3.

## 1. Purpose

Add a plugin tool that lets an agent run bounded shell/code snippets in a
disposable, network-isolated sandbox, subject to an explicit per-run approval
gate, with capped/spilled output. The plugin is developed and verified ONLY
in the omnidev dev stack (omnidev-omniagent-1); it is never installed in a
production deployment, and its runtime construction cannot reach production
containers, the production DB, or the production network (section 4).

Non-goals (v0.1): persistent workspaces, file exchange into/out of the
sandbox, package installation inside the sandbox image, docker-socket
exposure to the payload, auto-approval modes.

## 2. Requirements restated (task body)

- New omni-plugins PLUGIN TOOL (not core).
- DESIGN FIRST: this document is written and committed before implementation.
- Explicit approval gate for execution.
- MUST have NO production access (cannot reach production containers/DB/network
  by design).
- Cap + spill output.
- Develop and verify ONLY in the omnidev stack.

## 3. Plugin shape and conventions

Mirrors the R1/R2 `tools/web` plugin (approved on this board):

- `tools/exec/{plugin.json,mcp-config.json,server.py,sandbox.py,README.md}`
- MCP JSON-RPC over stdio, python stdlib only (no pip deps), server name
  `exec`; tools surface as `exec_run`, `exec_approve` via the
  `{server}_{tool}` convention.
- config_schema keys become env vars; secret values are referenced by name
  (`$secret:EXEC_APPROVAL_KEY`) and resolved by the core at configure time;
  no secret is ever committed.
- Registered in `remote.yml` under `tools:` and in
  `scripts/test_plugins.py` `EXPECTED_TOOLS` as
  `"exec": ["exec_run", "exec_approve"]` (registration proof; benign
  invocations never execute: they hit the approval gate or a missing-key
  deny and return a normal result, matching the existing external-dependency
  documented-skip pattern).

## 4. Threat model and no-production-access guarantees

Environment where the plugin runs: the omniagent container of a deployment.
In omnidev that is `omnidev-omniagent-1` (runs as root, has the docker CLI
and `/var/run/docker.sock`, python 3.13).

What an adversary can control: the `command` string an LLM asks to run
(arbitrary shell text). The plugin itself is vetted code.

Guarantees, each independently enforced:

1. NETWORK: the payload runs in a throwaway docker container launched with
   `--network none`. It has NO network interfaces at all: it cannot reach
   production containers, the production DB, host-published ports, or the
   internet. `--network none` is fixed in code; there is no config key that
   weakens it (host/ bridge modes are rejected alternatives, section 9).
2. PROCESS/PRIVILEGE: the payload runs as uid/gid 65534 (`nobody`,
   `--user 65534:65534`), with `--cap-drop ALL`, `--security-opt
   no-new-privileges`, read-only rootfs (`--read-only`, tmpfs `/tmp`), and
   pids/cpu/memory bounds. It cannot reach the docker socket (not mounted
   into the sandbox container; also the daemon socket is not exposed to the
   payload).
3. EXECUTION CONTROL: nothing ever runs without a valid, unexpired approval
   (section 5). Default configuration (no `EXEC_APPROVAL_KEY`, or mode
   `deny`) makes the plugin inert: `exec_run` refuses at once.
4. FAIL CLOSED: if the docker CLI is missing, the configured image is
   absent/unusable, or any required sandbox flag cannot be applied, the
   plugin returns a deny error and executes nothing. There is no
   un-sandboxed fallback path.
5. SECRET HYGIENE: the child container gets a minimal explicit environment
   (`PATH`, `HOME`, `LANG`, `TMPDIR` only). The plugin process env
   (including `EXEC_APPROVAL_KEY`) is never forwarded into the container, so
   a payload cannot read approval keys or sibling secrets from env. No key
   value is logged; the audit log stores only sha256 digests.
6. DEPLOYMENT: this plugin is a dev/omnidev capability. It is not part of any
   production omni-stack release configuration (operator directive:
   production only changes via release; R3 is repo-only until a release
   ships it to an environment that chooses to enable it, and even then
   approval defaults deny).

Residual risks (documented, accepted for v0.1): the docker CLI runs on the
plugin host with the host daemon socket, so the *plugin server* (vetted code
behind the approval gate) has daemon access; the payload never does. An
operator who enables this plugin accepts that the plugin binary itself is
trusted. File reads: the payload sees the sandbox image's own filesystem; no
host or OMNI_DIR path is mounted, so production config files are not visible
to it at all (stronger than process-level sandboxes).

## 5. Approval gate (explicit, per-run, auditable)

Two tools implement a request/approve handshake. The model proposes, an
operator-controlled key approves; each run is approved exactly once and is
bound to the exact command string.

Flow:

1. `exec_run(command, timeout_secs?, ...)` NEVER executes. It:
   - refuses immediately (result text, no error) when mode=deny or no
     `EXEC_APPROVAL_KEY` is configured;
   - otherwise validates the command (non-empty, length <= 4000 chars),
     hashes it (sha256), creates a pending request
     `{id: R-<epoch>-<rand>, command, timeout_secs, sha256, created_at,
     ttl_secs}` persisted under the state dir, and returns a result that
     reports the request id + command digest + expiry and instructs the
     caller to have an operator approve it. The full command text is stored
     server-side so the approval is bound to exactly what will run.
2. `exec_approve(request_id, approval_key)`:
   - looks up the pending request; denies (normal result text) when the
     request is unknown, already used, or expired (TTL, default 600 s);
   - verifies `approval_key` against the configured key with a
     constant-time compare (hmac.compare_digest); denies on mismatch;
   - on success marks the request approved (single-use), executes the STORED
     command in the sandbox (section 6), records the outcome
     (exit code, duration, output size, digest) in the request record and in
     the audit log, and returns the output (section 7).
   - The approval key is NOT accepted as an argument to `exec_run`, so no
     command can self-approve in one call.

Audit: append-only JSONL at `<state>/audit.jsonl` with events
request_created / approved / denied / executed, each carrying the command
sha256, request id, outcome and a sha256 of the approval key - never the key
or the raw command if the command may be sensitive? The raw command is
needed for operator review before approval; the audit stores the command
string for executed runs (that is what was approved) and only digests for
denied runs. Log lines go to stderr (MCP server log) as well.

Revocation: clearing `EXEC_APPROVAL_KEY` or setting mode=deny in config
stops all future executions immediately; pending requests without an
approval expire by TTL anyway.

## 6. Sandbox execution backend (disposable container)

Probe evidence from omnidev (2026-09-09, container omnidev-omniagent-1,
image omniagent-dev:latest, runs as root):
- `os.unshare(os.CLONE_NEWNET)` -> EPERM; `unshare -n` -> EPERM. The dev
  container has no CAP_SYS_ADMIN, so in-process network-namespace isolation
  is impossible there (and on typical omniagent containers).
- docker CLI 29.8.0 + `/var/run/docker.sock` work inside the container;
  `docker run --rm --network none --user 65534:65534 <image> /bin/sh -c ...`
  executes successfully as nobody with no network.

Therefore the sandbox backend is a throwaway docker container:

```
docker run --rm --detach=false \
  --network none \
  --user 65534:65534 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:rw,size=16m \
  --cpus <EXEC_CPUS=1> --memory <EXEC_MEM=256m> --pids-limit <EXEC_PIDS=64> \
  --name exec_<nonce> \
  --workdir /tmp \
  -e PATH=/usr/bin:/bin -e HOME=/tmp -e LANG=C.UTF-8 \
  -e TMPDIR=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  <EXEC_IMAGE> /bin/sh -c <command>
```

- `EXEC_IMAGE` (config, required for execution): the operator pins the image
  the payload runs in (omnidev: `omniagent-dev:latest`, local and present;
  any deployment must configure an image already present on its host - the
  plugin never pulls). The image value is validated (safe charset, no
  whitespace) and passed as a single argv element (no shell interpolation).
- Timeout: the docker CLI subprocess is watched; on expiry the unique
  container name is used for a best-effort `docker kill` and a timeout
  result is returned. Per-call `timeout_secs` clamps to 1..300 (default 30).
  cpu/mem/pids flags bound the container independently of the wall timeout.
- No volumes are mounted: the sandbox starts empty (fresh `/tmp`), writes
  nothing that persists, and is removed (`--rm`). This is a disposable
  snippet runner by construction.
- The command runs via `/bin/sh -c` (this is a shell-exec tool); the command
  string is the payload, isolated as described.

Why docker and not alternatives:
- unshare/netns: unavailable (EPERM, no CAP_SYS_ADMIN) in the runtime.
- bubblewrap/firejail: not installed; also weaker network story without
  netns.
- subprocess-only with uid drop: no network isolation possible.
- `--network none` container is the strongest practical isolation: zero
  network devices, not attached to any docker network, payload never given
  the daemon socket.

## 7. Output discipline (cap + spill)

- stdout and stderr of the docker CLI are merged into one stream; read is
  bounded to a hard cap `EXEC_OUTPUT_HARD_CAP` (1 MiB) - on overflow the run
  is killed and a truncation note is emitted.
- Inline result: capped at `EXEC_INLINE_MAX_CHARS` (default 8000, clamped
  1000..50000 per config).
- When output exceeds the inline cap, the FULL output is spilled to
  `<EXEC_SPILL_DIR>/exec_<sha10(command)>_<epoch>.out` and the result
  reports the spill path FIRST plus an inline head preview (web_extract
  pattern), so a chatty command can never flood the agent context.
- Result reports: exit code, wall-clock seconds, output bytes, and (on
  spill) the file path; errors (timeout, sandbox deny, docker failure) are
  returned as clear is_error results with actionable text.

## 8. Config schema (plugin.json config_schema)

| key | default | meaning |
|---|---|---|
| EXEC_APPROVAL_MODE | require | `require` = approvals enforced; `deny` = plugin inert |
| EXEC_APPROVAL_KEY | (empty) | operator secret; empty => nothing can be approved |
| EXEC_APPROVAL_TTL_SECS | 600 | request validity window (clamp 60..3600) |
| EXEC_IMAGE | (empty) | pinned sandbox image; empty => execution denied |
| EXEC_TIMEOUT_SECS | 30 | default run timeout (clamp 1..300) |
| EXEC_INLINE_MAX_CHARS | 8000 | inline cap (clamp 1000..50000) |
| EXEC_OUTPUT_HARD_CAP | 1048576 | total captured bytes before kill |
| EXEC_SPILL_DIR | "" (system tmp/omni-exec) | spill + request-state dir |
| EXEC_CPUS | 1 | container cpu limit |
| EXEC_MEM | 256m | container memory limit |
| EXEC_PIDS | 64 | container pids limit |

## 9. Rejected alternatives (why not)

- In-process netns via os.unshare / unshare binary: EPERM in the runtime
  (no CAP_SYS_ADMIN); fail-closed would make the tool permanently unusable
  in omnidev.
- Host-network mode or bridge-network sandbox containers: can reach other
  containers/host ports; violates the no-production-access requirement.
- Auto-approval / "approve once per session" exec: removes the explicit
  per-run gate the task requires.
- docker backend with socket exposed to the payload: would be production
  access; never.
- Mounting the workspace or OMNI_DIR into the sandbox: increases blast
  radius and needs read-only mount plumbing; v0.1 keeps the sandbox empty.

## 10. Verification plan (omnidev only, executor)

1. `python3 -m py_compile server.py sandbox.py` in the dev container.
2. Direct stdio MCP driver (server.py spawned with env:
   EXEC_APPROVAL_KEY=testkey, EXEC_IMAGE=omniagent-dev:latest,
   EXEC_SPILL_DIR=/tmp/exec-smoke): tools/list; exec_run (approval
   required, nothing executed); exec_approve wrong key (denied);
   exec_approve correct key (runs `echo hi; id` as nobody, exit 0);
   network isolation (`getent hosts` / socket connect must fail);
   spill (command printing > 8000 chars reports spill path);
   timeout (sleep 5 with timeout 1 -> killed); exec_run with no key in env
   (deny).
3. Registration smoke against the dev omniagent: install-git from local
   file:// clone + enable via the dev API, confirm exec_run/exec_approve in
   /mcp/tools, benign invoke returns the approval/deny result without
   executing.
4. Confirm zero production paths: sandbox is `--network none` with a
   non-production image; payload uid nobody; no prod mount; plugin not
   registered in any production config (repo-only change; production
   untouched, ships in the next release).
