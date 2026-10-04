#!/bin/sh
# Explicit opt-in only. No root, shell-profile edits, login, or cloud operations.
set -eu

fail() {
    printf '%s\n' "install-launchagent: $*" >&2
    exit 1
}
for command in python3 uname launchctl curl; do
    command -v "$command" >/dev/null 2>&1 || fail "missing prerequisite: $command"
done
[ "$(uname -s)" = Darwin ] || fail "macOS required"
script_dir=$(CDPATH='' cd -- "${0%/*}" && pwd -P)

exec python3 - "$script_dir/.." <<'PY'
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time


LABEL = "io.github.copilot-otel.collector"
HEALTH = "http://127.0.0.1:13133/"


def fail(message):
    raise RuntimeError(message)


def run(arguments, env=None):
    return subprocess.run(arguments, env=env, capture_output=True, text=True,
                          timeout=30)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_private(path, data):
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
        os.replace(staged, path)
    finally:
        if staged is not None and staged.exists():
            staged.unlink()


def main():
    repo = Path(sys.argv[1]).resolve()
    home_value = os.environ.get("HOME", "")
    if not home_value or not Path(home_value).is_absolute():
        fail("HOME must be an absolute user-local directory")
    home = Path(home_value).resolve(strict=True)
    if os.getuid() == 0:
        fail("run as the signed-in user, not root")
    project = os.environ.get("GCP_PROJECT_ID", "")
    quota = os.environ.get("GCP_QUOTA_PROJECT_ID", project)
    for name, value in [("GCP_PROJECT_ID", project),
                        ("GCP_QUOTA_PROJECT_ID", quota)]:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", value):
            fail(f"{name} must be a valid GCP project ID (6-30 characters)")

    source = repo / "otel/collector.yaml"
    template = repo / "launchd/collector.plist"
    if not source.is_file():
        fail("missing production config: otel/collector.yaml")
    if not template.is_file():
        fail("missing LaunchAgent template: launchd/collector.plist")
    install_dir = Path(os.environ.get("INSTALL_DIR", str(home / ".local/bin")))
    if not install_dir.is_absolute():
        fail("INSTALL_DIR must be absolute")
    binary = (install_dir / "otelcol-contrib").resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        fail("missing executable Collector binary; run scripts/install-collector.sh")
    if binary.is_relative_to(repo):
        fail("Collector binary must be outside the repository")

    default_adc = Path(os.environ.get("CLOUDSDK_CONFIG", str(home / ".config/gcloud")))
    adc_value = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        str(default_adc / "application_default_credentials.json"),
    )
    if not adc_value or not Path(adc_value).is_absolute():
        fail("credentials path must be absolute")
    adc = Path(adc_value).resolve()
    if adc.is_relative_to(repo):
        fail("ADC credentials must remain outside the repository")
    if not adc.is_file() or not os.access(adc, os.R_OK):
        fail("missing readable ADC credentials; configure ADC outside the repository")
    try:
        credentials = json.loads(adc.read_text())
    except (ValueError, UnicodeError):
        fail("ADC credentials must be valid JSON")
    if not isinstance(credentials, dict) or credentials.get("type") not in {
        "authorized_user", "service_account", "external_account",
        "external_account_authorized_user", "impersonated_service_account",
    }:
        fail("unsupported ADC credentials type")

    service_dir = home / ".local/share/copilot-otel"
    config = service_dir / "collector.yaml"
    state = service_dir / "install-state.json"
    plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
    logs = home / "Library/Logs/copilot-otel"
    stdout = logs / "collector.stdout.log"
    stderr = logs / "collector.stderr.log"
    paths = [config, state, plist, stdout, stderr]
    for path in paths:
        for component in [path, *path.parents]:
            if component == home:
                break
            if component.is_symlink():
                fail(f"refusing symlink service destination: {component}")
        if path.exists() and not path.is_file():
            fail(f"unrelated non-file service destination: {path}")

    owned = False
    if state.exists():
        try:
            previous = json.loads(state.read_text())
        except (ValueError, UnicodeError):
            fail("unrelated or invalid installation state")
        if (not isinstance(previous, dict) or previous.get("schema") != 1
                or set(previous) != {"schema", "config_sha256", "plist_sha256"}):
            fail("unrelated installation state")
        for path, key in [(config, "config_sha256"), (plist, "plist_sha256")]:
            if not path.is_file() or digest(path.read_bytes()) != previous[key]:
                fail(f"modified or missing owned artifact; refusing overwrite: {path}")
        owned = True
    elif any(path.exists() for path in [config, plist, stdout, stderr]):
        fail("unrelated service files exist; refusing overwrite")

    service_env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin",
        "GCP_PROJECT_ID": project, "GCP_QUOTA_PROJECT_ID": quota,
        "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
    }
    validation = run([str(binary), "validate", "--config", str(source)],
                     env=service_env)
    if validation.returncode:
        # Collector validation does not ingest telemetry; preserve its diagnostic.
        fail("Collector config validation failed:\n" + validation.stderr.strip())

    launchctl = shutil.which("launchctl")
    curl = shutil.which("curl")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"

    def service():
        result = run([launchctl, "print", target])
        if result.returncode == 113:
            return None
        if result.returncode:
            fail("launchctl print failed: " + result.stderr.strip())
        path = re.search(r"^\s*path = (.+)$", result.stdout, re.MULTILINE)
        if not path or path.group(1).strip() != str(plist):
            fail("unrelated loaded service uses the designated label")
        return result.stdout

    def healthy():
        return run([curl, "--fail", "--silent", "--show-error", "--noproxy", "*",
                    "--connect-timeout", "1", "--max-time", "2", HEALTH]).returncode == 0

    existing = service()
    if existing is not None and not owned:
        fail("unrelated loaded service; no owned installation state")
    if existing is None and healthy():
        fail("health endpoint already served by another process; refusing start")

    with template.open("rb") as stream:
        agent = plistlib.load(stream)
    if agent.get("Label") != LABEL:
        fail("template label does not match the designated service")
    agent["ProgramArguments"] = [str(binary), "--config", str(config)]
    agent["EnvironmentVariables"] = service_env
    agent["StandardOutPath"] = str(stdout)
    agent["StandardErrorPath"] = str(stderr)
    config_data = source.read_bytes()
    plist_data = plistlib.dumps(agent, sort_keys=False)
    # kickstart rereads config, but launchd caches the job's arguments/environment.
    if existing is not None and previous["plist_sha256"] != digest(plist_data):
        result = run([launchctl, "bootout", target])
        if result.returncode:
            fail("launchctl bootout failed: " + result.stderr.strip())
        existing = None
    for directory in [service_dir, plist.parent, logs]:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in [stdout, stderr]:
        if not path.exists():
            path.touch(mode=0o600)
    write_private(config, config_data)
    write_private(plist, plist_data)
    write_private(state, json.dumps({
        "schema": 1, "config_sha256": digest(config_data),
        "plist_sha256": digest(plist_data),
    }).encode())

    if existing is None:
        result = run([launchctl, "bootstrap", domain, str(plist)])
        if result.returncode:
            fail("launchctl bootstrap failed: " + result.stderr.strip())
    result = run([launchctl, "kickstart", "-k", target])
    if result.returncode:
        fail("launchctl kickstart failed: " + result.stderr.strip())

    running = False
    for attempt in range(10):
        status = service()
        running = bool(status and re.search(
            r"^\s*state = running\s*$", status, re.MULTILINE
        ) and re.search(r"^\s*pid = [1-9][0-9]*\s*$", status, re.MULTILINE))
        if running and healthy():
            print("Local Collector ready; GCP delivery not verified.")
            print(f"Service: {target}\nLogs: {logs}")
            return
        if attempt < 9:
            time.sleep(0.2)
    fail(f"Collector {'health check failed' if running else 'not running'}; "
         f"inspect {stderr}; artifacts retained for diagnosis and retry")


try:
    main()
except (RuntimeError, OSError, ValueError, plistlib.InvalidFileException,
        subprocess.TimeoutExpired) as error:
    print(f"install-launchagent: {error}", file=sys.stderr)
    sys.exit(1)
PY
