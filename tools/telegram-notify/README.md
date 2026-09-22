# telegram-notify (standalone operator notification listener)

LISTENER plugin for the canonical `solve-captcha` published event. It turns the
event into ONE standalone Telegram message sent with the Bot API
(`sendMessage`) - no thread, no kanban task, no cause row, no platform-plugin
thread machinery.

Message body (the two lines are exact and always first):

```
Human intervention required
[Open browser session]
```

Optional context follows after a blank line (session id, URL, access hint).
No secret ever appears in the message.

## Tools (MCP stdio)

| tool | purpose |
| --- | --- |
| `telegram_notify` | send ONE standalone message for the given event payload (idempotent per correlation id); returns `ok` + `message_id` or `unavailable` when token/chat id are not configured |
| `telegram_notify_status` | describe the local configuration state (never the secret value) |

## Configuration

| key | meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | bot token, reference the secret by name: `$secret:TELEGRAM_TOKEN` |
| `TELEGRAM_CHAT_ID` | operator chat id the hand-off is delivered to |
| `TELEGRAM_API_BASE` | optional Bot API base override (tests / mirrors / mock) |
| `TELEGRAM_NOTIFY_STATE` | optional state file for per-correlation idempotency |

When the token or the chat id is missing the tool returns `unavailable` instead
of failing the flow (the solver keeps working; the delivery is simply not made).

## Wiring

```yaml
# {data_dir}/config/tasks.yml
hooks:
  solve-captcha-telegram:
    enabled: true
    event: solve-captcha
    scope: global
    count: 1
    mode: action
    action: solve_captcha_telegram

# {data_dir}/config/actions.yml
actions:
  solve_captcha_telegram:
    enabled: true
    tool_name: telegram-notify__telegram_notify
    params: {}
```

Disable it by setting `enabled: false` on the hook: the solver keeps
publishing, every other listener keeps delivering.

## Tests

```bash
python3 -m unittest discover -s tools/telegram-notify/tests -p "test_*.py"
```

against a fake Bot API (stdlib HTTP server): the exact two-line body, exactly
ONE `sendMessage` call, no thread/cause, `unavailable` without credentials, and
the MCP stdio surface.
