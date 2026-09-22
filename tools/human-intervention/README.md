# human-intervention (solver-side half of the `solve-captcha` hand-off)

Channel-agnostic SOLVER plugin for the case where a browser flow the agent
drives hits a challenge it must not (and may not) solve itself. It never talks
to a delivery channel: it PUBLISHES a canonical event and lets LISTENER plugins
do the delivery.

```
browser flow  --solve-captcha-->  published-event bus  --fan-out-->  listener plugins
     |                                     |                              |
     |<---------- solved | aborted | timeout (correlation id) -------------
   resume
```

## Contract

* **publish**: `POST /events/publish` with event `solve-captcha` and a BOUNDED
  payload: `session_id`, `url`, `reason`, `access_hint`, `timeout_s`,
  `correlation_id` (generated when the caller omits it).
* **wait**: `POST /events/wait` -> `solved | aborted | timeout | pending`
  (bounded; never an unbounded block).
* **terminal event**: when the plugin's own generic page-state detection sees
  the challenged document replaced by the original page, it publishes
  `solve-captcha-resolved` (or `-aborted` / `-timeout`) with the SAME
  correlation id.
* **resume**: on `solved` the caller re-runs its next step against the SAME
  session/context; on `aborted` the flow stops cleanly with a report; on
  `timeout` it reports the timeout, does not hang and leaves the session usable.

ZERO delivery code: no messaging integration, no HTTP send call, no credential,
no recipient id. `tests/test_two_listeners_fan_out.py` enforces this by grepping
this plugin's own sources for delivery vocabulary.

## Tools (MCP stdio)

| tool | purpose |
| --- | --- |
| `intervention_request` | guardrails + publish `solve-captcha`, return a handle (`correlation_id`, per-listener deliveries, `delivered`) |
| `intervention_wait` | bounded wait -> `solved` / `aborted` / `timeout` / `pending` + a `human-intervention: solved` resume marker |
| `intervention_detect` | generic page-state poll; publishes the terminal event when the challenge is gone |
| `intervention_probe` | ONE generic structural page-state snapshot over CDP |
| `intervention_status` | describe one interaction + the local guardrail state |

## Configuration (plugin config_schema)

| key | default | meaning |
| --- | --- | --- |
| `OMNI_API_URL` | `http://127.0.0.1:8080` | omniagent API hosting `/events/*` |
| `HI_STATE_DIR` | `/opt/omni/data/human-intervention` | guardrail state + `hand-offs.jsonl` audit log |
| `HI_COOLDOWN_S` | `600` | at most ONE hand-off per session per window |
| `HI_MAX_HANDOFFS` | `3` | hard cap per session (`capped` above it) |
| `HI_DEFAULT_TIMEOUT_S` | `900` | default interaction deadline |
| `HI_CDP_URL` | `http://browser:9222` | CDP base URL used ONLY by the structural probe |

## Registering a listener (no solver change)

A listener is a normal `action` hook bound to the published event. In
`{data_dir}/config/tasks.yml`:

```yaml
hooks:
  solve-captcha-<name>:
    enabled: true
    event: solve-captcha
    scope: global      # published events have no thread scope
    count: 1           # published events always deliver per event
    mode: action
    action: <action id>
```

and in `{data_dir}/config/actions.yml`:

```yaml
actions:
  <action id>:
    enabled: true
    tool_name: <plugin>__<tool>   # e.g. echo-notify__echo_notify
    params: {}
```

Several listeners may be bound to the same event (fan-out) and each handles it
independently: one failing listener is isolated and never breaks the others nor
the solver. Unregister a listener by setting `enabled: false` (or removing the
hook). The solver is untouched by any of this.

The repository ships two listeners next to this plugin:

* `tools/echo-notify` - appends every hand-off to a JSONL log (no delivery
  channel at all; the reference "second listener").
* a messaging notifier plugin that turns the event into ONE standalone message.

## Tests

```bash
python3 -m unittest discover -s tools/human-intervention/tests -p "test_*.py"
```

Covers: publish + fan-out to two real listener servers, listener isolation,
`unavailable` when nobody delivered, cooldown + hard cap, the structural
page-state predicate, terminal-event publication on recovery, bounded timeout
(no hang), abort handling and the zero-delivery-code grep.

No site knowledge: the detection is structure/state based only (document
identity/URL of the challenged document vs. the original target, challenge
overlay/iframe presence, page load state). No vendor vocabulary, no page-text
classification.
