"""Contract and fail-closed tests; no Azure credentials or network required."""

import os
import concurrent.futures
import threading
from email.message import Message
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from opensandbox_server.api.schema import CreateSandboxRequest
from opensandbox_server.services.azure_service import (
    ALLOWED_PATHS,
    AzureSandboxService,
    ForegroundCommand,
    foreground_command,
    stream_timeout,
)


class Credential:
    def get_token(self, *_):
        return SimpleNamespace(token="synthetic-test-only")

    def close(self):
        pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "AZURE_SESSION_IMAGE": "example.invalid/runtime@sha256:" + "a" * 64,
                "AZURE_SESSION_ENDPOINT": "https://proof.example.eastus2.azurecontainerapps.io",
                "AZURE_SESSION_STATE": self.tmp.name + "/state.db",
            },
        )
        self.env.start()
        self.allocated = {}
        self.calls = []
        self.block_next_ping = False
        self.blocked_sid = None
        self.ping_started = threading.Event()
        self.release_ping = threading.Event()
        self.bindings = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.transport))
        self.service = AzureSandboxService(
            SimpleNamespace(tenants=None),
            credential=Credential(),
            client=self.client,
            start_threads=False,
        )

    def transport(self, request):
        self.calls.append(request.url.path)
        sid = request.url.params["identifier"]
        if request.url.path == "/ping" and self.block_next_ping:
            self.block_next_ping = False
            self.blocked_sid = sid
            self.ping_started.set()
            if not self.release_ping.wait(5):
                raise TimeoutError("test did not release blocked ping")
        if request.url.path == "/ping":
            self.allocated[sid] = {"createdAt": "2026-11-03T00:00:00Z"}
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/init":
            binding = json.loads(request.content)
            self.bindings.append(binding)
            self.assertTrue(binding["accessTokenHash"].startswith("sha256:"))
            self.assertEqual(
                binding["envs"],
                {
                    "LD_LIBRARY_PATH": "/usr/local/lib",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/workspace",
                },
            )
            return httpx.Response(
                200,
                json={
                    "status": "initialized",
                    "sandboxId": sid,
                    "generation": 1,
                    "runtimeContract": "workspace-command-files-v1",
                },
            )
        if request.url.path == "/.management/stopSession":
            self.allocated.pop(sid, None)
            return httpx.Response(200)
        if sid not in self.allocated:
            return httpx.Response(
                400, json={"error": {"code": "SessionWithIdentifierNotFound"}}
            )
        return httpx.Response(200, json=self.allocated[sid])

    def tearDown(self):
        self.service.close()
        self.service.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, **overrides):
        data = {
            "image": {"uri": self.service.image},
            "timeout": 120,
            "resourceLimits": {"cpu": "0.5", "memory": "1Gi"},
            "entrypoint": ["sleep", "infinity"],
        }
        data.update(overrides)
        return CreateSandboxRequest.model_validate(data)

    def assertRejected(self, status, fn):
        with self.assertRaises(HTTPException) as error:
            fn()
        self.assertEqual(error.exception.status_code, status)

    def test_create_get_and_endpoint_do_not_expose_token(self):
        result = self.service._create(self.request())
        self.assertEqual(self.service.get_sandbox(result.id).id, result.id)
        endpoint = self.service.get_endpoint(result.id, 44772)
        self.assertIsNone(endpoint.headers)
        self.assertNotIn("azurecontainerapps.io", endpoint.endpoint)

    def test_runtime_contract_cannot_be_forged_by_caller(self):
        self.assertRejected(
            400,
            lambda: self.service._create(
                self.request(
                    metadata={
                        "opsrabbit.runtime.contract": "workspace-command-files-v1"
                    }
                )
            ),
        )
        self.assertEqual(self.allocated, {})

    def test_runtime_contract_is_confirmed_before_advertisement(self):
        original = self.client.post

        def missing_contract(*args, **kwargs):
            response = original(*args, **kwargs)
            data = response.json()
            data.pop("runtimeContract", None)
            return httpx.Response(200, json=data)

        with patch.object(self.client, "post", side_effect=missing_contract):
            self.assertRejected(503, lambda: self.service._create(self.request()))
        self.assertEqual(self.allocated, {})
        self.assertEqual(
            self.service._execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
        )

    def test_authenticated_metadata_binds_runtime_contract_to_image(self):
        result = self.service._create(self.request(metadata={"purpose": "fixture"}))
        info = self.service.get_sandbox(result.id)
        self.assertEqual(
            info.metadata["opsrabbit.runtime.contract"], "workspace-command-files-v1"
        )
        self.assertEqual(info.metadata["purpose"], "fixture")
        self.assertEqual(info.image.uri, self.service.image)
        persisted, contract = self.service._execute(
            "SELECT data,runtime_contract FROM sessions WHERE id=?", (result.id,)
        ).fetchone()
        self.assertEqual(contract, "workspace-command-files-v1")
        self.assertNotIn(
            "opsrabbit.runtime.contract", json.loads(persisted)["metadata"]
        )

    def test_init_failure_has_durable_generation_but_no_contract(self):
        original = self.client.post

        def failed_init(*args, **kwargs):
            sid = kwargs["params"]["identifier"]
            row = self.service._execute(
                "SELECT etag,runtime_contract,state FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            self.assertEqual(row, (self.allocated[sid]["createdAt"], "", "allocating"))
            original(*args, **kwargs)
            return httpx.Response(503)

        with patch.object(self.client, "post", side_effect=failed_init):
            self.assertRejected(503, lambda: self.service._create(self.request()))
        self.assertEqual(self.allocated, {})
        self.assertEqual(
            self.service._execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
        )

    def test_generation_change_during_init_is_not_activated_or_stopped(self):
        original = self.client.post

        def replace_after_init(*args, **kwargs):
            response = original(*args, **kwargs)
            sid = kwargs["params"]["identifier"]
            self.allocated[sid]["createdAt"] = "replacement-generation"
            return response

        with patch.object(self.client, "post", side_effect=replace_after_init):
            self.assertRejected(503, lambda: self.service._create(self.request()))
        self.assertNotIn("/.management/stopSession", self.calls)
        self.assertEqual(len(self.allocated), 1)
        self.assertEqual(
            self.service._execute(
                "SELECT state,runtime_contract FROM sessions"
            ).fetchone(),
            ("retiring", ""),
        )

    def test_confirmed_delete_refuses_replacement_generation(self):
        sid = self.service._create(self.request()).id
        self.allocated[sid]["createdAt"] = "replacement-generation"
        self.calls.clear()
        self.assertRejected(503, lambda: self.service.delete_sandbox_confirmed(sid))
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertIn(sid, self.allocated)
        self.assertEqual(
            self.service._execute(
                "SELECT state FROM sessions WHERE id=?", (sid,)
            ).fetchone()[0],
            "retiring",
        )

    def test_retirement_rechecks_journal_ownership_after_azure_lookup(self):
        sid = self.service._create(self.request()).id
        original = self.service._request

        def remove_ownership(sandbox_id, path, method="POST"):
            response = original(sandbox_id, path, method)
            if path == "/.management/getSession":
                self.service._execute("DELETE FROM sessions WHERE id=?", (sid,))
                self.service.db.commit()
            return response

        self.calls.clear()
        with patch.object(self.service, "_request", side_effect=remove_ownership):
            self.assertRejected(503, lambda: self.service._retire(sid))
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertIn(sid, self.allocated)

    def test_public_contract_comes_from_provenance_not_saved_metadata(self):
        sid = self.service._create(self.request()).id
        data = json.loads(self.service._row(sid)[0])
        data["metadata"] = {
            "purpose": "fixture",
            "opsrabbit.runtime.contract": "forged-contract",
            "opsrabbit.runtime.extra": "forged-capability",
        }
        self.service._execute(
            "UPDATE sessions SET data=? WHERE id=?", (json.dumps(data), sid)
        )
        self.service.db.commit()
        self.assertEqual(
            self.service.get_sandbox(sid).metadata,
            {
                "purpose": "fixture",
                "opsrabbit.runtime.contract": "workspace-command-files-v1",
            },
        )

    def test_stale_janitor_snapshot_cannot_stop_an_untracked_allocation(self):
        sid = self.service._create(self.request()).id
        self.service._execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.service.db.commit()
        original = self.service._execute

        def stale_snapshot(query, params=()):
            if query.startswith("SELECT id FROM sessions WHERE state='retiring'"):
                return SimpleNamespace(fetchall=lambda: [(sid,)])
            return original(query, params)

        self.calls.clear()
        with (
            patch.object(self.service, "_execute", side_effect=stale_snapshot),
            patch.object(self.service.shutdown, "wait", side_effect=[False, True]),
        ):
            self.service._janitor()
        self.assertEqual(self.calls, [])
        self.assertIn(sid, self.allocated)

    def test_recovery_cannot_stop_replaced_retiring_or_expired_allocations(self):
        for state, deadline in (
            ("retiring", time.time() + 120),
            ("active", time.time() - 1),
        ):
            sid = self.service._create(self.request()).id
            self.service._execute(
                "UPDATE sessions SET state=?,deadline=? WHERE id=?",
                (state, deadline, sid),
            )
            self.service.db.commit()
            self.allocated[sid]["createdAt"] = "replacement-generation"
            self.calls.clear()
            self.assertRejected(503, self.service._recover)
            self.assertNotIn("/.management/stopSession", self.calls)
            self.assertIn(sid, self.allocated)
            self.assertIsNotNone(
                self.service._execute(
                    "SELECT id FROM sessions WHERE id=?", (sid,)
                ).fetchone()
            )
            # Isolate the next recovery case from this deliberately fenced row.
            self.service._execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.service.db.commit()
            self.allocated.pop(sid)

    def test_recovery_without_generation_keeps_unknown_allocation_fenced(self):
        sid = self.service._create(self.request()).id
        self.service._execute(
            "UPDATE sessions SET state='retiring',etag='',runtime_contract='' WHERE id=?",
            (sid,),
        )
        self.service.db.commit()
        self.calls.clear()
        self.assertRejected(503, self.service._recover)
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertIn(sid, self.allocated)
        self.assertEqual(
            self.service._execute(
                "SELECT etag,state FROM sessions WHERE id=?", (sid,)
            ).fetchone(),
            ("", "retiring"),
        )
        # Reconciliation may erase the row only after Azure proves absence.
        self.allocated.pop(sid)
        self.calls.clear()
        self.service._recover()
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertIsNone(
            self.service._execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        )

    def test_legacy_forged_metadata_is_not_contract_provenance_on_restart(self):
        sid = self.service._create(self.request()).id
        data = json.loads(self.service._row(sid)[0])
        data["metadata"]["opsrabbit.runtime.contract"] = "workspace-command-files-v1"
        self.service._execute(
            "UPDATE sessions SET data=?,runtime_contract='' WHERE id=?",
            (json.dumps(data), sid),
        )
        self.service.db.commit()
        self.calls.clear()
        self.restart_controller()
        self.assertRejected(404, lambda: self.service.get_sandbox(sid))
        self.assertNotIn(sid, self.allocated)
        self.assertNotIn("/internal/init", self.calls)

    def test_sqlite_upgrade_adds_empty_provenance_without_trusting_legacy_json(self):
        import sqlite3

        self.service.db.close()
        self.service.db = sqlite3.connect(
            os.environ["AZURE_SESSION_STATE"], check_same_thread=False
        )
        self.service.db.execute("DROP TABLE sessions")
        self.service.db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT, deadline REAL, state TEXT, etag TEXT, token TEXT, secure_token TEXT, caller_token INTEGER)"
        )
        sid = "legacy-unowned"
        self.service.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                json.dumps(
                    {
                        "id": sid,
                        "image": {"uri": self.service.image},
                        "metadata": {
                            "opsrabbit.runtime.contract": "workspace-command-files-v1"
                        },
                    }
                ),
                time.time() + 120,
                "active",
                "2026-11-03T00:00:00Z",
                "synthetic",
                "",
                0,
            ),
        )
        self.service.db.commit()
        self.allocated[sid] = {"createdAt": "2026-11-03T00:00:00Z"}
        self.restart_controller()
        self.assertIn(
            "runtime_contract",
            [row[1] for row in self.service.db.execute("PRAGMA table_info(sessions)")],
        )
        self.assertNotIn(sid, self.allocated)
        self.assertRejected(404, lambda: self.service.get_sandbox(sid))
        self.restart_controller()  # Migration remains idempotent.

    def test_confirmed_retirement_rechecks_azure_after_prior_deletion(self):
        sid = self.service._create(self.request()).id
        self.service.delete_sandbox(sid)
        self.calls.clear()
        self.service.delete_sandbox_confirmed(sid)
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertRejected(404, lambda: self.service.get_sandbox(sid))

    def test_confirmed_retirement_does_not_delete_untracked_allocations(self):
        sid = "untracked-fixture"
        self.allocated[sid] = {"createdAt": "2026-11-03T00:00:00Z"}
        self.calls.clear()
        self.assertRejected(503, lambda: self.service.delete_sandbox_confirmed(sid))
        self.assertEqual(self.calls, ["/.management/getSession"])
        self.assertIn(sid, self.allocated)

    def test_confirmed_retirement_rejects_ambiguous_azure_failures(self):
        with patch.object(self.service, "_request", return_value=httpx.Response(503)):
            self.assertRejected(
                503, lambda: self.service.delete_sandbox_confirmed("absent")
            )

    def test_command_read_timeout_tracks_remaining_session_lease(self):
        timeout = stream_timeout(1800)
        self.assertEqual(timeout.connect, 45)
        self.assertEqual(timeout.write, 45)
        self.assertEqual(timeout.read, 1800)
        short_timeout = stream_timeout(20)
        self.assertEqual(short_timeout.connect, 20)
        self.assertEqual(short_timeout.read, 20)

    def test_sessions_from_superseded_runtime_images_are_retired(self):
        sid = self.service._create(self.request()).id
        self.service.image = "example.invalid/runtime@sha256:" + "b" * 64
        self.service.leader_ready = False
        self.service._recover()
        self.assertNotIn(sid, self.allocated)
        self.assertIsNone(
            self.service.db.execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        )

    def test_slow_allocation_does_not_block_an_existing_session(self):
        existing = self.service._create(self.request()).id
        self.block_next_ping = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            creating = pool.submit(self.service._create, self.request())
            self.assertTrue(self.ping_started.wait(1))
            reading = pool.submit(self.service.get_sandbox, existing)
            try:
                self.assertEqual(reading.result(timeout=1).id, existing)
            finally:
                self.release_ping.set()
            self.assertTrue(creating.result(timeout=3).id)

    def test_expired_allocating_session_is_not_activated(self):
        self.block_next_ping = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            creating = pool.submit(self.service._create, self.request())
            self.assertTrue(self.ping_started.wait(1))
            sid = self.blocked_sid
            self.service.db.execute(
                "UPDATE sessions SET deadline=? WHERE id=?", (time.time() - 1, sid)
            )
            self.service.db.commit()
            self.release_ping.set()
            with self.assertRaises(HTTPException) as error:
                creating.result(timeout=3)
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn(sid, self.allocated)
        self.assertIsNone(
            self.service.db.execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        )

    def test_delete_confirms_absence_and_never_reallocates(self):
        sid = self.service._create(self.request()).id
        self.service.delete_sandbox(sid)
        self.calls.clear()
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 44772))
        self.assertEqual(self.calls, [])

    def test_sdk_egress_endpoint_is_reserved_and_never_forwarded(self):
        sid = self.service._create(self.request()).id
        endpoint = self.service.get_endpoint(sid, 18080)
        self.assertEqual(endpoint.endpoint, f"127.0.0.1:8091/{sid}/egress")
        for internal in (True, False):
            self.assertRejected(
                501,
                lambda: self.service.get_endpoint(
                    sid, 18080, resolve_internal=internal, use_proxy_host=not internal
                ),
            )
        self.calls.clear()
        for path in ("policy", "command", "../command"):
            handler = SimpleNamespace(path=f"/{sid}/egress/{path}")
            self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])
        self.assertRejected(400, lambda: self.service.get_endpoint(sid, 8080))
        self.service.delete_sandbox(sid)
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 18080))

    def test_sdk_empty_chunked_delete_and_rejected_other_framing(self):
        sid = self.service._create(self.request()).id
        statuses = []
        handler = SimpleNamespace(
            path=f"/{sid}/directories?path=%2Fworkspace%2Ftemporary",
            command="DELETE",
            headers={"Transfer-Encoding": "chunked"},
            rfile=io.BytesIO(b"0\r\n\r\n"),
            wfile=io.BytesIO(),
            send_response=statuses.append,
            send_header=lambda *_: None,
            end_headers=lambda: None,
        )
        self.service.forward(handler)
        self.assertEqual(statuses, [200])
        self.calls.clear()
        for method, headers, body in [
            ("POST", {"Transfer-Encoding": "chunked"}, b"0\r\n\r\n"),
            ("DELETE", {"Transfer-Encoding": "chunked"}, b"1\r\nx\r\n0\r\n\r\n"),
            (
                "DELETE",
                {"Transfer-Encoding": "chunked", "Content-Length": "0"},
                b"0\r\n\r\n",
            ),
            ("DELETE", {"Transfer-Encoding": "gzip, chunked"}, b"0\r\n\r\n"),
            ("DELETE", {"Transfer-Encoding": "chunked"}, b"0\r\n"),
        ]:
            handler.command, handler.headers, handler.rfile = (
                method,
                headers,
                io.BytesIO(body),
            )
            self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])
        duplicate_headers = Message()
        duplicate_headers["Transfer-Encoding"] = "chunked"
        duplicate_headers["Transfer-Encoding"] = "gzip"
        handler.headers = duplicate_headers
        handler.rfile = io.BytesIO(b"0\r\n\r\n")
        self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])

    def test_expired_id_is_retired_without_allocation(self):
        sid = self.service._create(self.request()).id
        self.service.db.execute(
            "UPDATE sessions SET deadline=? WHERE id=?", (time.time() - 1, sid)
        )
        self.calls.clear()
        self.assertRejected(404, lambda: self.service.get_sandbox(sid))
        self.assertNotIn("/ping", self.calls)
        self.assertNotIn(sid, self.allocated)

    def test_missing_azure_session_does_not_reallocate(self):
        sid = self.service._create(self.request()).id
        self.allocated.clear()
        self.calls.clear()
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 44772))
        self.assertNotIn("/ping", self.calls)

    def restart_controller(self):
        self.service.close()
        self.service.db.close()
        self.service = AzureSandboxService(
            SimpleNamespace(tenants=None),
            credential=Credential(),
            client=httpx.Client(transport=httpx.MockTransport(self.transport)),
            start_threads=False,
        )

    def test_restart_recovers_verified_active_session_from_persistent_journal(self):
        sid = self.service._create(self.request()).id
        self.restart_controller()
        self.assertEqual(self.service.get_sandbox(sid).id, sid)
        self.assertEqual(
            self.service.db.execute(
                "SELECT state FROM sessions WHERE id=?", (sid,)
            ).fetchone()[0],
            "active",
        )

    def test_recovery_can_validate_active_session_before_leader_ready(self):
        sid = self.service._create(self.request()).id
        self.service.leader_ready = False
        row = self.service._live(sid, require_leader=False)
        self.assertEqual(row[2], "active")
        self.assertFalse(self.service.leader_ready)

    def test_restart_retires_interrupted_allocation_before_serving(self):
        sid = self.service._create(self.request()).id
        self.service.db.execute(
            "UPDATE sessions SET state='allocating' WHERE id=?", (sid,)
        )
        self.service.db.commit()
        self.restart_controller()
        self.assertNotIn(sid, self.allocated)
        self.assertIsNone(
            self.service.db.execute(
                "SELECT id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        )

    def test_generation_change_rejected(self):
        sid = self.service._create(self.request()).id
        self.allocated[sid]["createdAt"] = "different-generation"
        self.assertRejected(409, lambda: self.service.get_endpoint(sid, 44772))

    def test_fixed_image_and_resources(self):
        for override in [
            {"image": {"uri": "another-image"}},
            {"resourceLimits": {"cpu": "2", "memory": "4Gi"}},
        ]:
            self.assertRejected(
                400, lambda: self.service._create(self.request(**override))
            )
        self.assertEqual(self.calls, [])

    def test_environment_and_lifecycle_not_silently_ignored(self):
        self.assertRejected(
            400, lambda: self.service._create(self.request(env={"KEY": "value"}))
        )
        self.assertRejected(
            400, lambda: self.service._create(self.request(timeout=None))
        )
        self.assertEqual(self.calls, [])

    def test_unknown_identifier_never_reaches_azure(self):
        self.assertRejected(404, lambda: self.service.get_endpoint("unknown", 44772))
        self.assertEqual(self.calls, [])

    def test_pool_capacity_is_bounded(self):
        self.service._create(self.request())
        self.service._create(self.request())
        self.assertRejected(429, lambda: self.service._create(self.request()))

    def test_pool_capacity_matches_configured_limit(self):
        with patch.dict(os.environ, {"AZURE_SESSION_MAX_SESSIONS": "3"}):
            service = AzureSandboxService(
                SimpleNamespace(tenants=None),
                credential=Credential(),
                client=self.client,
                start_threads=False,
            )
        try:
            for _ in range(3):
                service._create(self.request())
            self.assertRejected(429, lambda: service._create(self.request()))
        finally:
            service.close()

    def test_invalid_pool_capacity_fails_startup(self):
        for value in ("0", "101", "bad"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"AZURE_SESSION_MAX_SESSIONS": value}),
            ):
                with self.assertRaises(ValueError):
                    AzureSandboxService(
                        SimpleNamespace(tenants=None),
                        credential=Credential(),
                        client=self.client,
                        start_threads=False,
                    )

    def test_unsupported_operations_fail_explicitly(self):
        self.assertRejected(501, lambda: self.service.pause_sandbox("id"))
        self.assertRejected(400, lambda: self.service.get_endpoint("id", 22))

    def test_reserved_query_cannot_choose_another_session(self):
        handler = SimpleNamespace(path="/some-id/ping?identifier=other", headers={})
        self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])

    def test_status_path_cannot_traverse_to_management(self):
        for path in (
            "/command/status/../../internal/init",
            "/command/status/%2e%2e/%2e%2e/internal/init",
        ):
            handler = SimpleNamespace(path="/some-id" + path, headers={})
            self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])

    def test_management_paths_not_proxied(self):
        handler = SimpleNamespace(path="/some-id/.management/stopSession", headers={})
        self.assertRejected(400, lambda: self.service.forward(handler))
        self.assertEqual(self.calls, [])

    def test_controller_allows_all_gateway_confined_file_routes(self):
        self.assertTrue(
            {
                "/files/search",
                "/files/mv",
                "/files/permissions",
                "/files/replace",
            }.issubset(ALLOWED_PATHS)
        )

    def test_retirement_failure_keeps_execution_denied(self):
        sid = self.service._create(self.request()).id
        original = self.service._request

        def failed_stop(sid, path, method="POST"):
            if path.endswith("stopSession"):
                return httpx.Response(503)
            return original(sid, path, method)

        self.service._request = failed_stop
        self.assertRejected(503, lambda: self.service.delete_sandbox(sid))
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 44772))
        self.assertEqual(
            self.service.db.execute(
                "SELECT state FROM sessions WHERE id=?", (sid,)
            ).fetchone()[0],
            "retiring",
        )

    def test_restart_retains_tombstone(self):
        sid = self.service._create(self.request()).id
        self.service.delete_sandbox(sid)
        self.service.db.close()
        import sqlite3

        self.service.db = sqlite3.connect(
            os.environ["AZURE_SESSION_STATE"], check_same_thread=False
        )
        self.calls.clear()
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 44772))
        self.assertEqual(self.calls, [])

    def test_no_automatic_renewal(self):
        self.assertIsNone(self.service.get_access_renew_extend_seconds("any"))

    def test_product_create_preserves_isolation_auth_contract(self):
        token = "synthetic-test-token-" + "x" * 32
        result = self.service._create(
            self.request(
                timeout=3600,
                entrypoint=["tail", "-f", "/dev/null"],
                secureAccess=True,
                env={"EXECD_ACCESS_TOKEN": token},
                extensions={"bootstrap.execd.isolation": "enable"},
                networkPolicy={"defaultAction": "deny", "egress": []},
            )
        )
        endpoint = self.service.get_endpoint(result.id, 44772)
        self.assertGreater(len(endpoint.headers["OpenSandbox-Secure-Access"]), 30)
        self.assertNotEqual(endpoint.headers["OpenSandbox-Secure-Access"], token)
        self.assertNotIn(token, result.model_dump_json())
        self.assertNotIn(token, json.dumps(self.bindings[0]["envs"]))
        self.assertEqual(
            self.bindings[0]["accessTokenHash"],
            "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        )
        handler = SimpleNamespace(
            path=f"/{result.id}/ping", headers={}, rfile=io.BytesIO()
        )
        self.assertRejected(401, lambda: self.service.forward(handler))

    def test_product_settings_cannot_expand_pool_permissions(self):
        for override in [
            {"networkPolicy": {"defaultAction": "allow", "egress": []}},
            {"entrypoint": ["sh", "-c", "echo ignored"]},
            {"extensions": {"bootstrap.execd.isolation": "disable"}},
            {"env": {"EXECD_ACCESS_TOKEN": "short"}},
            {"env": {"EXECD_ACCESS_TOKEN": "x" * 40}},
            {"timeout": 604801},
            {"timeout": 3601},
        ]:
            self.assertRejected(
                400, lambda: self.service._create(self.request(**override))
            )
        self.assertEqual(self.calls, [])

    def test_renewal_preserves_session_and_updates_deadline(self):
        sid = self.service._create(self.request()).id
        created_at = datetime.fromisoformat(
            json.loads(self.service._row(sid)[0])["createdAt"]
        )
        expiry = created_at + timedelta(minutes=30)
        response = self.service.renew_expiration(
            sid, SimpleNamespace(expires_at=expiry)
        )
        self.assertEqual(response.expires_at, expiry)
        self.assertEqual(self.service.get_sandbox(sid).expires_at, expiry)
        self.assertEqual(self.calls.count("/ping"), 1)
        self.assertAlmostEqual(self.service._row(sid)[1], expiry.timestamp())

    def test_renewal_rejects_expired_or_replaced_runtime(self):
        sid = self.service._create(self.request()).id
        self.allocated[sid]["createdAt"] = "replacement"
        self.assertRejected(
            409,
            lambda: self.service.renew_expiration(
                sid,
                SimpleNamespace(
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
                ),
            ),
        )
        self.assertEqual(self.calls.count("/ping"), 1)

    def test_renewal_cannot_exceed_the_pool_idle_limit(self):
        sid = self.service._create(self.request()).id
        self.assertRejected(
            400,
            lambda: self.service.renew_expiration(
                sid,
                SimpleNamespace(
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=2)
                ),
            ),
        )

    def test_renewal_cannot_extend_past_absolute_session_lifetime(self):
        sid = self.service._create(self.request()).id
        created_at = datetime.fromisoformat(
            json.loads(self.service._row(sid)[0])["createdAt"]
        )
        self.assertRejected(
            400,
            lambda: self.service.renew_expiration(
                sid,
                SimpleNamespace(expires_at=created_at + timedelta(hours=1, seconds=1)),
            ),
        )

    def test_retirement_erases_stored_credentials_and_metadata(self):
        sid = self.service._create(self.request(metadata={"purpose": "synthetic"})).id
        self.service.delete_sandbox(sid)
        self.assertIsNone(
            self.service.db.execute(
                "SELECT * FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        )

    def test_file_range_and_response_headers_are_preserved(self):
        sid = self.service._create(self.request()).id
        original = self.transport

        def transport(request):
            if request.url.path == "/files/download":
                self.assertEqual(request.headers["Range"], "bytes=1-2")
                self.assertNotIn("OpenSandbox-Secure-Access", request.headers)
                return httpx.Response(
                    206, content=b"bc", headers={"Content-Range": "bytes 1-2/4"}
                )
            return original(request)

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        response_headers = {}
        statuses = []
        handler = SimpleNamespace(
            path=f"/{sid}/files/download?path=%2Fworkspace%2Ftest",
            command="GET",
            headers={"Range": "bytes=1-2"},
            rfile=io.BytesIO(),
            wfile=io.BytesIO(),
            send_response=statuses.append,
            send_header=response_headers.__setitem__,
            end_headers=lambda: None,
        )
        self.service.forward(handler)
        self.assertEqual(statuses, [206])
        self.assertEqual(handler.wfile.getvalue(), b"bc")
        self.assertEqual(response_headers["Content-Range"], "bytes 1-2/4")
        self.assertEqual(response_headers["Content-Length"], "2")

    def test_incomplete_file_download_fails_before_success_headers(self):
        sid = self.service._create(self.request()).id
        original = self.transport

        def truncated(request):
            if request.url.path != "/files/download":
                return original(request)
            return httpx.Response(
                200,
                content=b"short",
                headers={
                    "Content-Length": "10",
                    "Content-Type": "application/octet-stream",
                },
            )

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(truncated))
        statuses = []
        handler = SimpleNamespace(
            path=f"/{sid}/files/download?path=%2Fworkspace%2Ffile",
            command="GET",
            headers={},
            rfile=io.BytesIO(),
            wfile=io.BytesIO(),
            send_response=statuses.append,
        )
        self.assertRejected(502, lambda: self.service.forward(handler))
        self.assertEqual(statuses, [])
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_oversized_file_response_never_starts_success(self):
        sid = self.service._create(self.request()).id
        original = self.transport

        def transport(request):
            if request.url.path == "/files/download":
                return httpx.Response(200, content=b"x" * 32)
            return original(request)

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        statuses = []
        handler = SimpleNamespace(
            path=f"/{sid}/files/download?path=%2Fworkspace%2Ftest",
            command="GET",
            headers={},
            rfile=io.BytesIO(),
            wfile=io.BytesIO(),
            send_response=statuses.append,
        )
        with patch("opensandbox_server.services.azure_service.MAX_BODY", 16):
            self.assertRejected(413, lambda: self.service.forward(handler))
        self.assertEqual(statuses, [])
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_foreground_requires_strict_boolean_or_omission(self):
        self.assertTrue(foreground_command(b"{}"))
        self.assertTrue(foreground_command(b'{"background":false}'))
        self.assertFalse(foreground_command(b'{"background":true}'))
        for value in (None, 0, 1, "false", [], {}):
            self.assertRejected(
                400, lambda: foreground_command(json.dumps({"background": value}))
            )
        for value in (b"null", b"[]", b"{"):
            self.assertRejected(400, lambda: foreground_command(value))

    def test_command_tracker_fragmented_wire_forms_and_terminal_races(self):
        execution_id = "a" * 32
        for prefix in (b"", b"data: "):
            state = ForegroundCommand()
            record = (
                prefix
                + json.dumps({"type": "init", "text": execution_id}).encode()
                + b"\n\n"
            )
            for chunk in (record[:7], record[7:25], record[25:]):
                state.feed(chunk)
            state.abandoned.set()
            self.assertEqual(state.cancel_id(), execution_id)
            state.feed(b'{"type":"stdout","text":"execution_complete"}\n')
            self.assertEqual(state.cancel_id(), execution_id)
            state.feed(prefix + b'{"type":"execution_complete"}\n')
            self.assertIsNone(state.cancel_id())
        for value in ("../other", "a" * 31, "B" * 32, 1):
            state = ForegroundCommand()
            state.feed(json.dumps({"type": "init", "text": value}).encode() + b"\n")
            state.abandoned.set()
            self.assertIsNone(state.cancel_id())

    def test_tracker_never_reads_forged_init_inside_oversized_stdout(self):
        state = ForegroundCommand()
        state.feed(b"x" * (64 * 1024 + 1))
        self.assertLessEqual(len(state.pending), 64 * 1024)
        state.feed(b'{"type":"init","text":"' + b"a" * 32 + b'"}\n')
        state.abandoned.set()
        self.assertIsNone(state.cancel_id())
        state.feed(b'{"type":"init","text":"' + b"b" * 32 + b'"}\n')
        self.assertEqual(state.cancel_id(), "b" * 32)
        state.feed(b'{"type":"init","text":"' + b"c" * 32 + b'"}\n')
        self.assertIsNone(state.cancel_id())

    def cancel_fixture(self):
        self.service.max_sessions = 16  # Independent bindings for each subcase.
        sid = self.service._create(self.request()).id
        row = self.service._row(sid)
        state = ForegroundCommand()
        state.feed(b'{"type":"init","text":"' + b"a" * 32 + b'"}\n')
        state.abandoned.set()
        headers = {"Authorization": "Bearer synthetic", "X-EXECD-ACCESS-TOKEN": row[4]}
        return sid, row, state, headers

    def test_exact_cancel_checks_generation_and_never_reuses_callers_selectors(self):
        sid, row, state, headers = self.cancel_fixture()
        requests = []
        original = self.transport

        def transport(request):
            requests.append(request)
            return original(request)

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        for _ in range(2):
            self.service._cancel_foreground(sid, row, state, headers)
        self.assertEqual(
            [(r.method, r.url.path) for r in requests],
            [("POST", "/.management/getSession"), ("DELETE", "/command")],
        )
        self.assertEqual(
            dict(requests[-1].url.params),
            {"identifier": sid, "api-version": "2025-02-02-preview", "id": "a" * 32},
        )
        self.assertEqual(requests[-1].headers["X-EXECD-ACCESS-TOKEN"], row[4])
        self.assertEqual(requests[-1].headers["Connection"], "close")
        self.assertEqual(requests[-1].content, b"")
        self.assertLessEqual(requests[-1].extensions["timeout"]["read"], 0.25)

    def test_cancel_refuses_missing_expired_rebound_and_nonleader_sessions(self):
        for mutation in (
            "missing",
            "expired",
            "token",
            "generation",
            "nonleader",
            "complete",
        ):
            with self.subTest(mutation=mutation):
                sid, row, state, headers = self.cancel_fixture()
                if mutation == "missing":
                    self.service.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                elif mutation == "expired":
                    self.service.db.execute(
                        "UPDATE sessions SET deadline=0 WHERE id=?", (sid,)
                    )
                elif mutation in ("token", "generation"):
                    column = "token" if mutation == "token" else "etag"
                    self.service.db.execute(
                        f"UPDATE sessions SET {column}='changed' WHERE id=?", (sid,)
                    )
                elif mutation == "nonleader":
                    self.service.leader_ready = False
                else:
                    state.feed(b'{"type":"execution_complete"}\n')
                with patch.object(self.service, "_cancel_request") as request:
                    self.service._cancel_foreground(sid, row, state, headers)
                request.assert_not_called()
                self.service.leader_ready = True

    def test_cancel_refuses_changed_azure_generation_and_late_completion(self):
        for outcome in ("replacement", "completion", "expiry", "timeout"):
            with self.subTest(outcome=outcome):
                sid, row, state, headers = self.cancel_fixture()

                def generation(*args, **kwargs):
                    if outcome == "timeout":
                        raise httpx.ReadTimeout("synthetic")
                    if outcome == "completion":
                        state.feed(b'{"type":"execution_complete"}\n')
                    if outcome == "expiry":
                        self.service.db.execute(
                            "UPDATE sessions SET deadline=0 WHERE id=?", (sid,)
                        )
                    return 200, {
                        "createdAt": "replacement"
                        if outcome == "replacement"
                        else row[3]
                    }

                with patch.object(
                    self.service, "_cancel_request", side_effect=generation
                ) as request:
                    self.service._cancel_foreground(sid, row, state, headers)
                self.assertEqual(request.call_count, 1)

    def test_background_and_completed_foreground_never_send_cancel(self):
        sid = self.service._create(self.request()).id
        original = self.transport
        deletes = []

        def transport(request):
            if request.url.path == "/command":
                if request.method == "DELETE":
                    deletes.append(request)
                    return httpx.Response(200)
                payload = json.loads(request.content)
                events = b'{"type":"init","text":"' + b"e" * 32 + b'"}\n'
                if payload.get("background") is not True:
                    events += b'{"type":"execution_complete"}'
                return httpx.Response(200, content=events)
            return original(request)

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        for background in (True, False, "omitted"):
            with self.subTest(background=background):
                payload = {"command": "sleep 30"}
                if background != "omitted":
                    payload["background"] = background
                body = json.dumps(payload).encode()
                downstream, peer = socket.socketpair()
                try:
                    handler = SimpleNamespace(
                        path=f"/{sid}/command",
                        command="POST",
                        headers={"Content-Length": str(len(body))},
                        rfile=io.BytesIO(body),
                        wfile=io.BytesIO(),
                        connection=downstream,
                        send_response=lambda _: None,
                        send_header=lambda *_: None,
                        end_headers=lambda: None,
                    )
                    self.service.forward(handler)
                finally:
                    downstream.close()
                    peer.close()
        self.assertEqual(deletes, [])

    def test_cancel_does_not_wait_for_ordinary_same_session_io(self):
        sid, row, state, headers = self.cancel_fixture()
        held = threading.Event()
        release = threading.Event()

        def ordinary_io():
            with self.service._session_lock(sid):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=ordinary_io)
        worker.start()
        try:
            self.assertTrue(held.wait(1))
            before = time.monotonic()
            with patch.object(
                self.service,
                "_cancel_request",
                side_effect=[(200, {"createdAt": row[3]}), (200, None)],
            ) as request:
                self.service._cancel_foreground(sid, row, state, headers)
            self.assertLess(time.monotonic() - before, 1.5)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[2], "DELETE")
            self.assertTrue(worker.is_alive())  # Ordinary I/O still holds its lock.
        finally:
            release.set()
            worker.join(2)

    def test_cancel_diagnostics_never_include_request_or_exception_data(self):
        sid, row, state, headers = self.cancel_fixture()
        module = "opensandbox_server.services.azure_service"
        with (
            patch.object(
                self.service,
                "_cancel_request",
                side_effect=[
                    (200, {"createdAt": row[3]}),
                    httpx.ReadTimeout("sensitive-header-command-url-must-not-appear"),
                ],
            ),
            self.assertLogs(module, level="INFO") as logs,
        ):
            self.service._cancel_foreground(sid, row, state, headers)
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["command_cancel.delete_dispatch", "command_cancel.transport_timeout"],
        )
        for record in logs.records:
            self.assertFalse(record.exc_info)
            self.assertEqual(record.args, ())

    def disconnect_init_case(self, mode):
        sid = self.service._create(self.request()).id
        execution_id = "f" * 32
        state = ForegroundCommand()
        released = threading.Event()
        written = threading.Event()
        deletes = []
        errors = []
        owner = self
        no_id = mode in ("missing", "oversized")

        class InitStream(httpx.SyncByteStream):
            def __iter__(self):
                record = json.dumps({"type": "init", "text": execution_id}).encode()
                if mode in ("header", "missing", "oversized"):
                    owner.assertTrue(state.abandoned.wait(1))
                if mode == "missing":
                    yield b'{"type":"init","text":"'
                elif mode == "oversized":
                    yield b"x" * 256
                elif mode == "unterminated":
                    yield record
                    raise httpx.ReadError("synthetic interruption after complete JSON")
                elif mode == "write":
                    yield record  # Complete JSON, no trailing newline, then broken pipe.
                else:
                    yield record[:10]
                    owner.assertTrue(state.abandoned.wait(1))
                    owner.assertFalse(released.is_set(), "closed before remaining init")
                    yield record[10:]  # Fragmented init with no trailing newline.
                owner.assertTrue(released.wait(2), "stream shutdown missing")

            def close(self):
                released.set()

        class Writer(io.BytesIO):
            def write(self, value):
                written.set()
                if mode == "write":
                    raise BrokenPipeError("synthetic downstream failure")
                return super().write(value)

        original = self.transport

        def transport(request):
            if request.url.path == "/command" and request.method == "POST":
                return httpx.Response(
                    200,
                    stream=InitStream(),
                    extensions={
                        "network_stream": SimpleNamespace(
                            get_extra_info=lambda _: SimpleNamespace(
                                shutdown=lambda _: released.set()
                            )
                        )
                    },
                )
            if request.url.path == "/command" and request.method == "DELETE":
                deletes.append(request)
                return httpx.Response(200)
            return original(request)

        def end_headers():
            if mode in ("header", "missing", "oversized"):
                raise BrokenPipeError("synthetic header failure")

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        downstream, peer = socket.socketpair()
        body = b'{"command":"sleep 30","background":false}'
        handler = SimpleNamespace(
            path=f"/{sid}/command?id=unrelated",
            command="POST",
            headers={"Content-Length": str(len(body))},
            rfile=io.BytesIO(body),
            wfile=Writer(),
            connection=downstream,
            send_response=lambda _: None,
            send_header=lambda *_: None,
            end_headers=end_headers,
        )

        def run():
            try:
                self.service.forward(handler)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        module = "opensandbox_server.services.azure_service."
        try:
            with (
                patch(module + "ForegroundCommand", return_value=state),
                patch(
                    module + "COMMAND_INIT_DRAIN_SECONDS",
                    0.2 if mode == "missing" else 2,
                ),
                patch(
                    module + "COMMAND_INIT_DRAIN_BYTES",
                    128 if mode == "oversized" else 128 * 1024,
                ),
            ):
                worker.start()
                if mode == "fragmented":
                    self.assertTrue(written.wait(1))
                    peer.close()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            if mode == "unterminated":
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], httpx.ReadError)
            else:
                self.assertEqual(errors, [])
            self.assertEqual(len(deletes), 0 if no_id else 1)
            if deletes:
                self.assertEqual(
                    dict(deletes[0].url.params),
                    {
                        "identifier": sid,
                        "api-version": "2025-02-02-preview",
                        "id": execution_id,
                    },
                )
            self.assertTrue(released.is_set())
        finally:
            released.set()
            peer.close()
            downstream.close()
            worker.join(3)

    def test_disconnect_reads_fragmented_init_before_interrupting(self):
        self.disconnect_init_case("fragmented")

    def test_failed_headers_still_read_delayed_init_and_cancel(self):
        self.disconnect_init_case("header")

    def test_write_failure_preserves_unterminated_complete_init(self):
        self.disconnect_init_case("write")

    def test_transport_failure_preserves_unterminated_complete_init(self):
        self.disconnect_init_case("unterminated")

    def test_missing_init_has_bounded_drain_without_broad_delete(self):
        self.disconnect_init_case("missing")

    def test_oversized_init_drain_stops_without_broad_delete(self):
        self.disconnect_init_case("oversized")

    def test_quiet_foreground_disconnect_interrupts_stream_and_cancels_exact_command(
        self,
    ):
        sid = self.service._create(self.request()).id
        execution_id = "d" * 32
        released = threading.Event()
        written = threading.Event()
        deleted = threading.Event()
        errors = []
        owner = self

        class QuietStream(httpx.SyncByteStream):
            def __iter__(self):
                yield (
                    json.dumps({"type": "init", "text": execution_id}).encode() + b"\n"
                )
                if not released.wait(4):
                    raise AssertionError("quiet upstream not interrupted")

            def close(self):
                released.set()

        class Writer(io.BytesIO):
            def write(self, value):
                result = super().write(value)
                written.set()
                return result

        original = self.transport

        def transport(request):
            if request.url.path == "/command" and request.method == "POST":
                owner.assertEqual(request.headers["Connection"], "close")
                return httpx.Response(
                    200,
                    stream=QuietStream(),
                    extensions={
                        "network_stream": SimpleNamespace(
                            get_extra_info=lambda _: SimpleNamespace(
                                shutdown=lambda _: released.set()
                            )
                        )
                    },
                )
            if request.url.path == "/command" and request.method == "DELETE":
                owner.assertEqual(
                    dict(request.url.params),
                    {
                        "id": execution_id,
                        "identifier": sid,
                        "api-version": "2025-02-02-preview",
                    },
                )
                deleted.set()
                return httpx.Response(200)
            return original(request)

        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(transport))
        downstream, peer = socket.socketpair()
        body = b'{"command":"sleep 30","background":false}'
        handler = SimpleNamespace(
            path=f"/{sid}/command?id=unrelated",
            command="POST",
            headers={"Content-Length": str(len(body))},
            rfile=io.BytesIO(body),
            wfile=Writer(),
            connection=downstream,
            send_response=lambda _: None,
            send_header=lambda *_: None,
            end_headers=lambda: None,
        )

        def run():
            try:
                self.service.forward(handler)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        try:
            worker.start()
            self.assertTrue(written.wait(2))
            peer.close()
            self.assertTrue(deleted.wait(3))
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        finally:
            released.set()
            peer.close()
            downstream.close()
            worker.join(5)


if __name__ == "__main__":
    unittest.main()
