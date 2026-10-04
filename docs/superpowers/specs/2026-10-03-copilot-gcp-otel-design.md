# Copilot CLI telemetry in GCP

## Goal and approved scope

Collect usage and performance telemetry from Copilot CLI on one macOS
machine and visualize it using GCP-native managed services. Do not export
prompts, responses, source code, tool arguments, or tool results. Token
usage is operational telemetry, not an authoritative Copilot billing record.

Publish the reusable artifacts in the public GitHub repository
`miqui/gh-copilot-otel`. Creating and publishing artifacts does not authorize
enabling APIs, modifying IAM, creating dashboards, or installing background
services on the user's behalf.

## Architecture

Copilot CLI sends OTLP/HTTP protobuf to a Collector bound to
`127.0.0.1:4318`. The Collector exports traces and metrics over authenticated
HTTPS to `https://telemetry.googleapis.com`, using the `googleclientauth`
extension and Google Application Default Credentials (ADC).

Cloud Trace provides trace exploration. Cloud Monitoring and Google Cloud
Managed Service for Prometheus store and query metrics. No self-hosted cloud
server, Kubernetes cluster, OpenObserve, Grafana, or Prometheus server is
required. Do not create an application-log pipeline: the CLI's monitoring
help documents traces and metrics, not application-log export.

Use an explicitly pinned OpenTelemetry Collector Contrib release that
includes the necessary components. Provide a native macOS installation
script with release checksum verification, rather than requiring Docker.
Support macOS arm64 and amd64.

## Artifacts

| Path | Responsibility |
| --- | --- |
| `otel/collector.yaml` | Loopback receiver, health endpoint, metadata filtering, resource labels, memory limiting, batching, bounded retries and authenticated GCP export. |
| `scripts/install-collector.sh` | Download and verify the pinned native Collector without root privileges. |
| `scripts/copilot-otel.sh` | Launch Copilot with explicit protobuf OTLP export and content capture disabled; preserve CLI arguments and exit status. |
| `launchd/` and `scripts/install-launchagent.sh` | Template and installer for an opt-in per-user Collector background service. |
| `scripts/setup-gcp.sh` | Explicitly invoked GCP API/IAM preparation using supplied project and principal; no embedded account identifiers. |
| `scripts/import-dashboard.sh` | Import the dashboard into the selected project on explicit invocation. |
| `dashboards/copilot-usage.json` | Cloud Monitoring dashboard using PromQL for tokens, model latency and tool activity. |
| `scripts/verify.sh` | Collector readiness check and content-free synthetic traces and metrics. |
| `tests/` | Automated configuration and script-contract tests, including failure paths. |
| `README.md` | Prerequisites, authentication, foreground and background operation, verification, limitations, costs, troubleshooting and removal. |
| `.gitignore` | Exclude credentials, local settings, downloaded binaries and generated telemetry. |

## Interfaces and authentication

Require a GCP project ID at runtime. ADC is supplied outside the repository.
Document interactive ADC for personal development and service-account
impersonation as the keyless alternative for a restricted identity.
Never generate or commit service-account keys or persist access tokens.

Set the destination resource attribute `gcp.project_id` and configure the
quota project for authentication. GCP setup enables the APIs required by
the Telemetry API and destination services. Grant the selected identity
`roles/telemetry.writer` on the destination project and
`roles/serviceusage.serviceUsageConsumer` on the quota project.
Dashboard creation and viewing permissions are separate from ingestion.

The Collector's health endpoint listens on `127.0.0.1:13133`. Local paths,
project selection and credential locations must work with spaces.
Installation and service operations must not use root privileges, edit
shell startup files, or replace unrelated services.

## Privacy and metric shape

The wrapper explicitly sets
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`.
Collector filtering supplies a second boundary: allowlist operational
attributes and remove span-event content before export. Keep operational
error classification, not arbitrary exception messages or stack traces.
Strip unapproved resource metadata, including local paths and user identity.
Tests must inject sensitive sentinel values and prove they are removed.

Keep metric dimensions bounded: model, operation, tool category/name where
appropriate, and error classification. Do not use session IDs, trace IDs,
repository paths, prompts, or user identities as metric dimensions.

Dashboard queries use the actual Prometheus-translated metric names and
histogram conventions of the selected export path. Automated synthetic
tests check the expected shape; real GCP verification checks actual ingestion
before claiming that a deployed dashboard works.

## Reliability and errors

Use a memory limiter, batching, finite retry duration and a bounded sending
queue. Buffering is not durable: process termination, extended outages or
queue exhaustion can lose telemetry. Document this explicitly.
Exporter failures must be visible in local Collector logs.
Readiness confirms a running Collector, not successful GCP delivery.

Scripts fail clearly for missing commands, credentials, project selection,
invalid input and unsuccessful external operations. Never report successful
cloud delivery based only on an HTTP acceptance response from the Collector.
Cloud budget alerts do not enforce a hard spending limit.

## Acceptance and publication

Validate the pinned Collector configuration with its native validation
command. Exercise wrapper argument preservation, disabled content capture,
GCP command construction, dashboard structure and service installation
using isolated temporary directories and fake external commands.
Run a local Collector integration check for synthetic telemetry and filtering.

Live GCP acceptance is separate and requires supplied project, credentials
and permission to perform cloud operations: synthetic telemetry and a
harmless CLI session must appear in metrics and traces without content.
If unavailable, document that live GCP acceptance has not been performed.

Commit reusable artifacts only, with the required Copilot co-author trailer,
and push to the requested public repository. Never include credentials,
real usage telemetry, machine-specific configuration or unrelated files.

## References

- Installed CLI: `copilot help monitoring` and `copilot help environment`.
- GCP Telemetry API: https://docs.cloud.google.com/stackdriver/docs/reference/telemetry/overview
- Collector: https://opentelemetry.io/docs/collector/
- Google OTLP migration: https://github.com/GoogleCloudPlatform/opentelemetry-operations-go/blob/main/MIGRATION.md
