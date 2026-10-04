#!/bin/bash
set -euo pipefail
export LC_ALL=C

usage() {
  printf 'Usage: %s DESTINATION_PROJECT_ID QUOTA_PROJECT_ID IAM_PRINCIPAL\n' "$0" >&2
  printf 'IAM_PRINCIPAL: user:EMAIL, serviceAccount:EMAIL, or group:EMAIL\n' >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
destination=$1
quota=$2
principal=$3
project_pattern='^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
principal_pattern='^(user|serviceAccount|group):[A-Za-z0-9._%+-]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?[.][A-Za-z]{2,}$'

if [[ ! $destination =~ $project_pattern ]]; then
  printf 'Invalid destination project ID: use 6-30 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit.\n' >&2
  exit 2
fi
if [[ ! $quota =~ $project_pattern ]]; then
  printf 'Invalid quota project ID: use 6-30 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit.\n' >&2
  exit 2
fi
if [[ ! $principal =~ $principal_pattern ]]; then
  printf 'Invalid IAM principal: use user:EMAIL, serviceAccount:EMAIL, or group:EMAIL; public and other principal forms are not supported.\n' >&2
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  printf 'gcloud is required; install the Google Cloud CLI and configure authorized credentials separately.\n' >&2
  exit 1
fi

run() {
  if "$@"; then
    return 0
  else
    status=$?
    printf 'GCP setup operation failed (exit %s); stopping. Earlier API/IAM changes are not rolled back.\n' "$status" >&2
    exit "$status"
  fi
}

run gcloud services enable telemetry.googleapis.com monitoring.googleapis.com \
  cloudtrace.googleapis.com "--project=$destination" --quiet
run gcloud services enable serviceusage.googleapis.com "--project=$quota" --quiet
run gcloud projects add-iam-policy-binding "$destination" \
  "--member=$principal" --role=roles/telemetry.writer --condition=None --quiet
run gcloud projects add-iam-policy-binding "$quota" \
  "--member=$principal" --role=roles/serviceusage.serviceUsageConsumer \
  --condition=None --quiet

printf 'Setup complete: destination=%s quota=%s. Configure Collector ADC and both project variables separately; no telemetry delivery was checked.\n' "$destination" "$quota"
