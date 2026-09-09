#!/usr/bin/env python3
"""exec sandbox runtime: approval-request store + disposable-container runner.

R3 sandboxed code/shell execution plugin (omni-plugins tools/exec).

Two layers live here:

1. RequestStore  - per-run approval records (pending -> approved -> executed),
   TTL expiry and an append-only audit log. No secrets are stored; only
   sha256 digests of the approval key and of the command are recorded.

2. DockerRunner  - executes an approved command inside a THROWAWAY docker
   container (python stdlib only, no pip deps):

       docker run --rm --network none --user 65534:65534 \
         --cap-drop ALL --security-opt no-new-privileges \
         --read-only --tmpfs /tmp:rw,size=16m \
         --cpus <cpus> --memory <mem> --pids-limit <pids> \
         --name exec_<nonce> --workdir /tmp \
         -e PATH=/usr/bin:/bin -e HOME=/tmp -e LANG=C.UTF-8 \
         -e TMPDIR=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
         <image> /bin/sh -c <command>

   Isolation guarantees (see docs/r3-sandboxed-exec-design.md):
   - --network none: the payload has NO network devices; it cannot reach
     production containers, the production DB, host ports or the internet.
   - --user 65534:65534 + --cap-drop ALL + no-new-privileges: nobody uid,
     no capabilities, no privilege escalation.
   - --read-only rootfs with a tmpfs /tmp: nothing on the image or host is
     writable except the ephemeral /tmp of the throwaway container.
   - Only the explicit -e environment is present in the container; the
     plugin process env (including EXEC_APPROVAL_KEY) is NEVER forwarded,
     so a payload cannot read secrets from the environment.
   - The docker CLI argv is constructed element-wise (no shell), the image
     is operator-pinned and validated, and no volume mounts are used.
   - Fail closed: missing docker CLI / unstartable container => ExecError,
     never an un-sandboxed fallback.
"""

import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time

log = logging.getLogger("exec-mcp")

COMMAND_MAX_CHARS = 4000
RID_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
IMAGE_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
CPUS_SAFE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
MEM_SAFE = re.compile(r"^[0-9]+[kmgKMG]?$")
PIDS_SAFE = re.compile(r"^[0-9]+$")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_EXECUTED = "executed"


class ExecError(Exception):
    """Sandbox/approval infrastructure failure (execution did not happen)."""


class RunResult:
    def __init__(self, output="", exit_code=None, timed_out=False,
                 overflow=False, duration_secs=0.0, container="", error=""):
        self.output = output
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.overflow = overflow
        self.duration_secs = duration_secs
        self.container = container
        self.error = error

    def to_dict(self):
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "overflow": self.overflow,
            "duration_secs": round(self.duration_secs, 2),
            "container": self.container,
            "error": self.error,
        }


def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_nonce(length=6):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Approval request store
# ---------------------------------------------------------------------------

class RequestStore:
    """Persist per-run approval requests under <root>/requests and audit
    events under <root>/audit.jsonl. JSON only, no secrets."""

    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.requests_dir = os.path.join(root_dir, "requests")
        self.audit_path = os.path.join(root_dir, "audit.jsonl")
        os.makedirs(self.requests_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.requests_dir, 0o700)
        except OSError:
            pass

    def _request_path(self, rid):
        if not rid or not RID_SAFE.match(rid) or not rid.startswith("R-"):
            raise ExecError("invalid request id")
        return os.path.join(self.requests_dir, rid + ".json")

    def audit(self, event):
        try:
            line = dict(event)
            line["ts"] = int(time.time())
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except Exception as e:  # pragma: no cover - audit must never crash a run
            log.warning("audit write failed: %s", e)

    def create(self, command, timeout_secs, ttl_secs):
        if not command or not command.strip():
            raise ExecError("empty command")
        if len(command) > COMMAND_MAX_CHARS:
            raise ExecError("command too long (max %d chars)" % COMMAND_MAX_CHARS)
        rid = "R-%d-%s" % (int(time.time()), _new_nonce())
        record = {
            "id": rid,
            "command": command,
            "sha256": sha256_hex(command),
            "timeout_secs": int(timeout_secs),
            "ttl_secs": int(ttl_secs),
            "created_at": int(time.time()),
            "status": STATUS_PENDING,
        }
        with open(self._request_path(rid), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        try:
            os.chmod(self._request_path(rid), 0o600)
        except OSError:
            pass
        self.audit({"event": "request_created", "request_id": rid,
                    "sha256": record["sha256"]})
        return record

    def get(self, rid):
        try:
            path = self._request_path(rid)
        except ExecError:
            return None
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def expired(self, record):
        ttl = int(record.get("ttl_secs") or 0)
        return (int(time.time()) - int(record.get("created_at") or 0)) > ttl

    def update(self, rid, **fields):
        record = self.get(rid)
        if record is None:
            return None
        record.update(fields)
        with open(self._request_path(rid), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return record

    def mark_approved(self, rid, key_sha256):
        return self.update(rid, status=STATUS_APPROVED,
                           approved_at=int(time.time()),
                           key_sha256=key_sha256)

    def mark_executed(self, rid, outcome):
        return self.update(rid, status=STATUS_EXECUTED,
                           executed_at=int(time.time()), **outcome)


# ---------------------------------------------------------------------------
# Disposable-container runner
# ---------------------------------------------------------------------------

class DockerRunner:
    """Execute a command in a throwaway, network-isolated container."""

    def __init__(self, image, cpus="1", mem="256m", pids="64",
                 docker_bin=None, timeout_kill_grace_secs=5):
        self.image = image
        self.cpus = cpus
        self.mem = mem
        self.pids = pids
        self.docker_bin = docker_bin or shutil.which("docker")
        self.timeout_kill_grace_secs = timeout_kill_grace_secs

    def check_image(self):
        if not self.image:
            raise ExecError("no sandbox image configured (EXEC_IMAGE); "
                            "execution denied")
        if len(self.image) > 200 or not IMAGE_SAFE.match(self.image) \
                or ":" not in self.image:
            raise ExecError("invalid EXEC_IMAGE value (must be a pinned "
                            "image name with a tag, e.g. omniagent-dev:latest)")
        if not CPUS_SAFE.match(self.cpus) or not MEM_SAFE.match(self.mem) \
                or not PIDS_SAFE.match(self.pids):
            raise ExecError("invalid EXEC_CPUS/EXEC_MEM/EXEC_PIDS value")

    def available(self):
        return bool(self.docker_bin)

    def _kill_container(self, cname):
        try:
            subprocess.run([self.docker_bin, "kill", cname],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except Exception:  # pragma: no cover - best effort
            pass

    def run(self, command, timeout_secs, hard_cap_bytes):
        """Run `command` (already approved) in the sandbox.

        Returns a RunResult; raises ExecError only when the sandbox itself
        cannot start (fail closed).
        """
        self.check_image()
        if not self.available():
            raise ExecError("docker CLI not found in the omniagent runtime; "
                            "the sandbox cannot start (fail closed)")

        cname = "exec_%d_%s" % (int(time.time()), _new_nonce(8))
        argv = [self.docker_bin, "run", "--rm",
                "--network", "none",
                "--user", "65534:65534",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=16m",
                "--cpus", self.cpus,
                "--memory", self.mem,
                "--pids-limit", self.pids,
                "--name", cname,
                "--workdir", "/tmp",
                "-e", "PATH=/usr/bin:/bin",
                "-e", "HOME=/tmp",
                "-e", "LANG=C.UTF-8",
                "-e", "TMPDIR=/tmp",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                self.image,
                "/bin/sh", "-c", command]
        log.info("sandbox run %s sha256=%s timeout=%ss", cname,
                 sha256_hex(command), timeout_secs)

        deadline = time.time() + float(timeout_secs)

        def watchdog():
            # After the deadline (+grace) force-kill the container so the
            # attached CLI exits and the read loop below unblocks.
            remaining = deadline + self.timeout_kill_grace_secs - time.time()
            if remaining > 0:
                time.sleep(remaining)
            if proc.poll() is None:
                self._kill_container(cname)
                try:
                    proc.kill()
                except OSError:
                    pass

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    env=os.environ.copy())
        except OSError as e:
            raise ExecError("docker launch failed: %s" % e)

        start = time.time()
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()

        chunks = []
        total = 0
        overflow = False
        timed_out = False
        assert proc.stdout is not None
        while True:
            try:
                chunk = proc.stdout.read(65536)
            except Exception:  # pragma: no cover - defensive
                chunk = b""
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > hard_cap_bytes:
                overflow = True
                self._kill_container(cname)
                break
        proc.wait()
        if time.time() > deadline:
            timed_out = True

        raw = b"".join(chunks)
        if overflow:
            raw = raw[:hard_cap_bytes]
        output = raw.decode("utf-8", errors="replace")

        result = RunResult(
            output=output,
            exit_code=proc.returncode,
            timed_out=timed_out,
            overflow=overflow,
            duration_secs=time.time() - start,
            container=cname,
        )
        if timed_out:
            result.output += ("\n[exec: run exceeded %ss and was killed; "
                              "output above may be partial]" % timeout_secs)
        if overflow:
            result.output += ("\n[exec: output exceeded the %d-byte hard "
                              "cap; capture truncated]" % hard_cap_bytes)
        return result


def deny_reason(mode, has_key):
    if mode != "require":
        return ("execution is disabled in this deployment "
                "(EXEC_APPROVAL_MODE is not 'require')")
    if not has_key:
        return ("execution is disabled in this deployment "
                "(EXEC_APPROVAL_KEY is not configured)")
    return ""


def approval_key_matches(configured_key, provided):
    if not configured_key or not provided:
        return False
    if not isinstance(configured_key, str) or not isinstance(provided, str):
        return False
    return hmac.compare_digest(configured_key.encode("utf-8"),
                               provided.encode("utf-8"))
