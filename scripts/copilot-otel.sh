#!/bin/sh
# Opt-in local telemetry; leave Copilot authentication and arguments intact.
set -eu

copilot_bin=${COPILOT_BIN-copilot}
copilot_bin=$(command -v "$copilot_bin" 2>/dev/null) \
    && [ -f "$copilot_bin" ] && [ -x "$copilot_bin" ] || {
    printf '%s\n' 'copilot-otel: COPILOT_BIN must select an executable (default: copilot)' >&2
    exit 127
}

unset COPILOT_OTEL_FILE_EXPORTER_PATH
for signal in '' _TRACES _METRICS _LOGS; do
    for setting in ENDPOINT PROTOCOL HEADERS CERTIFICATE CLIENT_CERTIFICATE CLIENT_KEY INSECURE; do
        unset "OTEL_EXPORTER_OTLP${signal}_${setting}"
    done
done

export COPILOT_OTEL_ENABLED=true
export COPILOT_OTEL_EXPORTER_TYPE=otlp-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=none
export OTEL_SDK_DISABLED=false

exec "$copilot_bin" "$@"
