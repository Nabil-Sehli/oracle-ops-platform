# LLM observability

Prometheus can tell me a container is up. n8n can tell me an execution failed. Neither can tell
me *which node* failed, on which model, after how long, or what the run cost. `llmobs` closes
that gap: every workflow run posts one event, which becomes both a readable trace and a set of
counters.

- **Trace:** `https://llmobs.nabil-ops.duckdns.org` (behind the same login as Grafana)
- **Dashboard:** Grafana → *LLM runs*
- **Collector:** `ansible/roles/llmobs/files/collector.py`, ~600 lines, standard library only

## Why a collector and not a library

The workflows are n8n nodes, not a Python service, so there is no process to instrument with an
SDK. What they *can* do is make one HTTP call. So the contract is an HTTP event, and everything
else — storage, pricing, aggregation, exposition — lives in one small service that the existing
Prometheus already scrapes.

Running it on a stock `python:3.13-alpine` image with the script bind-mounted means there is no
image to build on an arm64 box, no dependency to patch, and a code change is a file copy plus a
container restart.

## The event

`POST http://llmobs:9109/v1/events` from inside the Docker network, with
`X-Telemetry-Token: <llm_telemetry_token>`. One object, or `{"events": [...]}` for up to 100.

```json
{
  "run_id": "n8n-4711",
  "workflow": "AI Lead Qualification",
  "status": "failed",
  "started_at": 1758276000,
  "duration_ms": 3100,
  "failed_node": "Gemini: Score Lead",
  "error": "The model is overloaded. Please try again later.",
  "steps": [
    { "node": "Validate Lead", "status": "ok", "duration_ms": 8 },
    { "node": "Gemini: Score Lead", "status": "failed", "duration_ms": 2900,
      "provider": "gemini", "model": "gemini-3.5-flash-lite",
      "tokens_in": 812, "tokens_out": 0, "http_status": 503 }
  ],
  "attrs": { "tier": "hot", "score": 95 }
}
```

Only `workflow` is required. Everything else the collector will infer or leave out:

| Left out | What happens |
|---|---|
| `run_id` | One is generated. Sending your own is better: it makes retries idempotent. |
| `failed_node` | Taken from the last step whose status isn't `ok`. |
| `error_type` | Classified from the HTTP status, then from the message: `rate_limited`, `provider_overloaded`, `auth`, `bad_request`, `provider_error`, `timeout`, `unreachable`, `bad_model_output`, `unknown`. |
| `status` | Defaults to `ok`. |

Sending the same `run_id` twice returns `200 {"duplicate": true}` and changes nothing. n8n retries
a failed HTTP node three times; without that guard, a retry would inflate every rate and every
cost figure.

## The metrics

| Metric | Labels | Reads as |
|---|---|---|
| `llm_runs_total` | workflow, status | How much ran, and how much of it worked |
| `llm_run_failures_total` | workflow, failed_node, error_type | **Where** it breaks and **why** |
| `llm_run_duration_seconds` | workflow | End-to-end latency (histogram) |
| `llm_steps_total` | workflow, node, status | Per-node outcomes |
| `llm_step_duration_seconds` | workflow, node | Which node is slow (histogram) |
| `llm_tokens_total` | workflow, model, direction | Token volume |
| `llm_cost_usd_total` | workflow, model | Spend |
| `llm_provider_responses_total` | workflow, model, code | 429s and 503s, before they become failures |

Plus `llmobs_*` self-metrics: events stored, rejected (by reason), runs kept, database size,
unpriced models, and label sets collapsed at the cardinality cap.

## Three decisions worth the words

**Counters are stored, not derived.** Traces are pruned after 14 days; counters live in their own
table and are never pruned. Deriving `llm_runs_total` from the trace rows would have made the
counter drop every night at prune time, and a Prometheus counter that goes backwards is read as a
restart — every `rate()` over it would be wrong.

**An unpriced model is louder than a free one.** Cost is tokens times a price table in `site.yml`.
A model missing from that table is counted but not priced, and raises `llmobs_unknown_model_total`
→ the `LlmModelNotPriced` alert. A spend panel that silently reads `$0.00` after a model switch is
worse than no panel.

**Cardinality has a hard cap.** Label values come from workflow JSON, so a renamed-per-run node
could mint unbounded series and take Prometheus down with it. Past `LLMOBS_MAX_SERIES` per metric,
new label sets fold into `other` and `llmobs_series_collapsed_total` says it happened.

## Wiring a workflow

**Failures, with no edit to any workflow.** `scripts/n8n/telemetry-error-trigger.json` is an
error-trigger workflow: import it, set it as the error workflow, and every failed execution in
this n8n reports itself. It needs one credential — Header Auth named `llmobs telemetry token`,
header `X-Telemetry-Token`, value `llm_telemetry_token` from `secrets.yml` — and its HTTP node is
set to continue on error, because telemetry must never turn one failed run into two.

**Successes** need a node inside the workflow, and both solar workflows now have one: a Code node
that builds the event and an HTTP node that posts it, hanging off the branch that runs *after* the
caller has been answered and the row written. They live in the
[workflow repository](https://github.com/Nabil-Sehli/n8n-solar-callcenter-automations) and stay off
until `telemetry_url` is set in **Config & Prompt**, so importing those workflows without a
collector changes nothing.

Two things they get right that are easy to get wrong:

- Gemini reports thinking tokens separately but bills them as output; Anthropic already folds them
  into `output_tokens`. Both are normalized, so a provider switch doesn't change what "output
  tokens" means halfway through a cost chart.
- The follow-up workflow runs attempt 2 an hour later *inside the same n8n execution*, so it puts
  the attempt number in `run_id`. Without that, the deduplication above would read the second
  attempt as a retry and silently drop a whole model call's tokens and cost.
- **Branch order decides when telemetry fires.** A Wait node suspends the *whole* execution and
  saves every sibling branch that hasn't run yet along with it. The telemetry branch hangs off the
  same node as the branch leading into "Wait 1 Hour", and connected second it sat in the execution
  stack for an hour before reporting. It is connected first now. Caught by watching a live run
  stop at exactly that point - the node was in n8n's saved `nodeExecutionStack`, which looks
  identical to "ran and returned nothing" unless you go looking.

A lead the model couldn't score is reported as `partial` rather than `failed`: the pipeline logged
it, answered the caller and emailed a human. Only the model let go. Counting that as a pipeline
failure would hide real breakage behind API weather.

## Alerts

All to Telegram through the existing Alertmanager: failure rate over 20% for 10 minutes, more
than three 429/5xx from a provider in 15 minutes, p95 run duration over 30s, 24h spend over
budget, and any model without a price. Thresholds are variables in `ansible/site.yml`.
