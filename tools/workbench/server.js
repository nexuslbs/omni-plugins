#!/usr/bin/env node

/**
 * workbench MCP server : a THIN fetch wrapper around the workbench HTTP API.
 *
 * It exposes exactly ONE MCP tool, `tool` (exposed to the agent as
 * `workbench__tool`) and forwards every call as a single HTTP POST:
 *
 *   POST {base_url}{tool_path}
 *   Content-Type: application/json
 *   body: {"tool": <tool>, "params": <params>}
 *
 * No business logic, no SDK, no state: the plugin never inspects, retries or
 * reshapes the workbench payload. The HTTP response body is returned to the
 * agent as pretty-printed JSON text.
 *
 * Config (config_schema in plugin.json, delivered as a `configure` JSON-RPC
 * request and/or as environment variables):
 *   base_url     default http://workbench:8080
 *   tool_path    default /api/tool/call
 *   timeout_secs default 60
 *   auth_header  optional Authorization header value (empty = header omitted)
 *
 * Runtime: Node >= 18 (global `fetch` + `AbortController`), no npm deps.
 */

const readline = require("readline");
const process = require("process");

const MCP_PROTOCOL_VERSION = "2025-03-26";
const SERVER_NAME = "workbench";
const SERVER_VERSION = "0.1.0";

const DEFAULT_BASE_URL = "http://workbench:8080";
const DEFAULT_TOOL_PATH = "/api/tool/call";
const DEFAULT_TIMEOUT_SECS = 60;

// The single declared tool name. `tool_qualify("workbench", "tool")` in core
// turns it into the exposed name `workbench__tool`.
const TOOL_NAME = "tool";

let initialized = false;

// ── Configuration ──────────────────────────────────────────────────────────

function pickString(candidates, fallback) {
  for (const value of candidates) {
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text !== "") return text;
  }
  return fallback;
}

function pickInt(candidates, fallback) {
  for (const value of candidates) {
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text === "") continue;
    const parsed = Number.parseInt(text, 10);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return fallback;
}

const config = {
  base_url: pickString(
    [process.env.base_url, process.env.BASE_URL, process.env.WORKBENCH_BASE_URL],
    DEFAULT_BASE_URL
  ),
  tool_path: pickString(
    [process.env.tool_path, process.env.TOOL_PATH, process.env.WORKBENCH_TOOL_PATH],
    DEFAULT_TOOL_PATH
  ),
  timeout_secs: pickInt(
    [
      process.env.timeout_secs,
      process.env.TIMEOUT_SECS,
      process.env.WORKBENCH_TIMEOUT_SECS,
    ],
    DEFAULT_TIMEOUT_SECS
  ),
  auth_header: pickString(
    [
      process.env.auth_header,
      process.env.AUTH_HEADER,
      process.env.WORKBENCH_AUTH_HEADER,
    ],
    ""
  ),
};

/** Merge a `configure` payload (config keys as declared in plugin.json). */
function applyConfig(payload) {
  if (!payload || typeof payload !== "object") return;
  if (payload.base_url !== undefined)
    config.base_url = pickString([payload.base_url], config.base_url);
  if (payload.tool_path !== undefined)
    config.tool_path = pickString([payload.tool_path], config.tool_path);
  if (payload.timeout_secs !== undefined)
    config.timeout_secs = pickInt([payload.timeout_secs], config.timeout_secs);
  if (payload.auth_header !== undefined)
    config.auth_header = pickString([payload.auth_header], "");
  console.error(
    "[workbench] config: base_url=" +
      config.base_url +
      " tool_path=" +
      config.tool_path +
      " timeout_secs=" +
      config.timeout_secs +
      " auth_header=" +
      (config.auth_header ? "<set>" : "<none>")
  );
}

// ── JSON-RPC plumbing ──────────────────────────────────────────────────────

function sendJson(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function makeSuccess(reqId, result) {
  return { jsonrpc: "2.0", id: reqId, result };
}

function makeError(reqId, code, message) {
  return { jsonrpc: "2.0", id: reqId, error: { code, message } };
}

function toolResult(text, isError) {
  return { content: [{ type: "text", text }], isError: isError === true };
}

function handleInitialize(reqId) {
  sendJson(
    makeSuccess(reqId, {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
    })
  );
  console.error("[workbench] initialized: " + SERVER_NAME + " v" + SERVER_VERSION);
}

function handleToolsList(reqId) {
  const tools = [
    {
      name: TOOL_NAME,
      description:
        "[workbench] Call a workbench tool by name: POSTs {\"tool\": <tool>, " +
        "\"params\": <params>} to the workbench HTTP API (base_url + tool_path) " +
        "and returns the response body. Thin wrapper, no business logic.",
      inputSchema: {
        type: "object",
        properties: {
          tool: {
            type: "string",
            description: "workbench tool/command name, e.g. hello_world",
          },
          params: {
            type: "object",
            additionalProperties: true,
            default: {},
            description: "arguments passed to the workbench tool",
          },
        },
        required: ["tool"],
      },
    },
  ];
  sendJson(makeSuccess(reqId, { tools }));
  console.error("[workbench] tools/list returned 1 tool");
}

// ── HTTP forwarding ────────────────────────────────────────────────────────

function prettyBody(bodyText) {
  const text = bodyText === null || bodyText === undefined ? "" : String(bodyText);
  if (text.trim() === "") return "(empty response body)";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch (e) {
    return text;
  }
}

function targetUrl() {
  const base = String(config.base_url).replace(/\/+$/, "");
  const path = String(config.tool_path);
  return base + (path.startsWith("/") ? path : "/" + path);
}

async function callWorkbench(toolName, params) {
  const url = targetUrl();
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (config.auth_header) headers.Authorization = config.auth_header;

  const body = JSON.stringify({ tool: toolName, params: params });
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    Math.max(1, config.timeout_secs) * 1000
  );

  console.error("[workbench] POST " + url + " body=" + body);

  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    const aborted = err && err.name === "AbortError";
    const reason = aborted
      ? "request timed out after " + config.timeout_secs + "s"
      : (err && err.message) || String(err);
    console.error("[workbench] request failed: " + reason);
    return {
      isError: true,
      text: "workbench request to " + url + " failed: " + reason,
    };
  }
  clearTimeout(timer);

  let bodyText = "";
  try {
    bodyText = await response.text();
  } catch (err) {
    const reason = (err && err.message) || String(err);
    console.error("[workbench] failed to read response body: " + reason);
    return {
      isError: true,
      text:
        "workbench " +
        url +
        " returned HTTP " +
        response.status +
        " but the response body could not be read: " +
        reason,
    };
  }

  const pretty = prettyBody(bodyText);
  if (!response.ok) {
    const statusText = response.statusText ? " " + response.statusText : "";
    console.error("[workbench] HTTP " + response.status + " from " + url);
    return {
      isError: true,
      text:
        "workbench " +
        url +
        " returned HTTP " +
        response.status +
        statusText +
        "\n" +
        pretty,
    };
  }

  console.error("[workbench] HTTP " + response.status + " from " + url);
  return { isError: false, text: pretty };
}

async function handleCall(reqId, params) {
  const toolName = params.name || "";
  const args = params.arguments || {};

  if (toolName !== TOOL_NAME && toolName !== SERVER_NAME + "__" + TOOL_NAME) {
    sendJson(makeError(reqId, -32602, "Unknown tool: " + toolName));
    return;
  }

  const workbenchTool = args.tool;
  if (typeof workbenchTool !== "string" || workbenchTool.trim() === "") {
    sendJson(
      makeError(reqId, -32602, "Invalid params: 'tool' is required (string)")
    );
    console.error("[workbench] tools/call rejected: missing 'tool' argument");
    return;
  }

  let workbenchParams = args.params;
  if (workbenchParams === undefined || workbenchParams === null) {
    workbenchParams = {};
  } else if (
    typeof workbenchParams !== "object" ||
    Array.isArray(workbenchParams)
  ) {
    sendJson(
      makeError(reqId, -32602, "Invalid params: 'params' must be an object")
    );
    console.error("[workbench] tools/call rejected: 'params' is not an object");
    return;
  }

  // NOT awaited by the readline loop: calls are concurrent (150 parallel
  // calls must all be in flight without blocking each other).
  const outcome = await callWorkbench(workbenchTool, workbenchParams);
  sendJson(makeSuccess(reqId, toolResult(outcome.text, outcome.isError)));
}

// ── Main loop ──────────────────────────────────────────────────────────────

const rl = readline.createInterface({ input: process.stdin, terminal: false });

console.error("[workbench] MCP server starting (PID=" + process.pid + ")");
applyConfigFromEnvSummary();

function applyConfigFromEnvSummary() {
  console.error(
    "[workbench] env config: base_url=" +
      config.base_url +
      " tool_path=" +
      config.tool_path +
      " timeout_secs=" +
      config.timeout_secs +
      " auth_header=" +
      (config.auth_header ? "<set>" : "<none>")
  );
}

rl.on("line", function (line) {
  const trimmed = line.trim();
  if (!trimmed) return;

  if (trimmed === "__EOF__") {
    console.error("[workbench] EOF marker received, shutting down");
    process.exit(0);
  }

  let request;
  try {
    request = JSON.parse(trimmed);
  } catch (e) {
    console.error("[workbench] failed to parse JSON-RPC: " + e.message);
    return;
  }

  const method = request.method || "";
  const reqId = request.id;
  const params = request.params || {};

  if (method === "configure") {
    applyConfig(params);
    if (reqId !== undefined && reqId !== null) {
      sendJson(makeSuccess(reqId, {}));
    }
  } else if (method === "initialize") {
    if (reqId !== undefined && reqId !== null) {
      handleInitialize(reqId);
      initialized = true;
    }
  } else if (method === "notifications/initialized") {
    console.error("[workbench] client initialized notification received");
  } else if (method === "ping") {
    if (reqId !== undefined && reqId !== null) sendJson(makeSuccess(reqId, {}));
  } else if (method === "tools/list") {
    if (!initialized) {
      if (reqId !== undefined && reqId !== null)
        sendJson(makeError(reqId, -32000, "Server not initialized"));
      return;
    }
    if (reqId !== undefined && reqId !== null) handleToolsList(reqId);
  } else if (method === "tools/call") {
    if (!initialized) {
      if (reqId !== undefined && reqId !== null)
        sendJson(makeError(reqId, -32000, "Server not initialized"));
      return;
    }
    if (reqId !== undefined && reqId !== null) {
      handleCall(reqId, params).catch(function (err) {
        const reason = (err && err.message) || String(err);
        console.error("[workbench] tools/call failed: " + reason);
        sendJson(
          makeSuccess(reqId, toolResult("workbench call failed: " + reason, true))
        );
      });
    }
  } else {
    console.error("[workbench] unknown method: " + method);
    if (reqId !== undefined && reqId !== null) {
      sendJson(makeError(reqId, -32601, "Method not found: " + method));
    }
  }
});

rl.on("close", function () {
  console.error("[workbench] MCP server shutting down (stdin closed)");
  process.exit(0);
});
