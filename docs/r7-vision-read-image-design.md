# R7 design: vision tool (read_image), model-gated plugin

Status: implemented (R7 executor, 2026-09-09)
Repo: omni-plugins only (this repo is not a deploy target; the plugin is
consumed by deployments at runtime, shipped in the next release).

## Problem

The toolset gap research (task_research_research_toolset_gap_quality) listed
R7 as a P1 gap: omniagent has no media/vision tool. A model with image input
support should be able to read an image and act on it; a text-only model
should never be handed image bytes it cannot see. Operator placement: R7 is
an omni-plugins PLUGIN TOOL, NOT a core change.

## Constraints discovered during orientation

1. Core LLM transport is text-only. `ChatMessage.content` is a String and
   MCP `CallToolResult` content only deserializes `text` and `resource`
   blocks (src/mcp/external/protocol.rs). An MCP `image` content block would
   FAIL deserialization in the core today. Therefore read_image returns TEXT:
   a base64 data URI (`data:<mime>;base64,...`) plus metadata. This is
   forward-compatible: a future multimodal core can consume the same URI.
2. The MCP call `_meta` carries channel_id / thread_id / profile_name /
   platform but NOT the active model. A plugin therefore cannot observe the
   per-thread model at call time. The model gate must be deployment
   configuration (config_schema keys resolved to env vars before the server
   starts), matching how exec resolves its approval key.
3. models.yml (core) has no vision-capability flag today; the active model
   family (deepseek chat) is text-only. Adding such a flag is core work,
   out of scope per operator placement.

## Design

New plugin `tools/vision` (manifest type `mcp`, python stdio server,
stdlib only, no pip dependencies), single tool `read_image`.

Input: exactly one of
- `path`: local image file (PNG/JPEG/GIF/WebP/BMP), or
- `url`: http(s) image URL (never file:// or other schemes).

Output: text result with metadata (format, mime, pixel dimensions, byte
size, sha256 prefix) plus the data URI `data:<mime>;base64,...`.

### Model gate (fail closed)

| VISION_MODELS | VISION_ACTIVE_MODEL | read_image |
| --- | --- | --- |
| empty | any | INERT: guard refusal (nothing read) |
| set | empty | allowed (deployment declared vision-capable) |
| set | set, NOT in list | guard refusal naming the active model |
| set | set, in list | allowed |

Default config is inert (no vision model declared), mirroring exec's
fail-closed default. The guard refusal is returned as ordinary tool text
(not isError), exactly like exec's inert-denial pattern, so the refusal
reads as an explanation rather than a crash.

### Output discipline

- Byte cap: `VISION_MAX_BYTES` (default 10 MiB, clamp 65536..67108864);
  larger local files and URL bodies are refused. URL bodies are streamed and
  counted, never buffered unbounded.
- Cap + spill: data URI inline while the whole result is <=
  `VISION_INLINE_MAX_CHARS` (default 8000, clamp 1000..50000); otherwise the
  complete URI is spilled to `VISION_SPILL_DIR/read_image_<sha10>_<epoch>.uri`
  (default system temp `omni-vision`) and the path is reported FIRST, with a
  short inline preview. Same discipline as exec output spill and web_extract.

### Probing (no decoder dependency)

`image_probe.py` identifies format from magic bytes and reads dimensions
from container headers only: PNG IHDR, JPEG SOF markers, GIF logical screen
descriptor, WebP VP8/VP8L/VP8X, BMP DIB header. sha256 over the raw bytes.
Pillow is deliberately NOT a dependency: the plugin never decodes pixels, so
it stays tiny and install-free.

## Files

- tools/vision/plugin.json (manifest + config_schema)
- tools/vision/mcp-config.json (stdio server wiring)
- tools/vision/server.py (MCP JSON-RPC over stdio, mirrors exec/web servers)
- tools/vision/image_probe.py (pure-stdlib image probe)
- tools/vision/README.md

## Verification (executor level)

- py_compile on server.py and image_probe.py.
- stdio MCP smoke run (probe script under /opt/workspace/tmp): tools/list
  exposes read_image; no config => guard refusal; wrong active model => guard
  naming it; configured vision model => PNG read returns metadata + data URI;
  missing path and non-image bytes return clean isError results.
- plugin is NOT added to remote.yml (kanban/cron/subtasks/actions only), so
  the omni-deployer plugin suite is unaffected.

## Out of scope / future

- Core multimodal message transport (image content blocks) is a separate
  core task; read_image output (data URI) is ready for it.
- Per-thread model visibility into MCP _meta would allow a dynamic gate;
  today the gate is deployment config.
