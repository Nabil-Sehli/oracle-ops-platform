#!/usr/bin/env python3
"""Post two synthetic runs and check they came back out of /metrics.

Run it inside the collector's own container, where the token is in the
environment already:

    sudo docker compose -f /opt/ops/compose.yml exec -T llmobs python /app/smoke.py

It writes two runs under the workflow name "smoke-test": one success and one
503 failure. They show up in the trace list for the retention window and add
two runs to the counters, which is the point - the check is end to end.
"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("LLMOBS_SMOKE_URL", "http://127.0.0.1:9109")
TOKEN = os.environ.get("LLMOBS_TOKEN", "")
STAMP = int(time.time())


def post(event):
    request = urllib.request.Request(BASE + "/v1/events", method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Telemetry-Token", TOKEN)
    request.data = json.dumps(event).encode()
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.read().decode()


def check(label, condition):
    print("{0} {1}".format("ok  " if condition else "FAIL", label))
    return condition


ok_run = {
    "run_id": "smoke-ok-{0}".format(STAMP),
    "workflow": "smoke-test",
    "status": "ok",
    "duration_ms": 1850,
    "steps": [
        {"node": "Validate Lead", "status": "ok", "duration_ms": 6},
        {"node": "Gemini: Score Lead", "status": "ok", "duration_ms": 1700,
         "provider": "gemini", "model": "gemini-3.5-flash-lite",
         "tokens_in": 900, "tokens_out": 120, "http_status": 200},
        {"node": "Log Lead to Sheet", "status": "ok", "duration_ms": 140},
    ],
    "attrs": {"source": "smoke.py"},
}

failed_run = {
    "run_id": "smoke-fail-{0}".format(STAMP),
    "workflow": "smoke-test",
    "status": "failed",
    "duration_ms": 6200,
    "error": "The model is overloaded. Please try again later.",
    "steps": [
        {"node": "Validate Lead", "status": "ok", "duration_ms": 6},
        {"node": "Gemini: Score Lead", "status": "failed", "duration_ms": 6100,
         "provider": "gemini", "model": "gemini-3.5-flash-lite",
         "tokens_in": 900, "http_status": 503},
    ],
    "attrs": {"source": "smoke.py"},
}


def main():
    passed = []
    code, body = post(ok_run)
    passed.append(check("success run accepted ({0})".format(code), code == 202))

    code, body = post(failed_run)
    passed.append(check("failed run accepted ({0})".format(code), code == 202))

    code, body = post(failed_run)
    passed.append(check("the same run twice is ignored", body.get("duplicate") is True))

    trace = json.loads(get("/v1/runs/" + failed_run["run_id"]))
    passed.append(check("failure blamed on the right node: {0}".format(trace["failed_node"]),
                        trace["failed_node"] == "Gemini: Score Lead"))
    passed.append(check("failure classified as {0}".format(trace["error_type"]),
                        trace["error_type"] == "provider_overloaded"))

    metrics = get("/metrics")
    for needle in (
        'llm_runs_total{status="ok",workflow="smoke-test"}',
        'llm_runs_total{status="failed",workflow="smoke-test"}',
        'llm_run_failures_total{error_type="provider_overloaded"',
        'llm_cost_usd_total{model="gemini-3.5-flash-lite"',
        "llm_run_duration_seconds_bucket",
    ):
        passed.append(check("exported {0}".format(needle), needle in metrics))
    passed.append(check("no unpriced model in the price table",
                        "llmobs_unknown_model_total" not in metrics))

    print("\n{0}/{1} checks passed".format(sum(passed), len(passed)))
    print("Trace: /runs/{0}".format(failed_run["run_id"]))
    return 0 if all(passed) else 1


if __name__ == "__main__":
    sys.exit(main())
