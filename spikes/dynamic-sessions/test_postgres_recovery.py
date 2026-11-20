"""Restart-recovery tests for the durable PostgreSQL session journal."""

import json
import os
import signal
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from fastapi import HTTPException
from opensandbox_server.services.azure_service import AzureSandboxService


class Credential:
    def get_token(self, *_):
        return SimpleNamespace(token="synthetic-test-only")

    def close(self):
        pass


class PostgresRecoveryTests(unittest.TestCase):
    def setUp(self):
        os.environ["AZURE_SESSION_IMAGE"] = "example.invalid/runtime@sha256:" + "a" * 64
        os.environ["AZURE_SESSION_ENDPOINT"] = (
            "https://proof.example.eastus2.azurecontainerapps.io"
        )
        admin_url = os.environ["AZURE_SESSION_TEST_ADMIN_URL"]
        parsed = urlsplit(admin_url)
        self.database_url = urlunsplit(
            (
                parsed.scheme,
                "opensandbox_test:synthetic-controller-password@"
                + parsed.netloc.rsplit("@", 1)[-1],
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        self.requests = []
        self.allocated = {"active-session": {"createdAt": "2026-11-09T00:00:00Z"}}
        with psycopg.connect(admin_url, autocommit=True) as db:
            db.execute("DROP SCHEMA IF EXISTS opensandbox CASCADE")
            db.execute("DROP ROLE IF EXISTS opensandbox_test")
            db.execute(
                "CREATE ROLE opensandbox_test LOGIN PASSWORD 'synthetic-controller-password'"
            )
            db.execute("CREATE SCHEMA opensandbox AUTHORIZATION opensandbox_test")
        db.close()
        os.environ["AZURE_SESSION_DATABASE_URL"] = self.database_url
        bootstrap = AzureSandboxService(
            SimpleNamespace(tenants=None),
            credential=Credential(),
            client=httpx.Client(transport=httpx.MockTransport(self.transport)),
            start_threads=False,
        )
        bootstrap.close()

    def tearDown(self):
        with psycopg.connect(
            os.environ["AZURE_SESSION_TEST_ADMIN_URL"], autocommit=True
        ) as db:
            db.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='opensandbox_test' AND pid<>pg_backend_pid()"
            )
            db.execute("DROP SCHEMA IF EXISTS opensandbox CASCADE")
            db.execute("DROP ROLE IF EXISTS opensandbox_test")
        db.close()

    def transport(self, request):
        sid = request.url.params["identifier"]
        self.requests.append((request.method, request.url.path, sid))
        if request.url.path == "/.management/stopSession":
            self.allocated.pop(sid, None)
            return httpx.Response(200)
        if request.url.path == "/.management/getSession":
            if sid in self.allocated:
                return httpx.Response(200, json=self.allocated[sid])
            return httpx.Response(
                400,
                json={"error": {"code": "SessionWithIdentifierNotFound"}},
            )
        raise AssertionError("Unexpected recovery request")

    def service(self):
        return AzureSandboxService(
            SimpleNamespace(tenants=None),
            credential=Credential(),
            client=httpx.Client(transport=httpx.MockTransport(self.transport)),
            start_threads=False,
        )

    def test_restart_revalidates_active_session_without_reallocating(self):
        with psycopg.connect(self.database_url) as db:
            db.execute(
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token,secure_token,caller_token,runtime_contract) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "active-session",
                    json.dumps(
                        {
                            "id": "active-session",
                            "image": {
                                "uri": "example.invalid/runtime@sha256:" + "a" * 64
                            },
                        }
                    ),
                    4_102_444_800,
                    "active",
                    "2026-11-09T00:00:00Z",
                    "synthetic-token",
                    "",
                    0,
                    "workspace-command-files-v1",
                ),
            )
        db.close()

        service = self.service()
        try:
            self.assertEqual(
                service._execute(
                    "SELECT has_schema_privilege(current_user, 'opensandbox', 'CREATE'), has_schema_privilege(current_user, 'public', 'CREATE')"
                ).fetchone(),
                (True, False),
            )
            self.assertEqual(service._row("active-session")[2], "active")
            self.assertEqual(
                self.requests,
                [("POST", "/.management/getSession", "active-session")],
            )
        finally:
            service.close()
            self.assertTrue(service.db.closed)

    def test_restart_retires_interrupted_allocation_before_serving(self):
        self.allocated["interrupted-session"] = {"createdAt": "2026-11-09T01:00:00Z"}
        with psycopg.connect(self.database_url) as db:
            db.execute(
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token,secure_token,caller_token) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "interrupted-session",
                    json.dumps({"id": "interrupted-session"}),
                    4_102_444_800,
                    "allocating",
                    "2026-11-09T01:00:00Z",
                    "synthetic-token",
                    "",
                    0,
                ),
            )
        db.close()

        service = self.service()
        try:
            with self.assertRaises(HTTPException):
                service._row("interrupted-session")
            self.assertEqual(
                self.requests,
                [
                    ("POST", "/.management/getSession", "interrupted-session"),
                    ("POST", "/.management/stopSession", "interrupted-session"),
                    ("POST", "/.management/getSession", "interrupted-session"),
                ],
            )
            self.assertEqual(
                service._execute(
                    "SELECT COUNT(*) FROM sessions WHERE id=?",
                    ("interrupted-session",),
                ).fetchone()[0],
                0,
            )
        finally:
            service.close()
            self.assertTrue(service.db.closed)

    def test_controller_schema_can_be_removed_with_its_journal_table(self):
        with psycopg.connect(
            os.environ["AZURE_SESSION_TEST_ADMIN_URL"], autocommit=True
        ) as db:
            db.execute("CREATE TABLE opensandbox.destroy_probe (id integer)")
            db.execute("DROP SCHEMA opensandbox CASCADE")
            self.assertFalse(
                db.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name='opensandbox')"
                ).fetchone()[0]
            )

    def test_legacy_provenance_migration_retires_forged_metadata_idempotently(self):
        with psycopg.connect(self.database_url, autocommit=True) as db:
            db.execute("ALTER TABLE opensandbox.sessions DROP COLUMN runtime_contract")
            db.execute(
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    "active-session",
                    json.dumps(
                        {
                            "id": "active-session",
                            "image": {"uri": os.environ["AZURE_SESSION_IMAGE"]},
                            "metadata": {
                                "opsrabbit.runtime.contract": "workspace-command-files-v1"
                            },
                        }
                    ),
                    4_102_444_800,
                    "active",
                    "2026-11-09T00:00:00Z",
                    "synthetic-token",
                ),
            )
        service = self.service()
        try:
            self.assertTrue(service.leader_ready)
            self.assertTrue(service.db.autocommit)
            self.assertEqual(
                service._execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
            )
            self.assertNotIn("active-session", self.allocated)
            self.assertEqual(
                self.requests,
                [
                    ("POST", "/.management/getSession", "active-session"),
                    ("POST", "/.management/stopSession", "active-session"),
                    ("POST", "/.management/getSession", "active-session"),
                ],
            )
            self.assertEqual(
                service.db.execute(
                    "SELECT is_nullable FROM information_schema.columns WHERE table_schema='opensandbox' AND table_name='sessions' AND column_name='runtime_contract'"
                ).fetchone(),
                ("NO",),
            )
        finally:
            service.close()
        service = self.service()
        try:
            self.assertTrue(service.leader_ready)
        finally:
            service.close()

    def test_interrupted_allocation_without_generation_blocks_recovery_until_absent(
        self,
    ):
        service = self.service()
        sid = "legacy-interrupted"
        self.allocated[sid] = {"createdAt": "unknown-generation"}
        try:
            service._execute(
                "INSERT INTO sessions (id,data,deadline,state,etag,token) VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    json.dumps({"id": sid}),
                    4_102_444_800,
                    "allocating",
                    "",
                    "synthetic",
                ),
            )
            service.db.execute(
                "SELECT pg_advisory_unlock(%s,%s)", service.leadership_lock_key
            )
            service.leader_ready = False
            with self.assertRaises(HTTPException) as error:
                service._try_become_leader()
            self.assertEqual(error.exception.status_code, 503)
            self.assertFalse(service.leader_ready)
            self.assertEqual(self.requests, [("POST", "/.management/getSession", sid)])
            self.assertEqual(
                service._execute(
                    "SELECT state,etag,runtime_contract FROM sessions WHERE id=?",
                    (sid,),
                ).fetchone(),
                ("retiring", "", ""),
            )
            self.assertIn(sid, self.allocated)
            self.assertTrue(service.db.autocommit)
            self.allocated.pop(sid)
            self.assertTrue(service._try_become_leader())
            self.assertIsNone(
                service._execute(
                    "SELECT id FROM sessions WHERE id=?", (sid,)
                ).fetchone()
            )
            self.assertNotIn(("POST", "/.management/stopSession", sid), self.requests)
        finally:
            service.close()

    def test_confirmed_retirement_checks_durable_generation_and_missing_rows(self):
        service = self.service()
        try:
            service._execute(
                "INSERT INTO sessions (id,data,deadline,state,etag,token,runtime_contract) VALUES (?,?,?,?,?,?,?)",
                (
                    "active-session",
                    "{}",
                    4_102_444_800,
                    "retiring",
                    "old-generation",
                    "synthetic",
                    "workspace-command-files-v1",
                ),
            )
            with self.assertRaises(HTTPException) as error:
                service.delete_sandbox_confirmed("active-session")
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(
                self.requests, [("POST", "/.management/getSession", "active-session")]
            )
            self.assertIn("active-session", self.allocated)
            service._execute("DELETE FROM sessions WHERE id=?", ("active-session",))
            self.requests.clear()
            with self.assertRaises(HTTPException):
                service._retire("active-session")
            self.assertEqual(self.requests, [])
            self.assertIn("active-session", self.allocated)
        finally:
            service.close()

    def test_overlapping_controllers_serialize_recovery_with_advisory_lock(self):
        leader = self.service()
        follower = self.service()
        try:
            self.assertTrue(leader.leader_ready)
            self.assertFalse(follower.leader_ready)
            with self.assertRaises(HTTPException) as error:
                follower._require_leader()
            self.assertEqual(error.exception.status_code, 503)
            leader.close()
            self.assertTrue(follower._try_become_leader())
            self.assertTrue(follower.leader_ready)
        finally:
            follower.close()

    def test_startup_recovery_does_not_block_requests_on_inverted_lock_order(self):
        service = self.service()
        with psycopg.connect(self.database_url) as db:
            db.execute(
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token,secure_token,caller_token,runtime_contract) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "active-session",
                    json.dumps(
                        {
                            "id": "active-session",
                            "image": {
                                "uri": "example.invalid/runtime@sha256:" + "a" * 64
                            },
                        }
                    ),
                    4_102_444_800,
                    "active",
                    "2026-11-09T00:00:00Z",
                    "synthetic-token",
                    "",
                    0,
                    "workspace-command-files-v1",
                ),
            )
        # Simulate a new controller acquiring leadership after a revision
        # handover. The constructor's initial recovery has already completed.
        service.db.execute(
            "SELECT pg_advisory_unlock(%s, %s)", service.leadership_lock_key
        )
        service.db.commit()
        recovery_entered = threading.Event()
        allow_recovery = threading.Event()
        request_finished = threading.Event()
        request_status = []
        recovery_result = []
        original_recover = service._recover

        def pause_recovery():
            recovery_entered.set()
            if not allow_recovery.wait(3):
                raise TimeoutError("Test did not release startup recovery")
            original_recover()

        def recover():
            try:
                recovery_result.append(service._try_become_leader())
            except Exception as exc:
                recovery_result.append(exc)

        def request_session():
            try:
                service._live("active-session")
            except HTTPException as exc:
                request_status.append(exc.status_code)
            finally:
                request_finished.set()

        service.leader_ready = False
        try:
            with patch.object(service, "_recover", side_effect=pause_recovery):
                recovery_thread = threading.Thread(target=recover)
                recovery_thread.start()
                self.assertTrue(recovery_entered.wait(2))
                request_thread = threading.Thread(target=request_session)
                request_thread.start()
                self.assertTrue(
                    request_finished.wait(1),
                    "A request hung while startup recovery held the journal lock",
                )
                self.assertEqual(request_status, [503])
                allow_recovery.set()
                recovery_thread.join(3)
                request_thread.join(3)
            self.assertFalse(recovery_thread.is_alive())
            self.assertEqual(recovery_result, [True])
            self.assertTrue(service.leader_ready)
        finally:
            allow_recovery.set()
            service.close()

    def test_lost_leadership_connection_terminates_for_aca_restart(self):
        service = self.service()
        try:
            self.assertTrue(service.leader_ready)
            service.db.close()
            with patch("opensandbox_server.services.azure_service.os.kill") as kill:
                service._leadership_loop()
            self.assertFalse(service.leader_ready)
            self.assertTrue(service.shutdown.is_set())
            kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        finally:
            service.close()

    def test_idle_controller_does_not_block_application_online_index(self):
        with psycopg.connect(
            os.environ["AZURE_SESSION_TEST_ADMIN_URL"], autocommit=True
        ) as admin:
            admin.execute("CREATE TABLE public.controller_index_probe (value TEXT)")
            admin.execute("INSERT INTO public.controller_index_probe VALUES ('probe')")
            service = self.service()
            follower = None
            try:
                # Exercise the same read-only heartbeat as the live controller.
                with patch.object(service.shutdown, "wait", side_effect=[False, True]):
                    service._leadership_loop()
                follower = self.service()
                self.assertFalse(follower.leader_ready)
                for controller in (service, follower):
                    state = admin.execute(
                        "SELECT state FROM pg_stat_activity WHERE pid=%s",
                        (controller.db.info.backend_pid,),
                    ).fetchone()[0]
                    self.assertEqual(state, "idle")
                admin.execute("SET statement_timeout = '5s'")
                admin.execute(
                    "CREATE INDEX CONCURRENTLY controller_index_probe_idx "
                    "ON public.controller_index_probe ((lower(value)))"
                )
                valid = admin.execute(
                    "SELECT indisvalid FROM pg_index "
                    "WHERE indexrelid='public.controller_index_probe_idx'::regclass"
                ).fetchone()[0]
                self.assertTrue(valid)
                # Autocommit must not release the leader's session-level fence.
                self.assertFalse(follower._try_become_leader())
            finally:
                if follower is not None:
                    follower.close()
                service.close()
                admin.execute("DROP TABLE public.controller_index_probe")


if __name__ == "__main__":
    unittest.main()
