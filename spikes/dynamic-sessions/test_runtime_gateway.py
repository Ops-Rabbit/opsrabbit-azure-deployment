"""Verify worker admission and supervision without starting sandbox processes."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_gateway():
    source = Path(__file__).with_name("runtime-gateway.py")
    spec = importlib.util.spec_from_file_location("runtime_gateway_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.command = Mock()
    module.files = Mock()
    return vars(module)


class GatewayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.g = load_gateway()
        self.g["command"].poll.return_value = None
        self.g["files"].poll.return_value = None

    def test_command_timeout_includes_runtime_margin(self):
        timeout = self.g["request_timeout"]("POST", "/command", b'{"timeout":180000}')
        self.assertEqual(timeout, 210)

    def test_disconnected_client_cancels_upstream_without_waiting_for_output(self):
        client, upstream, finished = Mock(), Mock(), Mock()
        finished.is_set.return_value = False
        client.recv.return_value = b""
        with patch.object(self.g["select"], "select", return_value=([client], [], [])):
            self.g["cancel_on_disconnect"](client, upstream, finished)
        upstream.shutdown.assert_called_once_with(self.g["socket"].SHUT_RDWR)

    def test_completed_stream_does_not_cancel_another_command(self):
        client, upstream, finished = Mock(), Mock(), Mock()
        finished.is_set.return_value = True
        self.g["cancel_on_disconnect"](client, upstream, finished)
        upstream.shutdown.assert_not_called()

    def test_command_timeout_is_capped_at_session_maximum(self):
        timeout = self.g["request_timeout"]("POST", "/command", b'{"timeout":7200000}')
        self.assertEqual(timeout, 3600)

    def test_other_requests_keep_the_short_timeout(self):
        self.assertEqual(self.g["request_timeout"]("GET", "/files/info", b""), 120)

    def test_admission_requires_both_workers(self):
        response = Mock(status=200)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(
            self.g["http"].client, "HTTPConnection", return_value=connection
        ) as factory:
            self.g["wait_for_workers"]()
        self.assertEqual(
            [call.args[1] for call in factory.call_args_list], [8081, 8082]
        )
        self.assertEqual(connection.close.call_count, 2)

    def test_exited_worker_fails_startup(self):
        self.g["files"].poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "exited"):
            self.g["wait_for_workers"]()

    def test_unavailable_worker_has_bounded_deadline(self):
        connection = Mock()
        connection.request.side_effect = OSError("unavailable")
        with (
            patch.object(
                self.g["http"].client, "HTTPConnection", return_value=connection
            ),
            patch.object(self.g["time"], "monotonic", side_effect=[0, 0, 31]),
            patch.object(self.g["time"], "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "within 30 seconds"):
                self.g["wait_for_workers"]()
        self.assertEqual(connection.close.call_count, 2)

    def test_child_exit_shuts_down_gateway(self):
        self.g["command"].poll.side_effect = [None, 1]
        server = Mock()
        with patch.object(self.g["time"], "sleep"):
            self.g["supervise"](server)
        server.shutdown.assert_called_once()

    def test_second_spawn_failure_reaps_first_worker(self):
        child = Mock()
        child.poll.return_value = None
        with (
            patch.object(self.g["signal"], "signal"),
            patch.object(
                self.g["subprocess"],
                "Popen",
                side_effect=[child, OSError("spawn failed")],
            ),
        ):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                self.g["main"]()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_file_query_paths_must_stay_in_workspace(self):
        validate = self.g["validate_file_request"]
        validate("GET", "/files/download", "path=report.csv", "", b"")
        with self.assertRaisesRegex(ValueError, "workspace"):
            validate("GET", "/files/download", "path=%2Fetc%2Fpasswd", "", b"")
        with self.assertRaisesRegex(ValueError, "workspace"):
            validate(
                "GET",
                "/files/info",
                "path=%2Fworkspace%2F..%2Fetc%2Fpasswd",
                "",
                b"",
            )

    def test_file_query_paths_reject_symlinks_outside_workspace(self):
        with patch.object(self.g["os"].path, "realpath", return_value="/etc/passwd"):
            with self.assertRaisesRegex(ValueError, "workspace"):
                self.g["validate_file_request"](
                    "GET", "/files/download", "path=%2Fworkspace%2Fescape", "", b""
                )

    def test_reserved_paths_are_rejected_but_stdin_runtime_is_supported(self):
        validate = self.g["validate_file_request"]
        for name in self.g["PROTECTED_NAMES"]:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "reserved"),
            ):
                validate(
                    "GET",
                    "/files/download",
                    "path=/workspace/" + name + "/secret",
                    "",
                    b"",
                )
        validate(
            "GET",
            "/files/download",
            "path=/workspace/.opsrabbit-runtime/stdin",
            "",
            b"",
        )

    def test_symlink_to_reserved_path_is_rejected(self):
        with patch.object(
            self.g["os"].path,
            "realpath",
            return_value="/workspace/.opsrabbit-system/key",
        ):
            with self.assertRaisesRegex(ValueError, "reserved"):
                self.g["confined_path"]("/workspace/alias")

    def init_connections(self, replies):
        connections = []
        for status, payload in replies:
            response = Mock(status=status)
            response.read.return_value = json.dumps(payload).encode()
            connection = Mock()
            connection.getresponse.return_value = response
            connections.append(connection)
        return connections

    def test_contract_requires_both_bound_and_ready_workers(self):
        binding = {"sandboxId": "fixture", "generation": 1}
        initialized = {**binding, "status": "initialized"}
        ready = {**binding, "initialized": True}
        connections = self.init_connections(
            [(200, initialized), (200, initialized), (200, ready), (200, ready)]
        )
        with patch.object(
            self.g["http"].client, "HTTPConnection", side_effect=connections
        ) as factory:
            reply = self.g["initialize_workers"](json.dumps(binding).encode(), {})
        self.assertEqual(reply["runtimeContract"], "workspace-command-files-v1")
        self.assertEqual(
            [call.args[1] for call in factory.call_args_list], [8081, 8082, 8081, 8082]
        )
        self.assertEqual(
            [conn.request.call_args.args[:2] for conn in connections],
            [
                ("POST", "/internal/init"),
                ("POST", "/internal/init"),
                ("GET", "/ready"),
                ("GET", "/ready"),
            ],
        )
        for connection in connections:
            connection.close.assert_called_once()

    def test_contract_is_not_returned_on_partial_init_or_readiness_mismatch(self):
        binding = {"sandboxId": "fixture", "generation": 1}
        initialized = {**binding, "status": "initialized"}
        ready = {**binding, "initialized": True}
        for replies in (
            [(200, initialized), (409, {})],
            [(200, initialized), (200, {**initialized, "sandboxId": "different"})],
            [(200, initialized), (200, initialized), (503, {})],
            [
                (200, initialized),
                (200, initialized),
                (200, ready),
                (200, {**ready, "generation": 2}),
            ],
        ):
            with patch.object(
                self.g["http"].client,
                "HTTPConnection",
                side_effect=self.init_connections(replies),
            ):
                with self.assertRaises(RuntimeError):
                    self.g["initialize_workers"](json.dumps(binding).encode(), {})

    def test_init_cannot_inject_parent_hooks_or_claim_contract(self):
        for extra in (
            {"runtimeContract": "workspace-command-files-v1"},
            {"lifecycle": {"preStart": "touch /tmp/parent"}},
            {"envs": {"BASH_ENV": "/workspace/startup"}},
            {"envs": {"PATH": "/workspace"}},
        ):
            with self.assertRaises(ValueError):
                self.g["initialize_workers"](
                    json.dumps(
                        {"sandboxId": "fixture", "generation": 1, **extra}
                    ).encode(),
                    {},
                )

    def test_file_json_path_maps_are_confined(self):
        validate = self.g["validate_file_request"]
        validate(
            "POST",
            "/files/permissions",
            "",
            "application/json",
            b'{"/workspace/a.txt":{"mode":420}}',
        )
        with self.assertRaisesRegex(ValueError, "workspace"):
            validate(
                "POST",
                "/files/replace",
                "",
                "application/json",
                b'{"/tmp/a.txt":{"old":"x","new":"y"}}',
            )

    def test_file_moves_validate_both_paths(self):
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.g["validate_file_request"](
                "POST",
                "/files/mv",
                "",
                "application/json",
                b'[{"src":"/workspace/a","dest":"/etc/a"}]',
            )

    def test_file_upload_metadata_path_is_confined(self):
        body = (
            b'--b\r\nContent-Disposition: form-data; name="metadata"\r\n'
            b"Content-Type: application/json\r\n\r\n"
            b'{"path":"/etc/hosts"}\r\n--b--\r\n'
        )
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.g["validate_file_request"](
                "POST", "/files/upload", "", "multipart/form-data; boundary=b", body
            )

    def test_file_upload_accepts_workspace_metadata(self):
        body = (
            b'--b\r\nContent-Disposition: form-data; name="metadata"\r\n'
            b"Content-Type: application/json\r\n\r\n"
            b'{"path":"/workspace/upload.txt"}\r\n'
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="upload.txt"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\nhello\r\n--b--\r\n"
        )
        self.g["validate_file_request"](
            "POST", "/files/upload", "", "multipart/form-data; boundary=b", body
        )


if __name__ == "__main__":
    unittest.main()
