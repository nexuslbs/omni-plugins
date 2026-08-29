# Telegram Platform Plugin - mock-based testing, no real token

A Python implementation of the omniagent **telegram platform plugin**
(JSON-lines protocol over stdio) that talks to the Telegram Bot API.

## Files

| Path | Purpose |
|---|---|
| `plugin.json` | Platform manifest: capabilities inbound+outbound, config schema |
| `platform.py` | The plugin - stdlib-only (urllib), no external deps |
| `tests/mock_telegram_api.py` | Mock Telegram Bot API server (in-memory state) |
| `tests/smoke_test.py` | End-to-end smoke test against the mock (no real token) |

## Protocol support

* `initialize` → name `telegram`, capabilities `{inbound: true, outbound: true}`
* `configure` → stores `bot_token`, `api_base_url`, `polling_enabled`,
  `poll_interval_secs`, `parent_by_chat`
* `deliver` → `POST sendMessage` (chat_id = resource_identifier, text = content)
* `edit_message` → `POST editMessageText`
* `delete_message` → `POST deleteMessage`
* `react` → `POST setMessageReaction`
* `typing` → `POST sendChatAction` (action `typing`)
* inbound → background long-poll of `getUpdates` (offset-based); each inbound
  message is emitted to stdout as an `inbound_message` notification
  (`resource_identifier` = chat id, `text`, `external_id` = message_id,
  `metadata`), edits as `message_edited` - same contract as the built-in
  mattermost platform.

## Config

| Key | Type | Default | Notes |
|---|---|---|---|
| `bot_token` | secret, required | - | Telegram Bot API token from @BotFather |
| `api_base_url` | string | `https://api.telegram.org` | Override to point at the mock for tests |
| `polling_enabled` | boolean | `true` | Enable inbound getUpdates long-polling |
| `poll_interval_secs` | integer | `5` | getUpdates long-poll timeout + loop cadence |
| `parent_by_chat` | boolean | `false` | When `true`, every inbound user message carries the chat id as the **parent external id** (delivered via `metadata["root_id"]`, the envelope key omniagent reads as `parent_external_id`), so threads created from the same chat always share one parent and pending messages from that chat merge into a processing thread via omniagent's existing pending/sub-prompt machinery. Default `false`: no parent id - current behavior (each message creates its own thread). |

## Testing without a real token (default)

```bash
# 1. start the mock Telegram Bot API
python3 tests/mock_telegram_api.py --port 8091

# 2. run the smoke test (starts its own mock on a free port)
python3 tests/smoke_test.py
```

Configure the platform with `"api_base_url": "http://127.0.0.1:8091"` and any
non-empty `bot_token` (the mock accepts any token). All testing is local -
nothing ever touches the real Telegram API and no real credentials are used.

The mock implements `getUpdates` (long-poll + offset confirmation),
`sendMessage`, `editMessageText`, `deleteMessage`, `setMessageReaction`,
`getMe`, `sendChatAction`, plus admin endpoints for tests:
`POST /admin/inject`, `GET /admin/sent`, `GET /admin/actions`,
`GET /admin/reactions`, `GET /admin/updates`, `POST /admin/reset`.

## Real full test (operator - requires a NEW bot, never Hermes')

A real end-to-end test needs a **fresh bot token created for omniagent** -
**never reuse the Hermes telegram bot token** (it belongs to Hermes, not
omniagent; do not reference or test against it).

1. Operator creates a new bot via **@BotFather** (`/newbot`) and names it for
   omniagent (e.g. `omniagent-bot`).
2. Store the token as an omniagent secret (e.g. secret name
   `telegram_bot_token`) and set `bot_token` to reference it in the platform
   config, or export it in the plugin environment.
3. Configure the platform in `config/channels.yml` + `config/plugins.yml`
   (platform: `telegram`, source: remote from omni-plugins):
   ```yaml
   platforms:
     telegram:
       enabled: true
       source: remote
       config:
         bot_token: "$secret:telegram_bot_token"   # or the raw new token
         api_base_url: "https://api.telegram.org"
         polling_enabled: true
         poll_interval_secs: 5
         parent_by_chat: false
   ```
4. Register the channel: send a message to the bot from the target Telegram
   chat, or start a chat with the bot and run `$new` - the agent creates a
   channel bound to that Telegram chat id.
5. Verify outbound (agent replies arrive in Telegram) and inbound (Telegram
   messages reach the agent and are answered).

The smoke test + G33 integration tests are the default verification path and
do not require any real token.
