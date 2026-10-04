"""Contract and real pinned-Collector privacy tests (no GCP calls or ADC)."""

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch" / "task-1"
CONFIG = ROOT / "otel" / "collector.yaml"
SECRET = "SENSITIVE_SENTINEL_prompt_source_path_identity"
ENV = {**os.environ, "GCP_PROJECT_ID": "test-destination",
       "GCP_QUOTA_PROJECT_ID": "test-quota"}


def attributes(values):
    return [{"key": key, "value": {"intValue": str(value)}
             if isinstance(value, int) else {"stringValue": value}}
            for key, value in values.items()]


def attribute_map(items):
    return {item["key"]: next(iter(item["value"].values())) for item in items}


def trace_payload(names=None, poisoned=False):
    spans = []
    for index, name in enumerate(names or [SECRET]):
        operational = {
            "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-5",
            "gen_ai.response.model": "gpt-5", "gen_ai.provider.name": "github",
            "gen_ai.usage.input_tokens": 123, "gen_ai.usage.output_tokens": 45,
            "gen_ai.usage.cache_read.input_tokens": 12,
            "gen_ai.usage.cache_write.input_tokens": 3,
            "gen_ai.usage.reasoning.output_tokens": 7, "error.type": "timeout",
            "gen_ai.tool.name": "bash",
        }
        if poisoned:
            operational = {key: SECRET for key in operational}
        operational.update({"gen_ai.input.messages": SECRET,
                            "gen_ai.output.messages": SECRET,
                            "gen_ai.tool.call.arguments": SECRET,
                            "gen_ai.tool.call.result": SECRET,
                            "exception.message": SECRET,
                            "exception.stacktrace": SECRET, "session.id": SECRET})
        spans.append({
            "traceId": "0123456789abcdef0123456789abcdef",
            "spanId": f"{index + 1:016x}", "name": name, "kind": 3,
            "traceState": "private=" + SECRET,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000001500000000",
            "attributes": attributes(operational),
            "status": {"code": 2, "message": SECRET},
            "events": [{"name": SECRET, "timeUnixNano": "1700000000100000000",
                        "attributes": attributes({"exception.message": SECRET})}],
            "links": [{"traceId": "fedcba9876543210fedcba9876543210",
                       "spanId": "fedcba9876543210", "traceState": SECRET,
                       "attributes": attributes({"content": SECRET})}],
        })
    return {"resourceSpans": [{
        "resource": {"attributes": attributes({
            "service.name": SECRET, "service.version": SECRET,
            "gcp.project_id": SECRET, "host.name": SECRET, "user.name": SECRET,
            "process.command_line": SECRET, "repository.path": SECRET})},
        "schemaUrl": SECRET,
        "scopeSpans": [{"schemaUrl": SECRET,
                        "scope": {"name": SECRET, "version": SECRET,
                                  "attributes": attributes({"private": SECRET})},
                        "spans": spans}],
    }]}


def metric_payload(poisoned=False):
    labels = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-5",
              "gen_ai.response.model": "gpt-5", "gen_ai.provider.name": "github",
              "gen_ai.tool.name": "bash", "error.type": "timeout"}
    if poisoned:
        labels = {key: SECRET for key in labels}
    labels.update({"session.id": SECRET, "trace.id": SECRET,
                   "repository.path": SECRET, "prompt": SECRET})
    point = {"attributes": attributes(labels),
             "startTimeUnixNano": "1700000000000000000",
             "timeUnixNano": "1700000001500000000",
             "exemplars": [{"asDouble": 1.5, "timeUnixNano": "1700000001500000000",
                            "filteredAttributes": attributes({"prompt": SECRET})}]}
    metrics = [
        {"name": "gen_ai.client.inference.usage.input_tokens",
         "unit": SECRET, "description": SECRET,
         "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                 "dataPoints": [{**point, "asInt": "123"}]}},
        {"name": "gen_ai.client.operation.duration",
         "unit": SECRET, "description": SECRET,
         "histogram": {"aggregationTemporality": 2,
                       "dataPoints": [{**point, "count": "2", "sum": 1.5,
                                       "min": 0.5, "max": 1.0,
                                       "bucketCounts": ["1", "1"],
                                       "explicitBounds": [0.75]}]}},
        {"name": SECRET, "description": SECRET, "unit": SECRET,
         "gauge": {"dataPoints": [{**point, "asDouble": 99}]}},
    ]
    return {"resourceMetrics": [{
        "resource": {"attributes": attributes({"service.name": SECRET,
                                               "gcp.project_id": SECRET,
                                               "user.name": SECRET})},
        "schemaUrl": SECRET,
        "scopeMetrics": [{"scope": {"name": SECRET, "version": SECRET,
                                    "attributes": attributes({"private": SECRET})},
                          "schemaUrl": SECRET, "metrics": metrics}],
    }]}


class CollectorTests(unittest.TestCase):
    def config(self):
        self.assertTrue(CONFIG.is_file(), "Task 1 Collector config is missing")
        import yaml
        return yaml.safe_load(CONFIG.read_text())

    def binary(self):
        path = os.environ.get("OTELCOL_BIN")
        if not path:
            self.skipTest("Set OTELCOL_BIN to the pinned v0.162.0 contrib binary")
        self.assertTrue(Path(path).is_file(), "OTELCOL_BIN does not exist")
        result = subprocess.run([path, "--version"], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\b0\.162\.0\b")
        return path

    def test_production_pipeline_contract(self):
        config = self.config()
        self.assertEqual(set(config["service"]["pipelines"]), {"traces", "metrics"})
        self.assertEqual(config["receivers"]["otlp"]["protocols"],
                         {"http": {"endpoint": "127.0.0.1:4318"}})
        self.assertEqual(config["extensions"]["health_check"]["endpoint"],
                         "127.0.0.1:13133")
        auth = config["extensions"]["googleclientauth"]
        self.assertEqual(auth["project"], "${env:GCP_PROJECT_ID}")
        self.assertEqual(auth["quota_project"], "${env:GCP_QUOTA_PROJECT_ID}")
        exporter = config["exporters"]["otlp_http/gcp"]
        self.assertEqual(exporter["endpoint"], "https://telemetry.googleapis.com")
        self.assertEqual(exporter["encoding"], "proto")
        self.assertEqual(exporter["auth"]["authenticator"], "googleclientauth")
        self.assertEqual(exporter["sending_queue"]["queue_size"], 256)
        self.assertEqual(exporter["sending_queue"]["sizer"], "requests")
        self.assertEqual(exporter["retry_on_failure"]["max_elapsed_time"], "300s")
        self.assertEqual(config["processors"]["memory_limiter"],
                         {"check_interval": "1s", "limit_mib": 128,
                          "spike_limit_mib": 32})
        self.assertEqual(config["processors"]["batch"],
                         {"timeout": "5s", "send_batch_size": 256,
                          "send_batch_max_size": 256})
        for pipeline in config["service"]["pipelines"].values():
            self.assertEqual(pipeline["exporters"], ["otlp_http/gcp"])
            self.assertEqual(pipeline["processors"][0], "memory_limiter")
            self.assertEqual(pipeline["processors"][-1], "batch")
            for name in pipeline["processors"]:
                if name.startswith(("transform", "filter")):
                    self.assertEqual(config["processors"][name]["error_mode"],
                                     "propagate")

    def test_native_production_validation(self):
        self.config()
        result = subprocess.run([self.binary(), "validate", "--config", str(CONFIG)],
                                env=ENV, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def run_local(self, payloads, mutate=None):
        config = copy.deepcopy(self.config())
        binary = self.binary()
        import yaml
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="collector ", dir=SCRATCH) as work:
            directory = Path(work)
            output = directory / "filtered.jsonl"
            port, health = self.free_port(), self.free_port()
            config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] = (
                f"127.0.0.1:{port}")
            config["extensions"] = {
                "health_check": {"endpoint": f"127.0.0.1:{health}"}}
            config["service"]["extensions"] = ["health_check"]
            config["exporters"] = {"file": {"path": str(output), "format": "json"}}
            for pipeline in config["service"]["pipelines"].values():
                pipeline["exporters"] = ["file"]
            if mutate:
                mutate(config)
            local = directory / "integration.yaml"
            local.write_text(yaml.safe_dump(config))
            with (directory / "collector.log").open("w+") as log:
                process = subprocess.Popen(
                    [binary, "--config", str(local)], env=ENV,
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(
                                    f"http://127.0.0.1:{health}", timeout=0.5) as response:
                                if response.status == 200:
                                    break
                        except (urllib.error.URLError, TimeoutError):
                            pass
                        if process.poll() is not None:
                            break
                        time.sleep(0.1)
                    else:
                        self.fail("Collector readiness timeout")
                    if process.poll() is not None:
                        log.seek(0)
                        self.fail(log.read())
                    responses = []
                    for signal, payload in payloads:
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{port}/v1/{signal}",
                            data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"})
                        try:
                            with urllib.request.urlopen(request, timeout=10) as response:
                                responses.append(response.status)
                        except urllib.error.HTTPError as error:
                            responses.append(error.code)
                            error.close()
                    # Keep the production five-second batch timer intact.
                    time.sleep(5.5)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                log.seek(0)
                logs = log.read()
            data = output.read_text() if output.exists() else ""
            return [json.loads(line) for line in data.splitlines()], responses, logs

    def assert_private(self, data):
        self.assertNotIn(SECRET, json.dumps(data))

    def test_sensitive_fields_removed_operational_counts_durations_preserved(self):
        data, responses, logs = self.run_local([
            ("traces", trace_payload()),
            ("metrics", metric_payload()),
        ])
        self.assertEqual(responses, [200, 200], logs)
        self.assert_private(data)
        spans = [span for record in data for resource in record.get("resourceSpans", [])
                 for scope in resource["scopeSpans"] for span in scope["spans"]]
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span["name"], "chat")
        self.assertEqual(span["startTimeUnixNano"], "1700000000000000000")
        self.assertEqual(span["endTimeUnixNano"], "1700000001500000000")
        self.assertEqual(span["status"]["code"], 2)
        self.assertEqual(attribute_map(span["attributes"])["gen_ai.usage.input_tokens"],
                         "123")
        self.assertEqual(attribute_map(span["attributes"])["gen_ai.usage.output_tokens"],
                         "45")
        for key, value in [("cache_read.input_tokens", "12"),
                           ("cache_write.input_tokens", "3"),
                           ("reasoning.output_tokens", "7")]:
            self.assertEqual(attribute_map(span["attributes"])["gen_ai.usage." + key],
                             value)
        self.assertFalse(span.get("events"))
        self.assertFalse(span.get("links"))
        metrics = [metric for record in data
                   for resource in record.get("resourceMetrics", [])
                   for scope in resource["scopeMetrics"] for metric in scope["metrics"]]
        self.assertEqual(len(metrics), 2)
        by_name = {metric["name"]: metric for metric in metrics}
        tokens = by_name["gen_ai.client.inference.usage.input_tokens"]
        self.assertEqual(tokens["sum"]["dataPoints"][0]["asInt"], "123")
        self.assertEqual(tokens["unit"], "{token}")
        duration = by_name["gen_ai.client.operation.duration"]
        self.assertEqual(duration["unit"], "s")
        point = duration["histogram"]["dataPoints"][0]
        self.assertEqual(point["count"], "2")
        self.assertEqual(point["sum"], 1.5)
        self.assertEqual(point["bucketCounts"], ["1", "1"])
        self.assertEqual(point["explicitBounds"], [0.75])
        self.assertFalse(point.get("exemplars"))
        self.assertEqual(attribute_map(point["attributes"]), {
            "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-5",
            "gen_ai.response.model": "gpt-5", "gen_ai.provider.name": "github",
            "gen_ai.tool.name": "bash", "error.type": "timeout"})
        for record in data:
            for signal in ("resourceSpans", "resourceMetrics"):
                for resource in record.get(signal, []):
                    self.assertEqual(attribute_map(resource["resource"]["attributes"]),
                                     {"service.name": "github-copilot",
                                      "gcp.project_id": "test-destination"})

    def test_poisoned_allowlisted_values_cannot_leak(self):
        data, responses, logs = self.run_local([
            ("traces", trace_payload(poisoned=True)),
            ("metrics", metric_payload(poisoned=True)),
        ])
        self.assertEqual(responses, [200, 200], logs)
        self.assert_private(data)
        self.assertTrue(data, "Failing closed must not discard valid numeric samples")

    def test_canonical_span_categories_preserved_without_model_or_tool_suffix(self):
        names = ["invoke_agent", "plan", "chat " + SECRET,
                 "execute_tool " + SECRET, SECRET]
        payload = trace_payload(names)
        for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            span["attributes"] = attributes({"prompt": SECRET})
        data, responses, logs = self.run_local([("traces", payload)])
        self.assertEqual(responses, [200], logs)
        self.assert_private(data)
        exported = [span["name"] for record in data
                    for resource in record.get("resourceSpans", [])
                    for scope in resource["scopeSpans"] for span in scope["spans"]]
        self.assertEqual(exported, ["invoke_agent", "plan", "chat",
                                    "execute_tool", "operation"])

    def test_transform_runtime_error_drops_payload(self):
        def fail_transform(config):
            for key in ("trace_statements", "metric_statements"):
                config["processors"]["transform/privacy"][key].insert(0, {
                    "context": "resource",
                    "statements": [
                        'set(resource.attributes["failure"], Int(resource.attributes["service.name"]))'],
                })

        data, responses, logs = self.run_local([
            ("traces", trace_payload()), ("metrics", metric_payload()),
        ], mutate=fail_transform)
        self.assertTrue(all(status >= 400 for status in responses), logs)
        self.assertFalse(data, "Transform failure exported a payload")

    def test_nested_dimension_values_are_normalized_without_content(self):
        traces = trace_payload()
        metrics = metric_payload()
        span = traces["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        metric = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        for items in (span["attributes"], metric["sum"]["dataPoints"][0]["attributes"]):
            for item in items:
                if item["key"] == "gen_ai.request.model":
                    item["value"] = {"arrayValue": {
                        "values": [{"stringValue": SECRET}]}}
        data, responses, logs = self.run_local([
            ("traces", traces), ("metrics", metrics)])
        self.assertEqual(responses, [200, 200], logs)
        self.assert_private(data)
        self.assertTrue(data, "Nested dimensions must not lose valid numeric samples")

    def test_application_logs_are_not_received_or_exported(self):
        payload = {"resourceLogs": [{
            "scopeLogs": [{"logRecords": [{
                "body": {"stringValue": SECRET},
                "timeUnixNano": "1700000000000000000"}]}]}]}
        data, responses, logs = self.run_local([("logs", payload)])
        self.assertEqual(responses, [404], logs)
        self.assertFalse(data)


if __name__ == "__main__":
    unittest.main()
