"""Contract and fail-closed tests; no Azure credentials or network required."""

import os
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from opensandbox_server.api.schema import CreateSandboxRequest
from opensandbox_server.services.azure_service import AzureSandboxService


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
        if request.url.path == "/ping":
            self.allocated[sid] = {"createdAt": "2026-11-03T00:00:00Z"}
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/internal/init":
            binding = json.loads(request.content)
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
                200, json={"status": "initialized", "sandboxId": sid, "generation": 1}
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

    def test_delete_confirms_absence_and_never_reallocates(self):
        sid = self.service._create(self.request()).id
        self.service.delete_sandbox(sid)
        self.calls.clear()
        self.assertRejected(404, lambda: self.service.get_endpoint(sid, 44772))
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


if __name__ == "__main__":
    unittest.main()
