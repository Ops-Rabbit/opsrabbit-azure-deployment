"""Offline regressions for live-probe verdicts, disconnect and owned cleanup."""

import ast
import importlib.util
import json
import runpy
import threading
import time
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "live_probe", ROOT / "verify-end-to-end.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
parse_event = runpy.run_path(str(ROOT / "test-runtime-container.py"))["parse_event"]


def bare_probe():
    instance = probe.Probe.__new__(probe.Probe)
    instance.owned = ["created-a", "created-b"]
    instance.report = {"checks": [], "outcome": "passed"}
    instance.save = Mock()
    return instance


class StreamVerdictTests(unittest.TestCase):
    def command_stream(self, chunks):
        instance = bare_probe()
        instance.parse_event = parse_event
        response = probe.Response.__new__(probe.Response)
        response.status = 200
        response.chunk = Mock(side_effect=chunks)
        opened = Mock()
        opened.__enter__ = Mock(return_value=response)
        opened.__exit__ = Mock(return_value=False)
        instance.open = Mock(return_value=opened)
        return instance

    def test_background_accepts_init_only_without_claiming_foreground_completion(self):
        instance = self.command_stream(
            [b'{"type":"init","text":"execution-id"}\n', b""]
        )
        events = instance.command("/owned/proxy", "sleep 10", background=True)
        self.assertEqual(probe.execution_id(events), "execution-id")
        self.assertEqual([event["type"] for event in events], ["init"])
        instance = self.command_stream(
            [b'{"type":"init","text":"execution-id"}\n', b""]
        )
        with self.assertRaises(RuntimeError):
            instance.command("/owned/proxy", "sleep 10")

    def test_background_rejects_errors_missing_ids_and_malformed_trailing_records(self):
        for records in (
            b'{"type":"init","text":"id"}\n{"type":"error"}\n',
            b'{"type":"execution_complete"}\n',
            b'{"type":"init","text":"id"}\nnot-json\n',
            b'{"type":"init","text":"id"}\n{"type":',
            b'{"type":"init","text":"id"}\n{"type":"init","text":"duplicate"}\n',
        ):
            instance = self.command_stream([records, b""])
            with (
                self.subTest(records=records),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                instance.command("/owned/proxy", "sleep 10", background=True)

    def test_success_requires_completion_and_rejects_error_even_with_completion(self):
        for events in (
            [],
            [{"type": "stdout", "text": "ok"}],
            [{"type": "error"}, {"type": "execution_complete"}],
        ):
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                probe.assert_result(events)
        probe.assert_result([{"type": "execution_complete"}])

    def test_expected_failure_requires_error_not_empty_or_successful_stream(self):
        for events in ([], [{"type": "execution_complete"}]):
            with self.assertRaises(RuntimeError):
                probe.assert_result(events, succeeds=False)
        probe.assert_result([{"type": "error"}], succeeds=False)

    def test_shared_parser_rejects_malformed_records_and_accepts_both_wire_forms(self):
        for line in (b"not-json", b"[]", b'{"text":"missing-type"}'):
            with self.assertRaises(ValueError):
                parse_event(line)
        expected = {"type": "execution_complete"}
        self.assertEqual(parse_event(json.dumps(expected)), expected)
        self.assertEqual(parse_event("data: " + json.dumps(expected)), expected)

    def test_record_split_across_chunks_is_preserved_and_oversized_line_rejected(self):
        response = probe.Response.__new__(probe.Response)
        response.chunk = Mock(
            side_effect=[b'{"type":"std', b'out","text":"ok"}\n', b""]
        )
        self.assertEqual(
            list(response.events(parse_event)), [{"type": "stdout", "text": "ok"}]
        )
        response.chunk = Mock(side_effect=[b"x" * (probe.MAX_LINE + 1), b""])
        with self.assertRaises(RuntimeError):
            list(response.events(parse_event))

    def test_body_cap_and_truncated_content_length_are_not_success(self):
        response = probe.Response.__new__(probe.Response)
        response.deadline = time.monotonic() + 5
        response.transport = Mock()
        response.response = Mock()
        response.response.isclosed.return_value = False
        response.response.length = None
        response.count = probe.MAX_BODY
        response.response.read1.return_value = b"x"
        with self.assertRaises(RuntimeError):
            response.chunk()
        response.count = 0
        response.response.length = 10
        response.response.read1.return_value = b""
        with self.assertRaises(RuntimeError):
            response.chunk()
        response.response.isclosed.return_value = True
        with self.assertRaises(RuntimeError):
            response.chunk()
        response.response.length = 0
        response.transport.reset_mock()
        self.assertEqual(response.chunk(), b"")
        response.transport.settimeout.assert_not_called()


class OwnershipAndIdentityTests(unittest.TestCase):
    def test_upload_metadata_encodes_posix_mode_as_decimal_420(self):
        instance = bare_probe()
        instance.request = Mock(return_value=(200, b"", None))
        instance.upload("/owned/proxy", "/workspace/fixture.txt", b"synthetic-file")
        path, method, body, content_type = instance.request.call_args.args
        self.assertEqual((path, method), ("/owned/proxy/files/upload", "POST"))
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + content_type + "\r\n\r\n").encode() + body
        )
        parts = list(message.iter_parts())
        metadata = json.loads(parts[0].get_payload(decode=True))
        self.assertEqual(metadata, {"path": "/workspace/fixture.txt", "mode": 420})
        self.assertEqual(parts[1].get_payload(decode=True), b"synthetic-file")

    def test_cleanup_attempts_each_created_id_after_failure_without_listing(self):
        instance = bare_probe()
        instance.request = Mock(
            side_effect=[(503, b"", None), (204, b"", "true"), (404, b"", None)]
        )
        with patch("builtins.print"):
            instance.cleanup()
        self.assertEqual(instance.report["outcome"], "failed")
        self.assertEqual(
            [call.args[0] for call in instance.request.call_args_list],
            [
                "/sandboxes/created-a?wait_for_termination=true",
                "/sandboxes/created-b?wait_for_termination=true",
                "/sandboxes/created-b/proxy/44772/ping",
            ],
        )
        self.assertTrue(
            all(call.kwargs["cleanup"] for call in instance.request.call_args_list)
        )

    def test_cleanup_requires_receipt_and_no_owned_ids_means_no_requests(self):
        instance = bare_probe()
        instance.owned = ["created-a"]
        instance.request = Mock(return_value=(204, b"", None))
        with patch("builtins.print"):
            instance.cleanup()
        self.assertEqual(instance.report["outcome"], "failed")
        instance.owned = []
        instance.request.reset_mock()
        instance.cleanup()
        instance.request.assert_not_called()

    def test_identity_get_rejects_wrong_id_image_and_claim(self):
        instance = bare_probe()
        instance.args = Mock(image="registry/runtime@sha256:" + "a" * 64)
        good = {
            "id": "created-a",
            "image": {"uri": instance.args.image},
            "metadata": {"opsrabbit.runtime.contract": probe.CONTRACT},
        }
        variants = [
            {**good, "id": "another-session"},
            {**good, "image": {"uri": "registry/runtime:latest"}},
            {**good, "metadata": {}},
            {**good, "metadata": {"opsrabbit.runtime.contract": "unknown"}},
        ]
        for value in variants:
            instance.request = Mock(
                return_value=(200, json.dumps(value).encode(), None)
            )
            with (
                self.subTest(value=value),
                patch("builtins.print"),
                self.assertRaises(RuntimeError),
            ):
                instance.identity("created-a", "identity")
        instance.request = Mock(return_value=(200, json.dumps(good).encode(), None))
        with patch("builtins.print"):
            instance.identity("created-a", "identity")
        instance.request.assert_called_once_with("/sandboxes/created-a")

    def test_endpoint_failure_still_registers_created_id_for_cleanup(self):
        instance = bare_probe()
        instance.owned = []
        instance.identity = Mock()
        instance.args = Mock(image="registry/runtime@sha256:" + "a" * 64)
        instance.request = Mock(
            side_effect=[
                (202, b'{"id":"newly-created"}', None),
                (503, b"not-an-endpoint", None),
            ]
        )
        with patch("builtins.print"), self.assertRaises(ValueError):
            instance.create("probe")
        self.assertEqual(instance.owned, ["newly-created"])


class EndpointTests(unittest.TestCase):
    def test_same_authority_and_exact_mount_path_accept_normalized_wire_forms(self):
        origin = probe.urllib.parse.urlsplit("https://API.EXAMPLE:443/tenant/v1/")
        path = "/sandboxes/owned/proxy/44772"
        for endpoint in (
            "api.example/tenant/v1" + path,
            "https://API.EXAMPLE:443/tenant/v1" + path,
            "//api.example/tenant/v1" + path,
        ):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(probe.endpoint_matches(endpoint, origin, path))
        origin = probe.urllib.parse.urlsplit("https://api.example:8443/v1")
        self.assertTrue(
            probe.endpoint_matches("api.example:8443/v1" + path, origin, path)
        )
        self.assertFalse(probe.endpoint_matches("api.example/v1" + path, origin, path))

    def test_wrong_authority_prefix_or_url_components_cannot_match_suffix(self):
        origin = probe.urllib.parse.urlsplit("https://api.example/tenant/v1")
        path = "/sandboxes/owned/proxy/44772"
        for endpoint in (
            "other.example/tenant/v1" + path,
            "api.example:8443/tenant/v1" + path,
            "api.example:0/tenant/v1" + path,
            "api.example/wrong/tenant/v1" + path,
            "api.example" + path,
            "api.example/tenant/v1/../v1" + path,
            "http://api.example/tenant/v1" + path,
            "https://user@api.example/tenant/v1" + path,
            "https://api.example/tenant/v1" + path + "?identifier=other",
            "https://api.example/tenant/v1" + path + "#fragment",
            "https://api.example/tenant/v1" + path + "/",
            "https://api.example/tenant/v1/sandboxes/other/proxy/44772",
            "https://api.example/tenant/v1/sandboxes/owned/proxy/8081",
            "https://api.example/tenant/v1" + path + "\n",
            "/tenant/v1" + path,
            "https://api.example:invalid/tenant/v1" + path,
            None,
        ):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(probe.endpoint_matches(endpoint, origin, path))

    def test_create_rejects_foreign_endpoint_but_retains_ownership_for_cleanup(self):
        instance = bare_probe()
        instance.owned = []
        instance.origin = probe.urllib.parse.urlsplit("https://api.example/v1")
        instance.identity = Mock()
        instance.args = Mock(image="registry/runtime@sha256:" + "a" * 64)
        instance.request = Mock(
            side_effect=[
                (202, b'{"id":"created-a"}', None),
                (
                    200,
                    b'{"endpoint":"other.example/v1/sandboxes/created-a/proxy/44772"}',
                    None,
                ),
            ]
        )
        with patch("builtins.print"), self.assertRaises(RuntimeError):
            instance.create("probe")
        self.assertEqual(instance.owned, ["created-a"])
        self.assertEqual(instance.report["checks"][-1]["status"], "failed")


class NormalExitObservationTests(unittest.TestCase):
    def delayed_start(self, escaped):
        instance = bare_probe()
        clock = {"now": 100.0}
        instance.adversarial_argv = Mock(return_value=["shared-adversarial-payload"])
        instance.check_file = Mock()
        instance.record = Mock()

        def command(_prefix, text=None, *, argv=None, **_kwargs):
            if argv == ["shared-adversarial-payload"]:
                # Remote startup consumes much more than OBSERVE_SECONDS. The
                # parent then exits, but an escaped child writes three seconds
                # later. A request-start anchor would check too early and pass.
                clock["now"] += 20
                clock["completed"] = clock["now"]
                clock["child_write"] = clock["now"] + probe.SURVIVAL_SECONDS
            elif argv is not None:
                # Simulate only the observer's fixed sleep, without executing
                # code or waiting in real time; keep the real observe() logic.
                sleep_call = ast.parse(argv[2]).body[-1].value
                clock["now"] += ast.literal_eval(sleep_call.args[0])
            elif text == "printf workspace-still-usable":
                return [{"type": "stdout", "text": "workspace-still-usable"}]
            else:
                self.fail("unexpected probe command")
            return [{"type": "execution_complete"}]

        def download(_prefix, path):
            if (
                escaped
                and path.endswith("-survived-child")
                and clock["now"] >= clock["child_write"]
            ):
                return 200, b"bad", None
            return 404, b"", None

        instance.command = Mock(side_effect=command)
        instance.download = Mock(side_effect=download)
        return instance, clock

    def test_delayed_start_cannot_hide_escaped_descendant_write(self):
        instance, clock = self.delayed_start(escaped=True)
        with patch.object(probe.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(RuntimeError):
                instance.normal_exit("/owned/proxy")
        self.assertGreaterEqual(
            clock["now"], clock["completed"] + probe.OBSERVE_SECONDS
        )
        instance.record.assert_not_called()
        self.assertTrue(instance.adversarial_argv.call_args.kwargs["exit_after_fork"])

    def test_delayed_start_with_reaped_child_observes_full_window_before_pass(self):
        instance, clock = self.delayed_start(escaped=False)
        with patch.object(probe.time, "monotonic", side_effect=lambda: clock["now"]):
            instance.normal_exit("/owned/proxy")
        self.assertGreaterEqual(
            clock["now"], clock["completed"] + probe.OBSERVE_SECONDS
        )
        instance.record.assert_called_once_with(
            "Normal exit reaps detached descendant", True
        )


class DisconnectTests(unittest.TestCase):
    def instance(self):
        instance = bare_probe()
        instance.deadline = 1000
        instance.adversarial_argv = Mock(
            return_value=["python", "-c", "shared-payload"]
        )
        instance.parse_event = parse_event
        instance.request = Mock()
        instance.stopped = Mock()
        instance.observe = Mock()
        instance.record = Mock()
        return instance

    def test_disconnect_waits_for_payload_handshake_and_closes_without_delete(self):
        instance = self.instance()
        stream = Mock(status=200)
        stream.events.return_value = iter(
            [
                {"type": "init", "text": "execution-id"},
                {"type": "stdout", "text": "proof-disconnect-fixed-ready\n"},
            ]
        )
        opened = Mock()
        opened.__enter__ = Mock(return_value=stream)
        opened.__exit__ = Mock(return_value=False)
        instance.open = Mock(return_value=opened)
        with (
            patch.object(probe.uuid, "uuid4", return_value=Mock(hex="fixed")),
            patch.object(probe.time, "monotonic", return_value=100),
        ):
            instance.cancellation("/owned/proxy", "disconnect")
        instance.request.assert_not_called()
        opened.__exit__.assert_called_once()
        instance.stopped.assert_called_once()
        instance.observe.assert_called_once_with(
            "/owned/proxy", "proof-disconnect-fixed", 100, expected=False
        )
        payload = instance.open.call_args.args[2]
        self.assertEqual(payload["argv"], instance.adversarial_argv.return_value)
        self.assertEqual(payload["timeout"], probe.LONG_COMMAND_MS)

    def test_init_without_payload_handshake_cannot_prove_cancellation(self):
        instance = self.instance()
        stream = Mock(status=200)
        stream.events.return_value = iter([{"type": "init", "text": "execution-id"}])
        opened = Mock()
        opened.__enter__ = Mock(return_value=stream)
        opened.__exit__ = Mock(return_value=False)
        instance.open = Mock(return_value=opened)
        with (
            patch.object(probe.time, "monotonic", return_value=100),
            self.assertRaises(RuntimeError),
        ):
            instance.cancellation("/owned/proxy", "disconnect")
        instance.observe.assert_not_called()
        instance.record.assert_not_called()


class ConnectionDeadlineTests(unittest.TestCase):
    def network(self):
        origin = probe.urllib.parse.urlsplit("https://probe.example")
        connection = Mock(host="probe.example", port=443)
        connection.getresponse.return_value.status = 200
        raw, secure = Mock(), Mock()
        context = Mock()
        context.wrap_socket.return_value = secure
        address = (
            probe.socket.AF_INET,
            probe.socket.SOCK_STREAM,
            6,
            "",
            ("192.0.2.1", 443),
        )
        return origin, connection, raw, secure, context, address

    def test_stalled_dns_is_bounded_and_late_resolution_never_opens_connection(self):
        origin, connection, raw, _secure, context, address = self.network()
        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        resolver_threads = []

        def stalled_dns(*_args, **_kwargs):
            resolver_threads.append(threading.current_thread())
            entered.set()
            try:
                release.wait(2)
                return [address]
            finally:
                finished.set()

        with (
            patch.object(probe.ssl, "create_default_context", return_value=context),
            patch.object(probe.http.client, "HTTPSConnection", return_value=connection),
            patch.object(probe.socket, "getaddrinfo", side_effect=stalled_dns),
            patch.object(probe.socket, "socket", return_value=raw) as create_socket,
        ):
            start = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    probe.Response(origin, "/ping", "GET", None, {}, start + 0.1)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 1)
                self.assertTrue(resolver_threads[0].daemon)
                create_socket.assert_not_called()
                connection.request.assert_not_called()
                connection.close.assert_called()
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                # Complete the worker while patches remain active, ensuring a
                # late resolver result cannot initiate any connection.
                for worker in resolver_threads:
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
            create_socket.assert_not_called()
            connection.connect.assert_not_called()

    def test_numeric_connect_preserves_hostname_for_verified_tls_and_http(self):
        origin, connection, raw, secure, context, address = self.network()
        with (
            patch.object(
                probe.ssl, "create_default_context", return_value=context
            ) as default_context,
            patch.object(
                probe.http.client, "HTTPSConnection", return_value=connection
            ) as https,
            patch.object(
                probe.socket, "getaddrinfo", return_value=[address]
            ) as resolver,
            patch.object(probe.socket, "socket", return_value=raw),
        ):
            with probe.Response(origin, "/ping", "GET", None, {}, time.monotonic() + 5):
                raw.connect.assert_called_once_with(("192.0.2.1", 443))
                context.wrap_socket.assert_called_once_with(
                    raw, server_hostname="probe.example", do_handshake_on_connect=False
                )
                secure.do_handshake.assert_called_once()
                self.assertIs(connection.sock, secure)
            default_context.assert_called_once_with()
            self.assertEqual(https.call_args.args[:2], ("probe.example", 443))
            resolver.assert_called_once_with(
                "probe.example", 443, type=probe.socket.SOCK_STREAM
            )
            connection.connect.assert_not_called()
            secure.close.assert_called()

    def test_tls_failure_closes_both_owned_sockets_without_sending_request(self):
        origin, connection, raw, secure, context, address = self.network()
        secure.do_handshake.side_effect = probe.ssl.SSLCertVerificationError(
            "synthetic"
        )
        with (
            patch.object(probe.ssl, "create_default_context", return_value=context),
            patch.object(probe.http.client, "HTTPSConnection", return_value=connection),
            patch.object(probe.socket, "getaddrinfo", return_value=[address]),
            patch.object(probe.socket, "socket", return_value=raw),
            self.assertRaises(probe.ssl.SSLCertVerificationError),
        ):
            probe.Response(origin, "/ping", "GET", None, {}, time.monotonic() + 5)
        raw.close.assert_called()
        secure.close.assert_called()
        connection.request.assert_not_called()

    def test_watchdog_firing_during_tls_wrap_closes_late_tls_socket(self):
        origin, connection, raw, secure, context, address = self.network()
        timer = Mock()
        deadline_callbacks = []

        def make_timer(_delay, callback):
            deadline_callbacks.append(callback)
            return timer

        def wrap(*_args, **_kwargs):
            deadline_callbacks[0]()
            return secure

        context.wrap_socket.side_effect = wrap
        with (
            patch.object(probe.ssl, "create_default_context", return_value=context),
            patch.object(probe.http.client, "HTTPSConnection", return_value=connection),
            patch.object(probe.socket, "getaddrinfo", return_value=[address]),
            patch.object(probe.socket, "socket", return_value=raw),
            patch.object(probe.threading, "Timer", side_effect=make_timer),
            self.assertRaises(TimeoutError),
        ):
            probe.Response(origin, "/ping", "GET", None, {}, time.monotonic() + 5)
        secure.close.assert_called()
        raw.close.assert_called()
        secure.do_handshake.assert_not_called()
        connection.request.assert_not_called()
        timer.cancel.assert_called()


if __name__ == "__main__":
    unittest.main()
