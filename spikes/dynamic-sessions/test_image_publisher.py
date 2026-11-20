"""Exercise publication boundaries without Docker, Azure access, or real credentials."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("build-azure-images.sh")
MOCK = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["PUBLISH_TRACE"], "a") as trace:
    trace.write(json.dumps([tool, *args]) + "\n")
if tool == "docker" and args[:2] == ["buildx", "build"]:
    pathlib.Path(args[args.index("--metadata-file") + 1]).write_text(
        json.dumps({"containerimage.digest": "sha256:" + "a" * 64}))
elif tool == "az":
    print("synthetic-publisher-token")
elif tool == "skopeo" and args[0] == "login":
    assert sys.stdin.read().strip() == "synthetic-publisher-token"
    auth = pathlib.Path(args[args.index("--authfile") + 1])
    auth.write_text("synthetic-auth-fixture")
    assert auth.stat().st_mode & 0o077 == 0
elif tool == "trivy" and "--exit-code" in args and os.environ.get("FAIL_SCAN"):
    sys.exit(1)
elif tool == "jq":
    print("sha256:" + "a" * 64)
"""


class PublisherTests(unittest.TestCase):
    def run_publisher(self, registries=("appregistry", "runtimeregistry"), fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("docker", "az", "skopeo", "trivy", "jq"):
                command = root / name
                command.write_text(MOCK)
                command.chmod(0o700)
            trace = root / "trace.jsonl"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}")
            env["PUBLISH_TRACE"] = str(trace)
            if fail:
                env["FAIL_SCAN"] = "1"
            result = subprocess.run(
                ["bash", str(SCRIPT), *registries, "test-build"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            calls = (
                [json.loads(line) for line in trace.read_text().splitlines()]
                if trace.exists()
                else []
            )
            self.assertNotIn("synthetic-publisher-token", result.stdout + result.stderr)
            for call in calls:
                if "--authfile" in call:
                    self.assertFalse(Path(call[call.index("--authfile") + 1]).exists())
            return result, calls

    def test_rejects_same_registry_before_external_commands(self):
        result, calls = self.run_publisher(("appregistry", "appregistry"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_scanned_images_go_to_separate_registries(self):
        result, calls = self.run_publisher()
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = [
            (index, call)
            for index, call in enumerate(calls)
            if call[:2] == ["skopeo", "copy"]
        ]
        self.assertEqual(len(copies), 2)
        self.assertEqual(
            copies[0][1][-1],
            "docker://runtimeregistry.azurecr.io/opsrabbit/opensandbox-runtime:test-build",
        )
        self.assertEqual(
            copies[1][1][-1],
            "docker://appregistry.azurecr.io/opsrabbit/opensandbox-server:test-build",
        )
        previous = 0
        for index, call in copies:
            self.assertIn("--all", call)
            self.assertIn("--preserve-digests", call)
            self.assertTrue(
                any(
                    row[0] == "trivy" and "--exit-code" in row
                    for row in calls[previous:index]
                )
            )
            previous = index + 1
        logins = [call[call.index("--name") + 1] for call in calls if call[0] == "az"]
        self.assertEqual(logins, ["runtimeregistry", "appregistry"])

    def test_scan_failure_prevents_authentication_and_publication(self):
        result, calls = self.run_publisher(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] in ("az", "skopeo") for call in calls))


if __name__ == "__main__":
    unittest.main()
