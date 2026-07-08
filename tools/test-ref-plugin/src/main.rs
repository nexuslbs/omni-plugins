use anyhow::{Context, Result};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tracing_subscriber::EnvFilter;

const MCP_PROTOCOL_VERSION: &str = "2025-03-26";

// Minimal MCP server — just returns "echo" tool for verification
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env()
            .add_directive(tracing::Level::INFO.into()))
        .init();

    let mut stdin = BufReader::new(tokio::io::stdin());
    let mut line = String::new();

    while stdin.read_line(&mut line).await? {
        if line.trim().is_empty() {
            line.clear();
            continue;
        }

        let request: Value = serde_json::from_str(&line)
            .context("Failed to parse JSON-RPC request")?;

        let id = request.get("id");
        let method = request.get("method")
            .and_then(|m| m.as_str())
            .unwrap_or("");

        match method {
            "initialize" => {
                let result = serde_json::json!({
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "test-ref-plugin",
                        "version": "0.1.0"
                    }
                });
                send_response(&mut stdin, id, result).await?;
            }
            "notifications/initialized" => {
                // no response needed
            }
            "tools/list" => {
                let result = serde_json::json!({
                    "tools": [
                        {
                            "name": "test-ref-plugin_echo",
                            "description": "Echo back the input",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"}
                                }
                            }
                        }
                    ]
                });
                send_response(&mut stdin, id, result).await?;
            }
            "tools/call" => {
                let args = request.get("params")
                    .and_then(|p| p.get("arguments"))
                    .and_then(|a| a.as_object())
                    .cloned()
                    .unwrap_or_default();
                let result = serde_json::json!({
                    "content": [
                        {
                            "type": "text",
                            "text": format!("echoed: {}", serde_json::to_string(&args).unwrap_or_default())
                        }
                    ]
                });
                send_response(&mut stdin, id, result).await?;
            }
            _ => {
                let error = serde_json::json!({
                    "code": -32601,
                    "message": format!("Method not found: {}", method)
                });
                send_error(&mut stdin, id, error).await?;
            }
        }
        line.clear();
    }
    Ok(())
}

async fn send_response(stdin: &mut BufReader<tokio::io::Stdin>, id: Option<&Value>, result: Value) -> Result<()> {
    let response = serde_json::json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    });
    let mut stdout = tokio::io::stdout();
    stdout.write_all(serde_json::to_string(&response)?.as_bytes()).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

async fn send_error(stdin: &mut BufReader<tokio::io::Stdin>, id: Option<&Value>, error: Value) -> Result<()> {
    let response = serde_json::json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": error
    });
    let mut stdout = tokio::io::stdout();
    stdout.write_all(serde_json::to_string(&response)?.as_bytes()).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}
