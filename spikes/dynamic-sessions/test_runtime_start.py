"""execd APIs launch with the minimum filesystem view each endpoint needs."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_runtime_start():
    source = Path(__file__).with_name("runtime-start.py")
    spec = importlib.util.spec_from_file_location("runtime_start_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeStartTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_runtime_start()

    def launch(self, arguments):
        with (
            patch.object(sys, "argv", ["runtime-start.py", *arguments]),
            patch.object(self.runtime.os, "chdir") as chdir,
            patch.object(self.runtime, "ensure_workspace_tmp") as prepare,
            patch.object(self.runtime.os, "execvp") as execvp,
        ):
            self.runtime.main()
        chdir.assert_called_once_with("/workspace")
        prepare.assert_called_once_with()
        execvp.assert_called_once()
        return execvp.call_args.args[1]

    def test_command_api_runs_in_workspace_mount_view(self):
        args = self.launch(["command"])
        self.assertIn("--bind", args)
        self.assertIn(
            ["--ro-bind", "/usr/local", "/usr/local"],
            [args[i : i + 3] for i in range(len(args) - 2)],
        )
        self.assertEqual(
            args[args.index("--bind") + 1 : args.index("--bind") + 3],
            ["/workspace", "/workspace"],
        )
        self.assertNotIn("/etc", args)
        self.assertNotIn("/proc", args)
        self.assertEqual(args[args.index("--port") + 1], "8081")

    def test_file_api_mounts_only_the_workspace_and_static_worker(self):
        args = self.launch(["files"])
        self.assertIn("--bind", args)
        self.assertEqual(
            args[-6:],
            [
                "--runtime-init",
                "--runtime-init-once",
                "--port",
                "8082",
                "--log-level",
                "3",
            ],
        )
        self.assertEqual(args[args.index("--ro-bind") + 1], "/opt/execd")
        self.assertNotIn("/usr", args)
        self.assertNotIn("/dev", args)
        self.assertIn("TMPDIR", args)
        self.assertIn("/workspace/.tmp", args)
        self.assertNotIn("/etc", args)
        self.assertNotIn("/proc", args)

    def test_default_entrypoint_starts_gateway(self):
        with (
            patch.object(sys, "argv", ["runtime-start.py"]),
            patch.object(self.runtime.os, "chdir") as chdir,
            patch.object(self.runtime.os, "execve") as execve,
        ):
            self.runtime.main()
        chdir.assert_called_once_with("/workspace")
        self.assertEqual(execve.call_args.args[0], "/usr/local/bin/python")
        self.assertIn("/opt/runtime-gateway.py", execve.call_args.args[1])

    def test_unknown_worker_mode_is_rejected(self):
        with patch.object(sys, "argv", ["runtime-start.py", "admin"]):
            with self.assertRaisesRegex(SystemExit, "usage"):
                self.runtime.main()

    def test_workspace_tmp_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as parent:
            target = Path(parent, "target")
            target.mkdir()
            link = Path(parent, "link")
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "not a directory"):
                self.runtime.ensure_workspace_tmp(str(link))


if __name__ == "__main__":
    unittest.main()
