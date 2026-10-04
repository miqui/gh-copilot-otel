"""Cloud command contracts; every gcloud invocation is an isolated fake."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch" / "task-3"
DESTINATION = "test-destination"
QUOTA = "test-quota"
MEMBER = "serviceAccount:writer@test-destination.iam.gserviceaccount.com"


class GCPScriptTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="gcp scripts ", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.tools = self.work / "fake tools"
        self.tools.mkdir()
        self.events = self.work / "events.jsonl"
        self.env = {
            "PATH": str(self.tools), "HOME": str(self.work),
            "EVENTS": str(self.events),
            # Ambient values must never substitute for explicit arguments.
            "CLOUDSDK_CORE_PROJECT": "wrong-project",
            "GCP_PROJECT_ID": "wrong-destination",
            "GCP_QUOTA_PROJECT_ID": "wrong-quota",
        }
        fake = self.tools / "gcloud"
        fake.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys
events = Path(os.environ['EVENTS'])
with events.open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if len(events.read_text().splitlines()) == int(os.environ.get('FAIL_CALL', '0')):
    print('simulated gcloud permission/credential failure', file=sys.stderr)
    sys.exit(17)
print('fake cloud operation completed')
""")
        fake.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()
                ] if self.events.exists() else []

    def run_script(self, name, *args, **env):
        script = ROOT / "scripts" / name
        self.assertTrue(script.is_file(), f"Missing Task 3 script: {name}")
        self.assertTrue(os.access(script, os.X_OK), f"{name} is not executable")
        return subprocess.run(
            ["/bin/bash", str(script), *args],
            env={**self.env, **env}, capture_output=True, text=True, timeout=15)

    def expected_setup(self, quota=QUOTA, member=MEMBER):
        return [
            ["services", "enable", "telemetry.googleapis.com",
             "monitoring.googleapis.com", "cloudtrace.googleapis.com",
             f"--project={DESTINATION}", "--quiet"],
            ["services", "enable", "serviceusage.googleapis.com",
             f"--project={quota}", "--quiet"],
            ["projects", "add-iam-policy-binding", DESTINATION,
             f"--member={member}", "--role=roles/telemetry.writer",
             "--condition=None", "--quiet"],
            ["projects", "add-iam-policy-binding", quota,
             f"--member={member}", "--role=roles/serviceusage.serviceUsageConsumer",
             "--condition=None", "--quiet"],
        ]

    def test_setup_selects_explicit_projects_and_only_ingestion_roles(self):
        result = self.run_script("setup-gcp.sh", DESTINATION, QUOTA, MEMBER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), self.expected_setup())

    def test_setup_supports_same_project_and_user_or_group(self):
        for member in ("user:person@example.com", "group:team@example.com"):
            with self.subTest(member=member):
                self.events.unlink(missing_ok=True)
                result = self.run_script(
                    "setup-gcp.sh", DESTINATION, DESTINATION, member)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), self.expected_setup(DESTINATION, member))

    def test_setup_rejects_missing_extra_and_invalid_inputs_before_cloud_calls(self):
        cases = [
            ((), "Usage:"),
            ((DESTINATION, QUOTA), "Usage:"),
            ((DESTINATION, QUOTA, MEMBER, "extra"), "Usage:"),
            (("", QUOTA, MEMBER), "destination project"),
            (("Uppercase", QUOTA, MEMBER), "destination project"),
            (("abcde", QUOTA, MEMBER), "destination project"),
            (("a" * 31, QUOTA, MEMBER), "destination project"),
            ((DESTINATION, "-bad-option", MEMBER), "quota project"),
            ((DESTINATION, "invalid project", MEMBER), "quota project"),
            ((DESTINATION, "project-", MEMBER), "quota project"),
            ((DESTINATION, QUOTA, ""), "IAM principal"),
            ((DESTINATION, QUOTA, "allUsers"), "IAM principal"),
            ((DESTINATION, QUOTA, "user:not-an-email"), "IAM principal"),
            ((DESTINATION, QUOTA, "user:a@example.com\n--role=roles/owner"),
             "IAM principal"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_script("setup-gcp.sh", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_setup_stops_at_each_failed_cloud_command_and_preserves_status(self):
        for call in range(1, 5):
            with self.subTest(call=call):
                self.events.unlink(missing_ok=True)
                result = self.run_script(
                    "setup-gcp.sh", DESTINATION, QUOTA, MEMBER, FAIL_CALL=str(call))
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertEqual(self.calls(), self.expected_setup()[:call])
                self.assertIn("simulated gcloud", result.stderr)
                self.assertIn("failed", result.stderr.lower())
                self.assertNotIn("Setup complete", result.stdout)

    def test_missing_gcloud_is_readable_for_both_scripts(self):
        (self.tools / "gcloud").unlink()
        for name, args in [
            ("setup-gcp.sh", (DESTINATION, QUOTA, MEMBER)),
            ("import-dashboard.sh", ("create", DESTINATION)),
        ]:
            with self.subTest(name=name):
                result = self.run_script(name, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("gcloud", result.stderr)
                self.assertIn("required", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_import_create_is_explicit_and_repeat_creates_again(self):
        for _ in range(2):
            result = self.run_script("import-dashboard.sh", "create", DESTINATION)
            self.assertEqual(result.returncode, 0, result.stderr)
        expected = ["monitoring", "dashboards", "create",
                    f"--project={DESTINATION}",
                    f"--config-from-file={ROOT / 'dashboards/copilot-usage.json'}",
                    "--quiet"]
        self.assertEqual(self.calls(), [expected, expected])
        self.assertIn("new dashboard", result.stderr.lower())

    def test_import_update_targets_only_the_supplied_dashboard_id(self):
        result = self.run_script(
            "import-dashboard.sh", "update", DESTINATION, "1234-abcd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [[
            "monitoring", "dashboards", "update", "1234-abcd",
            f"--project={DESTINATION}",
            f"--config-from-file={ROOT / 'dashboards/copilot-usage.json'}", "--quiet",
        ]])

    def test_import_works_outside_repo_and_with_spaces_in_repo_path(self):
        repo = self.work / "repository with spaces"
        (repo / "scripts").mkdir(parents=True)
        (repo / "dashboards").mkdir()
        script = ROOT / "scripts/import-dashboard.sh"
        self.assertTrue(script.is_file(), "Missing Task 3 import script")
        shutil.copyfile(script, repo / "scripts" / script.name)
        dashboard = repo / "dashboards/copilot-usage.json"
        source = ROOT / "dashboards/copilot-usage.json"
        self.assertTrue(source.is_file(), "Missing Task 3 dashboard")
        shutil.copyfile(source, dashboard)
        result = subprocess.run(
            ["/bin/bash", str(repo / "scripts" / script.name), "create", DESTINATION],
            cwd=self.work, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--config-from-file={dashboard}", self.calls()[0])
        dashboard.unlink()
        self.events.unlink()
        result = subprocess.run(
            ["/bin/bash", str(repo / "scripts" / script.name), "create", DESTINATION],
            cwd=self.work, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dashboard", result.stderr.lower())
        self.assertEqual(self.calls(), [])

    def test_import_rejects_invalid_action_project_or_id_without_cloud_calls(self):
        cases = [
            ((), "Usage:"),
            (("create",), "Usage:"),
            ((DESTINATION,), "Usage:"),
            (("upsert", DESTINATION), "Usage:"),
            (("create", DESTINATION, "extra"), "Usage:"),
            (("update", DESTINATION), "Usage:"),
            (("create", "BadProject"), "project"),
            (("update", DESTINATION, ""), "dashboard ID"),
            (("update", DESTINATION, "--bad-option"), "dashboard ID"),
            (("update", DESTINATION, "projects/another/dashboards/id"), "dashboard ID"),
            (("update", DESTINATION, "id with spaces"), "dashboard ID"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_script("import-dashboard.sh", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_import_propagates_create_and_update_failures(self):
        for args in [("create", DESTINATION), ("update", DESTINATION, "1234-abcd")]:
            with self.subTest(args=args):
                self.events.unlink(missing_ok=True)
                result = self.run_script("import-dashboard.sh", *args, FAIL_CALL="1")
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertEqual(len(self.calls()), 1)
                self.assertIn("simulated gcloud", result.stderr)
                self.assertIn("failed", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
