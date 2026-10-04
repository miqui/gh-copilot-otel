#!/bin/bash
set -euo pipefail
export LC_ALL=C

usage() {
  printf 'Usage: %s create PROJECT_ID\n       %s update PROJECT_ID DASHBOARD_ID\n' "$0" "$0" >&2
  printf 'create always creates a new dashboard; update replaces the supplied dashboard without an etag concurrency guard.\n' >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
action=$1
project=$2
case "$action" in
  create) [[ $# -eq 2 ]] || usage ;;
  update) [[ $# -eq 3 ]] || usage ;;
  *) usage ;;
esac
project_pattern='^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
if [[ ! $project =~ $project_pattern ]]; then
  printf 'Invalid project ID: use 6-30 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit.\n' >&2
  exit 2
fi
if [[ $action == update ]]; then
  id_pattern='^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
  if [[ ! $3 =~ $id_pattern ]]; then
    printf 'Invalid dashboard ID: supply the short ID (1-128 letters, digits, underscores or hyphens), not a resource path or option.\n' >&2
    exit 2
  fi
fi

root=$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd)
dashboard="$root/dashboards/copilot-usage.json"
if [[ ! -r $dashboard || ! -f $dashboard ]]; then
  printf 'Bundled dashboard JSON is missing or unreadable: %s\n' "$dashboard" >&2
  exit 1
fi
if ! command -v gcloud >/dev/null 2>&1; then
  printf 'gcloud is required; install the Google Cloud CLI and configure authorized credentials separately.\n' >&2
  exit 1
fi

if [[ $action == create ]]; then
  printf 'Creating a new dashboard; repeated create calls create additional dashboards. Keep the returned ID for update.\n' >&2
  args=(monitoring dashboards create)
else
  printf 'Replacing dashboard %s; no etag concurrency guard is used.\n' "$3" >&2
  args=(monitoring dashboards update "$3")
fi
if gcloud "${args[@]}" "--project=$project" "--config-from-file=$dashboard" --quiet; then
  exit 0
else
  status=$?
  printf 'Dashboard %s failed (exit %s); check gcloud credentials and separate dashboard permissions.\n' "$action" "$status" >&2
  exit "$status"
fi
