"""Verify worker admission and supervision without starting sandbox processes."""

import importlib.util
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
