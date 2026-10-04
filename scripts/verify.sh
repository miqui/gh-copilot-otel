#!/bin/sh
# Readiness and synthetic ingestion only; never proof of GCP delivery.
set -eu
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'verify: Python 3.9+ is required' >&2
    exit 127
}
directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$directory/send-synthetic.py" --check-health "$@"
