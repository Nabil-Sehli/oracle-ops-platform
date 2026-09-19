#!/usr/bin/env python3
"""LLM run collector: ingest, failure forensics and Prometheus metrics.

n8n posts one event per workflow run to /v1/events. The run and its steps land
in SQLite, so a broken run can be traced to the exact node that failed, and the
same event bumps counters scraped by the Prometheus already on this box.

Standard library only, on a stock python:3.x-alpine image with this file
bind-mounted: nothing to build on an arm64 server, and the only thing to patch
is the base image tag in compose.yml.

Counters live in their own table and are never pruned; only the run and step
rows are, after LLMOBS_RETENTION_DAYS. A Prometheus counter that went backwards
every night would poison every rate() over it.
"""

import hmac
import html
import json
import os
import signal
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

DB_PATH = os.environ.get("LLMOBS_DB", "/data/llmobs.db")
PORT = int(os.environ.get("LLMOBS_PORT", "9109"))
TOKEN = os.environ.get("LLMOBS_TOKEN", "")
RETENTION_DAYS = int(os.environ.get("LLMOBS_RETENTION_DAYS", "14"))
# {"model": {"in": <USD per 1M input tokens>, "out": <USD per 1M output tokens>}}
PRICES = json.loads(os.environ.get("LLMOBS_PRICES", "{}"))
# A runaway label value (a node renamed per run, say) would blow up Prometheus
# memory. Past this many series for one metric, extra label sets collapse.
MAX_SERIES = int(os.environ.get("LLMOBS_MAX_SERIES", "500"))
MAX_BODY = 256 * 1024
MAX_STEPS = 50

# n8n's HTTP Request nodes time out at 120s, so that is the top bucket.
BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

RUN_STATUSES = {"ok", "failed", "partial"}

# Rendered in this order. Histograms expand to _bucket/_sum/_count.
METRICS = {
    "llm_runs_total": ("counter", "Workflow runs recorded, by outcome."),
    "llm_run_failures_total": ("counter", "Failed runs, by the node that failed."),
    "llm_run_duration_seconds": ("histogram", "End-to-end run duration."),
    "llm_steps_total": ("counter", "Steps recorded, by node and outcome."),
    "llm_step_duration_seconds": ("histogram", "Per-node step duration."),
    "llm_tokens_total": ("counter", "Tokens billed, by model and direction."),
    "llm_cost_usd_total": ("counter", "Spend in USD, priced from LLMOBS_PRICES."),
    "llm_provider_responses_total": ("counter", "Provider HTTP responses, by status code."),
    "llmobs_events_total": ("counter", "Events accepted, by result."),
    "llmobs_events_rejected_total": ("counter", "Events rejected before storage, by reason."),
    "llmobs_unknown_model_total": ("counter", "Token counts for a model missing from the price list."),
    "llmobs_series_collapsed_total": ("counter", "Label sets folded into 'other' at the cardinality cap."),
    "llm_last_run_timestamp_seconds": ("gauge", "Unix time of the most recent run, by workflow and outcome."),
    "llmobs_runs_stored": ("gauge", "Runs currently inside the retention window."),
    "llmobs_db_bytes": ("gauge", "Size of the SQLite file on disk."),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  workflow    TEXT NOT NULL,
  status      TEXT NOT NULL,
  started_at  REAL NOT NULL,
  duration_ms INTEGER,
  failed_node TEXT,
  error_type  TEXT,
  error       TEXT,
  attrs       TEXT,
  received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_recent ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, started_at DESC);

CREATE TABLE IF NOT EXISTS steps (
  run_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  node        TEXT NOT NULL,
  status      TEXT NOT NULL,
  duration_ms INTEGER,
  provider    TEXT,
  model       TEXT,
  tokens_in   INTEGER,
  tokens_out  INTEGER,
  cost_usd    REAL,
  http_status INTEGER,
  error       TEXT,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS counters (
  name   TEXT NOT NULL,
  labels TEXT NOT NULL,
  value  REAL NOT NULL,
  PRIMARY KEY (name, labels)
);
"""

_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.executescript(SCHEMA)
        _db.commit()
    return _db


# --- metrics -----------------------------------------------------------------

def _label_key(labels):
    """Stable key for a label set: sorted, so {a,b} and {b,a} are one series."""
    return json.dumps(labels, sort_keys=True, separators=(",", ":"))


def bump(conn, name, labels, value=1.0):
    """Add to a counter, collapsing new label sets once the cap is reached."""
    key = _label_key(labels)
    row = conn.execute(
        "SELECT value FROM counters WHERE name = ? AND labels = ?", (name, key)
    ).fetchone()
    if row is None:
        seen = conn.execute(
            "SELECT count(*) AS n FROM counters WHERE name = ?", (name,)
        ).fetchone()["n"]
        if seen >= MAX_SERIES:
            key = _label_key({k: "other" for k in labels})
            collapsed = _label_key({"metric": name})
            conn.execute(
                "INSERT INTO counters (name, labels, value) VALUES (?, ?, 1)"
                " ON CONFLICT (name, labels) DO UPDATE SET value = value + 1",
                ("llmobs_series_collapsed_total", collapsed),
            )
    conn.execute(
        "INSERT INTO counters (name, labels, value) VALUES (?, ?, ?)"
        " ON CONFLICT (name, labels) DO UPDATE SET value = value + excluded.value",
        (name, key, value),
    )


def observe(conn, name, labels, seconds):
    """Record one histogram sample: cumulative buckets, plus sum and count."""
    if seconds is None or seconds < 0:
        return
    for le in BUCKETS:
        if seconds <= le:
            bump(conn, name + "_bucket", dict(labels, le=_fmt(le)), 1.0)
    bump(conn, name + "_bucket", dict(labels, le="+Inf"), 1.0)
    bump(conn, name + "_sum", labels, seconds)
    bump(conn, name + "_count", labels, 1.0)


def _fmt(value):
    """Prometheus number formatting, without trailing .0 noise."""
    if value is None:
        return "0"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(round(value, 6))
    return str(value)


def _escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _series(name, labels):
    if not labels:
        return name
    inner = ",".join('{0}="{1}"'.format(k, _escape(v)) for k, v in sorted(labels.items()))
    return "{0}{{{1}}}".format(name, inner)


def _bucket_order(item):
    labels = item[0]
    rest = _label_key({k: v for k, v in labels.items() if k != "le"})
    le = labels.get("le", "+Inf")
    return (rest, float("inf") if le == "+Inf" else float(le))


def render_metrics():
    """Build the exposition text from the counter table plus live gauges."""
    conn = db()
    with _lock:
        rows = conn.execute("SELECT name, labels, value FROM counters").fetchall()
        gauges = _gauges(conn)

    stored = {}
    for row in rows:
        stored.setdefault(row["name"], []).append((json.loads(row["labels"]), row["value"]))
    for name, series in gauges.items():
        stored.setdefault(name, []).extend(series)

    out = []
    for family, (kind, help_text) in METRICS.items():
        names = (
            [family + "_bucket", family + "_sum", family + "_count"]
            if kind == "histogram"
            else [family]
        )
        if not any(name in stored for name in names):
            continue
        out.append("# HELP {0} {1}".format(family, help_text))
        out.append("# TYPE {0} {1}".format(family, kind))
        for name in names:
            series = stored.get(name, [])
            if name.endswith("_bucket"):
                series = sorted(series, key=_bucket_order)
            else:
                series = sorted(series, key=lambda item: _label_key(item[0]))
            for labels, value in series:
                out.append("{0} {1}".format(_series(name, labels), _fmt(float(value))))
    return "\n".join(out) + "\n"


def _gauges(conn):
    """Gauges answer questions about now, so they are read at scrape time."""
    series = {}
    last = conn.execute(
        "SELECT workflow, status, max(started_at) AS ts FROM runs GROUP BY workflow, status"
    ).fetchall()
    series["llm_last_run_timestamp_seconds"] = [
        ({"workflow": row["workflow"], "status": row["status"]}, row["ts"]) for row in last
    ]
    total = conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"]
    series["llmobs_runs_stored"] = [({}, total)]
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = 0
    series["llmobs_db_bytes"] = [({}, size)]
    return series


# --- ingest ------------------------------------------------------------------

def clean(value, limit=120):
    """Label values come from workflow JSON; keep them short and printable."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def classify(http_status, message):
    """Best-effort error type when the workflow did not send one."""
    if http_status:
        if http_status == 429:
            return "rate_limited"
        if http_status in (503, 529):
            return "provider_overloaded"
        if http_status in (401, 403):
            return "auth"
        if 400 <= http_status < 500:
            return "bad_request"
        if http_status >= 500:
            return "provider_error"
    text = (message or "").lower()
    for needle, kind in (
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("econnrefused", "unreachable"),
        ("enotfound", "unreachable"),
        ("quota", "rate_limited"),
        ("overload", "provider_overloaded"),
        ("json", "bad_model_output"),
        ("parse", "bad_model_output"),
    ):
        if needle in text:
            return kind
    return "unknown"


def price(model, tokens_in, tokens_out):
    entry = PRICES.get(model or "")
    if not entry:
        return None
    return ((tokens_in or 0) / 1e6 * entry.get("in", 0)
            + (tokens_out or 0) / 1e6 * entry.get("out", 0))


def ingest(event):
    """Store one run and fold it into the counters. Returns (code, body)."""
    if not isinstance(event, dict):
        return 400, {"error": "each event must be an object"}
    workflow = clean(event.get("workflow"))
    if not workflow:
        return 400, {"error": "workflow is required"}
    status = str(event.get("status", "ok")).lower()
    if status not in RUN_STATUSES:
        return 400, {"error": "status must be one of " + ", ".join(sorted(RUN_STATUSES))}

    run_id = clean(event.get("run_id"), 80) or "gen-" + uuid.uuid4().hex[:12]
    try:
        started_at = float(event.get("started_at") or time.time())
    except (TypeError, ValueError):
        started_at = time.time()
    duration_ms = event.get("duration_ms")
    duration_ms = int(duration_ms) if isinstance(duration_ms, (int, float)) else None
    failed_node = clean(event.get("failed_node"))
    error = clean(event.get("error"), 500)
    steps = event.get("steps") or []
    if not isinstance(steps, list):
        return 400, {"error": "steps must be a list"}
    steps = steps[:MAX_STEPS]

    # A failed run that named no node still has one: the last step that broke.
    if status != "ok" and not failed_node:
        broken = [s for s in steps if isinstance(s, dict)
                  and str(s.get("status", "ok")).lower() != "ok"]
        if broken:
            failed_node = clean(broken[-1].get("node"))
    http_status = next(
        (s.get("http_status") for s in reversed(steps)
         if isinstance(s, dict) and s.get("http_status")), None
    )
    error_type = clean(event.get("error_type")) or (
        classify(http_status, error) if status != "ok" else None
    )

    conn = db()
    with _lock:
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO runs (run_id, workflow, status, started_at,"
                    " duration_ms, failed_node, error_type, error, attrs, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, workflow, status, started_at, duration_ms, failed_node,
                     error_type, error, json.dumps(event.get("attrs") or {})[:4000],
                     time.time()),
                )
                # n8n retries a failed telemetry POST; counting the retry would
                # quietly inflate every rate and every cost number.
                if cur.rowcount == 0:
                    bump(conn, "llmobs_events_total", {"result": "duplicate"})
                    return 200, {"run_id": run_id, "duplicate": True}

                bump(conn, "llm_runs_total", {"workflow": workflow, "status": status})
                if duration_ms is not None:
                    observe(conn, "llm_run_duration_seconds", {"workflow": workflow},
                            duration_ms / 1000.0)
                if status != "ok":
                    bump(conn, "llm_run_failures_total", {
                        "workflow": workflow,
                        "failed_node": failed_node or "unknown",
                        "error_type": error_type or "unknown",
                    })
                for seq, step in enumerate(steps):
                    _ingest_step(conn, run_id, seq, workflow, step)
                bump(conn, "llmobs_events_total", {"result": "stored"})
        except sqlite3.Error as exc:
            return 500, {"error": "storage failed: {0}".format(exc)}
    return 202, {"run_id": run_id, "steps": len(steps)}


def _ingest_step(conn, run_id, seq, workflow, step):
    if not isinstance(step, dict):
        return
    node = clean(step.get("node")) or "step-{0}".format(seq)
    status = str(step.get("status", "ok")).lower()
    status = status if status in RUN_STATUSES else "failed"
    duration_ms = step.get("duration_ms")
    duration_ms = int(duration_ms) if isinstance(duration_ms, (int, float)) else None
    model = clean(step.get("model"), 60)
    provider = clean(step.get("provider"), 40)
    tokens_in = _int(step.get("tokens_in"))
    tokens_out = _int(step.get("tokens_out"))
    http_status = step.get("http_status")
    http_status = int(http_status) if isinstance(http_status, (int, float)) else None
    cost = price(model, tokens_in, tokens_out)

    conn.execute(
        "INSERT OR REPLACE INTO steps (run_id, seq, node, status, duration_ms, provider,"
        " model, tokens_in, tokens_out, cost_usd, http_status, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, seq, node, status, duration_ms, provider, model, tokens_in,
         tokens_out, cost, http_status, clean(step.get("error"), 500)),
    )

    bump(conn, "llm_steps_total", {"workflow": workflow, "node": node, "status": status})
    if duration_ms is not None:
        observe(conn, "llm_step_duration_seconds", {"workflow": workflow, "node": node},
                duration_ms / 1000.0)
    if model:
        if tokens_in:
            bump(conn, "llm_tokens_total",
                 {"workflow": workflow, "model": model, "direction": "in"}, tokens_in)
        if tokens_out:
            bump(conn, "llm_tokens_total",
                 {"workflow": workflow, "model": model, "direction": "out"}, tokens_out)
        if cost is not None:
            bump(conn, "llm_cost_usd_total", {"workflow": workflow, "model": model}, cost)
        elif tokens_in or tokens_out:
            # A silent zero would read as a free model, not as a missing price.
            bump(conn, "llmobs_unknown_model_total", {"model": model})
    if http_status is not None:
        bump(conn, "llm_provider_responses_total",
             {"workflow": workflow, "model": model or "none", "code": str(http_status)})


def _int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def prune():
    """Drop detail rows past the retention window. Counters stay."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn = db()
    with _lock, conn:
        conn.execute(
            "DELETE FROM steps WHERE run_id IN"
            " (SELECT run_id FROM runs WHERE started_at < ?)", (cutoff,))
        conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))


# --- reads -------------------------------------------------------------------

def list_runs(workflow=None, status=None, limit=50):
    sql = "SELECT * FROM runs WHERE 1 = 1"
    args = []
    if workflow:
        sql += " AND workflow = ?"
        args.append(workflow)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with _lock:
        return [dict(row) for row in db().execute(sql, args).fetchall()]


def get_run(run_id):
    with _lock:
        conn = db()
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    run = dict(row)
    run["steps"] = [dict(step) for step in steps]
    return run


def workflows():
    with _lock:
        rows = db().execute("SELECT DISTINCT workflow FROM runs ORDER BY workflow").fetchall()
    return [row["workflow"] for row in rows]


# --- HTML --------------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1b1b1f; --muted:#5f6368;
  --line:#e3e3e8; --card:#fafafa; --ok:#1a7f4b; --fail:#b3261e; --warn:#8a6100; }
@media (prefers-color-scheme: dark) { :root { --bg:#16171a; --fg:#e8e8ea;
  --muted:#9aa0a6; --line:#2c2e33; --card:#1d1e22; --ok:#6ddc9b; --fail:#f2b8b5;
  --warn:#e8c468; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
main { max-width: 1000px; margin: 0 auto; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
p.sub { color:var(--muted); margin:0 0 20px; }
nav { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
nav a { font-size:13px; padding:4px 10px; border:1px solid var(--line);
  border-radius:999px; text-decoration:none; color:var(--fg); }
nav a.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
  letter-spacing:.04em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
a { color:inherit; }
.ok { color:var(--ok); } .failed { color:var(--fail); } .partial { color:var(--warn); }
.pill { font-size:12px; font-weight:600; }
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin-bottom:16px; }
.err { white-space:pre-wrap; word-break:break-word; color:var(--fail); }
.bar { height:6px; border-radius:3px; background:var(--muted); opacity:.45;
  min-width:2px; }
.bar.failed { background:var(--fail); opacity:1; }
.empty { color:var(--muted); padding:24px 0; }
@media (max-width:640px) { .hide-s { display:none; } }
"""


def _page(title, body):
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>{0}</title><style>{1}</style></head><body><main>{2}</main></body></html>"
    ).format(html.escape(title), CSS, body)


def _age(ts):
    delta = max(0, time.time() - (ts or 0))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if delta < limit:
            return "{0}{1} ago".format(int(delta // size), unit)
    return "{0}d ago".format(int(delta // 86400))


def _ms(value):
    if value is None:
        return "—"
    return "{0:.2f}s".format(value / 1000.0) if value >= 1000 else "{0}ms".format(int(value))


def index_html(query):
    workflow = query.get("workflow", [None])[0]
    status = query.get("status", [None])[0]
    runs = list_runs(workflow, status, query.get("limit", ["100"])[0])

    def tab(label, params, active):
        href = "/?" + "&".join(
            "{0}={1}".format(k, quote(v)) for k, v in params.items() if v)
        return "<a class=\"{0}\" href=\"{1}\">{2}</a>".format(
            "on" if active else "", href or "/", html.escape(label))

    nav = [tab("All runs", {"workflow": workflow}, not status),
           tab("Failures", {"workflow": workflow, "status": "failed"}, status == "failed")]
    for name in workflows():
        nav.append(tab(name, {"workflow": name, "status": status}, workflow == name))
    if workflow or status:
        nav.append(tab("Clear filters", {}, False))

    rows = []
    for run in runs:
        where = run["failed_node"] or ("—" if run["status"] == "ok" else "unknown")
        rows.append((
            "<tr><td class=\"mono\"><a href=\"/runs/{id}\">{short}</a></td>"
            "<td class=\"hide-s\">{wf}</td>"
            "<td><span class=\"pill {st}\">{st}</span></td>"
            "<td>{where}</td><td class=\"num\">{dur}</td>"
            "<td class=\"num hide-s\">{age}</td></tr>"
        ).format(
            id=quote(run["run_id"]), short=html.escape(run["run_id"][:24]),
            wf=html.escape(run["workflow"]), st=html.escape(run["status"]),
            where=html.escape(where), dur=_ms(run["duration_ms"]),
            age=html.escape(_age(run["started_at"])),
        ))

    table = (
        "<table><thead><tr><th>Run</th><th class=\"hide-s\">Workflow</th><th>Status</th>"
        "<th>Failed at</th><th class=\"num\">Duration</th>"
        "<th class=\"num hide-s\">When</th></tr></thead><tbody>{0}</tbody></table>"
    ).format("".join(rows)) if rows else (
        "<p class=\"empty\">No runs recorded yet in the last {0} days.</p>"
    ).format(RETENTION_DAYS)

    body = (
        "<h1>LLM run forensics</h1>"
        "<p class=\"sub\">Every workflow run of the last {0} days, newest first. "
        "Open a run to see which node broke it.</p><nav>{1}</nav>{2}"
    ).format(RETENTION_DAYS, "".join(nav), table)
    return _page("LLM run forensics", body)


def run_html(run):
    slowest = max((s["duration_ms"] or 0) for s in run["steps"]) if run["steps"] else 0
    rows = []
    for step in run["steps"]:
        width = int(100 * (step["duration_ms"] or 0) / slowest) if slowest else 0
        tokens = "—"
        if step["tokens_in"] or step["tokens_out"]:
            tokens = "{0} in / {1} out".format(step["tokens_in"] or 0, step["tokens_out"] or 0)
        cost = "${0:.5f}".format(step["cost_usd"]) if step["cost_usd"] else "—"
        rows.append((
            "<tr><td>{node}</td><td><span class=\"pill {st}\">{st}</span></td>"
            "<td class=\"num\">{dur}</td>"
            "<td style=\"width:28%\"><div class=\"bar {st}\" style=\"width:{w}%\"></div></td>"
            "<td class=\"hide-s\">{model}</td><td class=\"hide-s\">{tok}</td>"
            "<td class=\"num hide-s\">{cost}</td></tr>"
        ).format(
            node=html.escape(step["node"]), st=html.escape(step["status"]),
            dur=_ms(step["duration_ms"]), w=max(width, 2),
            model=html.escape(step["model"] or "—"), tok=html.escape(tokens), cost=cost,
        ))
        if step["error"]:
            rows.append(
                "<tr><td colspan=\"7\" class=\"err mono\">{0}</td></tr>".format(
                    html.escape(step["error"])))

    steps = (
        "<table><thead><tr><th>Node</th><th>Status</th><th class=\"num\">Took</th>"
        "<th></th><th class=\"hide-s\">Model</th><th class=\"hide-s\">Tokens</th>"
        "<th class=\"num hide-s\">Cost</th></tr></thead><tbody>{0}</tbody></table>"
    ).format("".join(rows)) if rows else (
        "<p class=\"empty\">This run reported no steps. Only the outcome was sent.</p>")

    summary = [
        "<div class=\"card\"><table><tbody>",
        _kv("Workflow", run["workflow"]),
        _kv("Status", run["status"]),
        _kv("Started", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(run["started_at"]))),
        _kv("Duration", _ms(run["duration_ms"])),
    ]
    if run["status"] != "ok":
        summary.append(_kv("Failed at", run["failed_node"] or "unknown"))
        summary.append(_kv("Error type", run["error_type"] or "unknown"))
    attrs = json.loads(run["attrs"] or "{}")
    if attrs:
        summary.append(_kv("Attributes", json.dumps(attrs, sort_keys=True)))
    summary.append("</tbody></table></div>")

    error = ""
    if run["error"]:
        error = "<div class=\"card err mono\">{0}</div>".format(html.escape(run["error"]))

    body = (
        "<h1>Run {0}</h1><p class=\"sub\"><a href=\"/\">&larr; all runs</a></p>"
        "{1}{2}<h2>Steps</h2>{3}"
    ).format(html.escape(run["run_id"]), "".join(summary), error, steps)
    return _page("Run " + run["run_id"], body)


def _kv(key, value):
    return "<tr><th>{0}</th><td class=\"mono\">{1}</td></tr>".format(
        html.escape(key), html.escape(str(value)))


# --- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "llmobs"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one line per request, docker captures it
        print("{0} {1}".format(self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _html(self, code, markup):
        self._send(code, markup, "text/html; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
        try:
            if path == "/healthz":
                return self._send(200, "ok\n", "text/plain; charset=utf-8")
            if path == "/metrics":
                return self._send(200, render_metrics(),
                                  "text/plain; version=0.0.4; charset=utf-8")
            if path == "/":
                return self._html(200, index_html(query))
            if path.startswith("/runs/"):
                run = get_run(path[len("/runs/"):])
                if run is None:
                    return self._html(404, _page("Not found", "<h1>No such run</h1>"))
                return self._html(200, run_html(run))
            if path == "/v1/runs":
                return self._json(200, {"runs": list_runs(
                    query.get("workflow", [None])[0], query.get("status", [None])[0],
                    query.get("limit", ["50"])[0])})
            if path.startswith("/v1/runs/"):
                run = get_run(path[len("/v1/runs/"):])
                return self._json(200, run) if run else self._json(404, {"error": "not found"})
        except Exception as exc:  # a broken page must not take the collector down
            self.log_message("error handling %s: %s", path, exc)
            return self._json(500, {"error": "internal error"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path.rstrip("/") != "/v1/events":
            return self._json(404, {"error": "not found"})
        # Ingest is reachable only from the docker network, but the token means a
        # mistaken proxy rule can't turn it into an open write endpoint.
        sent = self.headers.get("X-Telemetry-Token", "")
        if not TOKEN or not hmac.compare_digest(sent, TOKEN):
            self._reject("bad_token")
            return self._json(401, {"error": "missing or wrong X-Telemetry-Token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._reject("empty_body")
            return self._json(400, {"error": "empty body"})
        if length > MAX_BODY:
            self._reject("too_large")
            return self._json(413, {"error": "body over 256 KB"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reject("bad_json")
            return self._json(400, {"error": "body is not valid JSON"})

        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            events = [payload]
        if len(events) > 100:
            self._reject("too_many_events")
            return self._json(413, {"error": "at most 100 events per request"})

        results, worst = [], 202
        for event in events:
            code, body = ingest(event)
            if code >= 400:
                self._reject("invalid_event")
                worst = max(worst, code)
            results.append(body)
        return self._json(worst, results[0] if len(results) == 1 else {"results": results})

    def _reject(self, reason):
        conn = db()
        with _lock, conn:
            bump(conn, "llmobs_events_rejected_total", {"reason": reason})


def _prune_loop(stop):
    """Prune at start, then once a day, until the process is asked to stop."""
    while True:
        try:
            prune()
        except sqlite3.Error as exc:
            print("prune failed: {0}".format(exc), flush=True)
        if stop.wait(86400):
            return


def main():
    if not TOKEN:
        raise SystemExit("LLMOBS_TOKEN is not set; refusing to accept unauthenticated writes")
    db()
    stop = threading.Event()
    threading.Thread(target=_prune_loop, args=(stop,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    def shutdown(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("llmobs listening on :{0}, retention {1}d".format(PORT, RETENTION_DAYS), flush=True)
    server.serve_forever()
    server.server_close()
    with _lock:
        db().close()


if __name__ == "__main__":
    main()
