"""Restart-recovery tests for the durable PostgreSQL session journal."""

import json
import os
import signal
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
            return httpx.Response(200)
        if request.url.path == "/.management/getSession":
            if sid == "active-session":
                return httpx.Response(200, json={"createdAt": "2026-11-09T00:00:00Z"})
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
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token,secure_token,caller_token) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
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
        with psycopg.connect(self.database_url) as db:
            db.execute(
                "INSERT INTO opensandbox.sessions (id,data,deadline,state,etag,token,secure_token,caller_token) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "interrupted-session",
                    json.dumps({"id": "interrupted-session"}),
                    4_102_444_800,
                    "allocating",
                    "",
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


if __name__ == "__main__":
    unittest.main()
