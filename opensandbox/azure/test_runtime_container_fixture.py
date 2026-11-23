"""Host policy lifecycle regressions; no Docker or host policy mutations."""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "runtime_container_fixture",
        Path(__file__).with_name("test-runtime-container.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixturePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture()

    def test_scoped_profile_removed_after_failed_container_test(self):
        with patch.object(self.fixture, "policy_command") as parser:
            with self.assertRaisesRegex(RuntimeError, "command isolation failed"):
                with self.fixture.fixture_apparmor(True) as name:
                    self.assertTrue(name.startswith("skills156-ci-"))
                    raise RuntimeError("command isolation failed")
        calls = parser.call_args_list
        self.assertEqual([call.args[1] for call in calls], ["-Q", "-a", "-R"])
        self.assertEqual(len({call.args[0] for call in calls}), 1)

    def test_parser_rejection_never_runs_container_or_falls_back(self):
        with (
            patch.object(self.fixture, "host_prerequisites"),
            patch.object(self.fixture, "exercise_runtime") as run,
            patch.object(
                self.fixture,
                "policy_command",
                side_effect=subprocess.CalledProcessError(1, "apparmor_parser"),
            ) as parser,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                self.fixture.main("runtime:immutable", ci_apparmor=True)
        run.assert_not_called()
        self.assertEqual(parser.call_count, 1)

    def test_ambiguous_load_failure_still_attempts_removal(self):
        with patch.object(
            self.fixture,
            "policy_command",
            side_effect=[None, subprocess.TimeoutExpired("parser", 30), None],
        ) as parser:
            with self.assertRaises(subprocess.TimeoutExpired):
                with self.fixture.fixture_apparmor(True):
                    self.fail("must not run after load failure")
        self.assertEqual(parser.call_args.args[1], "-R")

    def test_removal_failure_is_not_reported_as_success(self):
        with patch.object(
            self.fixture,
            "policy_command",
            side_effect=[None, None, subprocess.CalledProcessError(1, "parser")],
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                with self.fixture.fixture_apparmor(True):
                    pass

    def test_policy_inherits_without_host_executable_attachment(self):
        policy = self.fixture.apparmor_policy("skills156-ci-abc123")
        self.assertIn("profile skills156-ci-abc123 flags=", policy)
        self.assertIn("allow ix /**,", policy)
        self.assertIn("allow userns create,", policy)
        for forbidden in (
            "unconfined",
            "complain",
            "pix",
            "change_profile",
            "/usr/bin/bwrap",
        ):
            self.assertNotIn(forbidden, policy)
        with self.assertRaises(ValueError):
            self.fixture.apparmor_policy("skills156-ci-abc {}")

    def test_wrong_attached_profile_rejects_before_commands(self):
        inspected = b"""[{"AppArmorProfile":"unconfined","HostConfig":{
          "SecurityOpt":["apparmor=unconfined"],"CapAdd":["SYS_ADMIN"]},
          "Config":{"User":"1000:1000"}}]"""
        with (
            patch.object(
                self.fixture.subprocess, "check_output", return_value=inspected
            ),
            patch.object(self.fixture.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "not attached"):
                self.fixture.container_prerequisites(
                    "owned-container", "skills156-ci-abc"
                )
        run.assert_not_called()

    def test_policy_commands_bound_time_and_do_not_write_host_files_or_cache(self):
        with patch.object(self.fixture.subprocess, "run") as run:
            self.fixture.policy_command("policy", "-a", "--Werror")
        run.assert_called_once_with(
            ["sudo", "-n", "/usr/sbin/apparmor_parser", "-K", "-a", "--Werror"],
            input="policy",
            text=True,
            check=True,
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
