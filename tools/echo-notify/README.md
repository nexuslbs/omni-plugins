# echo-notify (second, non-telegram listener)

LISTENER plugin for the canonical `solve-captcha` published event. It performs
NO delivery to any channel: it appends the event to a JSONL log file. It exists
as the reference PROOF that delivery is pluggable and that several listeners can
be bound to the same event, each handling it independently.

## Tools (MCP stdio)

| tool | purpose |
| --- | --- |
| `echo_notify` | append the event to the JSONL log and return `ok` + the logged row |
| `echo_notify_status` | describe the local configuration (log path, mode) |

## Configuration

| key | default | meaning |
| --- | --- | --- |
| `ECHO_NOTIFY_LOG` | `/opt/omni/data/human-intervention/notifications.jsonl` | JSONL destination |
| `ECHO_NOTIFY_MODE` | `log` | `log` = append; `fail` = always error (used by the isolation test) |

## Wiring

```yaml
# {data_dir}/config/tasks.yml
hooks:
  solve-captcha-echo:
    enabled: true
    event: solve-captcha
    scope: global
    count: 1
    mode: action
    action: solve_captcha_echo

# {data_dir}/config/actions.yml
actions:
  solve_captcha_echo:
    enabled: true
    tool_name: echo-notify__echo_notify
    params: {}
```

## Tests

```bash
python3 -m unittest discover -s tools/echo-notify/tests -p "test_*.py"
```

Covers the log append, the failure mode used to prove listener isolation, and
the MCP stdio surface.
