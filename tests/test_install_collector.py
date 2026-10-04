"""Installer contract tests; all external downloads are isolated fixtures."""

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch" / "task-1"
SCRIPT = ROOT / "scripts" / "install-collector.sh"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Task 1 installer is missing")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="installer ", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.tools = self.work / "fake tools"
        self.tools.mkdir()
        self.destination = self.work / "install with spaces" / "bin"
        self.destination.mkdir(parents=True)
        self.binary = self.destination / "otelcol-contrib"
        self.binary.write_text("previous installation\n")
        self.archive = self.work / "asset.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            data = b"#!/bin/sh\nprintf 'otelcol-contrib version 0.162.0\\n'\n"
            entry = tarfile.TarInfo("otelcol-contrib")
            entry.size = len(data)
            entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(data))
        self.checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.checksums = self.work / "checksums.txt"
        self.env = {
            **os.environ,
            "PATH": str(self.tools) + os.pathsep + os.environ["PATH"],
            "INSTALL_DIR": str(self.destination),
            "FIXTURE_ARCHIVE": str(self.archive),
            "FIXTURE_CHECKSUMS": str(self.checksums),
            "DOWNLOAD_LOG": str(self.work / "downloads"),
            "FAKE_OS": "Darwin",
            "FAKE_ARCH": "arm64",
        }
        self.tool("uname", """case "$1" in
  -s) printf '%s\\n' "$FAKE_OS";;
  -m) printf '%s\\n' "$FAKE_ARCH";;
  *) exit 2;;
esac
""")
        self.tool("curl", """output=
url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|--output) output=$2; shift 2;;
    https://*) url=$1; shift;;
    *) shift;;
  esac
done
printf '%s\\n' "$url" >> "$DOWNLOAD_LOG"
[ "${FAIL_DOWNLOAD:-0}" = 0 ] || exit 22
case "$url" in
  https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.162.0/otelcol-contrib_0.162.0_darwin_arm64.tar.gz|https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.162.0/otelcol-contrib_0.162.0_darwin_amd64.tar.gz)
    cp "$FIXTURE_ARCHIVE" "$output";;
  https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.162.0/otelcol-contrib_0.162.0_darwin_arm64.tar.gz.sha256|https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.162.0/otelcol-contrib_0.162.0_darwin_amd64.tar.gz.sha256)
    cp "$FIXTURE_CHECKSUMS" "$output";;
  *) exit 23;;
esac
""")

    def tool(self, name, body):
        path = self.tools / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o755)

    def run_installer(self, checksum=None):
        digest = self.checksum if checksum is None else checksum
        self.checksums.write_text(f"{digest}\n")
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)], env=self.env, text=True,
            capture_output=True, timeout=15,
        )

    def assert_preserved(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.binary.read_text(), "previous installation\n")
        self.assertEqual(sorted(p.name for p in self.destination.iterdir()),
                         ["otelcol-contrib"])

    def test_native_assets_verified_and_installed_in_paths_with_spaces(self):
        for machine, asset in [("arm64", "arm64"), ("x86_64", "amd64"),
                               ("amd64", "amd64")]:
            with self.subTest(machine=machine):
                self.env["FAKE_ARCH"] = machine
                result = self.run_installer()
                self.assertEqual(result.returncode, 0, result.stderr)
                version = subprocess.check_output([str(self.binary)], text=True)
                self.assertEqual(version, "otelcol-contrib version 0.162.0\n")
                downloads = Path(self.env["DOWNLOAD_LOG"]).read_text()
                self.assertIn(f"otelcol-contrib_0.162.0_darwin_{asset}.tar.gz",
                              downloads)
                self.assertEqual(list(self.destination.iterdir()), [self.binary])

    def test_invalid_checksum_preserves_existing_binary(self):
        result = self.run_installer("0" * 64)
        self.assert_preserved(result)
        self.assertIn("checksum", result.stderr.lower())

    def test_malformed_or_absent_checksum_preserves_existing_binary(self):
        for content in ["", "not-a-digest\n",
                        f"{self.checksum}\n{self.checksum}\n",
                        f"{self.checksum}  unrelated.tar.gz\n"]:
            with self.subTest(content=content):
                self.checksums.write_text(content)
                result = subprocess.run(["/bin/sh", str(SCRIPT)], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assert_preserved(result)

    def test_failed_download_preserves_existing_binary(self):
        self.env["FAIL_DOWNLOAD"] = "1"
        self.assert_preserved(self.run_installer())

    def test_unsupported_platforms_do_not_download(self):
        for system, machine in [("Linux", "arm64"), ("Darwin", "i386")]:
            with self.subTest(system=system, machine=machine):
                self.env.update(FAKE_OS=system, FAKE_ARCH=machine)
                result = self.run_installer()
                self.assert_preserved(result)
                self.assertIn("unsupported", result.stderr.lower())
                self.assertFalse(Path(self.env["DOWNLOAD_LOG"]).exists())

    def test_missing_prerequisites_fail_before_downloading(self):
        commands = ["uname", "curl", "tar", "shasum", "awk", "mkdir",
                    "chmod", "mv", "rm"]
        for missing in commands:
            with self.subTest(missing=missing):
                isolated = self.work / ("only-tools-except-" + missing)
                isolated.mkdir()
                for name in commands:
                    if name == missing:
                        continue
                    original = self.tools / name
                    if not original.exists():
                        original = Path(shutil.which(name))
                    (isolated / name).symlink_to(original)
                self.env["PATH"] = str(isolated)
                result = self.run_installer()
                self.assert_preserved(result)
                self.assertIn(missing, result.stderr)
                self.assertFalse(Path(self.env["DOWNLOAD_LOG"]).exists())


if __name__ == "__main__":
    unittest.main()
