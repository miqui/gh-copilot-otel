"""Native correctness regressions for privacy-normalized cumulative streams.

These tests intentionally fail while the normalization collision is unresolved.
Run with OTELCOL_BIN pointing at contrib v0.162.0; no cloud or ADC is used.
"""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from test_collector import ENV, SECRET, attributes
from test_dashboard import ROOT, TOKEN_METRICS, free_port


COUNTER = TOKEN_METRICS[0]
COUNT = "gen_ai_client_operation_duration_seconds_count"
SUM = "gen_ai_client_operation_duration_seconds_sum"
BUCKET = "gen_ai_client_operation_duration_seconds_bucket"
INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"


def payload(streams, start=1700000000, end=1700000010):
    """(process instance, raw model, counter value, histogram bucket counts)."""
    resources = []
    for instance, model, value, buckets in streams:
        point = {
            "attributes": attributes({"gen_ai.request.model": model,
                                      "gen_ai.operation.name": "chat"}),
            "startTimeUnixNano": str(start * 1_000_000_000),
            "timeUnixNano": str(end * 1_000_000_000),
        }
        resources.append({
            "resource": {"attributes": attributes({
                "service.instance.id": instance,
                "user.name": SECRET, "repository.path": SECRET})},
            "scopeMetrics": [{
                "scope": {"name": "github.copilot", "version": "1"},
                "metrics": [
                    {"name": "gen_ai.client.inference.usage.input_tokens",
                     "unit": "{token}",
                     "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                             "dataPoints": [{**point, "asInt": str(value)}]}},
                    {"name": "gen_ai.client.operation.duration", "unit": "s",
                     "histogram": {"aggregationTemporality": 2, "dataPoints": [
                         {**point, "count": str(sum(buckets)),
                          "sum": buckets[0] * 0.5 + buckets[1],
                          "explicitBounds": [0.75],
                          "bucketCounts": [str(n) for n in buckets]}]}},
                ],
            }],
        })
    return {"resourceMetrics": resources}


class CollisionTests(unittest.TestCase):
    def observe(self, payloads, mutate=None):
        binary = os.environ.get("OTELCOL_BIN")
        if not binary:
            self.skipTest("Set OTELCOL_BIN to pinned contrib v0.162.0")
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
        config["exporters"] = {"prometheus": {"endpoint": f"127.0.0.1:{prometheus}"}}
        pipeline = config["service"]["pipelines"]["metrics"]
        pipeline["exporters"] = ["prometheus"]
        if mutate:
            mutate(config)
        scratch = ROOT / ".scratch" / "final-fix"
        scratch.mkdir(parents=True, exist_ok=True)
        outputs = []
        with tempfile.TemporaryDirectory(prefix="collision ", dir=scratch) as work:
            local = Path(work) / "collector.yaml"
            local.write_text(yaml.safe_dump(config))
            with (Path(work) / "collector.log").open("w+") as log:
                process = subprocess.Popen([binary, "--config", str(local)],
                                           env=ENV, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            with urllib.request.urlopen(
                                f"http://127.0.0.1:{health}", timeout=0.5):
                                break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        self.fail("Collector readiness timeout")
                    for body in payloads:
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{receiver}/v1/metrics",
                            data=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=10) as response:
                            self.assertEqual(response.status, 200)
                        # An observation crosses a complete production batch interval,
                        # including when the new point has been dropped upstream.
                        time.sleep(5.5)
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{prometheus}/metrics", timeout=2) as response:
                            output = response.read().decode()
                        self.assertNotIn(SECRET, output)
                        outputs.append(output)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                log.seek(0)
                logs = log.read()
                self.assertNotIn("\terror\t", logs.lower(), logs)
        evidence = "\n\n".join(
            f"OBSERVATION {i + 1}\n{output}" for i, output in enumerate(outputs))
        (scratch / f"{self._testMethodName}.txt").write_text(evidence)
        return outputs

    def total(self, output, name, extra=""):
        samples = re.findall(
            rf"^{re.escape(name)}\{{([^}}]*)\}} ([0-9.eE+-]+)",
            output, re.MULTILINE)
        return sum(float(value) for labels, value in samples if extra in labels)

    def assert_totals(self, output, counter, buckets):
        for name, expected, extra in [
            (COUNTER, counter, ""),
            (COUNT, sum(buckets), ""),
            (SUM, buckets[0] * 0.5 + buckets[1], ""),
            (BUCKET, buckets[0], 'le="0.75"'),
            (BUCKET, sum(buckets), 'le="+Inf"'),
        ]:
            with self.subTest(metric=name, selector=extra):
                self.assertEqual(self.total(output, name, extra), expected, output)

    def test_unknown_dimensions_keep_counts_histograms(self):
        first = payload([(INSTANCE_A, "unknown-a", 100, [2, 1]),
                         (INSTANCE_A, "unknown-b", 40, [1, 3])])
        # Two distinct datapoints in one metric, as in the review reproduction.
        scopes = [r["scopeMetrics"][0] for r in first["resourceMetrics"]]
        for left, right in zip(scopes[0]["metrics"], scopes[1]["metrics"]):
            field = "sum" if "sum" in left else "histogram"
            left[field]["dataPoints"].extend(right[field]["dataPoints"])
        first["resourceMetrics"] = first["resourceMetrics"][:1]
        output = self.observe([first])[0]
        self.assertIn('gen_ai_request_model="other"', output)
        self.assert_totals(output, 140, [3, 4])

    def test_multiple_producers_repeated_intervals_duplicate_and_reset(self):
        initial = payload([(INSTANCE_A, "gpt-5", 100, [2, 1]),
                           (INSTANCE_B, "gpt-5", 40, [1, 3])])
        update = payload([(INSTANCE_A, "gpt-5", 120, [3, 1]),
                          (INSTANCE_B, "gpt-5", 50, [1, 4])], end=1700000020)
        reset = payload([(INSTANCE_A, "gpt-5", 7, [1, 1])],
                        start=1700000020, end=1700000030)
        outputs = self.observe([initial, update, update, reset])
        for output, counter, buckets in zip(
            outputs, [140, 170, 170, 177], [[3, 4], [4, 5], [4, 5], [5, 6]]):
            self.assert_totals(output, counter, buckets)

    def test_cumulative_delta_cumulative_reclock_delayed_duplicate(self):
        """Candidate fails: old duplicate rewinds C2D's baseline, then recounts."""
        def candidate(config):
            config["processors"]["cumulative_to_delta"] = {
                "initial_value": "keep", "max_staleness": "1h"}
            config["processors"]["transform/reclock"] = {
                "error_mode": "propagate",
                "metric_statements": [{"context": "datapoint", "statements": [
                    "set(datapoint.time, Now())",
                    "set(datapoint.start_time, datapoint.time)"]}]}
            config["processors"]["delta_to_cumulative"] = {
                "max_stale": "1h", "max_streams": 4096}
            config["service"]["pipelines"]["metrics"]["processors"] = [
                "memory_limiter", "filter/metrics", "cumulative_to_delta",
                "transform/privacy", "transform/reclock", "delta_to_cumulative", "batch"]

        initial = payload([(INSTANCE_A, "unknown-a", 100, [2, 1])])
        update = payload([(INSTANCE_A, "unknown-a", 120, [3, 1])], end=1700000020)
        outputs = self.observe([initial, update, initial, update], candidate)
        for output in outputs[1:]:
            self.assert_totals(output, 120, [3, 1])

    def test_cumulative_delta_cumulative_same_interval_collision(self):
        """No reclocking: D2C rejects the second normalized point at the same time."""
        def candidate(config):
            config["processors"]["cumulative_to_delta"] = {
                "initial_value": "keep", "max_staleness": "1h"}
            config["processors"]["delta_to_cumulative"] = {
                "max_stale": "1h", "max_streams": 4096}
            config["service"]["pipelines"]["metrics"]["processors"] = [
                "memory_limiter", "filter/metrics", "cumulative_to_delta",
                "transform/privacy", "delta_to_cumulative", "batch"]

        output = self.observe([payload([
            (INSTANCE_A, "unknown-a", 100, [2, 1]),
            (INSTANCE_A, "unknown-b", 40, [1, 3])])], candidate)[0]
        self.assert_totals(output, 140, [3, 4])

    def test_stateless_aggregation_independent_source_reset(self):
        """Batch-local summing is not a running total of independently reset streams."""
        def candidate(config):
            config["processors"]["transform/aggregate"] = {
                "error_mode": "propagate",
                "metric_statements": [{"context": "metric", "statements": [
                    'aggregate_on_attributes("sum")']}]}
            config["service"]["pipelines"]["metrics"]["processors"].insert(
                -1, "transform/aggregate")

        def combined(streams, end):
            body = payload(streams, end=end)
            scopes = [r["scopeMetrics"][0] for r in body["resourceMetrics"]]
            for left, right in zip(scopes[0]["metrics"], scopes[1]["metrics"]):
                field = "sum" if "sum" in left else "histogram"
                left[field]["dataPoints"].extend(right[field]["dataPoints"])
            body["resourceMetrics"] = body["resourceMetrics"][:1]
            return body

        initial = combined([(INSTANCE_A, "unknown-a", 100, [2, 1]),
                            (INSTANCE_A, "unknown-b", 40, [1, 3])], 1700000010)
        reset = combined([(INSTANCE_A, "unknown-a", 7, [1, 1]),
                          (INSTANCE_A, "unknown-b", 50, [1, 4])], 1700000020)
        for metric in reset["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
            field = "sum" if "sum" in metric else "histogram"
            metric[field]["dataPoints"][0]["startTimeUnixNano"] = "1700000010000000000"
        outputs = self.observe([initial, initial, reset], candidate)
        self.assert_totals(outputs[0], 140, [3, 4])
        self.assert_totals(outputs[1], 140, [3, 4])
        self.assert_totals(outputs[2], 157, [4, 6])

    def test_scope_canonicalization_does_not_erase_separate_streams(self):
        body = payload([(INSTANCE_A, "gpt-5", 100, [2, 1]),
                        (INSTANCE_A, "gpt-5", 40, [1, 3])])
        body["resourceMetrics"][0]["scopeMetrics"][0]["scope"]["version"] = "1"
        second = body["resourceMetrics"][1]["scopeMetrics"][0]
        second["scope"]["version"] = "2"
        body["resourceMetrics"][0]["scopeMetrics"].append(second)
        body["resourceMetrics"] = body["resourceMetrics"][:1]
        self.assert_totals(self.observe([body])[0], 140, [3, 4])


if __name__ == "__main__":
    unittest.main()
