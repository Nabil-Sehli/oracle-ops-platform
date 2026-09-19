#!/usr/bin/env python3
"""HTTP-level tests: routing, the ingest token, and what Prometheus will see."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from test_collector import close, load_collector

EVENT = {
    "run_id": "exec-http-1",
    "workflow": "ai-lead-qualification",
    "status": "failed",
    "duration_ms": 3100,
    "error": "Gemini returned 503",
    "steps": [
        {"node": "Validate Lead", "status": "ok", "duration_ms": 8},
        {"node": "Gemini: Score Lead", "status": "failed", "duration_ms": 2900,
         "model": "gemini-3.5-flash-lite", "http_status": 503, "tokens_in": 800},
    ],
}


def call(url, data=None, token=None):
    request = urllib.request.Request(url, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(data).encode()
    if token:
        request.add_header("X-Telemetry-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.collector = load_collector(os.path.join(cls.dir.name, "t.db"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.collector.Handler)
        cls.server.daemon_threads = True
        cls.base = "http://127.0.0.1:{0}".format(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        close(cls.collector)
        cls.dir.cleanup()

    def test_healthz(self):
        self.assertEqual(call(self.base + "/healthz"), (200, "ok\n"))

    def test_ingest_needs_the_token(self):
        code, _ = call(self.base + "/v1/events", EVENT)
        self.assertEqual(code, 401)
        code, _ = call(self.base + "/v1/events", EVENT, token="wrong")
        self.assertEqual(code, 401)

    def test_ingest_stores_and_shows_up_everywhere(self):
        code, body = call(self.base + "/v1/events", EVENT, token="test-token")
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)["steps"], 2)

        code, body = call(self.base + "/v1/runs/exec-http-1")
        self.assertEqual(code, 200)
        run = json.loads(body)
        self.assertEqual(run["failed_node"], "Gemini: Score Lead")
        self.assertEqual(run["error_type"], "provider_overloaded")

        code, body = call(self.base + "/metrics")
        self.assertEqual(code, 200)
        self.assertIn('llm_run_failures_total{error_type="provider_overloaded"', body)
        self.assertIn("# TYPE llm_step_duration_seconds histogram", body)

        code, body = call(self.base + "/runs/exec-http-1")
        self.assertEqual(code, 200)
        self.assertIn("Gemini: Score Lead", body)

    def test_batch_of_events(self):
        code, body = call(self.base + "/v1/events", {"events": [
            dict(EVENT, run_id="batch-1"), dict(EVENT, run_id="batch-2")]},
            token="test-token")
        self.assertEqual(code, 202)
        self.assertEqual(len(json.loads(body)["results"]), 2)

    def test_bad_json_is_rejected(self):
        request = urllib.request.Request(self.base + "/v1/events", method="POST")
        request.add_header("X-Telemetry-Token", "test-token")
        request.data = b"{not json"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_unknown_path(self):
        code, _ = call(self.base + "/nope")
        self.assertEqual(code, 404)
        code, _ = call(self.base + "/runs/does-not-exist")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
