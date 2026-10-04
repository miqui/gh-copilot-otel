"""Dashboard structure and pinned local Prometheus translation, never GCP."""

import copy
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from test_collector import ENV, SECRET, attributes


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch" / "task-3"
DASHBOARD = ROOT / "dashboards" / "copilot-usage.json"
TOKEN_METRICS = (
    "gen_ai_client_inference_usage_input_tokens_total",
    "gen_ai_client_inference_usage_output_tokens_total",
    "gen_ai_client_inference_usage_cache_read_input_tokens_total",
    "gen_ai_client_inference_usage_cache_write_input_tokens_total",
)
MODEL_BUCKET = "gen_ai_client_operation_duration_seconds_bucket"
TOOL_COUNTER = "github_copilot_tool_call_count_total"
TOOL_BUCKET = "github_copilot_tool_call_duration_seconds_bucket"


def synthetic_metrics():
    labels = attributes({
        "gen_ai.request.model": "gpt-5", "gen_ai.operation.name": "chat",
        "gen_ai.tool.name": "bash", "session.id": SECRET, "prompt": SECRET,
    })
    now = time.time_ns()
    point = {"attributes": labels, "startTimeUnixNano": str(now - 10_000_000_000),
             "timeUnixNano": str(now)}
    metrics = []
    for suffix in ("input_tokens", "output_tokens", "cache_read.input_tokens",
                   "cache_write.input_tokens"):
        metrics.append({
            "name": "gen_ai.client.inference.usage." + suffix, "unit": SECRET,
            "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                    "dataPoints": [{**point, "asInt": "123"}]},
        })
    metrics.append({
        "name": "github.copilot.tool.call.count", "unit": SECRET,
        "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [{**point, "asInt": "7"}]},
    })
    for name in ("gen_ai.client.operation.duration", "github.copilot.tool.call.duration"):
        metrics.append({
            "name": name, "unit": SECRET,
            "histogram": {"aggregationTemporality": 2, "dataPoints": [
                {**point, "count": "3", "sum": 1.5, "explicitBounds": [0.25, 1],
                 "bucketCounts": ["1", "2", "0"]}]},
        })
    metrics.append({"name": SECRET, "gauge": {
        "dataPoints": [{**point, "asInt": "999"}]}})
    return {"resourceMetrics": [{
        "resource": {"attributes": attributes({"service.name": SECRET,
                                               "user.name": SECRET})},
        "scopeMetrics": [{"scope": {"name": SECRET}, "metrics": metrics}],
    }]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DashboardTests(unittest.TestCase):
    def dashboard(self):
        self.assertTrue(DASHBOARD.is_file(), "Missing Task 3 dashboard")
        return json.loads(DASHBOARD.read_text())

    def charts(self):
        return [tile["widget"]["xyChart"]
                for tile in self.dashboard()["mosaicLayout"]["tiles"]]

    def queries(self):
        return [dataset["timeSeriesQuery"]["prometheusQuery"]
                for chart in self.charts() for dataset in chart["dataSets"]]

    def test_importable_chart_structures_and_nonoverlapping_layout(self):
        dashboard = self.dashboard()
        self.assertTrue(dashboard["displayName"])
        self.assertNotIn("name", dashboard, "Creation must not target an existing ID")
        self.assertNotIn("etag", dashboard, "A reusable dashboard has no stale etag")
        layout = dashboard["mosaicLayout"]
        self.assertEqual(layout["columns"], 12)
        self.assertEqual(len(layout["tiles"]), 7)
        occupied = set()
        for tile in layout["tiles"]:
            self.assertGreater(tile["width"], 0)
            self.assertGreater(tile["height"], 0)
            self.assertGreaterEqual(tile["xPos"], 0)
            self.assertGreaterEqual(tile["yPos"], 0)
            self.assertLessEqual(tile["xPos"] + tile["width"], 12)
            for x in range(tile["xPos"], tile["xPos"] + tile["width"]):
                for y in range(tile["yPos"], tile["yPos"] + tile["height"]):
                    self.assertNotIn((x, y), occupied, "Overlapping dashboard tiles")
                    occupied.add((x, y))
            widget = tile["widget"]
            self.assertTrue(widget["title"])
            chart = widget["xyChart"]
            self.assertEqual(chart["yAxis"]["scale"], "LINEAR")
            self.assertTrue(chart["yAxis"]["label"])
            self.assertTrue(chart["dataSets"])
            for dataset in chart["dataSets"]:
                self.assertEqual(dataset["plotType"], "LINE")
                self.assertEqual(dataset["targetAxis"], "Y1")
                query = dataset["timeSeriesQuery"]
                self.assertEqual(query["outputFullDuration"], False)
                self.assertTrue(query["unitOverride"])
                self.assertEqual(set(query), {
                    "prometheusQuery", "unitOverride", "outputFullDuration"})

    def test_token_rates_use_actual_counter_names_and_bounded_grouping(self):
        queries = self.queries()
        for name in TOKEN_METRICS:
            self.assertIn(
                "sum by (gen_ai_request_model, gen_ai_operation_name) "
                f'(rate({name}{{job="github-copilot"}}[5m]))', queries)

    def test_model_and_tool_percentiles_keep_le_until_histogram_quantile(self):
        queries = self.queries()
        for quantile in ("0.50", "0.95", "0.99"):
            self.assertIn(
                f"histogram_quantile({quantile}, sum by "
                "(le, gen_ai_request_model, gen_ai_operation_name) "
                f'(rate({MODEL_BUCKET}{{job="github-copilot"}}[5m])))', queries)
            self.assertIn(
                f"histogram_quantile({quantile}, sum by (le, gen_ai_tool_name) "
                f'(rate({TOOL_BUCKET}{{job="github-copilot"}}[5m])))', queries)
        self.assertIn(
            "sum by (gen_ai_tool_name) "
            f'(rate({TOOL_COUNTER}{{job="github-copilot"}}[5m]))', queries)
        self.assertEqual(len(queries), 11)

    def test_all_query_groupings_are_privacy_bounded_and_scope_to_copilot(self):
        for query in self.queries():
            groups = re.findall(r"sum by \(([^)]+)\)", query)
            self.assertEqual(len(groups), 1)
            self.assertLessEqual(set(groups[0].split(", ")), {
                "le", "gen_ai_request_model", "gen_ai_operation_name", "gen_ai_tool_name"})
            self.assertIn('{job="github-copilot"}[5m]', query)
            self.assertNotIn("workload.googleapis.com", query)
            self.assertNotRegex(query, r"session|trace_id|repository|user|prompt")

    def test_dashboard_names_match_observed_pinned_collector_prometheus_output(self):
        binary = os.environ.get("OTELCOL_BIN")
        if not binary:
            self.skipTest("Set OTELCOL_BIN to pinned v0.162.0 for translation evidence")
        version = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=15)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertRegex(version.stdout, r"\b0\.162\.0\b")
        import yaml
        config = copy.deepcopy(yaml.safe_load((ROOT / "otel/collector.yaml").read_text()))
        receiver, health, prometheus = free_port(), free_port(), free_port()
        config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = (
            f"127.0.0.1:{receiver}")
        config["extensions"] = {"health_check": {"endpoint": f"127.0.0.1:{health}"}}
        config["service"]["extensions"] = ["health_check"]
        config["service"]["pipelines"].pop("traces")
        # Default translation on the pinned release, not a hand-written translator.
        config["exporters"] = {"prometheus": {"endpoint": f"127.0.0.1:{prometheus}"}}
        config["service"]["pipelines"]["metrics"]["exporters"] = ["prometheus"]
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="translation ", dir=SCRATCH) as work:
            local = Path(work) / "collector.yaml"
            local.write_text(yaml.safe_dump(config))
            with (Path(work) / "collector.log").open("w+") as log:
                process = subprocess.Popen([binary, "--config", str(local)],
                                           env=ENV, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "Local Collector exited early")
                        try:
                            with urllib.request.urlopen(
                                f"http://127.0.0.1:{health}", timeout=0.5) as response:
                                if response.status == 200:
                                    break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        self.fail("Local Collector readiness timeout")
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{receiver}/v1/metrics",
                        data=json.dumps(synthetic_metrics()).encode(),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{prometheus}/metrics", timeout=2) as response:
                            output = response.read().decode()
                        if TOOL_COUNTER in output:
                            break
                        time.sleep(0.2)
                    else:
                        self.fail("Synthetic metrics not exposed by Prometheus exporter")
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                log.seek(0)
                self.assertNotIn("\terror\t", log.read().lower())
        # Persist content-free evidence for the Task 3 report, not generated telemetry in git.
        (SCRATCH / "prometheus-observed.txt").write_text(output)
        self.assertNotIn(SECRET, output)
        sample_names = set(re.findall(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)[{ ]",
                                      output, re.MULTILINE))
        for name in (*TOKEN_METRICS, MODEL_BUCKET, TOOL_COUNTER, TOOL_BUCKET):
            self.assertIn(name, sample_names, output)
        self.assertIn('job="github-copilot"', output)
        self.assertIn('gen_ai_request_model="gpt-5"', output)
        self.assertIn('gen_ai_tool_name="bash"', output)
        self.assertRegex(output, rf'{MODEL_BUCKET}\{{[^}}]*le="1"[^}}]*\}} 3')
        queried_names = set(re.findall(r"rate\(([a-zA-Z_:][a-zA-Z0-9_:]*)\{",
                                      "\n".join(self.queries())))
        self.assertEqual(queried_names, set((*TOKEN_METRICS, MODEL_BUCKET,
                                           TOOL_COUNTER, TOOL_BUCKET)))
        self.assertLessEqual(queried_names, sample_names)


if __name__ == "__main__":
    unittest.main()
