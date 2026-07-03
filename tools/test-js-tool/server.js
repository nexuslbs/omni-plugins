#!/usr/bin/env node

/**
 * test-js-tool MCP server — implements standard MCP JSON-RPC over stdio.
 *
 * Tools:
 *   - test-js-tool_wait: Sleep for N seconds (default 900)
 *   - test-js-tool_echo: Echo back input text
 *   - test-js-tool_save-datetime: Write current date/time to a file
 *   - test-js-tool_test-error: Return a test error
 */

const readline = require("readline");
const fs = require("fs");
const process = require("process");

const MCP_PROTOCOL_VERSION = "2025-03-26";
let initialized = false;

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
  if (isError === void 0) isError = false;
  return { content: [{ type: "text", text }], isError };
}

function handleInitialize(reqId) {
  sendJson(
    makeSuccess(reqId, {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "test-js-tool", version: "0.1.0" },
    })
  );
  console.error("[test-js-tool] Initialized: test-js-tool v0.1.0");
}

function handleToolsList(reqId) {
  const tools = [
    {
      name: "test-js-tool_wait",
      description:
        "[test-js-tool] Sleep for a specified duration in seconds (default 900 = 15 minutes)",
      inputSchema: {
        type: "object",
        properties: {
          duration_secs: {
            type: "integer",
            description: "Seconds to wait",
            default: 900,
          },
        },
        required: [],
      },
    },
    {
      name: "test-js-tool_echo",
      description: "[test-js-tool] Echo back a greeting: 'Hello, {input}'",
      inputSchema: {
        type: "object",
        properties: {
          input: {
            type: "string",
            description:
              "Name to greet (default: GREETING_NAME env var or 'World')",
          },
        },
        required: [],
      },
    },
    {
      name: "test-js-tool_save-datetime",
      description:
        "[test-js-tool] Write the current date/time (ISO 8601 format) to a file",
      inputSchema: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "File path to write the datetime to",
          },
        },
        required: ["path"],
      },
    },
    {
      name: "test-js-tool_test-error",
      description: "[test-js-tool] Return a test error: 'Test error from js: <input>'",
      inputSchema: {
        type: "object",
        properties: {
          input: {
            type: "string",
            description: "Error message input",
          },
        },
        required: ["input"],
      },
    },
  ];
  sendJson(makeSuccess(reqId, { tools }));
  console.error("[test-js-tool] tools/list returned 4 tools");
}

async function handleWait(reqId, args) {
  const durationSecs = (args && args.duration_secs) || 900;
  console.error(
    "[test-js-tool] wait tool called: sleeping for " + durationSecs + " second(s)"
  );

  let slept = 0;
  while (slept < durationSecs) {
    await new Promise(function (resolve) {
      setTimeout(resolve, 1000);
    });
    slept++;
  }

  sendJson(
    makeSuccess(reqId, toolResult("Waited for " + durationSecs + " seconds"))
  );
  console.error("[test-js-tool] wait tool completed: slept for " + durationSecs + " second(s)");
}

function handleEcho(reqId, args) {
  let name = (args && args.input) || "";
  const greetingName = process.env.GREETING_NAME || "World";
  if (!name) {
    name = greetingName;
  }
  const text = "Hello, " + name;
  console.error("[test-js-tool] echo tool called: text='" + text + "'");
  sendJson(makeSuccess(reqId, toolResult(text)));
  console.error("[test-js-tool] echo tool completed");
}

function handleSaveDatetime(reqId, args) {
  const filePath = args && args.path;
  if (!filePath) {
    sendJson(
      makeSuccess(
        reqId,
        toolResult("Error: 'path' argument is required", true)
      )
    );
    console.error("[test-js-tool] save-datetime tool called without path argument");
    return;
  }

  const datetimeStr = new Date()
    .toISOString()
    .replace(/\.\d{3}Z$/, "Z");
  console.error("[test-js-tool] save-datetime tool called: path='" + filePath + "'");

  try {
    fs.writeFileSync(filePath, datetimeStr, "utf-8");
    sendJson(
      makeSuccess(
        reqId,
        toolResult("Saved datetime to " + filePath + ": " + datetimeStr)
      )
    );
    console.error("[test-js-tool] save-datetime tool completed: wrote to " + filePath);
  } catch (e) {
    sendJson(
      makeSuccess(
        reqId,
        toolResult("Error writing to " + filePath + ": " + e.message, true)
      )
    );
    console.error("[test-js-tool] save-datetime tool failed: " + e.message);
  }
}

function handleTestError(reqId, args) {
  const inputVal = (args && args.input) || "";
  const text = "Test error from js: " + inputVal;
  console.error("[test-js-tool] test-error tool called: input='" + inputVal + "'");
  sendJson(makeSuccess(reqId, toolResult(text, true)));
  console.error("[test-js-tool] test-error tool completed");
}

// ── Main loop ──

const rl = readline.createInterface({ input: process.stdin, terminal: false });

console.error("[test-js-tool] MCP server starting (PID=" + process.pid + ")");

rl.on("line", function (line) {
  line = line.trim();
  if (!line) return;

  if (line === "__EOF__") {
    console.error("[test-js-tool] EOF marker received, shutting down");
    process.exit(0);
  }

  let request;
  try {
    request = JSON.parse(line);
  } catch (e) {
    console.error("[test-js-tool] Failed to parse JSON-RPC: " + e.message);
    return;
  }

  const method = request.method || "";
  const reqId = request.id;

  if (method === "initialize") {
    if (reqId != null) {
      handleInitialize(reqId);
      initialized = true;
    }
  } else if (method === "notifications/initialized") {
    console.error("[test-js-tool] Client initialized notification received");
  } else if (method === "tools/list") {
    if (!initialized) {
      if (reqId != null)
        sendJson(makeError(reqId, -32000, "Server not initialized"));
      return;
    }
    if (reqId != null) handleToolsList(reqId);
  } else if (method === "tools/call") {
    if (!initialized) {
      if (reqId != null)
        sendJson(makeError(reqId, -32000, "Server not initialized"));
      return;
    }
    if (reqId != null) {
      const params = request.params || {};
      const toolName = params.name || "";
      const args = params.arguments || {};

      if (toolName === "test-js-tool_wait") {
        handleWait(reqId, args).catch(function (err) {
          console.error("[test-js-tool] wait tool error: " + err.message);
        });
      } else if (toolName === "test-js-tool_echo") {
        handleEcho(reqId, args);
      } else if (toolName === "test-js-tool_save-datetime") {
        handleSaveDatetime(reqId, args);
      } else if (toolName === "test-js-tool_test-error") {
        handleTestError(reqId, args);
      } else {
        if (reqId != null) {
          sendJson(makeError(reqId, -32602, "Unknown tool: " + toolName));
        }
      }
    }
  } else {
    console.error("[test-js-tool] Unknown method: " + method);
    if (reqId != null) {
      sendJson(makeError(reqId, -32601, "Method not found: " + method));
    }
  }
});

rl.on("close", function () {
  console.error("[test-js-tool] MCP server shutting down (stdin closed)");
  process.exit(0);
});
