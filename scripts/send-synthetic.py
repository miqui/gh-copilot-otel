#!/usr/bin/env python3
"""Send constant, content-free OTLP JSON to loopback only; no third-party deps."""

import argparse
from contextlib import contextmanager
import http.client
import json
import math
import secrets
import signal
import sys
import time
from urllib.parse import urlsplit


INGESTION_URL = "http://127.0.0.1:4318"
HEALTH_URL = "http://127.0.0.1:13133/"
MAX_RESPONSE_BYTES = 65536


def attributes(values):
    return [{"key": key, "value": {"intValue": str(value)}
             if isinstance(value, int) else {"stringValue": value}}
            for key, value in values.items()]


def build_payloads():
    now = time.time_ns()
    start = now - 1_500_000_000
    trace_id, parent, child = secrets.token_hex(16), secrets.token_hex(8), secrets.token_hex(8)
    labels = {"gen_ai.provider.name": "github", "gen_ai.request.model": "other",
              "gen_ai.response.model": "other", "gen_ai.tool.name": "other",
              "gen_ai.operation.name": "chat"}
    resource = {"attributes": attributes({"service.name": "github-copilot"})}
    scope = {"name": "copilot-otel.synthetic"}
    spans = []
    for operation, span_id, span_start in (
            ("chat", parent, start), ("execute_tool", child, now - 250_000_000)):
        values = {**labels, "gen_ai.operation.name": operation}
        if operation == "chat":
            values.update({"gen_ai.usage.input_tokens": 100,
                           "gen_ai.usage.output_tokens": 40,
                           "gen_ai.usage.cache_read.input_tokens": 10,
                           "gen_ai.usage.cache_write.input_tokens": 3})
        span = {"traceId": trace_id, "spanId": span_id, "name": operation,
                "kind": 3, "startTimeUnixNano": str(span_start),
                "endTimeUnixNano": str(now), "attributes": attributes(values),
                "status": {"code": 1}}
        if operation == "execute_tool":
            span["parentSpanId"] = parent
        spans.append(span)
    traces = {"resourceSpans": [{"resource": resource, "scopeSpans": [
        {"scope": scope, "spans": spans}]}]}
    point = {"startTimeUnixNano": str(start), "timeUnixNano": str(now),
             "attributes": attributes(labels)}
    metrics = []
    for suffix, count in (("input_tokens", 100), ("output_tokens", 40),
                          ("cache_read.input_tokens", 10), ("cache_write.input_tokens", 3)):
        metrics.append({
            "name": "gen_ai.client.inference.usage." + suffix, "unit": "{token}",
            "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                    "dataPoints": [{**point, "asInt": str(count)}]},
        })
    tool_point = {**point, "attributes": attributes(
        {**labels, "gen_ai.operation.name": "execute_tool"})}
    metrics.append({
        "name": "github.copilot.tool.call.count",
        "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [{**tool_point, "asInt": "1"}]},
    })
    for name, sample, duration, buckets in (
            ("gen_ai.client.operation.duration", point, 1.5, ["0", "0", "1", "0"]),
            ("github.copilot.tool.call.duration", tool_point, 0.25, ["1", "0", "0", "0"])):
        metrics.append({
            "name": name, "unit": "s",
            "histogram": {"aggregationTemporality": 2, "dataPoints": [{
                **sample, "count": "1", "sum": duration,
                "explicitBounds": [0.25, 1, 2], "bucketCounts": buckets}]},
        })
    return traces, {"resourceMetrics": [{"resource": resource, "scopeMetrics": [
        {"scope": scope, "metrics": metrics}]}]}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("truncated protobuf response")
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value >= 1 << 64:
                raise ValueError("invalid protobuf integer")
            return value, position
    raise ValueError("invalid protobuf integer")


def protobuf_fields(data, schema):
    result, position = {}, 0
    while position < len(data):
        tag, position = varint(data, position)
        field, wire = tag >> 3, tag & 7
        if field not in schema or schema[field] != wire or field in result:
            raise ValueError("invalid protobuf response shape")
        value, position = varint(data, position)
        if wire == 2:
            end = position + value
            if end > len(data):
                raise ValueError("truncated protobuf response")
            value, position = data[position:end], end
        result[field] = value
    return result


def validate_response(body, content_type, signal):
    if not body and content_type in ("", "application/json", "application/x-protobuf"):
        return
    if content_type == "application/json":
        response = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(response, dict) or set(response) - {"partialSuccess"}:
            raise ValueError("invalid OTLP JSON response shape")
        partial = response.get("partialSuccess", {})
        counter = "rejectedSpans" if signal == "traces" else "rejectedDataPoints"
        if not isinstance(partial, dict) or set(partial) - {counter, "errorMessage"}:
            raise ValueError("invalid OTLP partial-success shape")
        rejected = partial.get(counter, 0)
        if isinstance(rejected, str):
            if not rejected.isascii() or not rejected.isdecimal():
                raise ValueError("invalid OTLP rejected counter")
            rejected = int(rejected)
        if type(rejected) is not int or not 0 <= rejected < 1 << 63:
            raise ValueError("invalid OTLP rejected counter")
        message = partial.get("errorMessage", "")
        if not isinstance(message, str):
            raise ValueError("invalid OTLP error message type")
    elif content_type == "application/x-protobuf":
        response = protobuf_fields(body, {1: 2})
        partial = protobuf_fields(response.get(1, b""), {1: 0, 2: 2})
        rejected = partial.get(1, 0)
        if rejected >= 1 << 63:
            raise ValueError("invalid OTLP rejected counter")
        message = partial.get(2, b"").decode("utf-8")
    else:
        raise ValueError("unsupported OTLP response content type")
    if rejected or message:
        raise ValueError("OTLP partial-success rejection or warning")


@contextmanager
def request_deadline(seconds):
    # On macOS, a real-time alarm also bounds slow-drip headers/bodies; socket
    # timeouts alone restart after every read. This command runs on the main thread.
    def expired(signum, frame):
        raise TimeoutError("local HTTP request deadline exceeded")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


def request(url, timeout, payload=None):
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname != "127.0.0.1":
        raise ValueError("only fixed loopback HTTP is supported")
    # HTTPConnection neither uses environment proxies nor follows redirects.
    connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=timeout)
    try:
        with request_deadline(timeout):
            return exchange(connection, target.path or "/", payload)
    finally:
        connection.close()


def exchange(connection, path, payload):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {
        "Content-Type": "application/json", "Accept": "application/json"}
    connection.request("GET" if body is None else "POST", path, body=body, headers=headers)
    response = connection.getresponse()
    if response.status != 200:
        raise ValueError(f"HTTP {response.status}")
    if payload is None:
        return b"", ""
    result = response.read(MAX_RESPONSE_BYTES + 1)
    if len(result) > MAX_RESPONSE_BYTES:
        raise ValueError("OTLP response exceeds size limit")
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    return result, content_type


def timeout_value(value):
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 30:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than 0, at most 30 seconds")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-health", action="store_true",
                        help="check loopback Collector health before sending")
    parser.add_argument("--timeout", type=timeout_value, default=3,
                        help="per-request deadline in seconds (0 < value <= 30; default: 3)")
    args = parser.parse_args(argv)
    stage = "health" if args.check_health else "traces"
    try:
        if args.check_health:
            request(HEALTH_URL, args.timeout)
        traces, metrics = build_payloads()
        for stage, payload in (("traces", traces), ("metrics", metrics)):
            body, content_type = request(
                INGESTION_URL + "/v1/" + stage, args.timeout, payload)
            validate_response(body, content_type, stage)
    except (OSError, ValueError, http.client.HTTPException) as error:
        # Never print arbitrary response bodies, remote messages or local environment.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        if isinstance(error, (json.JSONDecodeError, UnicodeError)):
            detail = "invalid OTLP response encoding"
        print(f"verify: {stage} failed ({detail}); no cloud delivery was verified",
              file=sys.stderr)
        return 1
    trace_id = traces["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
    print("Locally accepted synthetic traces and metrics; not proof of cloud delivery.")
    print("Synthetic model/tool labels: other; these samples contribute to shared usage totals.")
    print(f"Trace ID: {trace_id}")
    print(f"Sample time (Unix ns): {metrics['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['sum']['dataPoints'][0]['timeUnixNano']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
