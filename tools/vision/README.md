# vision: read_image (R7) - model-gated vision tool plugin

MCP tool plugin providing one model-gated vision tool:

- **read_image**: read an image from a local file path or an http(s) URL and
  return its metadata (format, mime, pixel dimensions, byte size, sha256
  prefix) plus a base64 data URI (`data:<mime>;base64,...`) ready to hand to
  a vision-capable model.

The plugin is pure Python stdlib (no pip dependencies): it never decodes or
re-encodes pixels, it only probes container headers to identify the format
and dimensions, and it passes the original bytes through unchanged.

## Model gate (fail closed)

A text-only model cannot see images, so `read_image` refuses to run unless
the deployment operator has declared the active model vision-capable. The
core does not send the per-thread model name to MCP plugins, so the gate is
configuration driven (`config_schema` keys resolved to env vars):

| Config | Meaning |
| --- | --- |
| `VISION_MODELS` | Comma-separated model names that support image/vision input. Empty = no vision-capable model declared, plugin INERT. |
| `VISION_ACTIVE_MODEL` | Deployment's active model name when pinned. Empty = operator declares the whole deployment vision-capable (any model in `VISION_MODELS`). |

Gate rules (fail closed):

- `VISION_MODELS` empty -> every call returns a guard refusal (nothing read).
- `VISION_ACTIVE_MODEL` set and NOT in `VISION_MODELS` -> guard refusal that
  names the offending model (so a text-only active model never wastes
  context on images).
- `VISION_ACTIVE_MODEL` set and in `VISION_MODELS` -> allowed.
- `VISION_ACTIVE_MODEL` empty and `VISION_MODELS` non-empty -> allowed.

Default configuration is inert: with no `VISION_MODELS` the tool refuses
everything, mirroring the exec plugin's fail-closed posture.

## Output discipline (cap + spill)

The base64 data URI is returned inline only while the whole result stays
under `VISION_INLINE_MAX_CHARS` (default 8000). Larger URIs are spilled to
`VISION_SPILL_DIR/read_image_<sha10>_<epoch>.uri` (default: system temp dir
`omni-vision`) and the file path is reported FIRST so it can never be cut by
the inline cap. Image byte size is bounded by `VISION_MAX_BYTES` (default
10 MiB); larger files and URL bodies are refused.

## Configuration (config_schema keys; `$secret:NAME` refs resolved by the core)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `VISION_MODELS` | string | `` | Comma-separated vision-capable model names. Empty = plugin inert. |
| `VISION_ACTIVE_MODEL` | string | `` | Active model name; when set it must be in `VISION_MODELS`. |
| `VISION_MAX_BYTES` | integer | 10485760 | Max image size (clamped 65536-67108864). |
| `VISION_INLINE_MAX_CHARS` | integer | 8000 | Inline data URI cap (clamped 1000-50000). |
| `VISION_SPILL_DIR` | string | `` | Spill dir for oversized data URIs. Empty = system temp dir. |

## Supported formats

PNG, JPEG, GIF, WebP, BMP (magic-byte detection, header-only dimension
reads). Anything else returns a clean error naming the limitation.

## Example

```
read_image(path="/tmp/chart.png")
# -> metadata + data:image/png;base64,iVBORw0KGgo...

read_image(url="https://example.com/photo.jpg")
# -> metadata + data:image/jpeg;base64,/9j/4AAQ...
```

Security notes: `url` only accepts http(s) (never `file://` or other
schemes); URL bodies are streamed and hard-capped at `VISION_MAX_BYTES`;
images are never written anywhere except the optional spill file.
