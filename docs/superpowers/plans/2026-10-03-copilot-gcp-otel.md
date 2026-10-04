# Copilot GCP telemetry implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish reusable, privacy-filtered Copilot CLI telemetry artifacts for macOS and GCP.

**Architecture:** A native local Collector accepts OTLP/HTTP from an opt-in
Copilot wrapper. It filters metadata and exports metrics and traces through
Google's authenticated Telemetry API to managed observability services.

**Tech Stack:** OpenTelemetry Collector Contrib 0.162.0, Bash, Python 3
standard library for scripts/tests, PyYAML for configuration integration
tests, Google Cloud CLI, macOS launchd, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-10-03-copilot-gcp-otel-design.md`

## Global constraints

- Receive OTLP/HTTP on `127.0.0.1:4318`.
- Serve Collector health on `127.0.0.1:13133`.
- Export to `https://telemetry.googleapis.com`.
- Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`.
- Grant ingestion identities `roles/telemetry.writer` and quota-project
  identities `roles/serviceusage.serviceUsageConsumer`.
- Support native macOS arm64 and amd64 without root privileges.
- Keep credentials, downloaded binaries, local configuration and telemetry
  out of the public `miqui/gh-copilot-otel` repository.
- Do not apply machine or GCP changes automatically.
- Treat live GCP acceptance separately from local verification.

## Review focus

- Paths containing spaces: wrapper, installer and LaunchAgent preserve paths.
- Inherited exporter overrides: wrapper prevents alternate endpoints,
  file export, authentication headers and content capture from bypassing
  the intended local path.
- Sensitive data outside attributes: remove span events, links, arbitrary
  status descriptions, scope metadata and dynamic names where necessary.
- Missing credentials and commands: fail visibly, without cloud-success claims.
- Failed downloads or occupied ports: do not install corrupt binaries or
  leave background test processes running.

## File structure and interfaces

`otel/collector.yaml` is the production configuration. Runtime selection uses
`GCP_PROJECT_ID`, optional `GCP_QUOTA_PROJECT_ID` (default destination),
and optional `GOOGLE_APPLICATION_CREDENTIALS`. Local Collector files live
under `~/.local/share/gh-copilot-otel`; this is not a credential store.

The wrapper consumes an optional `COPILOT_BIN` executable path and passes
all positional arguments unchanged. The service installer consumes
`GCP_PROJECT_ID` and ADC, writes a generated plist outside the repository,
and starts only the service label `io.github.copilot-otel`.

Scripts that modify GCP require explicit project and IAM principal
arguments. The dashboard importer prints the resulting dashboard identifier.
The verification script reports local ingestion acceptance and explains how
to confirm cloud delivery separately.

### Task 1: Collector pipeline and privacy boundary

**Files:** `otel/collector.yaml`, `scripts/install-collector.sh`,
`requirements-dev.txt`, `.gitignore`, `tests/test_collector.py`,
`tests/test_install_collector.py`.

**Interfaces:** Produce the pinned native Collector and configuration;
tests accept `OTELCOL_BIN` for an externally supplied pinned binary.

- [ ] Write failing tests that inject sensitive sentinel values into resources,
  scope metadata, span names/events/links/status descriptions and metric
  attributes; assert none survive export while token counts and durations do.
- [ ] Write installer tests for arm64/amd64 asset selection, checksum failure,
  paths containing spaces and unsupported platforms.
- [ ] Run `python3 -m unittest discover -s tests -v`; confirm relevant failures.
- [ ] Add PyYAML as the sole development dependency and ignore generated
  artifacts and local credential/configuration files.
- [ ] Implement checksum-verified user-local Collector installation from
  release `v0.162.0`; download to a temporary directory before replacement.
- [ ] Configure `googleclientauth` with destination and quota projects,
  HTTPS OTLP/protobuf export, loopback endpoints, privacy transforms with
  `error_mode: propagate`, and no application-log pipeline.
- [ ] Configure a 128 MiB memory limit with a 32 MiB spike allowance,
  5-second batching, 256-item maximum batches, a 256-batch sending queue
  and a 300-second maximum export retry period.
- [ ] Validate the production configuration using the pinned binary.
  In tests, use the same processors with a local file exporter and
  synthetic OTLP/HTTP JSON input to inspect filtered output.
- [ ] Run installer and Collector tests and commit this independently
  testable deliverable with the Copilot co-author trailer.

### Task 2: CLI wrapper and macOS background operation

**Files:** `scripts/copilot-otel.sh`, `launchd/collector.plist`,
`scripts/install-launchagent.sh`, `tests/test_wrapper.py`,
`tests/test_launchagent.py`.

**Interfaces:** Consume Task 1's binary/configuration; preserve the user's
Copilot argument vector and exit status; generate a user-local LaunchAgent.

- [ ] Write failing tests using fake Copilot and launchctl commands for
  argument preservation, exit status, spaces in paths, inherited conflicting
  exporter settings, missing prerequisites and unsuccessful service startup.
- [ ] Run the corresponding test modules and confirm relevant failures.
- [ ] Implement an opt-in wrapper that sets the local endpoint, protobuf
  protocol and disabled content capture, removes conflicting per-signal
  exporter/file/TLS overrides, and uses `exec` to preserve exit status.
- [ ] Implement the plist template and installer with Python's `plistlib`
  for safe path escaping, explicit environment variables, user-local logs,
  and launchctl bootstrap/kickstart restricted to the designated label.
- [ ] Validate prerequisites and refuse to overwrite unrelated files;
  document and test repeated installation behavior.
- [ ] Run wrapper/service tests and plist validation, then commit with
  the Copilot co-author trailer.

### Task 3: Explicit GCP setup and dashboard

**Files:** `scripts/setup-gcp.sh`, `scripts/import-dashboard.sh`,
`dashboards/copilot-usage.json`, `tests/test_gcp_scripts.py`,
`tests/test_dashboard.py`.

**Interfaces:** Require explicit project/principal inputs; consume native
OTLP metric names; produce an importable Cloud Monitoring dashboard.

- [ ] Write failing tests with a fake gcloud executable to check API
  enablement, exact IAM roles, project/quota selection, invalid or missing
  inputs and propagation of unsuccessful cloud commands.
- [ ] Write dashboard tests asserting valid JSON, valid chart structures,
  token/model-latency/tool panels and bounded label grouping.
- [ ] Run the corresponding test modules and confirm relevant failures.
- [ ] Implement explicit setup for Telemetry, Monitoring and Trace APIs,
  quota-project Service Usage and the two specified ingestion IAM roles.
  Do not create keys, log in, deploy services or change billing.
- [ ] Implement explicit dashboard import using gcloud.
- [ ] Build PromQL dashboard panels for input/output/cache token rates,
  model latency percentiles, tool call rates and tool duration.
  Confirm translated metric names and suffixes using the pinned
  Prometheus translation behavior; document live-GCP verification limits.
- [ ] Run GCP script/dashboard tests and commit with the co-author trailer.

### Task 4: Verification, documentation and publication

**Files:** `scripts/verify.sh`, `scripts/send-synthetic.py`,
`tests/test_verify.py`, `README.md`.

**Interfaces:** Send only synthetic content-free traces and metrics to the
local receiver; report trace identifiers for separate Cloud Trace lookup.

- [ ] Write failing tests for unavailable health/ingestion endpoints,
  OTLP partial-success responses and valid synthetic payload shapes.
- [ ] Run the corresponding test modules and confirm relevant failures.
- [ ] Implement bounded readiness/HTTP timeouts and synthetic trace/metric
  generation with fresh identifiers and nanosecond timestamps.
- [ ] Explain that local acceptance is not proof of cloud storage;
  distinguish synthetic telemetry from actual Copilot usage.
- [ ] Document foreground setup, ADC and impersonation, dashboard import,
  opt-in background service, health checks, errors, metric naming,
  metadata limitations, cost exposure and targeted uninstall.
- [ ] Run all tests, Bash syntax checks, JSON/plist validation and the
  pinned Collector's configuration/integration checks.
- [ ] Review the complete branch for correctness and accidental secrets.
  Do not run live GCP changes without a separate authorization.
- [ ] Commit artifacts with the co-author trailer.
- [ ] Create the public repository using
  `gh repo create miqui/gh-copilot-otel --public --source=. --remote=origin --push`.
  If the name already exists, stop rather than attaching to an unrelated repo.
- [ ] Confirm GitHub's default branch commit matches local `HEAD` and
  report the repository URL with any unperformed live-GCP acceptance.
