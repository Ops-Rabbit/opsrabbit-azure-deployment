"""Bounded single-controller Azure Dynamic Sessions proof adapter.

Uses the upstream OpenSandbox lifecycle and execution proxy. The pool image is
fixed; unsupported request semantics fail explicitly. Not an HA/tenant service.
"""

import asyncio
import json
import hashlib
import secrets
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx
from azure.identity import ManagedIdentityCredential
from fastapi import HTTPException
from opensandbox_server.api.schema import (
    CreateSandboxResponse,
    Endpoint,
    ListSandboxesResponse,
    PaginationInfo,
    Sandbox,
)
from opensandbox_server.services.sandbox_service import SandboxService
from opensandbox_server.services.extension_service import ExtensionService

API_VERSION = "2025-02-02-preview"
MAX_BODY = 2 * 1024 * 1024
ALLOWED_PATHS = {
    "/ping",
    "/command",
    "/files/upload",
    "/files/download",
    "/files/info",
    "/files",
    "/directories",
    "/directories/list",
}


def fail(status, message):
    raise HTTPException(
        status_code=status, detail={"code": "AZURE_SESSION", "message": message}
    )


class AzureSandboxService(SandboxService, ExtensionService):
    def __init__(self, config, *, credential=None, client=None, start_threads=True):
        if config.tenants is not None:
            raise ValueError(
                "Azure proof supports one trusted controller, not upstream tenant mode"
            )
        self.image = os.environ["AZURE_SESSION_IMAGE"]
        self.pool = os.environ["AZURE_SESSION_ENDPOINT"].rstrip("/")
        parsed = urlsplit(self.pool)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".azurecontainerapps.io")
            or parsed.path
            or parsed.query
            or parsed.username
            or parsed.port
        ):
            raise ValueError("Expected the exact HTTPS Azure session pool endpoint")
        self.credential = credential or ManagedIdentityCredential(
            client_id=os.environ["AZURE_CLIENT_ID"]
        )
        self.client = client or httpx.Client(
            timeout=45, follow_redirects=False, trust_env=False
        )
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            os.environ.get("AZURE_SESSION_STATE", "/tmp/azure-sessions.db"),
            check_same_thread=False,
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT, deadline REAL, state TEXT, etag TEXT, token TEXT)"
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "token" not in columns:
            self.db.execute("ALTER TABLE sessions ADD COLUMN token TEXT")
            self.db.execute(
                "UPDATE sessions SET state='retiring' WHERE state != 'deleted'"
            )
        self.db.commit()
        self.shutdown = threading.Event()
        if start_threads:
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def forward(self):
                    try:
                        owner.forward(self)
                    except HTTPException as exc:
                        self.send_error(exc.status_code, "Sandbox request rejected")
                    except Exception:
                        # No exception text: network errors can include sensitive headers/URLs.
                        self.send_error(502, "Azure session transport failed")

                do_GET = do_POST = do_DELETE = do_PUT = forward

            self.bridge = ThreadingHTTPServer(("127.0.0.1", 8091), Handler)
            threading.Thread(target=self.bridge.serve_forever, daemon=True).start()
            threading.Thread(target=self._janitor, daemon=True).start()

    def get_access_renew_extend_seconds(self, sandbox_id):
        return None  # Fixed lifetime; access must never silently extend it.

    def _headers(self):
        return {
            "Authorization": "Bearer "
            + self.credential.get_token("https://dynamicsessions.io/.default").token
        }

    def _request(self, sid, path, method="POST"):
        return self.client.request(
            method,
            self.pool + path,
            params={"identifier": sid, "api-version": API_VERSION},
            headers=self._headers(),
        )

    def _row(self, sid):
        row = self.db.execute(
            "SELECT data, deadline, state, etag, token FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None or row[2] != "active":
            fail(404, "Sandbox not found")
        if row[1] <= time.time():
            self._retire(sid)
            fail(404, "Sandbox expired")
        return row

    def _tombstone(self, sid):
        self.db.execute("UPDATE sessions SET state='deleted' WHERE id=?", (sid,))
        self.db.commit()

    def _live(self, sid):
        row = self._row(sid)
        response = self._request(sid, "/.management/getSession")
        if (
            response.status_code == 400
            and response.json().get("error", {}).get("code")
            == "SessionWithIdentifierNotFound"
        ):
            self._tombstone(sid)
            fail(404, "Sandbox is no longer allocated")
        if response.status_code != 200:
            fail(503, "Cannot verify Azure session allocation")
        # Compare creation time, not ETag: ETag can change with activity.
        if response.json().get("createdAt") != row[3]:
            self._tombstone(sid)
            fail(409, "Azure session generation changed; refusing execution")
        return row

    def _retire(self, sid):
        # Deny future execution before the external request; retain pending cleanup on failure.
        self.db.execute("UPDATE sessions SET state='retiring' WHERE id=?", (sid,))
        self.db.commit()
        response = self._request(sid, "/.management/stopSession")
        if response.status_code not in (200, 400, 404):
            fail(503, "Azure retirement not confirmed")
        check = self._request(sid, "/.management/getSession")
        if (
            check.status_code == 400
            and check.json().get("error", {}).get("code")
            == "SessionWithIdentifierNotFound"
        ):
            self._tombstone(sid)
        else:
            fail(503, "Azure retirement not confirmed")

    def _janitor(self):
        while not self.shutdown.wait(5):
            with self.lock:
                rows = self.db.execute(
                    "SELECT id FROM sessions WHERE state='retiring' OR (state IN ('active','allocating') AND deadline <= ?)",
                    (time.time(),),
                ).fetchall()
                for (sid,) in rows:
                    try:
                        self._retire(sid)
                    except Exception:
                        pass  # Retry retirement only; never allocate/retry user commands.

    async def create_sandbox(self, request):
        return await asyncio.to_thread(self._create, request)

    def _create(self, request):
        # No ignored caller semantics: this pool has one fixed shape and no egress.
        if (
            request.image is None
            or request.image.uri != self.image
            or request.image.auth
        ):
            fail(
                400,
                "Image must match the approved session pool digest; registry credentials are not accepted",
            )
        if request.timeout is None or not 60 <= request.timeout <= 300:
            fail(400, "Proof requires an explicit lifetime of 60 to 300 seconds")
        values = request.model_dump(exclude_none=True)
        allowed = {
            "image",
            "timeout",
            "metadata",
            "entrypoint",
            "resource_limits",
            "secure_access",
        }
        if any(
            value not in (None, {}, [], False) and key not in allowed
            for key, value in values.items()
        ):
            fail(400, "Request includes unsupported pool settings")
        if request.secure_access or request.entrypoint != ["sleep", "infinity"]:
            fail(
                400,
                "Proof requires the fixed pool entrypoint and server-proxied access",
            )
        if request.resource_limits and request.resource_limits.root != {
            "cpu": "0.5",
            "memory": "1Gi",
        }:
            fail(400, "Pool resources are fixed at 0.5 CPU and 1Gi")
        with self.lock:
            count = self.db.execute(
                "SELECT COUNT(*) FROM sessions WHERE state != 'deleted'"
            ).fetchone()[0]
            if count >= 2:
                fail(429, "Proof pool capacity reached")
            sid = str(uuid4())
            token = secrets.token_urlsafe(32)
            now = time.time()
            data = {
                "id": sid,
                "image": {"uri": self.image},
                "status": {"state": "Running"},
                "metadata": request.metadata,
                "entrypoint": request.entrypoint,
                "createdAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "expiresAt": datetime.fromtimestamp(
                    now + request.timeout, timezone.utc
                ).isoformat(),
            }
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                (sid, json.dumps(data), now + request.timeout, "allocating", "", token),
            )
            self.db.commit()
            try:
                ping = self._request(sid, "/ping", "GET")
                if ping.status_code != 200:
                    fail(
                        503,
                        "Azure session runtime is not ready; no command was retried",
                    )
                initialized = self.client.post(
                    self.pool + "/internal/init",
                    params={"identifier": sid, "api-version": API_VERSION},
                    headers=self._headers(),
                    json={
                        "sandboxId": sid,
                        "generation": 1,
                        "accessTokenHash": "sha256:"
                        + hashlib.sha256(token.encode()).hexdigest(),
                    },
                )
                if (
                    initialized.status_code != 200
                    or initialized.json().get("sandboxId") != sid
                    or initialized.json().get("status") != "initialized"
                    or initialized.json().get("generation") != 1
                ):
                    fail(503, "Runtime binding could not be established")
                metadata = self._request(sid, "/.management/getSession")
                if metadata.status_code != 200 or not metadata.json().get("createdAt"):
                    fail(503, "Azure session allocation could not be verified")
                self.db.execute(
                    "UPDATE sessions SET state='active',etag=? WHERE id=?",
                    (metadata.json()["createdAt"], sid),
                )
                self.db.commit()
            except Exception:
                try:
                    self._retire(sid)
                except Exception:
                    pass
                raise
            return CreateSandboxResponse.model_validate(data)

    def get_sandbox(self, sandbox_id):
        with self.lock:
            return Sandbox.model_validate_json(self._live(sandbox_id)[0])

    def delete_sandbox(self, sandbox_id):
        with self.lock:
            self._row(sandbox_id)
            self._retire(sandbox_id)

    def list_sandboxes(self, request):
        with self.lock:
            ids = self.db.execute(
                "SELECT id FROM sessions WHERE state='active'"
            ).fetchall()
            items = []
            for (sid,) in ids:
                try:
                    item = self.get_sandbox(sid)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                if (
                    request.filter.state
                    and item.status.state not in request.filter.state
                ):
                    continue
                if any(
                    (item.metadata or {}).get(k) != v
                    for k, v in (request.filter.metadata or {}).items()
                ):
                    continue
                items.append(item)
            page = request.pagination.page if request.pagination else 1
            size = request.pagination.page_size if request.pagination else 20
            return ListSandboxesResponse(
                items=items[(page - 1) * size : page * size],
                pagination=PaginationInfo(
                    page=page,
                    pageSize=size,
                    totalItems=len(items),
                    totalPages=math.ceil(len(items) / size),
                    hasNextPage=page * size < len(items),
                ),
            )

    def get_endpoint(
        self,
        sandbox_id,
        port,
        resolve_internal=False,
        expires=None,
        use_proxy_host=False,
    ):
        if port != 44772 or expires is not None:
            fail(400, "Only the execd server proxy is supported")
        with self.lock:
            self._live(sandbox_id)
        # Loopback transport only. No Azure URL, identifier override or token is exposed.
        return Endpoint(endpoint=f"127.0.0.1:8091/{sandbox_id}")

    def forward(self, handler):
        url = urlsplit(handler.path)
        parts = url.path.split("/", 2)
        if len(parts) != 3:
            fail(404, "Unknown endpoint")
        sid, path = parts[1], "/" + parts[2]
        if path not in ALLOWED_PATHS and not re.fullmatch(
            r"/command/status/[0-9a-f]{32}", path
        ):
            fail(400, "Unsupported runtime endpoint")
        pairs = parse_qsl(url.query, keep_blank_values=True)
        if any(k.lower() in ("identifier", "api-version") for k, _ in pairs):
            fail(400, "Reserved Azure query parameter")
        if handler.headers.get("Transfer-Encoding"):
            fail(400, "Chunked request bodies are not supported in this proof")
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 <= length <= MAX_BODY:
            fail(413, "Request too large")
        body = handler.rfile.read(length)
        with self.lock:
            row = self._live(sid)
            remaining = row[1] - time.time()
            if remaining < 1:
                fail(404, "Sandbox expired")
            headers = self._headers()
            headers["X-EXECD-ACCESS-TOKEN"] = row[4]
            if handler.headers.get("Content-Type"):
                headers["Content-Type"] = handler.headers["Content-Type"]
            params = pairs + [("identifier", sid), ("api-version", API_VERSION)]
            stream = self.client.stream(
                handler.command,
                self.pool + path,
                params=params,
                headers=headers,
                content=body,
                timeout=min(45, remaining),
            )
            # Hold the lifecycle lock until Azure acknowledges the request. After
            # headers arrive, cancellation/deletion can run while output streams.
            response = stream.__enter__()
        try:
            handler.send_response(response.status_code)
            handler.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/octet-stream"),
            )
            handler.send_header("Connection", "close")
            handler.end_headers()
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 8 * MAX_BODY or time.time() >= row[1]:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        finally:
            stream.__exit__(None, None, None)

    def _unsupported(self, *_args, **_kwargs):
        fail(501, "Operation is not supported by the Azure proof adapter")

    pause_sandbox = resume_sandbox = renew_expiration = patch_sandbox_metadata = (
        _unsupported
    )
    get_sandbox_log_diagnostics = get_sandbox_event_diagnostics = _unsupported
    get_sandbox_logs = get_sandbox_inspect = get_sandbox_events = _unsupported

    def close(self):
        self.shutdown.set()
        if hasattr(self, "bridge"):
            self.bridge.shutdown()
        self.client.close()
        self.credential.close()


class AzureSnapshotsUnsupported:
    """Advertise the capability honestly to the upstream snapshot service."""

    def supports_create_snapshot(self):
        return False

    def supports_synchronous_create(self):
        return False

    def create_snapshot_unsupported_message(self):
        return "Azure Dynamic Sessions proof does not support snapshots"

    def get_snapshot_status(self, *_args, **_kwargs):
        return None

    def _unsupported(self, *_args, **_kwargs):
        fail(501, self.create_snapshot_unsupported_message())

    preflight_create_snapshot = create_snapshot = delete_snapshot = inspect_snapshot = (
        _unsupported
    )
