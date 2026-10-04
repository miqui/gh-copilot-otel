# Copilot CLI telemetry → GCP

Opt-in, content-free usage and performance telemetry from one macOS machine:

```text
Copilot CLI → OTLP/HTTP protobuf, 127.0.0.1:4318
            → native OpenTelemetry Collector Contrib v0.162.0
            → authenticated HTTPS, telemetry.googleapis.com
            → Cloud Trace + Cloud Monitoring / Managed Service for Prometheus
```

No Docker, cloud server, application-log pipeline, or persistent telemetry
buffer. The seven-chart [dashboard](dashboards/copilot-usage.json) covers input,
output, cache-read and cache-write token rates, model latency, tool calls and
tool latency. Token counts are operational telemetry, **not Copilot billing**.

**Build/validation status:** automated tests and local native Collector checks
only. Real GCP ingestion and dashboard rendering have **not** been accepted.
Nothing here automatically enables APIs, changes IAM, imports a dashboard,
authenticates, or installs a background service. Commands below that change
GCP or install a service are instructions for a separately authorized operator,
not steps performed during this build.

## Prerequisites and local installation

- macOS arm64 or amd64, Bash, `curl`, `tar`, `shasum`, and Python **3.9+**.
- A Copilot CLI release with OpenTelemetry monitoring support, separately
  installed and authenticated. Check its `copilot help monitoring` and
  `copilot help environment` against the release you use.
- Google Cloud CLI (`gcloud`) only for explicit cloud setup, authentication and
  dashboard commands. A destination project and quota project with billing
  and appropriate operator permissions; they may be the same project.
- External Google Application Default Credentials (ADC). Do not put keys,
  tokens or credentials in this repository.

From a checkout, explicitly install the checksum-verified native binary:

```sh
scripts/install-collector.sh
# Default: "$HOME/.local/bin/otelcol-contrib".
# Optional absolute destination, including spaces:
# INSTALL_DIR="$HOME/Tools/Collector bin" scripts/install-collector.sh
```

No root privileges or shell startup edits. The release is pinned to **0.162.0**,
including its privacy transformations and native component support.
Upgrades require rerunning the privacy and metric-shape tests.

## GCP preparation and ADC (explicit opt-in cloud operations)

Choose project **IDs**, not project numbers. Set both variables in the terminal
that will run the Collector; unlike the service installer, the Collector
configuration has **no built-in quota-project fallback**:

```sh
export GCP_PROJECT_ID="my-destination-project"
export GCP_QUOTA_PROJECT_ID="${GCP_QUOTA_PROJECT_ID-$GCP_PROJECT_ID}"
# To use a different quota project, explicitly export that ID instead.
: "${GCP_PROJECT_ID:?required}" "${GCP_QUOTA_PROJECT_ID:?required}"
```

The unset-only fallback preserves a supplied quota project; an explicitly empty
one is invalid. Repeat these exports after opening a new terminal.

Only when authorized to modify the selected projects, an operator may run:

```sh
scripts/setup-gcp.sh "$GCP_PROJECT_ID" "$GCP_QUOTA_PROJECT_ID" "user:you@example.com"
# Also accepts serviceAccount:NAME@PROJECT.iam.gserviceaccount.com or group:EMAIL.
```

This enables Telemetry, Monitoring and Trace APIs on the destination and
Service Usage on the quota project; it grants the selected ADC identity
`roles/telemetry.writer` on the destination and
`roles/serviceusage.serviceUsageConsumer` on the quota project. Earlier
changes are not rolled back on failure. The operator needs API-enablement
and IAM-policy editing permissions. These ingestion roles do **not** grant
dashboard creation or viewing rights.

For personal development, **separately authorized interactive authentication**:

```sh
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GCP_QUOTA_PROJECT_ID"
```

`gcloud auth login` alone is not Collector ADC. Keep the ADC file outside the
checkout. Typical location:
`$HOME/.config/gcloud/application_default_credentials.json`, or the directory
selected by `CLOUDSDK_CONFIG`.

Optional restricted, keyless service-account impersonation:

```sh
gcloud auth application-default login \
  --impersonate-service-account="telemetry-writer@my-destination-project.iam.gserviceaccount.com"
gcloud auth application-default set-quota-project "$GCP_QUOTA_PROJECT_ID"
```

The source identity needs `roles/iam.serviceAccountTokenCreator` on the target
service account and permission to use the quota project; the impersonated
account needs the destination writer and quota consumer roles above.
Additional API/IAM preparation for impersonation is operator-owned.
Do not generate service-account keys. Other existing supported keyless
external-account ADC may be selected with
`GOOGLE_APPLICATION_CREDENTIALS="/absolute/path outside checkout/adc.json"`.
Do not print or copy its contents. The exporter sets its quota project from
`GCP_QUOTA_PROJECT_ID`; configuring the ADC quota project separately also makes
the credential workflow consistent.

## Foreground Collector and wrapped CLI

In the terminal with external ADC and the two project exports above:

```sh
COLLECTOR="$HOME/.local/bin/otelcol-contrib"
# If INSTALL_DIR was customized, set COLLECTOR to that installed binary.
"$COLLECTOR" validate --config "otel/collector.yaml"
"$COLLECTOR" --config "otel/collector.yaml"
```

The config uses `gcp.project_id` for destination routing and `googleclientauth`
for ADC and quota. Validation checks config syntax/components, **not** cloud
authorization or delivery. Foreground logs show exporter failures; Ctrl-C stops
this exact Collector. Keep its terminal open.

In another terminal, deliberately opt in for each Copilot invocation:

```sh
scripts/copilot-otel.sh --version
# Normal CLI arguments are passed unchanged. No project export needed here.
# Optional executable selection:
# COPILOT_BIN="/absolute/path with spaces/copilot" scripts/copilot-otel.sh --version
```

The wrapper `exec`s the CLI and preserves arguments and exit status, forces
loopback OTLP/HTTP protobuf, disables content capture and application-log
export, and removes inherited OTLP headers, per-signal endpoints, TLS overrides
and file-exporter selection. It leaves Copilot authentication/provider settings
alone. It does not enable telemetry for unwrapped commands. The wrapper does
not need project/ADC settings; the **Collector does**.

## Optional macOS startup service

Do not run a foreground Collector and a service on the same ports.
With the binary and external ADC already installed, an operator may explicitly
opt in from the terminal with the two project exports:

```sh
scripts/install-launchagent.sh
# Pass the same absolute INSTALL_DIR if the binary destination was customized.
```

See [launchd installation, lifecycle and removal](launchd/README.md). The service
label is `io.github.copilot-otel`, using the stable installed config
`$HOME/.local/share/gh-copilot-otel/collector.yaml`, not a moving checkout.
The installer serializes `launchd/collector.plist` with explicit project,
quota and ADC environment values (an unset quota defaults to the destination).
It does not inherit future terminal exports; rerun the installer to change
settings. It checks ownership before replacing only its own files, validates
the config, and checks both launchd PID state and local health.

Logs:
`$HOME/Library/Logs/copilot-otel/collector.stdout.log` and
`collector.stderr.log`. Keep log files private; exporter errors may contain
operational routing information. Do not publish real telemetry or logs.

## Local verification and synthetic samples

With a Collector already listening:

```sh
scripts/verify.sh
# Optional per-request deadline, default 3 seconds, maximum 30:
scripts/verify.sh --timeout 5
# Ingestion only, without readiness:
python3 scripts/send-synthetic.py
```

Verification checks `http://127.0.0.1:13133/` once, then sends constant
content-free **OTLP JSON traces and metrics** to `127.0.0.1:4318`.
The receiver supports JSON for synthetic verification even though the CLI
wrapper forces protobuf. There are no remote-endpoint flags, proxy use,
redirects, credentials, prompts, source code or user-provided telemetry
values. Requests have finite socket timeouts and a macOS real-time alarm
bounding each whole HTTP exchange (including slow-drip responses), no retries,
and a 64 KiB response size cap. HTTP errors, malformed JSON/protobuf, invalid response
shapes, nonzero OTLP partial-success rejected counters, and even partial-success
warnings fail nonzero. Empty successful responses and empty/zero partial-success
messages are valid. A trace ID and nanosecond sample time are printed only after
both signals pass; traces already accepted before a metric failure cannot be
rolled back.

**Local health / HTTP 200 is not proof of cloud delivery.** Authentication,
exporter retry, routing and cloud storage may still fail asynchronously.
Verification sends telemetry through the running configuration: with the
production GCP exporter and valid ADC it can incur cloud ingestion charges.
Do not run it against production if that export is not authorized. Local
automated tests instead replace the cloud exporter with file/Prometheus
exporters, use isolated loopback ports and scratch files, and need no ADC.

Synthetic samples use the existing bounded `gen_ai.request.model="other"`
and `gen_ai.tool.name="other"` categories. Their source scope identifies the
generator, but production filtering canonicalizes scope/resource fields, so
that marker does **not** survive. The trace ID identifies the synthetic trace;
metric series have no unique-run label. **The seven charts include these
samples in usage totals and in the `other` category**, which can also contain
real usage of unknown models/tools. They are not hidden or separately billed
as verification. Prefer a separately authorized test destination, or note
the printed sample time and exclude the affected window when analyzing real
usage. One sample per run is not sufficient evidence for a five-minute
`rate()` chart; repeated fresh cumulative series may reset counters, so this
is a shape/transport test, not a realistic load generator.

### Separate cloud lookup (not performed by this build)

An authorized viewer can verify actual storage without relying on readiness:

1. In **Cloud Trace → Trace explorer**, select the destination project, set
   the time range around the printed sample time, and filter by the exact
   printed **Trace ID**. Confirm the `chat` and `execute_tool` spans, bounded
   operational attributes, and absence of events, messages and tool content.
2. In **Cloud Monitoring → Metrics explorer → PromQL**, select the same
   destination and time window; query raw series such as:
   ```promql
   gen_ai_client_inference_usage_input_tokens_total{job="github-copilot",gen_ai_request_model="other"}
   gen_ai_client_operation_duration_seconds_bucket{job="github-copilot",gen_ai_request_model="other"}
   github_copilot_tool_call_count_total{job="github-copilot",gen_ai_tool_name="other"}
   github_copilot_tool_call_duration_seconds_bucket{job="github-copilot",gen_ai_tool_name="other"}
   ```
   Also check output, cache-read and cache-write counters. Match timestamps
   and expected synthetic counts, rather than treating an unrelated existing
   `other` series as proof. Allow ingestion delay and inspect exporter logs
   if signals are absent.
3. Only after actual series are observed, inspect the imported dashboard.
   Local native Prometheus translation tests support the expected shape,
   but **do not prove the GCP Telemetry API's stored names or rendering**.
   Adjust queries only against observed cloud data.
4. Full live acceptance additionally needs a separately authorized harmless
   real CLI session and privacy inspection. No real prompts have been run
   during this build.

## Dashboard import and metric naming

With separately authenticated `gcloud` and dashboard permissions, an operator
may explicitly run:

```sh
scripts/import-dashboard.sh create "$GCP_PROJECT_ID"
# Keep the returned short dashboard ID. create again creates a duplicate.
scripts/import-dashboard.sh update "$GCP_PROJECT_ID" "DASHBOARD_ID"
```

`create` always creates a new dashboard; `update` **replaces** the selected
dashboard with no etag/concurrency guard. Preserve any custom dashboard changes
elsewhere first. Ingestion writer rights do not include dashboard mutation:
use an identity with `monitoring.dashboards.create` / `.update` (for example
Monitoring Dashboard Configuration Editor), and separate read/query rights
(for example Monitoring Viewer and Cloud Trace User) for viewing signals.

Expected Prometheus names use underscores instead of dots, cumulative
monotonic counters end in `_total`, and seconds histograms expose
`_seconds_bucket`, `_seconds_sum`, `_seconds_count`. Quantile queries preserve
`le` until `histogram_quantile`. Source metrics:

| OTLP metric | Expected Prometheus series |
| --- | --- |
| `gen_ai.client.inference.usage.input_tokens` | `gen_ai_client_inference_usage_input_tokens_total` |
| `gen_ai.client.inference.usage.output_tokens` | `gen_ai_client_inference_usage_output_tokens_total` |
| `gen_ai.client.inference.usage.cache_read.input_tokens` | `gen_ai_client_inference_usage_cache_read_input_tokens_total` |
| `gen_ai.client.inference.usage.cache_write.input_tokens` | `gen_ai_client_inference_usage_cache_write_input_tokens_total` |
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_seconds_bucket` |
| `github.copilot.tool.call.count` | `github_copilot_tool_call_count_total` |
| `github.copilot.tool.call.duration` | `github_copilot_tool_call_duration_seconds_bucket` |

The pinned local exporter was tested with `job="github-copilot"` and bounded
`gen_ai_request_model`, `gen_ai_operation_name`, `gen_ai_tool_name` dimensions.
The actual CLI release may emit different instruments or no cache data;
an empty chart does not automatically mean collection failed.

## Privacy, reliability and costs

- The wrapper explicitly sets
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`. Collector
  allowlisting is the second boundary: resource identity/local paths,
  arbitrary scope metadata, span events/links, status text, exception
  messages/stack traces, unapproved attributes and metric exemplars are
  removed. Span names become bounded operation categories.
- Models are a **limited fixed enum** in `otel/collector.yaml`, not a
  dynamically discovered catalog. Unknown/new model values become `other`;
  even valid newer models can be grouped together until reviewed and added.
  Operations, providers, tools and error classifications are also bounded.
  No session, trace, repository or user IDs are metric dimensions.
- Only allowlisted metric names pass. Known token counts, timings and error
  classifications survive; free-form error diagnostics do not. Telemetry
  still reveals operational activity and timestamps: it is not anonymous
  merely because content is excluded. Filtering tests inject sensitive
  sentinels but are not a guarantee against all future CLI schema changes.
- Memory limiter: 128 MiB / 32 MiB spike; batching: 5 seconds, max 256.
  Queue: 256 requests; finite exporter retry window: 300 seconds.
  **Memory-only, lossy buffering**: crashes, shutdown, extended outages and
  queue exhaustion can lose telemetry; there is no durable replay.
- Cloud Trace and Monitoring/Managed Prometheus ingestion and retention can
  cost money. Review current GCP pricing, sample rates, volume, retention and
  label cardinality before use. This configuration does not impose a cloud
  cost cap. Budget alerts notify; **they are not hard spending limits**.
- `.gitignore` excludes `.scratch/`, `.venv/`, `.env*`, local YAML, ADC/key
  files, native binaries/downloads, JSONL telemetry and logs. Ignore rules
  are defense-in-depth, not secret detection. Never force-add such files.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Health unavailable | Collector PID and logs, ports 13133/4318, only one Collector; use foreground startup for diagnosis. |
| Missing project/quota or startup/auth error | Export **both** project variables, correct ADC path outside checkout, current credentials; terminal exports do not change an installed launchd job. |
| Exporter 401/403 or quota failure | ADC identity, enabled APIs, destination `telemetry.writer`, quota `serviceUsageConsumer`, ADC quota project; local 200 does not prove authorization. |
| Verification partial success / malformed response | Receiver logs and schema/config; do not retry blindly or call it cloud success. Trace-only acceptance can precede a metric failure. |
| Empty charts | Exact project/time range, raw series cloud lookup above, ingestion delay, instrument availability, counter samples for `rate()`, translated names and model `other` normalization. |
| Cannot import/view dashboard | Separate dashboard permissions and query/Trace viewer rights, not ingestion roles. |
| Lost telemetry | Memory-only queue/retry limits; inspect local exporter logs. |

## Development checks (no cloud calls)

Runtime scripts use only Python's standard library. Configuration tests require
the sole development dependency, **PyYAML 6.0.3**; install only in an ignored
local virtual environment. Supply the pinned native binary so integration
tests do not skip:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install PyYAML==6.0.3
OTELCOL_BIN="$HOME/.local/bin/otelcol-contrib" \
  .venv/bin/python -m unittest discover -s tests -v
for script in scripts/*.sh; do bash -n "$script"; done
python3 -m json.tool dashboards/copilot-usage.json >/dev/null
python3 -c 'import plistlib; plistlib.load(open("launchd/collector.plist", "rb"))'
GCP_PROJECT_ID=test-destination GCP_QUOTA_PROJECT_ID=test-quota \
  "$HOME/.local/bin/otelcol-contrib" validate --config otel/collector.yaml
```

Tests use scratch directories inside the checkout, fake external commands and
exact-process cleanup. They do not log in, enable APIs, change real IAM,
import real dashboards, install a real user service, or invoke real Copilot.

## Targeted removal

Stop a foreground Collector with Ctrl-C in its terminal. For the optional
service, explicitly unload **only** its label:

```sh
launchctl bootout "gui/$(id -u)/io.github.copilot-otel"
```

Confirm it is unloaded before removing these exact owned files:

```sh
rm "$HOME/Library/LaunchAgents/io.github.copilot-otel.plist"
rm "$HOME/.local/share/gh-copilot-otel/collector.yaml"
rm "$HOME/.local/share/gh-copilot-otel/install-state.json"
```

Retain logs for diagnosis or remove only the two named Collector logs.
Remove `"$HOME/.local/bin/otelcol-contrib"` (or the explicitly selected custom
binary) only if no other application uses it. Do not remove unrelated
LaunchAgents, shared binaries, ADC files, or shell settings. Cloud dashboard
deletion, IAM revocation and API disabling require separate operator decisions;
local uninstall does not delete retained cloud telemetry or stop its storage
costs. Review those resources explicitly rather than broad project cleanup.
