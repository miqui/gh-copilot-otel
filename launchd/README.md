# Opt-in macOS Collector service

The template is not directly loadable. From this repository, explicitly run
the installer as your signed-in user, never with `sudo`:

```sh
GCP_PROJECT_ID=my-project scripts/install-launchagent.sh
scripts/copilot-otel.sh --version
```

Prerequisites: macOS, Python 3.9+ (checked before installation), `launchctl`,
`curl`, the native Collector installed by `scripts/install-collector.sh`,
and existing Google Application
Default Credentials (ADC) outside the repository. The binary defaults to
`$HOME/.local/bin/otelcol-contrib`; pass the same absolute `INSTALL_DIR` to
both installers if you change it. The binary must also be outside the
repository.

The service installer requires a valid `GCP_PROJECT_ID`.
`GCP_QUOTA_PROJECT_ID` defaults to that destination project; set it explicitly
when the quota project differs. Both must be project IDs, not project numbers.
An explicitly empty quota project is an error.

ADC defaults to
`$HOME/.config/gcloud/application_default_credentials.json`, respecting
`CLOUDSDK_CONFIG` when provided. Alternatively, set an absolute
`GOOGLE_APPLICATION_CREDENTIALS` path outside the repository. Paths with
spaces are supported; symlinks resolving into the repository are rejected.
The installer checks readability and the ADC JSON type, not whether the
identity is authorized or its credentials are current. It never creates,
copies, or prints credentials, requests access tokens, logs in, changes IAM,
or enables APIs. Existing keyless impersonation/external-account ADC is
supported. Do not generate service-account keys for this workflow.

The installer validates the production config using the selected binary,
copies it to `$HOME/.local/share/gh-copilot-otel/collector.yaml`, and serializes
a safely escaped plist with Python `plistlib` at
`$HOME/Library/LaunchAgents/io.github.copilot-otel.plist`.
The service has explicit HOME, minimal PATH, project/quota, and ADC
environment settings; it does not depend on the repository's location or
inherit your terminal's authentication/exporter environment. Keep the
selected user-local binary and external ADC at their installed locations.
Logs go to `$HOME/Library/Logs/copilot-otel/collector.stdout.log` and
`collector.stderr.log`. Config, plist, ownership state, and new log files
are user-private.

Repeated installation is supported. It verifies ownership hashes before
replacing config/plist, restarts only the designated service, and reloads
that job if its arguments/environment/template changed. Unrelated files,
modified installed config/plist, symlink destinations, and another job
using this label are rejected rather than overwritten. Make config changes
in the repository and rerun, not in the installed copy. Do not modify the
ownership state at `.local/share/gh-copilot-otel/install-state.json`.

Bootstrap/kickstart success alone is not readiness: the installer polls
`launchctl print` for a running job with a PID and checks
`http://127.0.0.1:13133/`, bypassing HTTP proxies. Failure exits nonzero;
created artifacts and any started job remain for diagnosis/retry. Check
the stderr log if startup fails. An already-responsive health endpoint
without an owned loaded service is rejected. Local readiness does **not**
verify GCP delivery, credentials/IAM authorization, or dashboard ingestion.
Exporter errors appear in Collector logs. The queue is memory-only:
restarts, extended outages, and queue exhaustion can lose telemetry.

The CLI wrapper is independent of service/project/ADC validation. It sets
loopback OTLP/HTTP protobuf, disables content capture and application-log
export, removes inherited file, per-signal endpoint/protocol, header and
TLS overrides, and `exec`s Copilot with the original arguments. Normal
Copilot authentication and provider/proxy settings remain available.
It does not modify shell startup files or enable telemetry for unwrapped
CLI invocations. Collector filtering remains the second privacy boundary.
By default it resolves `copilot` on PATH. Set `COPILOT_BIN` to select a
different executable path or command name, for example
`COPILOT_BIN="/path with spaces/copilot" scripts/copilot-otel.sh --version`.
An empty, missing, or nonexecutable selection fails explicitly without
falling back to the default CLI.

For removal, explicitly unload only this job:

```sh
launchctl bootout "gui/$(id -u)/io.github.copilot-otel"
```

After confirming that it is unloaded, remove the named plist, installed
config, and ownership state above. Logs may be retained for diagnosis.
Do not delete unrelated LaunchAgents, credentials, or the shared binary.
