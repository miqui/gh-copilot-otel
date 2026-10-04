"""Execute the wrapper with a fake CLI; never start a Copilot session."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "copilot-otel.sh"


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wrapper with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.tools = self.work / "CLI & tools"
        self.tools.mkdir()
        self.output = self.work / "invocation.json"
        self.cli = self.tools / "copilot"
        self.cli.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['INVOCATION'], 'w') as stream:\n"
            "    json.dump({'args': sys.argv[1:], 'env': dict(os.environ), "
            "'pid': os.getpid()}, stream)\n"
            "sys.exit(int(os.environ.get('CLI_EXIT', '0')))\n"
        )
        self.cli.chmod(0o755)
        self.env = {
            "PATH": str(self.tools), "HOME": str(self.work),
            "INVOCATION": str(self.output),
            "COPILOT_GITHUB_TOKEN": "fake-copilot-token",
            "GH_TOKEN": "fake-gh-token", "GITHUB_TOKEN": "fake-github-token",
            "COPILOT_PROVIDER_API_KEY": "fake-provider-key",
            "HTTPS_PROXY": "http://authentication-proxy.invalid",
        }

    def run_wrapper(self, args=()):
        self.assertTrue(SCRIPT.is_file(), "Task 2 wrapper is missing")
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), *args], env=self.env,
            capture_output=True, text=True, timeout=5,
        )

    def test_argument_vector_and_exit_status_are_preserved(self):
        args = ["--prompt", "spaces & <xml> 'quotes'\nsecond line", "",
                "*", "--", "-leading-option"]
        for code in [0, 7, 130]:
            with self.subTest(code=code):
                self.env["CLI_EXIT"] = str(code)
                result = self.run_wrapper(args)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(json.loads(self.output.read_text())["args"], args)

    def test_exec_preserves_process_identity(self):
        self.assertTrue(SCRIPT.is_file(), "Task 2 wrapper is missing")
        process = subprocess.Popen(
            ["/bin/sh", str(SCRIPT)], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(self.output.read_text())["pid"], process.pid)

    def test_conflicting_export_content_tls_and_headers_cannot_escape(self):
        conflicts = {
            "COPILOT_OTEL_ENABLED": "false",
            "COPILOT_OTEL_EXPORTER_TYPE": "file",
            "COPILOT_OTEL_FILE_EXPORTER_PATH": str(self.work / "private.jsonl"),
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://remote.invalid",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_TRACES_EXPORTER": "console", "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "otlp", "OTEL_SDK_DISABLED": "true",
        }
        overrides = {}
        for signal in ["", "_TRACES", "_METRICS", "_LOGS"]:
            for setting in ["ENDPOINT", "PROTOCOL", "HEADERS", "CERTIFICATE",
                            "CLIENT_CERTIFICATE", "CLIENT_KEY", "INSECURE"]:
                overrides[f"OTEL_EXPORTER_OTLP{signal}_{setting}"] = "conflict"
        self.env.update(overrides)
        self.env.update(conflicts)
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(self.output.read_text())["env"]
        expected = {
            "COPILOT_OTEL_ENABLED": "true",
            "COPILOT_OTEL_EXPORTER_TYPE": "otlp-http",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
            "OTEL_TRACES_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "none", "OTEL_SDK_DISABLED": "false",
        }
        for key, value in expected.items():
            self.assertEqual(actual.get(key), value, key)
        for key in (overrides.keys() | conflicts.keys()) - expected.keys():
            self.assertNotIn(key, actual)
        self.assertFalse((self.work / "private.jsonl").exists())

    def test_authentication_survives_without_service_prerequisites(self):
        result = self.run_wrapper(["--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(self.output.read_text())["env"]
        for key in ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
                    "COPILOT_PROVIDER_API_KEY", "HTTPS_PROXY", "HOME"]:
            self.assertEqual(actual[key], self.env[key])

    def test_missing_copilot_fails_explicitly(self):
        self.cli.unlink()
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("copilot", result.stderr.lower())
        self.assertFalse(self.output.exists())

    def test_wrapper_path_with_spaces(self):
        self.assertTrue(SCRIPT.is_file(), "Task 2 wrapper is missing")
        copied = self.work / "wrapper with spaces.sh"
        shutil.copyfile(SCRIPT, copied)
        result = subprocess.run(
            ["/bin/sh", str(copied), "argument with spaces"], env=self.env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())["args"],
                         ["argument with spaces"])


if __name__ == "__main__":
    unittest.main()
