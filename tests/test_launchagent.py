"""Installer artifact tests: fake launchctl/health, isolated HOME, no cloud."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-launchagent.sh"
LABEL = "io.github.copilot-otel"


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="service test & spaces ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.home = self.work / "user home <&>"
        self.home.mkdir()
        self.tools = self.work / "fake tools"
        self.tools.mkdir()
        self.repo = self.work / "repository with spaces & xml"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "otel").mkdir()
        (self.repo / "launchd").mkdir()
        self.config = self.repo / "otel" / "collector.yaml"
        shutil.copyfile(ROOT / "otel" / "collector.yaml", self.config)
        self.installer = self.repo / "scripts" / SCRIPT.name
        self.binary = self.home / ".local" / "bin" / "otelcol-contrib"
        self.binary.parent.mkdir(parents=True)
        self.events = self.work / "events.jsonl"
        self.loaded = self.work / "loaded"
        self.running = self.work / "running"
        self.credentials = self.home / ".config/gcloud/application_default_credentials.json"
        self.credentials.parent.mkdir(parents=True)
        self.credentials.write_text(json.dumps({
            "type": "authorized_user", "client_id": "fixture",
            "client_secret": "fixture", "refresh_token": "fixture",
        }))
        self.plist = self.home / "Library/LaunchAgents" / (LABEL + ".plist")
        self.stable = self.home / ".local/share/gh-copilot-otel/collector.yaml"
        self.target = f"gui/{os.getuid()}/{LABEL}"
        self.env = {
            "PATH": str(self.tools), "HOME": str(self.home),
            "GCP_PROJECT_ID": "test-destination",
            "EVENTS": str(self.events), "LOADED": str(self.loaded),
            "RUNNING": str(self.running), "PLIST": str(self.plist),
        }
        (self.tools / "python3").symlink_to(sys.executable)
        self.tool("uname", "print('Darwin')\n")
        self.tool("launchctl", """
args = sys.argv[1:]
if args[0] == 'print':
    if not Path(os.environ['LOADED']).exists():
        sys.exit(113)
    print('path = ' + os.environ.get('SERVICE_PATH', os.environ['PLIST']))
    if Path(os.environ['RUNNING']).exists() and not os.environ.get('NOT_RUNNING'):
        print('state = running\\n pid = 12345')
    else:
        print('state = exited\\n last exit code = 1')
elif args[0] == 'bootstrap':
    if os.environ.get('FAIL_BOOTSTRAP'):
        sys.exit(5)
    import plistlib
    agent = plistlib.loads(Path(args[2]).read_bytes())
    Path(os.environ['LOADED']).write_text(json.dumps(agent))
elif args[0] == 'bootout':
    if os.environ.get('FAIL_BOOTOUT'):
        sys.exit(5)
    Path(os.environ['LOADED']).unlink()
    if Path(os.environ['RUNNING']).exists():
        Path(os.environ['RUNNING']).unlink()
elif args[0] == 'kickstart':
    if os.environ.get('FAIL_KICKSTART'):
        sys.exit(5)
    Path(os.environ['RUNNING']).touch()
else:
    sys.exit(99)
""")
        self.tool("curl", """
if os.environ.get('BUSY_HEALTH'):
    sys.exit(0)
if (not Path(os.environ['RUNNING']).exists()
        or os.environ.get('FAIL_HEALTH')):
    sys.exit(7)
print('{"status":"Server available"}')
""")
        self.tool_at(self.binary, f"""
if sys.argv[1:] != ['validate', '--config', {str(self.config)!r}]:
    sys.exit(98)
if Path({str(self.work / 'fail-validation')!r}).exists():
    sys.exit(1)
""")

    def tool_at(self, path, body):
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            f"with open({str(self.events)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps([Path(sys.argv[0]).name, "
            "*sys.argv[1:]]) + '\\n')\n" + body
        )
        path.chmod(0o755)

    def tool(self, name, body):
        self.tool_at(self.tools / name, body)

    def run_installer(self):
        self.assertTrue(SCRIPT.is_file(), "Task 2 service installer is missing")
        shutil.copyfile(SCRIPT, self.installer)
        template = ROOT / "launchd/collector.plist"
        if template.exists():
            shutil.copyfile(template, self.repo / "launchd/collector.plist")
        return subprocess.run(
            ["/bin/sh", str(self.installer)], env=self.env,
            text=True, capture_output=True, timeout=15,
        )

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def assert_failed(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message.lower(), result.stderr.lower())
        self.assertNotIn("ready", result.stdout.lower())

    def test_install_generates_escaped_stable_private_artifacts_and_checks_health(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist["Label"], LABEL)
        self.assertEqual(plist["ProgramArguments"],
                         [str(self.binary), "--config", str(self.stable)])
        self.assertEqual(plist["EnvironmentVariables"], {
            "HOME": str(self.home), "PATH": "/usr/bin:/bin",
            "GCP_PROJECT_ID": "test-destination",
            "GCP_QUOTA_PROJECT_ID": "test-destination",
            "GOOGLE_APPLICATION_CREDENTIALS": str(self.credentials),
        })
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        for key, name in [("StandardOutPath", "collector.stdout.log"),
                          ("StandardErrorPath", "collector.stderr.log")]:
            self.assertEqual(plist[key],
                             str(self.home / "Library/Logs/copilot-otel" / name))
        self.assertEqual(self.stable.read_bytes(), self.config.read_bytes())
        plutil = shutil.which("plutil")
        if plutil:
            lint = subprocess.run([plutil, "-lint", str(self.plist)],
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        for path in [self.plist, self.stable]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        calls = self.calls()
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}",
                       str(self.plist)], calls)
        self.assertIn(["launchctl", "kickstart", "-k", self.target], calls)
        self.assertIn("Local Collector ready", result.stdout)
        self.assertIn("not verified", result.stdout)
        health = [call for call in calls if call[0] == "curl"]
        self.assertTrue(health)
        self.assertTrue(all(call[-1] == "http://127.0.0.1:13133/" for call in health))
        self.assertTrue(all("--noproxy" in call for call in health))
        shutil.rmtree(self.repo)
        self.assertTrue(Path(plist["ProgramArguments"][2]).is_file())

    def test_explicit_external_adc_and_quota_project(self):
        external = self.work / "external ADC & spaces.json"
        self.credentials.rename(external)
        self.env.update(GOOGLE_APPLICATION_CREDENTIALS=str(external),
                        GCP_QUOTA_PROJECT_ID="test-quota")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(env["GOOGLE_APPLICATION_CREDENTIALS"], str(external))
        self.assertEqual(env["GCP_QUOTA_PROJECT_ID"], "test-quota")
        self.assertFalse(any(p.suffix == ".json" and "credentials" in p.name
                             for p in self.repo.rglob("*")))

    def test_cloudsdk_config_adc_is_explicitly_resolved(self):
        directory = self.work / "gcloud config"
        directory.mkdir()
        self.credentials.rename(directory / self.credentials.name)
        self.env["CLOUDSDK_CONFIG"] = str(directory)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(env["GOOGLE_APPLICATION_CREDENTIALS"],
                         str(directory / self.credentials.name))

    def test_repeated_install_restarts_only_owned_service_and_updates_config(self):
        self.assertEqual(self.run_installer().returncode, 0)
        self.config.write_text(self.config.read_text() + "\n# updated fixture\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stable.read_bytes(), self.config.read_bytes())
        calls = self.calls()
        self.assertEqual(sum(c[:2] == ["launchctl", "bootstrap"] for c in calls), 1)
        self.assertEqual(sum(c[:2] == ["launchctl", "kickstart"] for c in calls), 2)

    def test_repeat_reloads_changed_quota_and_adc_environment(self):
        self.assertEqual(self.run_installer().returncode, 0)
        external = self.work / "replacement ADC.json"
        shutil.copyfile(self.credentials, external)
        self.env.update(GCP_QUOTA_PROJECT_ID="new-quota",
                        GOOGLE_APPLICATION_CREDENTIALS=str(external))
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = json.loads(self.loaded.read_text())["EnvironmentVariables"]
        self.assertEqual(loaded["GCP_QUOTA_PROJECT_ID"], "new-quota")
        self.assertEqual(loaded["GOOGLE_APPLICATION_CREDENTIALS"], str(external))
        self.assertIn(["launchctl", "bootout", self.target], self.calls())
        self.assertEqual(sum(c[:2] == ["launchctl", "bootstrap"]
                             for c in self.calls()), 2)

    def test_failed_reload_does_not_restart_old_environment(self):
        self.assertEqual(self.run_installer().returncode, 0)
        self.env.update(GCP_QUOTA_PROJECT_ID="new-quota", FAIL_BOOTOUT="1")
        self.assert_failed(self.run_installer(), "bootout")
        self.assertEqual(sum(c[:2] == ["launchctl", "kickstart"]
                             for c in self.calls()), 1)

    def test_nonexecutable_binary_and_invalid_paths_are_rejected(self):
        self.binary.chmod(0o600)
        self.assert_failed(self.run_installer(), "binary")
        self.binary.chmod(0o755)
        for key, value in [("HOME", ""), ("HOME", "relative-home"),
                           ("INSTALL_DIR", ""), ("INSTALL_DIR", "relative-bin"),
                           ("GOOGLE_APPLICATION_CREDENTIALS", ""),
                           ("GOOGLE_APPLICATION_CREDENTIALS", "relative-adc")]:
            with self.subTest(key=key, value=value):
                prior = self.env.get(key)
                self.env[key] = value
                self.assert_failed(self.run_installer(), "absolute")
                self.assertFalse(self.plist.exists())
                if prior is None:
                    self.env.pop(key)
                else:
                    self.env[key] = prior

    def test_unexpected_launchctl_print_error_is_not_missing_service(self):
        self.tool("launchctl", "print('print fixture failed', file=sys.stderr)\nsys.exit(5)\n")
        self.assert_failed(self.run_installer(), "print")
        self.assertFalse(self.plist.exists())

    def test_parent_directory_symlink_cannot_redirect_service_writes(self):
        external = self.work / "outside launchagents"
        external.mkdir()
        self.plist.parent.parent.mkdir(parents=True)
        self.plist.parent.symlink_to(external, target_is_directory=True)
        self.assert_failed(self.run_installer(), "symlink")
        self.assertEqual(list(external.iterdir()), [])

    def test_missing_commands_fail_before_service_operations(self):
        for command in ["python3", "uname", "launchctl", "curl"]:
            with self.subTest(command=command):
                path = self.tools / command
                hidden = self.tools / (command + ".disabled")
                path.rename(hidden)
                try:
                    result = self.run_installer()
                    self.assert_failed(result, command)
                    self.assertFalse(self.plist.exists())
                    self.assertFalse(self.loaded.exists())
                finally:
                    hidden.rename(path)

    def test_old_python_has_actionable_error_before_path_api_or_service_use(self):
        (self.tools / "python3").unlink()
        self.tool("python3", """
sys.version_info = (3, 8, 20)
def unavailable_path_api(*args):
    raise AttributeError('Path.is_relative_to unavailable on Python 3.8')
Path.is_relative_to = unavailable_path_api
if sys.argv[1] == '-c':
    exec(sys.argv[2])
elif sys.argv[1] == '-':
    sys.argv = sys.argv[1:]
    exec(sys.stdin.read())
else:
    sys.exit(99)
""")
        result = self.run_installer()
        self.assert_failed(result, "Python 3.9+")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.stable.exists())
        self.assertFalse(any(c[0] in {"otelcol-contrib", "launchctl", "curl"}
                             for c in self.calls()))

    def test_missing_binary_or_config_or_template(self):
        for path, diagnostic in [(self.binary, "binary"), (self.config, "config")]:
            with self.subTest(path=path):
                saved = path.with_suffix(".saved")
                path.rename(saved)
                try:
                    self.assert_failed(self.run_installer(), diagnostic)
                    self.assertFalse(self.plist.exists())
                finally:
                    saved.rename(path)
        self.assertTrue(SCRIPT.is_file(), "Task 2 service installer is missing")
        shutil.copyfile(SCRIPT, self.installer)
        (self.repo / "launchd/collector.plist").unlink()
        result = subprocess.run(["/bin/sh", str(self.installer)], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assert_failed(result, "template")

    def test_project_selection_is_required_and_validated(self):
        for key, value in [("GCP_PROJECT_ID", ""), ("GCP_PROJECT_ID", "bad project"),
                           ("GCP_PROJECT_ID", "-option"),
                           ("GCP_QUOTA_PROJECT_ID", ""),
                           ("GCP_QUOTA_PROJECT_ID", "bad/quota")]:
            with self.subTest(key=key, value=value):
                previous = self.env.get(key)
                self.env[key] = value
                self.assert_failed(self.run_installer(), key)
                self.assertFalse(self.plist.exists())
                if previous is None:
                    self.env.pop(key)
                else:
                    self.env[key] = previous

    def test_missing_invalid_or_repository_credentials_are_rejected(self):
        for content in [None, "not-json", "{}", '{"type":"unknown"}']:
            with self.subTest(content=content):
                if self.credentials.exists():
                    self.credentials.unlink()
                if content is not None:
                    self.credentials.write_text(content)
                self.assert_failed(self.run_installer(), "credentials")
                self.assertFalse(self.plist.exists())
        self.credentials.write_text('{"type":"authorized_user"}')
        inside = self.repo / "adc.json"
        self.credentials.rename(inside)
        self.env["GOOGLE_APPLICATION_CREDENTIALS"] = str(inside)
        self.assert_failed(self.run_installer(), "outside")
        link = self.work / "outside-link.json"
        link.symlink_to(inside)
        self.env["GOOGLE_APPLICATION_CREDENTIALS"] = str(link)
        self.assert_failed(self.run_installer(), "outside")

    def test_collector_validation_failure_prevents_installation(self):
        (self.work / "fail-validation").touch()
        self.assert_failed(self.run_installer(), "validation")
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.loaded.exists())

    def test_unrelated_plist_or_config_is_never_overwritten(self):
        for path in [self.plist, self.stable]:
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unrelated file\n")
                self.assert_failed(self.run_installer(), "unrelated")
                self.assertEqual(path.read_text(), "unrelated file\n")
                self.assertFalse(self.loaded.exists())
                path.unlink()

    def test_modified_owned_artifacts_are_not_overwritten(self):
        self.assertEqual(self.run_installer().returncode, 0)
        for path in [self.plist, self.stable]:
            with self.subTest(path=path):
                saved = path.read_bytes()
                path.write_text("user modification\n")
                self.assert_failed(self.run_installer(), "modified")
                self.assertEqual(path.read_text(), "user modification\n")
                path.write_bytes(saved)

    def test_symlink_destination_is_refused(self):
        external = self.work / "unrelated"
        external.write_text("keep\n")
        self.plist.parent.mkdir(parents=True)
        self.plist.symlink_to(external)
        self.assert_failed(self.run_installer(), "symlink")
        self.assertEqual(external.read_text(), "keep\n")
        self.assertFalse(self.loaded.exists())

    def test_unrelated_loaded_service_is_refused(self):
        self.loaded.touch()
        self.env["SERVICE_PATH"] = "/some/unrelated.plist"
        self.assert_failed(self.run_installer(), "unrelated")
        self.assertFalse(self.plist.exists())
        self.assertFalse(any(c[:2] == ["launchctl", "kickstart"] for c in self.calls()))

    def test_existing_health_endpoint_is_not_mistaken_for_our_service(self):
        self.env["BUSY_HEALTH"] = "1"
        self.assert_failed(self.run_installer(), "health")
        self.assertFalse(self.loaded.exists())

    def test_startup_failures_do_not_claim_success(self):
        for setting, diagnostic in [
            ("FAIL_BOOTSTRAP", "bootstrap"), ("FAIL_KICKSTART", "kickstart"),
            ("NOT_RUNNING", "running"), ("FAIL_HEALTH", "health"),
        ]:
            with self.subTest(setting=setting):
                self.env[setting] = "1"
                self.assert_failed(self.run_installer(), diagnostic)
                self.env.pop(setting)
                for path in [self.loaded, self.running]:
                    if path.exists():
                        path.unlink()

    def test_custom_install_dir_and_platform_validation(self):
        custom = self.work / "custom bin & spaces"
        custom.mkdir()
        self.binary.rename(custom / self.binary.name)
        self.env["INSTALL_DIR"] = str(custom)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist["ProgramArguments"][0], str(custom / self.binary.name))
        self.tool("uname", "print('Linux')\n")
        self.assert_failed(self.run_installer(), "macOS")


if __name__ == "__main__":
    unittest.main()
