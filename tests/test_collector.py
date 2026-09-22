#!/usr/bin/env python3
"""Tests for the llmobs collector. Standard library only, like the collector.

    python -m unittest discover -s tests

Every test gets its own SQLite file in a temporary directory, so counters and
retention can be checked without touching a real database.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

COLLECTOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible", "roles", "llmobs", "files", "collector.py",
)

PRICES = {
    "gemini-3.5-flash-lite": {"in": 0.10, "out": 0.40},
    "claude-sonnet-5": {"in": 3.0, "out": 15.0},
}


def load_collector(db_path, **env):
    """Import collector.py fresh, with its environment set for this test."""
    os.environ["LLMOBS_DB"] = db_path
    os.environ["LLMOBS_TOKEN"] = "test-token"
    os.environ["LLMOBS_PRICES"] = json.dumps(PRICES)
    os.environ["LLMOBS_RETENTION_DAYS"] = "14"
    os.environ.pop("LLMOBS_MAX_SERIES", None)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("collector_under_test", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def close(module):
    """Windows will not delete an open SQLite file, so tests close theirs."""
    if module._db is not None:
        module._db.close()
        module._db = None


def series(text, name):
    """Pull {series line: value} for one metric name out of exposition text."""
    found = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, value = line.rpartition(" ")
        if key == name or key.startswith(name + "{"):
            found[key] = float(value)
    return found


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.c = load_collector(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(close, self.c)

    def run_event(self, **over):
        event = {
            "run_id": "exec-1",
            "workflow": "ai-lead-qualification",
            "status": "ok",
            "duration_ms": 2400,
            "steps": [{
                "node": "Gemini: Score Lead", "status": "ok", "duration_ms": 1800,
                "provider": "gemini", "model": "gemini-3.5-flash-lite",
                "tokens_in": 1_000_000, "tokens_out": 1_000_000, "http_status": 200,
            }],
        }
        event.update(over)
        return self.c.ingest(event)

    # --- ingest ---

    def test_stores_run_and_steps(self):
        code, body = self.run_event()
        self.assertEqual(code, 202)
        self.assertEqual(body["steps"], 1)
        run = self.c.get_run("exec-1")
        self.assertEqual(run["workflow"], "ai-lead-qualification")
        self.assertEqual(run["steps"][0]["node"], "Gemini: Score Lead")

    def test_workflow_is_required(self):
        code, body = self.c.ingest({"status": "ok"})
        self.assertEqual(code, 400)
        self.assertIn("workflow", body["error"])

    def test_rejects_unknown_status(self):
        code, _ = self.c.ingest({"workflow": "w", "status": "maybe"})
        self.assertEqual(code, 400)

    def test_retry_of_the_same_run_is_not_counted_twice(self):
        self.run_event()
        code, body = self.run_event()
        self.assertEqual(code, 200)
        self.assertTrue(body["duplicate"])
        counts = series(self.c.render_metrics(), "llm_runs_total")
        self.assertEqual(sum(counts.values()), 1)

    def test_missing_run_id_gets_one(self):
        code, body = self.c.ingest({"workflow": "w", "status": "ok"})
        self.assertEqual(code, 202)
        self.assertTrue(body["run_id"].startswith("gen-"))

    def test_garbage_steps_do_not_break_the_run(self):
        code, _ = self.c.ingest({
            "run_id": "exec-garbage", "workflow": "w", "status": "ok",
            "steps": ["not an object", {"node": "Real", "status": "ok"}],
        })
        self.assertEqual(code, 202)
        self.assertEqual(len(self.c.get_run("exec-garbage")["steps"]), 1)

    def test_steps_are_capped(self):
        self.c.ingest({
            "run_id": "exec-many", "workflow": "w", "status": "ok",
            "steps": [{"node": "n%d" % i, "status": "ok"} for i in range(200)],
        })
        self.assertEqual(len(self.c.get_run("exec-many")["steps"]), self.c.MAX_STEPS)

    # --- failure forensics ---

    def test_failed_node_is_inferred_from_the_broken_step(self):
        self.run_event(run_id="exec-2", status="failed", steps=[
            {"node": "Validate Lead", "status": "ok", "duration_ms": 12},
            {"node": "Gemini: Score Lead", "status": "failed", "http_status": 503,
             "error": "model is overloaded"},
        ])
        run = self.c.get_run("exec-2")
        self.assertEqual(run["failed_node"], "Gemini: Score Lead")
        self.assertEqual(run["error_type"], "provider_overloaded")

    def test_explicit_error_type_wins(self):
        self.run_event(run_id="exec-3", status="failed", error_type="sheet_quota",
                       failed_node="Log Lead to Sheet", steps=[])
        self.assertEqual(self.c.get_run("exec-3")["error_type"], "sheet_quota")

    def test_classify_reads_status_codes_and_messages(self):
        self.assertEqual(self.c.classify(429, ""), "rate_limited")
        self.assertEqual(self.c.classify(503, ""), "provider_overloaded")
        self.assertEqual(self.c.classify(401, ""), "auth")
        self.assertEqual(self.c.classify(None, "Socket timed out"), "timeout")
        self.assertEqual(self.c.classify(None, "Unexpected token in JSON"), "bad_model_output")
        self.assertEqual(self.c.classify(None, "something else"), "unknown")

    def test_classify_finds_the_status_inside_n8n_error_text(self):
        # Run n8n-25: the HTTP node reports the code only in its message.
        message = ('Auto-scoring failed: API error (503 - "{ "error": { "code": 503, '
                   '"message": "This model is currently experiencing high demand.')
        self.assertEqual(self.c.classify(None, message), "provider_overloaded")
        self.assertEqual(self.c.classify(None, "API error (429 - quota)"), "rate_limited")
        self.assertEqual(self.c.classify(None, "high demand, try later"), "provider_overloaded")
        self.assertEqual(self.c.classify(None, "timeout of 120000ms exceeded"), "timeout")

    def test_failure_counter_carries_the_node(self):
        self.run_event(run_id="exec-4", status="failed", steps=[
            {"node": "Gemini: Score Lead", "status": "failed", "http_status": 503},
        ])
        text = self.c.render_metrics()
        self.assertIn('failed_node="Gemini: Score Lead"',
                      "".join(series(text, "llm_run_failures_total")))

    # --- cost ---

    def test_cost_is_priced_per_million_tokens(self):
        self.run_event()
        costs = series(self.c.render_metrics(), "llm_cost_usd_total")
        self.assertAlmostEqual(sum(costs.values()), 0.50, places=6)

    def test_unknown_model_is_flagged_not_silently_free(self):
        self.run_event(run_id="exec-5", steps=[{
            "node": "Score", "status": "ok", "model": "gemini-9-ultra",
            "tokens_in": 100, "tokens_out": 10,
        }])
        text = self.c.render_metrics()
        self.assertTrue(series(text, "llmobs_unknown_model_total"))
        self.assertFalse(series(text, "llm_cost_usd_total"))

    def test_tokens_are_counted_per_direction(self):
        self.run_event()
        tokens = series(self.c.render_metrics(), "llm_tokens_total")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(sum(tokens.values()), 2_000_000)

    # --- metrics format ---

    def test_histogram_buckets_are_cumulative_and_ordered(self):
        self.run_event()
        text = self.c.render_metrics()
        buckets = series(text, "llm_run_duration_seconds_bucket")
        # 2.4s: below the 5s bucket, above the 2s one.
        self.assertEqual(buckets['llm_run_duration_seconds_bucket{le="5",workflow="ai-lead-qualification"}'], 1)
        self.assertNotIn('llm_run_duration_seconds_bucket{le="2",workflow="ai-lead-qualification"}', buckets)
        self.assertEqual(buckets['llm_run_duration_seconds_bucket{le="+Inf",workflow="ai-lead-qualification"}'], 1)
        lines = [line for line in text.splitlines()
                 if line.startswith("llm_run_duration_seconds_bucket")]
        self.assertTrue(lines[-1].startswith('llm_run_duration_seconds_bucket{le="+Inf"'))
        self.assertIn("# TYPE llm_run_duration_seconds histogram", text)

    def test_sum_and_count_accompany_the_buckets(self):
        self.run_event()
        text = self.c.render_metrics()
        self.assertAlmostEqual(sum(series(text, "llm_run_duration_seconds_sum").values()), 2.4)
        self.assertEqual(sum(series(text, "llm_run_duration_seconds_count").values()), 1)

    def test_label_values_are_escaped(self):
        self.run_event(run_id="exec-6", status="failed",
                       failed_node='Node "quoted" \\ odd', steps=[])
        text = self.c.render_metrics()
        self.assertIn('failed_node="Node \\"quoted\\" \\\\ odd"', text)
        # Every line still parses as `series value`.
        for line in text.splitlines():
            if not line.startswith("#"):
                self.assertRegex(line, r"^[a-zA-Z_][a-zA-Z0-9_]*(\{.*\})? -?[0-9.e+]+$")

    def test_gauges_report_the_latest_run(self):
        self.run_event()
        gauges = series(self.c.render_metrics(), "llm_last_run_timestamp_seconds")
        self.assertEqual(len(gauges), 1)
        self.assertGreater(list(gauges.values())[0], time.time() - 60)

    def test_empty_database_renders_nothing_but_gauges(self):
        text = self.c.render_metrics()
        self.assertIn("llmobs_runs_stored 0", text)
        self.assertNotIn("llm_runs_total{", text)

    # --- first sample of a new series ---

    def test_prometheus_sees_a_new_series_at_zero_first(self):
        # increase() only counts growth between samples: a series born at 1
        # would never count its first event.
        self.run_event(run_id="exec-7", status="partial", steps=[])
        key = 'llm_runs_total{status="partial",workflow="ai-lead-qualification"}'
        self.assertEqual(series(self.c.render_metrics(scrape=True), "llm_runs_total")[key], 0)
        self.assertEqual(series(self.c.render_metrics(scrape=True), "llm_runs_total")[key], 1)

    def test_events_before_the_first_scrape_all_arrive_on_the_second(self):
        self.run_event(run_id="a", status="partial", steps=[])
        self.run_event(run_id="b", status="partial", steps=[])
        self.c.render_metrics(scrape=True)
        self.run_event(run_id="c", status="partial", steps=[])
        counts = series(self.c.render_metrics(scrape=True), "llm_runs_total")
        self.assertEqual(sum(counts.values()), 3)

    def test_a_known_series_grows_at_once(self):
        self.run_event(run_id="a", steps=[])
        self.c.render_metrics(scrape=True)
        self.c.render_metrics(scrape=True)
        self.run_event(run_id="b", steps=[])
        counts = series(self.c.render_metrics(scrape=True), "llm_runs_total")
        self.assertEqual(sum(counts.values()), 2)

    def test_reading_by_hand_shows_totals_and_releases_nothing(self):
        self.run_event(run_id="a", status="partial", steps=[])
        self.assertEqual(sum(series(self.c.render_metrics(), "llm_runs_total").values()), 1)
        self.assertEqual(
            sum(series(self.c.render_metrics(scrape=True), "llm_runs_total").values()), 0)

    def test_new_histogram_series_stay_cumulative(self):
        self.run_event(run_id="a", steps=[])
        self.c.render_metrics(scrape=True)
        self.run_event(run_id="b", duration_ms=400, steps=[])  # opens the 0.5s bucket
        buckets = series(self.c.render_metrics(scrape=True), "llm_run_duration_seconds_bucket")
        ordered = [value for key, value in buckets.items()]
        self.assertEqual(ordered, sorted(ordered))

    # --- retention ---

    def test_prune_drops_old_runs_but_keeps_counters(self):
        self.run_event()
        self.run_event(run_id="old", started_at=time.time() - 30 * 86400)
        self.assertEqual(len(self.c.list_runs()), 2)
        self.c.prune()
        self.assertEqual(len(self.c.list_runs()), 1)
        self.assertIsNone(self.c.get_run("old"))
        counts = series(self.c.render_metrics(), "llm_runs_total")
        self.assertEqual(sum(counts.values()), 2, "counters must not go backwards")

    def test_prune_removes_the_steps_too(self):
        self.run_event(run_id="old", started_at=time.time() - 30 * 86400)
        self.c.prune()
        with self.c._lock:
            left = self.c.db().execute(
                "SELECT count(*) AS n FROM steps WHERE run_id = 'old'").fetchone()["n"]
        self.assertEqual(left, 0)

    # --- reads ---

    def test_list_filters_and_orders(self):
        self.run_event(run_id="a", started_at=time.time() - 100)
        self.run_event(run_id="b", status="failed", steps=[])
        self.run_event(run_id="c", workflow="missed-call-followup")
        self.assertEqual([r["run_id"] for r in self.c.list_runs(limit=10)][0], "c")
        self.assertEqual([r["run_id"] for r in self.c.list_runs(status="failed")], ["b"])
        self.assertEqual(
            [r["run_id"] for r in self.c.list_runs(workflow="missed-call-followup")], ["c"])
        self.assertEqual(self.c.workflows(), ["ai-lead-qualification", "missed-call-followup"])

    def test_pages_render(self):
        self.run_event(run_id="exec-7", status="failed", error="boom <script>",
                       attrs={"tier": "hot"}, steps=[
                           {"node": "Validate", "status": "ok", "duration_ms": 10},
                           {"node": "Score", "status": "failed", "error": "503"}])
        index = self.c.index_html({})
        self.assertIn("exec-7", index)
        self.assertIn("LLM run forensics", index)
        page = self.c.run_html(self.c.get_run("exec-7"))
        self.assertIn("Score", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)


class UpgradeTest(unittest.TestCase):
    def test_a_database_from_before_pending_keeps_its_counters(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "t.db")
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE counters (name TEXT NOT NULL, labels TEXT NOT NULL,"
                    " value REAL NOT NULL, PRIMARY KEY (name, labels))")
        old.execute("INSERT INTO counters VALUES ('llm_runs_total',"
                    " '{\"status\":\"ok\",\"workflow\":\"w\"}', 4)")
        old.commit()
        old.close()
        c = load_collector(path)
        self.addCleanup(close, c)
        c.ingest({"run_id": "r", "workflow": "w", "status": "ok"})
        counts = series(c.render_metrics(scrape=True), "llm_runs_total")
        self.assertEqual(counts['llm_runs_total{status="ok",workflow="w"}'], 5)


class CardinalityTest(unittest.TestCase):
    def test_new_label_sets_collapse_at_the_cap(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        c = load_collector(os.path.join(directory.name, "t.db"), LLMOBS_MAX_SERIES="5")
        self.addCleanup(close, c)
        for i in range(20):
            c.ingest({"run_id": "r%d" % i, "workflow": "w", "status": "failed",
                      "failed_node": "node-%d" % i})
        text = c.render_metrics()
        self.assertLessEqual(len(series(text, "llm_run_failures_total")), 6)
        self.assertIn('failed_node="other"', text)
        self.assertTrue(series(text, "llmobs_series_collapsed_total"))


if __name__ == "__main__":
    unittest.main()
