use anyhow::{Context, Result};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tracing_subscriber::EnvFilter;

/// Supported MCP protocol version.
const MCP_PROTOCOL_VERSION: &str = "2025-03-26";

// ---------------------------------------------------------------------------
// JSON-RPC types
// ---------------------------------------------------------------------------

/// A JSON-RPC request.
#[derive(Debug, Deserialize)]
struct JsonRpcRequest {
    jsonrpc: String,
    #[serde(default)]
    id: Option<u64>,
    method: String,
    #[serde(default)]
    params: Option<Value>,
}

/// A JSON-RPC success response.
#[derive(Debug, Serialize)]
struct JsonRpcSuccess {
    jsonrpc: String,
    id: u64,
    result: Value,
}

/// A JSON-RPC error response.
#[derive(Debug, Serialize)]
struct JsonRpcErrorResponse {
    jsonrpc: String,
    id: u64,
    error: JsonRpcError,
}

#[derive(Debug, Serialize)]
struct JsonRpcError {
    code: i64,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
}

// ---------------------------------------------------------------------------
// MCP Initialize types
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct InitializeResult {
    #[serde(rename = "protocolVersion")]
    protocol_version: String,
    capabilities: ServerCapabilities,
    #[serde(rename = "serverInfo")]
    server_info: Implementation,
}

#[derive(Debug, Serialize)]
struct ServerCapabilities {
    #[serde(skip_serializing_if = "Option::is_none")]
    tools: Option<ToolCapabilities>,
}

#[derive(Debug, Serialize)]
struct ToolCapabilities {
    #[serde(rename = "listChanged")]
    list_changed: bool,
}

#[derive(Debug, Serialize)]
struct Implementation {
    name: String,
    version: String,
}

// ---------------------------------------------------------------------------
// MCP tools/list types
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct ListToolsResult {
    tools: Vec<McpTool>,
}

#[derive(Debug, Serialize)]
struct McpTool {
    name: String,
    description: String,
    #[serde(rename = "inputSchema")]
    input_schema: Value,
}

// ---------------------------------------------------------------------------
// MCP tools/call types
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct CallToolParams {
    name: String,
    #[serde(default)]
    arguments: Option<Value>,
}

#[derive(Debug, Serialize)]
struct CallToolResult {
    content: Vec<ToolContent>,
    #[serde(default, rename = "isError")]
    is_error: bool,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type")]
enum ToolContent {
    #[serde(rename = "text")]
    Text { text: String },
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize tracing — log to stderr
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();

    tracing::info!("test-rust-tool MCP server starting");

    let stdin = tokio::io::stdin();
    let reader = BufReader::new(stdin);
    let mut lines = reader.lines();

    let stdout = tokio::io::stdout();
    let mut writer = tokio::io::BufWriter::new(stdout);

    let mut initialized = false;

    while let Some(line) = lines.next_line().await? {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }

        // Parse the JSON-RPC message
        let request: JsonRpcRequest = match serde_json::from_str(&line) {
            Ok(req) => req,
            Err(e) => {
                tracing::error!("Failed to parse JSON-RPC: {e}");
                continue;
            }
        };

        let req_id = request.id;
        let method = request.method.as_str();

        tracing::info!("Received method='{method}' id={req_id:?}");

        match method {
            "initialize" => {
                if let Some(id) = req_id {
                    handle_initialize(&mut writer, id).await?;
                    initialized = true;
                }
            }
            "notifications/initialized" => {
                // No response expected for notifications
                tracing::info!("Client initialized notification received");
            }
            "tools/list" => {
                if !initialized {
                    send_error(
                        &mut writer,
                        req_id.unwrap_or(0),
                        -32000,
                        "Server not initialized",
                    )
                    .await?;
                    continue;
                }
                if let Some(id) = req_id {
                    handle_tools_list(&mut writer, id).await?;
                }
            }
            "tools/call" => {
                if !initialized {
                    send_error(
                        &mut writer,
                        req_id.unwrap_or(0),
                        -32000,
                        "Server not initialized",
                    )
                    .await?;
                    continue;
                }
                if let Some(id) = req_id {
                    let params = request.params.unwrap_or_default();
                    let call_params: CallToolParams =
                        serde_json::from_value(params).context("Invalid tools/call params")?;
                    handle_tools_call(&mut writer, id, &call_params).await?;
                }
            }
            _ => {
                tracing::warn!("Unknown method: {method}");
                if let Some(id) = req_id {
                    send_error(&mut writer, id, -32601, format!("Method not found: {method}")).await?;
                }
            }
        }
    }

    tracing::info!("test-rust-tool MCP server shutting down (stdin closed)");
    Ok(())
}

// ---------------------------------------------------------------------------
// Handler implementations
// ---------------------------------------------------------------------------

async fn handle_initialize<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
) -> Result<()> {
    let result = InitializeResult {
        protocol_version: MCP_PROTOCOL_VERSION.to_string(),
        capabilities: ServerCapabilities {
            tools: Some(ToolCapabilities {
                list_changed: false,
            }),
        },
        server_info: Implementation {
            name: "test-rust-tool".to_string(),
            version: "0.1.0".to_string(),
        },
    };

    let response = JsonRpcSuccess {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        result: serde_json::to_value(result)?,
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    tracing::info!("Initialized: test-rust-tool v0.1.0");
    Ok(())
}

async fn handle_tools_list<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
) -> Result<()> {
    let wait_tool = McpTool {
        name: "test-rust-tool_wait".to_string(),
        description: "[test-rust-tool] Sleep for a specified duration in seconds (default 900 = 15 minutes)".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "duration_secs": {
                    "type": "integer",
                    "description": "Seconds to wait",
                    "default": 900
                }
            },
            "required": []
        }),
    };

    let echo_tool = McpTool {
        name: "test-rust-tool_echo".to_string(),
        description: "[test-rust-tool] Echo back a greeting: 'Hello, {input}'".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Name to greet (default: GREETING_NAME env var or 'World')"
                }
            },
            "required": []
        }),
    };

    let save_datetime_tool = McpTool {
        name: "test-rust-tool_save-datetime".to_string(),
        description: "[test-rust-tool] Write the current date/time (ISO 8601 format) to a file".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write the datetime to"
                }
            },
            "required": ["path"]
        }),
    };

    let test_error_tool = McpTool {
        name: "test-rust-tool_test-error".to_string(),
        description: "[test-rust-tool] Return a test error: 'Test error from rust: <input>'".to_string(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Error message input"
                }
            },
            "required": ["input"]
        }),
    };

    let result = ListToolsResult {
        tools: vec![wait_tool, echo_tool, save_datetime_tool, test_error_tool],
    };

    let response = JsonRpcSuccess {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        result: serde_json::to_value(result)?,
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    tracing::info!("tools/list returned 4 tools");
    Ok(())
}

async fn handle_tools_call<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    params: &CallToolParams,
) -> Result<()> {
    tracing::info!("tools/call: name='{}' arguments={:?}", params.name, params.arguments);

    match params.name.as_str() {
        "test-rust-tool_wait" => handle_wait(writer, req_id, params).await?,
        "test-rust-tool_echo" => handle_echo(writer, req_id, params).await?,
        "test-rust-tool_save-datetime" => handle_save_datetime(writer, req_id, params).await?,
        "test-rust-tool_test-error" => handle_test_error(writer, req_id, params).await?,
        _ => {
            send_error(
                writer,
                req_id,
                -32602,
                format!("Unknown tool: {}", params.name),
            )
            .await?;
        }
    }

    Ok(())
}

async fn handle_wait<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    params: &CallToolParams,
) -> Result<()> {
    // Extract duration_secs from arguments (default 900)
    let duration_secs = params
        .arguments
        .as_ref()
        .and_then(|a| a.get("duration_secs"))
        .and_then(|v| v.as_u64())
        .unwrap_or(900);

    tracing::info!("wait tool called: sleeping for {duration_secs} second(s)");

    // Sleep for the requested duration (single sleep — no stdin conflict).
    // Do NOT create a new stdin BufReader here: the main loop already owns
    // the stdin reader, and a second BufReader on tokio::io::stdin() shares
    // the same fd, causing data loss when the next queued request arrives
    // on stdin during the wait.
    tokio::time::sleep(Duration::from_secs(duration_secs)).await;

    let result = CallToolResult {
        content: vec![ToolContent::Text {
            text: format!("Waited for {duration_secs} seconds"),
        }],
        is_error: false,
    };

    let response = JsonRpcSuccess {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        result: serde_json::to_value(result)?,
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    tracing::info!("wait tool completed: slept for {duration_secs} second(s)");

    Ok(())
}

async fn handle_echo<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    params: &CallToolParams,
) -> Result<()> {
    let name = params
        .arguments
        .as_ref()
        .and_then(|a| a.get("input"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .or_else(|| std::env::var("GREETING_NAME").ok().filter(|s| !s.is_empty()))
        .unwrap_or_else(|| "World".to_string());

    let text = format!("Hello, {}", name);

    tracing::info!("echo tool called: name='{}'", name);

    let result = CallToolResult {
        content: vec![ToolContent::Text { text }],
        is_error: false,
    };

    let response = JsonRpcSuccess {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        result: serde_json::to_value(result)?,
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    tracing::info!("echo tool completed");
    Ok(())
}

async fn handle_save_datetime<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    params: &CallToolParams,
) -> Result<()> {
    let path = params
        .arguments
        .as_ref()
        .and_then(|a| a.get("path"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());

    let path = match path {
        Some(p) => p,
        None => {
            let result = CallToolResult {
                content: vec![ToolContent::Text {
                    text: "Error: 'path' argument is required".to_string(),
                }],
                is_error: true,
            };
            let response = JsonRpcSuccess {
                jsonrpc: "2.0".to_string(),
                id: req_id,
                result: serde_json::to_value(result)?,
            };
            let json = serde_json::to_string(&response)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
            tracing::warn!("save-datetime tool called without path argument");
            return Ok(());
        }
    };

    let datetime = Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();

    tracing::info!("save-datetime tool called: path='{}'", path);

    match tokio::fs::write(&path, &datetime).await {
        Ok(_) => {
            let result = CallToolResult {
                content: vec![ToolContent::Text {
                    text: format!("Saved datetime to {}: {}", path, datetime),
                }],
                is_error: false,
            };
            let response = JsonRpcSuccess {
                jsonrpc: "2.0".to_string(),
                id: req_id,
                result: serde_json::to_value(result)?,
            };
            let json = serde_json::to_string(&response)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
            tracing::info!("save-datetime tool completed: wrote to {}", path);
        }
        Err(e) => {
            let result = CallToolResult {
                content: vec![ToolContent::Text {
                    text: format!("Error writing to {}: {}", path, e),
                }],
                is_error: true,
            };
            let response = JsonRpcSuccess {
                jsonrpc: "2.0".to_string(),
                id: req_id,
                result: serde_json::to_value(result)?,
            };
            let json = serde_json::to_string(&response)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
            tracing::warn!("save-datetime tool failed to write to {}: {}", path, e);
        }
    }

    Ok(())
}

async fn handle_test_error<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    params: &CallToolParams,
) -> Result<()> {
    let input_val = params
        .arguments
        .as_ref()
        .and_then(|a| a.get("input"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    let text = format!("Test error from rust: {}", input_val);

    tracing::info!("test-error tool called: input='{}'", input_val);

    let result = CallToolResult {
        content: vec![ToolContent::Text { text }],
        is_error: true,
    };

    let response = JsonRpcSuccess {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        result: serde_json::to_value(result)?,
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    tracing::info!("test-error tool completed");
    Ok(())
}

// ---------------------------------------------------------------------------
// Helper: send a JSON-RPC error response
// ---------------------------------------------------------------------------

async fn send_error<W: AsyncWriteExt + Unpin, M: Into<String>>(
    writer: &mut tokio::io::BufWriter<W>,
    req_id: u64,
    code: i64,
    message: M,
) -> Result<()> {
    let response = JsonRpcErrorResponse {
        jsonrpc: "2.0".to_string(),
        id: req_id,
        error: JsonRpcError {
            code,
            message: message.into(),
            data: None,
        },
    };

    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;

    Ok(())
}
