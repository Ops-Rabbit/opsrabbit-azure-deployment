"""execd APIs launch with the minimum filesystem view each endpoint needs."""

import importlib.util
import json
import struct
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
            patch.object(self.runtime.os, "execve") as execve,
        ):
            self.runtime.main()
        chdir.assert_called_once_with("/workspace")
        self.assertEqual(prepare.call_args_list[0].args, ())
        self.assertEqual(prepare.call_count, 6)
        execve.assert_called_once()
        self.assertEqual(execve.call_args.args[0], "/usr/bin/bwrap")
        self.assertEqual(execve.call_args.args[2], self.runtime.LAUNCHER_ENV)
        return execve.call_args.args[1]

    def test_trusted_supervisor_inherits_proc_without_unmasking_or_new_pid_namespace(
        self,
    ):
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
        self.assertIn(
            ["--bind", "/proc", "/proc"],
            [args[i : i + 3] for i in range(len(args) - 2)],
        )
        self.assertNotIn("--proc", args)
        self.assertNotIn("--unshare-pid", args)
        self.assertEqual(args[args.index("--port") + 1], "8081")
        for name in self.runtime.PROTECTED_NAMES:
            self.assertIn(
                ["--ro-bind", "/opt/opsrabbit-system/empty", "/workspace/" + name],
                [args[i : i + 3] for i in range(len(args) - 2)],
            )
        self.assertEqual(args[args.index("TMPDIR") + 1], "/tmp")

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
        for name in self.runtime.PROTECTED_NAMES:
            self.assertIn("/workspace/" + name, args)
        self.assertNotIn("/workspace/.opsrabbit-runtime", args)

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


class WorkspaceCommandTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("workspace-command.py")
        spec = importlib.util.spec_from_file_location("workspace_command_test", source)
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)

    def request(self, **overrides):
        return {
            "command": "printf ready",
            "argv": None,
            "cwd": "/workspace",
            "envs": {},
            **overrides,
        }

    def test_every_payload_gets_no_proc_pid_cleanup_and_fixed_masks(self):
        args = self.launcher.command_args(self.request(), 7)
        self.assertIn("--unshare-pid", args)
        self.assertNotIn("--as-pid-1", args)
        self.assertIn("--die-with-parent", args)
        self.assertIn("--unshare-net", args)
        self.assertNotIn("--disable-userns", args)
        self.assertEqual(args[args.index("--seccomp") + 1], "7")
        self.assertNotIn("--proc", args)
        mounts = [args[i : i + 3] for i in range(len(args) - 2)]
        for dest in (
            "/proc",
            "/etc",
            "/root",
            "/home",
            "/var",
            "/data",
            "/mnt",
            "/media",
            "/srv",
            "/workspace/.opsrabbit-system",
            "/workspace/.opsrabbit-empty",
            "/workspace/.opsrabbit-skills",
            "/workspace/.opsrabbit-browser-runtime",
        ):
            self.assertIn(["--ro-bind", "/opt/opsrabbit-system/empty", dest], mounts)
        self.assertNotIn("/workspace/.opsrabbit-runtime", args)

    def test_request_environment_is_child_data_never_launcher_environment(self):
        envs = {
            "LD_PRELOAD": "/workspace/evil.so",
            "BASH_ENV": "/workspace/startup",
            "PATH": "/workspace",
            "PYTHONPATH": "/workspace",
        }
        request = self.request(command="$(touch /tmp/parent)", envs=envs)
        with (
            patch.object(sys, "argv", ["workspace-command.py", json.dumps(request)]),
            patch.object(self.launcher.os, "execve") as execute,
            patch.object(self.launcher.os, "open", return_value=7) as opened,
            patch.object(self.launcher.os, "set_inheritable") as inherited,
            patch.object(self.launcher.os, "close") as closed,
        ):
            self.launcher.main()
        opened.assert_called_once_with(
            self.launcher.NAMESPACE_FILTER,
            self.launcher.os.O_RDONLY | self.launcher.os.O_NOFOLLOW,
        )
        inherited.assert_called_once_with(7, True)
        closed.assert_called_once_with(7)
        executable, args, parent_env = execute.call_args.args
        self.assertEqual(executable, "/usr/bin/bwrap")
        self.assertEqual(
            parent_env,
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LD_LIBRARY_PATH": "/usr/local/lib",
                "HOME": "/workspace",
            },
        )
        self.assertEqual(
            args[-5:],
            ["/bin/bash", "--noprofile", "--norc", "-c", "$(touch /tmp/parent)"],
        )
        mounts = [args[i : i + 3] for i in range(len(args) - 2)]
        self.assertIn(["--setenv", "LD_PRELOAD", "/workspace/evil.so"], mounts)
        self.assertIn(["--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin"], mounts)

    def test_native_argv_is_not_evaluated_or_resolved_in_parent(self):
        argv = ["/workspace/tool", "$(id)", "--proc", "/proc"]
        args = self.launcher.command_args(self.request(command="", argv=argv), 7)
        self.assertEqual(args[-5:], ["--", *argv])

    def test_cwd_is_applied_after_child_mounts_not_resolved_in_parent(self):
        with patch.object(
            self.launcher.os.path,
            "realpath",
            side_effect=AssertionError("parent resolution"),
        ):
            args = self.launcher.command_args(self.request(cwd="/workspace/link"), 7)
        self.assertEqual(args[args.index("--chdir") + 1], "/workspace/link")

    def test_invalid_or_privilege_extending_envelopes_fail_closed(self):
        for request in (
            self.request(cwd="/proc"),
            self.request(cwd="/workspace/../proc"),
            self.request(cwd="/workspace/.opsrabbit-skills"),
            self.request(cwd="$HOME"),
            self.request(envs={"A=B": "x"}),
            self.request(envs={"A": "x\x00y"}),
            self.request(argv=["/bin/sh"]),
            self.request(command="", argv=[]),
            self.request(command="", argv=[1]),
            self.request(extra_mount="/proc"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.launcher.command_args(request, 7)

    def test_filter_failure_never_executes_payload(self):
        with (
            patch.object(
                sys, "argv", ["workspace-command.py", json.dumps(self.request())]
            ),
            patch.object(self.launcher.os, "open", side_effect=FileNotFoundError),
            patch.object(self.launcher.os, "execve") as execute,
            self.assertRaises(FileNotFoundError),
        ):
            self.launcher.main()
        execute.assert_not_called()
        for fd in (None, "7", True, 0, 1, 2, -1):
            with self.subTest(fd=fd), self.assertRaises(ValueError):
                self.launcher.command_args(self.request(), fd)

    def test_filter_denies_namespace_creation_join_and_alternate_abis(self):
        def evaluate(program, architecture, syscall, flags=0):
            data = struct.pack("<IIQQ", syscall, architecture, 0, flags)
            instructions = list(struct.iter_unpack("<HBBI", program))
            pc, accumulator = 0, 0
            while pc < len(instructions):
                code, yes, no, value = instructions[pc]
                pc += 1
                if code == 0x20:
                    accumulator = struct.unpack_from("<I", data, value)[0]
                elif code == 0x15:
                    pc += yes if accumulator == value else no
                elif code == 0x45:
                    pc += yes if accumulator & value else no
                elif code == 0x06:
                    return value
                else:
                    self.fail("unexpected BPF instruction")
            self.fail("filter did not return a verdict")

        for machine, arch, clone, unshare, setns in (
            ("x86_64", 0xC000003E, 56, 272, 308),
            ("aarch64", 0xC00000B7, 220, 97, 268),
        ):
            program = self.launcher.namespace_filter(machine)
            for syscall in (unshare, setns):
                self.assertEqual(evaluate(program, arch, syscall), 0x50001)
            self.assertEqual(evaluate(program, arch, 435), 0x50026)
            for flag in (
                0x80,
                0x20000,
                0x2000000,
                0x4000000,
                0x8000000,
                0x10000000,
                0x20000000,
                0x40000000,
            ):
                self.assertEqual(evaluate(program, arch, clone, flag | 17), 0x50001)
            for flags in (0, 17, 0x3D0F00):  # fork and pthread-style clone
                self.assertEqual(evaluate(program, arch, clone, flags), 0x7FFF0000)
            self.assertEqual(evaluate(program, arch, 1), 0x7FFF0000)
            self.assertEqual(evaluate(program, arch ^ 0x80000000, clone), 0x80000000)
            self.assertEqual(evaluate(program, arch, 0x40000000 | clone), 0x50026)
            mutation_syscalls = (
                (101, 311, 521, 540) if machine == "x86_64" else (117, 271)
            )
            for syscall in mutation_syscalls:
                self.assertEqual(evaluate(program, arch, syscall), 0x50001)
            # Cleanup belongs to trusted PID1, not the payload's mutable state.
            prctl, setuid, setgid, execve = (
                (157, 105, 106, 59) if machine == "x86_64" else (167, 146, 144, 221)
            )
            for syscall in (prctl, setuid, setgid, execve):
                self.assertEqual(evaluate(program, arch, syscall, 1), 0x7FFF0000)
        with self.assertRaises(ValueError):
            self.launcher.namespace_filter("riscv64")


class CommandStreamTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("test-runtime-container.py")
        spec = importlib.util.spec_from_file_location("runtime_container_test", source)
        self.harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.harness)

    def test_json_and_sse_preserve_stdout_completion_and_errors(self):
        for prefix in (b"", b"data: "):
            with self.subTest(prefix=prefix):
                body = b"\n\n".join(
                    prefix + json.dumps(event).encode()
                    for event in (
                        {"type": "init"},
                        {"type": "stdout", "text": "expected\n"},
                        {"type": "execution_complete"},
                    )
                )
                with patch.object(self.harness, "request", return_value=(200, body)):
                    self.assertEqual(
                        self.harness.run_command("host", "token", command="test"),
                        "expected\n",
                    )
                    with self.assertRaises(RuntimeError):
                        self.harness.run_command(
                            "host", "token", command="test", succeeds=False
                        )
                error = (
                    prefix + b'{"type":"error","error":{"ename":"CommandExecError"}}'
                )
                with patch.object(
                    self.harness, "request", return_value=(200, body + b"\n\n" + error)
                ):
                    with self.assertRaisesRegex(RuntimeError, "CommandExecError"):
                        self.harness.run_command("host", "token", command="test")
                    self.harness.run_command(
                        "host", "token", command="test", succeeds=False
                    )

    def test_empty_incomplete_and_malformed_streams_fail(self):
        for body in (b"", b"\n\n", b'{"type":"stdout","text":"expected"}'):
            for succeeds in (True, False):
                with (
                    patch.object(self.harness, "request", return_value=(200, body)),
                    self.assertRaises(RuntimeError),
                ):
                    self.harness.run_command(
                        "host", "token", command="test", succeeds=succeeds
                    )
        for line in (b"garbage", b"data: []", b'{"text":"missing type"}'):
            with self.assertRaises(ValueError):
                self.harness.parse_event(line)
        with (
            patch.object(self.harness, "request", return_value=(503, b"unavailable")),
            self.assertRaises(RuntimeError),
        ):
            self.harness.run_command("host", "token", command="test", succeeds=False)


if __name__ == "__main__":
    unittest.main()
