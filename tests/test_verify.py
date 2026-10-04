"""Content-free verification contracts; fake loopback HTTP and native local tests."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import test_collector


ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "scripts/send-synthetic.py"
VERIFY = ROOT / "scripts/verify.sh"
SECRET = "SERVER_RESPONSE_MUST_NOT_BE_PRINTED"


@contextmanager
def server(replies=None, health=200, delay=0, drip=0):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(health)
            self.end_headers()

        def do_POST(self):
            received.append((self.path, self.headers,
                             json.loads(self.rfile.read(
                                 int(self.headers["Content-Length"])))))
            reply = (replies or {}).get(
                self.path, (200, "application/json", b"{}"))
            status, content_type, body = reply[:3]
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in (reply[3] if len(reply) > 3 else {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                if drip:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(drip)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02))
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", received
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class VerifyTests(unittest.TestCase):
    def sender(self):
        self.assertTrue(SENDER.is_file(), "Task 4 synthetic sender is missing")
        spec = importlib.util.spec_from_file_location("send_synthetic", SENDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_sender(self, module, base, args=()):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(module, "INGESTION_URL", base), \
                patch.object(module, "HEALTH_URL", base + "/"), \
                redirect_stdout(out), redirect_stderr(err):
            status = module.main(list(args))
        return status, out.getvalue(), err.getvalue()

    def test_sends_both_signals_and_reports_local_not_cloud_acceptance(self):
        module = self.sender()
        with server() as (base, received):
            status, out, err = self.run_sender(module, base, ["--check-health"])
        self.assertEqual(status, 0, err)
        self.assertEqual([item[0] for item in received], ["/v1/traces", "/v1/metrics"])
        self.assertTrue(all(item[1]["Content-Type"] == "application/json"
                            for item in received))
        self.assertRegex(out, r"Trace ID: [0-9a-f]{32}")
        self.assertIn("not proof of cloud delivery", out)
        self.assertIn("synthetic", out.lower())

    def test_fresh_content_free_payloads_cover_all_seven_chart_metrics(self):
        module = self.sender()
        traces, metrics = module.build_payloads()
        again, _ = module.build_payloads()
        spans = traces["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        ids = {span["traceId"] for span in spans}
        self.assertEqual(len(ids), 1)
        self.assertNotEqual(spans[0]["traceId"],
                            again["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"])
        self.assertEqual(len({span["spanId"] for span in spans}), 2)
        for span in spans:
            self.assertRegex(span["traceId"], r"^[0-9a-f]{32}$")
            self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
            self.assertLess(int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"]))
            self.assertLess(abs(int(span["endTimeUnixNano"]) - time.time_ns()),
                            10_000_000_000)
            labels = test_collector.attribute_map(span["attributes"])
            self.assertEqual(labels["gen_ai.request.model"], "other")
            self.assertEqual(labels["gen_ai.tool.name"], "other")
        items = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        by_name = {item["name"]: item for item in items}
        self.assertEqual(set(by_name), {
            "gen_ai.client.inference.usage.input_tokens",
            "gen_ai.client.inference.usage.output_tokens",
            "gen_ai.client.inference.usage.cache_read.input_tokens",
            "gen_ai.client.inference.usage.cache_write.input_tokens",
            "gen_ai.client.operation.duration",
            "github.copilot.tool.call.count",
            "github.copilot.tool.call.duration",
        })
        for item in items:
            data = item.get("sum", item.get("histogram"))
            self.assertEqual(data["aggregationTemporality"], 2)
            point = data["dataPoints"][0]
            self.assertLess(int(point["startTimeUnixNano"]), int(point["timeUnixNano"]))
            if "sum" in item:
                self.assertTrue(data["isMonotonic"])
                self.assertGreater(int(point["asInt"]), 0)
            else:
                self.assertEqual(len(point["bucketCounts"]), len(point["explicitBounds"]) + 1)
                self.assertEqual(sum(map(int, point["bucketCounts"])), int(point["count"]))
                self.assertGreater(point["sum"], 0)
        serialized = json.dumps([traces, metrics])
        for forbidden in ("messages", "arguments", "result", "repository", "user.name",
                          "exception", "session.id"):
            self.assertNotIn(forbidden, serialized)

    def test_unavailable_health_is_failure_without_ingestion(self):
        module = self.sender()
        with server(health=503) as (base, received):
            status, out, err = self.run_sender(module, base, ["--check-health"])
        self.assertEqual(status, 1)
        self.assertEqual(received, [])
        self.assertIn("health", err)
        self.assertNotIn("Trace ID:", out)

    def test_connection_refused_for_health_and_ingestion_fails(self):
        module = self.sender()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        for args in ([], ["--check-health"]):
            with self.subTest(args=args):
                status, out, err = self.run_sender(module, base, args)
                self.assertEqual(status, 1)
                self.assertTrue(err)
                self.assertNotIn("accepted", out.lower())

    def test_http_failures_stop_before_metrics_and_do_not_echo_bodies(self):
        module = self.sender()
        for code in (302, 400, 401, 500):
            with self.subTest(code=code), server(replies={
                "/v1/traces": (code, "application/json", SECRET.encode())
            }) as (base, received):
                status, out, err = self.run_sender(module, base)
                self.assertEqual(status, 1)
                self.assertEqual(len(received), 1)
                self.assertNotIn(SECRET, out + err)

    def test_json_partial_success_rejected_counters_fail_for_both_signals(self):
        module = self.sender()
        for signal, field in (("traces", "rejectedSpans"), ("metrics", "rejectedDataPoints")):
            for count in (1, "2"):
                body = json.dumps({"partialSuccess": {field: count}}).encode()
                with self.subTest(signal=signal, count=count), server(replies={
                    "/v1/" + signal: (200, "application/json", body)
                }) as (base, received):
                    status, out, err = self.run_sender(module, base)
                    self.assertEqual(status, 1)
                    self.assertIn("partial", err)
                    self.assertNotIn("Locally accepted", out)

    def test_protobuf_success_and_rejected_counters(self):
        module = self.sender()
        for body, expected in ((b"", 0), (b"\x0a\x00", 0),
                               (b"\x0a\x02\x08\x00", 0),
                               (b"\x0a\x02\x08\x01", 1),
                               (b"\x0a\x01\x08", 1),
                               (b"\x0a\x05\x08\x01", 1)):
            for signal in ("traces", "metrics"):
                with self.subTest(body=body, signal=signal), server(replies={
                    "/v1/" + signal: (200, "application/x-protobuf", body)
                }) as (base, received):
                    status, out, err = self.run_sender(module, base)
                    self.assertEqual(status, expected, out + err)

    def test_empty_json_success_and_zero_partial_success(self):
        module = self.sender()
        for body in (b"", b"{}", b'{"partialSuccess":{}}',
                     b'{"partialSuccess":{"rejectedSpans":"0","errorMessage":""}}'):
            with self.subTest(body=body), server(replies={
                "/v1/traces": (200, "application/json; charset=utf-8", body)
            }) as (base, received):
                status, out, err = self.run_sender(module, base)
                self.assertEqual(status, 0, out + err)

    def test_invalid_json_shapes_types_and_warnings_fail_without_echo(self):
        module = self.sender()
        bodies = [b"not JSON", b"[]", b"null", b"true", b'{"code":7}',
                  b'{"partialSuccess":null}', b'{"partialSuccess":[]}',
                  b'{"partialSuccess":{"rejectedSpans":true}}',
                  b'{"partialSuccess":{"rejectedSpans":-1}}',
                  b'{"partialSuccess":{"rejectedSpans":1.5}}',
                  b'{"partialSuccess":{"rejectedSpans":"bad"}}',
                  b'{"partialSuccess":{"rejectedDataPoints":"0"}}',
                  b'{"partialSuccess":{"errorMessage":2}}',
                  json.dumps({"partialSuccess": {"errorMessage": SECRET}}).encode()]
        for body in bodies:
            with self.subTest(body=body), server(replies={
                "/v1/traces": (200, "application/json", body)
            }) as (base, received):
                status, out, err = self.run_sender(module, base)
                self.assertEqual(status, 1)
                self.assertNotIn(SECRET, out + err)

    def test_unsupported_content_type_and_oversized_response_fail(self):
        module = self.sender()
        for content_type, body in (("text/html", b"{}"),
                                   ("application/json", b" " * 65537)):
            with self.subTest(content_type=content_type), server(replies={
                "/v1/traces": (200, content_type, body)
            }) as (base, received):
                status, out, err = self.run_sender(module, base)
                self.assertEqual(status, 1)

    def test_truncated_declared_length_responses_fail_before_otlp_success(self):
        module = self.sender()
        for signal in ("traces", "metrics"):
            for content_type, body in (
                    ("application/json", b"{}"), ("application/json", b""),
                    ("application/x-protobuf", b"\x0a\x00"),
                    ("application/x-protobuf", b"")):
                with self.subTest(signal=signal, content_type=content_type, body=body), \
                        server(replies={"/v1/" + signal: (
                            200, content_type, body, {"Content-Length": "100"})
                        }) as (base, received):
                    status, out, err = self.run_sender(module, base)
                    self.assertEqual(status, 1, out + err)
                    self.assertIn("IncompleteRead", err)
                    self.assertNotIn("Locally accepted", out)
                    self.assertNotIn("Trace ID:", out)
                    self.assertEqual(len(received), 1 if signal == "traces" else 2)

    def test_complete_declared_length_responses_remain_successful(self):
        module = self.sender()
        for content_type, body in (
                ("application/json", b"{}"), ("application/json", b""),
                ("application/x-protobuf", b"\x0a\x00"),
                ("application/x-protobuf", b"")):
            with self.subTest(content_type=content_type, body=body), server(replies={
                "/v1/" + signal: (
                    200, content_type, body, {"Content-Length": str(len(body))})
                for signal in ("traces", "metrics")
            }) as (base, received):
                status, out, err = self.run_sender(module, base)
                self.assertEqual(status, 0, out + err)

    def test_chunked_response_requires_complete_chunk_and_terminator(self):
        module = self.sender()
        for signal in ("traces", "metrics"):
            for body, expected in ((b"2\r\n{}\r\n0\r\n\r\n", 0),
                                   (b"0\r\n\r\n", 0),
                                   (b"2\r\n{}", 1),
                                   (b"2\r\n{}\r\n", 1),
                                   (b"3\r\n{}", 1)):
                with self.subTest(signal=signal, body=body), server(replies={
                    "/v1/" + signal: (
                        200, "application/json", body, {"Transfer-Encoding": "chunked"})
                }) as (base, received):
                    status, out, err = self.run_sender(module, base)
                    self.assertEqual(status, expected, out + err)
                    if expected:
                        self.assertIn("IncompleteRead", err)
                        self.assertNotIn("Locally accepted", out)

    def test_timeout_is_bounded_and_proxies_are_bypassed(self):
        module = self.sender()
        with server(delay=0.4) as (base, received):
            started = time.monotonic()
            status, out, err = self.run_sender(module, base, ["--timeout", "0.1"])
            elapsed = time.monotonic() - started
        self.assertEqual(status, 1)
        self.assertLess(elapsed, 1)
        with patch.dict(os.environ, {
            "http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": "",
        }), server() as (base, received):
            status, out, err = self.run_sender(module, base, ["--check-health"])
        self.assertEqual(status, 0, out + err)
        self.assertEqual(len(received), 2)

    def test_timeout_range_and_remote_endpoint_options_are_rejected(self):
        self.assertTrue(SENDER.is_file(), "Task 4 synthetic sender is missing")
        for args in (["--timeout", "0"], ["--timeout", "31"], ["--timeout", "nan"],
                     ["--timeout", "inf"], ["--endpoint", "https://example.com"]):
            with self.subTest(args=args):
                result = subprocess.run(
                    [os.sys.executable, str(SENDER), *args],
                    capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2)

    def test_slow_drip_response_cannot_extend_request_deadline(self):
        module = self.sender()
        with server(drip=0.03, replies={
            "/v1/traces": (200, "application/json",
                           b'{"partialSuccess":{"rejectedSpans":"0"}}')
        }) as (base, received):
            started = time.monotonic()
            status, out, err = self.run_sender(module, base, ["--timeout", "0.1"])
            elapsed = time.monotonic() - started
        self.assertEqual(status, 1, out + err)
        self.assertLess(elapsed, 0.5)

    def test_shell_entrypoint_checks_health_and_preserves_exit_status_with_spaces(self):
        self.assertTrue(VERIFY.is_file(), "Task 4 verification entrypoint is missing")
        scratch = ROOT / ".scratch/task-4"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="verify ") as work:
            directory = Path(work)
            fake = directory / "python3"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit 7\n")
            fake.chmod(0o755)
            result = subprocess.run(
                [str(VERIFY), "--timeout", "2"],
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
                capture_output=True, text=True, timeout=3, cwd=directory)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [str(SENDER), "--check-health", "--timeout", "2"])

    def test_native_collector_preserves_synthetic_labels_and_chart_shape(self):
        module = self.sender()
        traces, metrics = module.build_payloads()
        helper = test_collector.CollectorTests()
        data, statuses, logs = helper.run_local([("traces", traces), ("metrics", metrics)])
        self.assertEqual(statuses, [200, 200], logs)
        spans = [span for record in data for resource in record.get("resourceSpans", [])
                 for scope in resource["scopeSpans"] for span in scope["spans"]]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["traceId"],
                         traces["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"])
        filtered = [metric for record in data
                    for resource in record.get("resourceMetrics", [])
                    for scope in resource["scopeMetrics"] for metric in scope["metrics"]]
        self.assertEqual(len(filtered), 7)
        for item in filtered:
            data = item.get("sum", item.get("histogram"))
            labels = test_collector.attribute_map(data["dataPoints"][0]["attributes"])
            self.assertEqual(labels["gen_ai.request.model"], "other")
            self.assertEqual(labels["gen_ai.tool.name"], "other")
            self.assertIn(labels["gen_ai.operation.name"], ("chat", "execute_tool"))
        self.assertNotIn("\terror\t", logs.lower())


if __name__ == "__main__":
    unittest.main()
