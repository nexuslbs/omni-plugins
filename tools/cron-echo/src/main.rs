use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tracing_subscriber::EnvFilter;

const MCP_PROTOCOL_VERSION: &str = "2025-03-26";

#[derive(Debug, Deserialize)]
struct JsonRpcRequest {
    #[allow(dead_code)]
    jsonrpc: String,
    #[serde(default)]
    id: Option<u64>,
    method: String,
    #[serde(default)]
    params: Option<Value>,
}

#[derive(Debug, Serialize)]
struct JsonRpcSuccess {
    jsonrpc: String,
    id: u64,
    result: Value,
}

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

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();

    tracing::info!("cron MCP server starting");

    let stdin = tokio::io::stdin();
    let reader = BufReader::new(stdin);
    let mut lines = reader.lines();
    let stdout = tokio::io::stdout();
    let mut writer = tokio::io::BufWriter::new(stdout);

    let mut initialized = false;

    while let Some(line) = lines.next_line().await? {
        let line = line.trim().to_string();
        if line.is_empty() { continue; }

        let request: JsonRpcRequest = match serde_json::from_str(&line) {
            Ok(req) => req,
            Err(e) => { tracing::error!("Parse error: {e}"); continue; }
        };

        let req_id = request.id;

        match request.method.as_str() {
            "initialize" => {
                if let Some(id) = req_id {
                    handle_initialize(&mut writer, id).await?;
                    initialized = true;
                }
            }
            "notifications/initialized" => {}
            "tools/list" => {
                if let Some(id) = req_id {
                    handle_tools_list(&mut writer, id).await?;
                }
            }
            "tools/call" => {
                if let Some(id) = req_id {
                    let params = request.params.unwrap_or_default();
                    let call_params: CallToolParams = serde_json::from_value(params)?;
                    handle_tools_call(&mut writer, id, &call_params).await?;
                }
            }
            _ => {
                if let Some(id) = req_id {
                    send_error(&mut writer, id, -32601, format!("Unknown: {}", request.method)).await?;
                }
            }
        }
    }

    let _ = &initialized;

    Ok(())
}

async fn handle_initialize<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>, req_id: u64,
) -> Result<()> {
    let result = InitializeResult {
        protocol_version: MCP_PROTOCOL_VERSION.to_string(),
        capabilities: ServerCapabilities { tools: Some(ToolCapabilities { list_changed: false }) },
        server_info: Implementation { name: "cron".into(), version: "0.1.0".into() },
    };
    let response = JsonRpcSuccess {
        jsonrpc: "2.0".into(), id: req_id,
        result: serde_json::to_value(result)?,
    };
    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}

async fn handle_tools_list<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>, req_id: u64,
) -> Result<()> {
    let echo_tool = McpTool {
        name: "cron_echo".into(),
        description: "[cron] Echo back the input message".into(),
        input_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to echo back"
                }
            },
            "required": []
        }),
    };
    let result = ListToolsResult { tools: vec![echo_tool] };
    let response = JsonRpcSuccess {
        jsonrpc: "2.0".into(), id: req_id,
        result: serde_json::to_value(result)?,
    };
    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}

async fn handle_tools_call<W: AsyncWriteExt + Unpin>(
    writer: &mut tokio::io::BufWriter<W>, req_id: u64, params: &CallToolParams,
) -> Result<()> {
    match params.name.as_str() {
        "cron_echo" => {
            let msg = params.arguments.as_ref()
                .and_then(|a| a.get("message"))
                .and_then(|v| v.as_str())
                .unwrap_or("hello");
            let result = CallToolResult {
                content: vec![ToolContent::Text { text: format!("Echo: {}", msg) }],
                is_error: false,
            };
            let response = JsonRpcSuccess {
                jsonrpc: "2.0".into(), id: req_id,
                result: serde_json::to_value(result)?,
            };
            let json = serde_json::to_string(&response)?;
            writer.write_all(json.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
        }
        _ => send_error(writer, req_id, -32602, format!("Unknown tool: {}", params.name)).await?,
    }
    Ok(())
}

async fn send_error<W: AsyncWriteExt + Unpin, M: Into<String>>(
    writer: &mut tokio::io::BufWriter<W>, req_id: u64, code: i64, message: M,
) -> Result<()> {
    let response = JsonRpcErrorResponse {
        jsonrpc: "2.0".into(), id: req_id,
        error: JsonRpcError { code, message: message.into(), data: None },
    };
    let json = serde_json::to_string(&response)?;
    writer.write_all(json.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}
