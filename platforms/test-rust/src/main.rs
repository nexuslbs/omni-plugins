//! Test Rust Platform Plugin for OmniAgent.
//!
//! Standalone binary that implements the platform plugin protocol over stdio.
//! Echoes the test-python plugin functionality in Rust.
//!
//! Protocol: JSON-lines over stdin/stdout.
//!
//! Methods:
//!   - initialize:      Return plugin info and capabilities
//!   - configure:       Receive configuration params
//!   - deliver:         Log a message delivery and return success
//!   - edit_message:    Log an edit and return success
//!   - delete_message:  Log a deletion and return success

use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

// ---------------------------------------------------------------------------
// Plugin protocol types
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct PluginRequest {
    #[serde(default)]
    id: Option<u64>,
    method: String,
    #[serde(default)]
    params: Option<Value>,
}

#[derive(Debug, Serialize)]
struct PluginResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<PluginError>,
}

#[derive(Debug, Serialize)]
struct PluginError {
    code: i64,
    message: String,
}

#[derive(Debug, Deserialize, Default)]
struct PluginConfig {
    #[serde(default)]
    platform_greeting: Option<String>,
}

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

fn make_success(id: u64, result: Value) -> PluginResponse {
    PluginResponse {
        id: Some(id),
        result: Some(result),
        error: None,
    }
}

fn make_error(id: u64, code: i64, message: &str) -> PluginResponse {
    PluginResponse {
        id: Some(id),
        result: None,
        error: Some(PluginError {
            code,
            message: message.to_string(),
        }),
    }
}

async fn send_response(writer: &mut tokio::io::BufWriter<tokio::io::Stdout>, response: &PluginResponse) -> Result<()> {
    let line = serde_json::to_string(response)?;
    writer.write_all(line.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Method handlers
// ---------------------------------------------------------------------------

fn handle_initialize(id: u64) -> PluginResponse {
    make_success(
        id,
        serde_json::json!({
            "name": "test-rust",
            "capabilities": {
                "inbound": false,
                "outbound": true,
            },
        }),
    )
}

fn handle_configure(id: u64, config: PluginConfig) -> PluginResponse {
    let greeting = config.platform_greeting.unwrap_or_else(|| "Hello from Rust".to_string());
    tracing::info!("Configured with greeting: {}", greeting);
    make_success(
        id,
        serde_json::json!({
            "configured": true,
            "greeting": greeting,
        }),
    )
}

fn handle_deliver(id: u64, params: Option<Value>, message_counter: &mut u64) -> PluginResponse {
    *message_counter += 1;
    let resource = params
        .as_ref()
        .and_then(|p| p.get("resource_identifier"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let content = params
        .as_ref()
        .and_then(|p| p.get("content"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let msg_type = params
        .as_ref()
        .and_then(|p| p.get("msg_type"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    tracing::info!(
        "Deliver [{}] to {} (type={}): {}",
        message_counter,
        resource,
        msg_type,
        &content[..content.len().min(80)],
    );

    make_success(
        id,
        serde_json::json!({
            "delivered": true,
            "external_id": format!("test-{}", message_counter),
        }),
    )
}

fn handle_edit_message(id: u64, params: Option<Value>) -> PluginResponse {
    let resource = params
        .as_ref()
        .and_then(|p| p.get("resource_identifier"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let external_id = params
        .as_ref()
        .and_then(|p| p.get("external_id"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let content = params
        .as_ref()
        .and_then(|p| p.get("content"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    tracing::info!(
        "Edit message {} in {}: {}",
        external_id,
        resource,
        &content[..content.len().min(80)],
    );

    make_success(id, serde_json::json!({"edited": true}))
}

fn handle_delete_message(id: u64, params: Option<Value>) -> PluginResponse {
    let resource = params
        .as_ref()
        .and_then(|p| p.get("resource_identifier"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let external_id = params
        .as_ref()
        .and_then(|p| p.get("external_id"))
        .and_then(|v| v.as_str())
        .unwrap_or("");

    tracing::info!("Delete message {} in {}", external_id, resource);

    make_success(id, serde_json::json!({"deleted": true}))
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new("info"))
        .with_writer(std::io::stderr)
        .init();

    tracing::info!("Test Rust platform plugin starting (PID={})", std::process::id());

    let stdin = tokio::io::stdin();
    let reader = BufReader::new(stdin);
    let mut lines = reader.lines();

    let stdout = tokio::io::stdout();
    let mut writer = tokio::io::BufWriter::new(stdout);

    let mut message_counter: u64 = 0;

    // ── Process messages ─────────────────────────────────────────────
    while let Some(line) = lines.next_line().await? {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }

        let request: PluginRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                tracing::error!("Failed to parse request: {}", e);
                continue;
            }
        };

        let req_id = request.id.unwrap_or(0);
        let response = match request.method.as_str() {
            "initialize" => handle_initialize(req_id),
            "configure" => {
                let config: PluginConfig = request
                    .params
                    .map(|p| serde_json::from_value(p).unwrap_or_default())
                    .unwrap_or_default();
                handle_configure(req_id, config)
            }
            "deliver" => handle_deliver(req_id, request.params, &mut message_counter),
            "edit_message" => handle_edit_message(req_id, request.params),
            "delete_message" => handle_delete_message(req_id, request.params),
            other => {
                tracing::warn!("Unknown method: {}", other);
                make_error(req_id, -1, &format!("Unknown method: {}", other))
            }
        };

        send_response(&mut writer, &response).await?;
    }

    tracing::info!("Test Rust platform plugin shutting down (stdin closed)");
    Ok(())
}
