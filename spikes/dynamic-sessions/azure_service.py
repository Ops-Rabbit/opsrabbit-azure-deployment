"""Bounded single-controller Azure Dynamic Sessions proof adapter.

Uses the upstream OpenSandbox lifecycle and execution proxy. The pool image is
fixed; unsupported request semantics fail explicitly. Not an HA/tenant service.
"""

import asyncio
import json
import hashlib
import hmac
import secrets
import math
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx
import psycopg
from azure.identity import ManagedIdentityCredential
from fastapi import HTTPException
from opensandbox_server.api.schema import (
    CreateSandboxResponse,
    Endpoint,
    ListSandboxesResponse,
    PaginationInfo,
    RenewSandboxExpirationResponse,
    Sandbox,
)
from opensandbox_server.services.sandbox_service import SandboxService
from opensandbox_server.services.extension_service import ExtensionService

API_VERSION = "2025-02-02-preview"
MAX_BODY = 33 * 1024 * 1024
# Keep the advertised absolute lease within the maximum Azure idle cooldown.
MAX_LEASE = 60 * 60
ALLOWED_PATHS = {
    "/ping",
    "/command",
    "/files/upload",
    "/files/download",
    "/files/info",
    "/files/search",
    "/files/mv",
    "/files/permissions",
    "/files/replace",
    "/files",
    "/directories",
    "/directories/list",
}


def fail(status, message):
    raise HTTPException(
        status_code=status, detail={"code": "AZURE_SESSION", "message": message}
    )


def stream_timeout(remaining):
    read_timeout = max(0.1, remaining)
    setup_timeout = min(45, read_timeout)
    return httpx.Timeout(
        connect=setup_timeout,
        read=read_timeout,
        write=setup_timeout,
        pool=setup_timeout,
    )


class AzureSandboxService(SandboxService, ExtensionService):
    def __init__(self, config, *, credential=None, client=None, start_threads=True):
        if config.tenants is not None:
            raise ValueError(
                "Azure proof supports one trusted controller, not upstream tenant mode"
            )
        self.image = os.environ["AZURE_SESSION_IMAGE"]
        try:
            self.max_sessions = int(os.environ.get("AZURE_SESSION_MAX_SESSIONS", "2"))
        except ValueError as exc:
            raise ValueError(
                "Expected an integer session capacity from 1 to 100"
            ) from exc
        if not 1 <= self.max_sessions <= 100:
            raise ValueError("Expected an integer session capacity from 1 to 100")
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
        self.session_locks = {}
        self.postgres = bool(os.environ.get("AZURE_SESSION_DATABASE_URL"))
        if self.postgres:
            import psycopg

            # Journal transitions are single statements under self.lock. Keep
            # reads and heartbeat probes out of idle transactions, which can
            # block concurrent index migrations in the application schema.
            # The session-level advisory lock survives statement commits.
            self.db = psycopg.connect(
                os.environ["AZURE_SESSION_DATABASE_URL"], autocommit=True
            )
            self.table = "opensandbox.sessions"
        else:
            import sqlite3

            self.db = sqlite3.connect(
                os.environ.get("AZURE_SESSION_STATE", "/tmp/azure-sessions.db"),
                check_same_thread=False,
            )
            self.table = "sessions"
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT, deadline REAL, state TEXT, etag TEXT, token TEXT)"
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
            if "token" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN token TEXT")
                self.db.execute(
                    "UPDATE sessions SET state='retiring' WHERE state != 'deleted'"
                )
            if "secure_token" not in columns:
                self.db.execute(
                    "ALTER TABLE sessions ADD COLUMN secure_token TEXT DEFAULT ''"
                )
            if "caller_token" not in columns:
                self.db.execute(
                    "ALTER TABLE sessions ADD COLUMN caller_token INTEGER DEFAULT 0"
                )
        if not self.postgres:
            self.db.execute("PRAGMA synchronous=FULL")
        self.shutdown = threading.Event()
        self.leader_ready = False
        self.leadership_thread = None
        self.leadership_lock_key = (156143, 726241)
        if self.postgres:
            if not start_threads:
                self._try_become_leader()
        else:
            self.leader_ready = True
            # SQLite is only a local fallback, but it must still fence an
            # interrupted allocation instead of leaving an unknown state.
            self._execute(
                "UPDATE sessions SET state='retiring' WHERE state='allocating'"
            )
            self.db.commit()
            self._recover()
        if start_threads:
            owner = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def forward(self):
                    try:
                        self.connection.settimeout(45)
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
            if self.postgres:
                self.leadership_thread = threading.Thread(
                    target=self._leadership_loop, daemon=True
                )
                self.leadership_thread.start()

    def _require_leader(self):
        if not self.leader_ready:
            fail(503, "Controller recovery is in progress; retry the request")

    def _initialize_postgres_schema(self):
        self._execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL, deadline DOUBLE PRECISION NOT NULL, state TEXT NOT NULL, etag TEXT NOT NULL DEFAULT '', token TEXT NOT NULL, secure_token TEXT NOT NULL DEFAULT '', caller_token INTEGER NOT NULL DEFAULT 0)"
        )
        self._execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS token TEXT")
        self._execute(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS secure_token TEXT NOT NULL DEFAULT ''"
        )
        self._execute(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS caller_token INTEGER NOT NULL DEFAULT 0"
        )
        self.db.commit()

    def _try_become_leader(self):
        """Hold a PostgreSQL session lock across recovery and controller service."""
        with self.lock:
            if self.leader_ready:
                return True
            if not self.postgres:
                self.leader_ready = True
                return True
            if not self.db.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", self.leadership_lock_key
            ).fetchone()[0]:
                return False
            try:
                # Serialize the controller's schema migrations as well as
                # the journal transition before recovery.
                self._initialize_postgres_schema()
                # Retire allocations interrupted by a prior controller; never
                # replay their user command.
                self._execute(
                    "UPDATE sessions SET state='retiring' WHERE state='allocating'"
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                self.db.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", self.leadership_lock_key
                )
                self.db.commit()
                raise
        try:
            # Recovery takes session locks and then briefly takes self.lock for
            # journal access, matching the request path's lock order. Keep the
            # advisory database lock, but release self.lock during Azure I/O.
            self._recover()
        except Exception:
            with self.lock:
                self.db.rollback()
                self.db.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", self.leadership_lock_key
                )
                self.db.commit()
            raise
        with self.lock:
            self.leader_ready = True
        return True

    def _leadership_loop(self):
        while not self.shutdown.wait(1):
            if self.leader_ready:
                try:
                    with self.lock:
                        self.db.execute("SELECT 1").fetchone()
                except psycopg.Error:
                    # The advisory lock belongs to this connection. If it is
                    # lost, exit so ACA restarts and startup recovery reacquires it.
                    self.leader_ready = False
                    self.shutdown.set()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                except Exception:
                    self.shutdown.wait(5)
            else:
                try:
                    self._try_become_leader()
                except psycopg.Error:
                    self.leader_ready = False
                    self.shutdown.set()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                except Exception:
                    # Startup recovery stays fail-closed and retries transient
                    # database or Azure errors until ACA restarts the revision.
                    self.shutdown.wait(5)

    def _execute(self, query, params=()):
        query = query.replace("sessions", self.table)
        if self.postgres:
            query = query.replace("?", "%s")
        return self.db.execute(query, params)

    def _session_lock(self, sid):
        with self.lock:
            return self.session_locks.setdefault(sid, threading.RLock())

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
        row = self._execute(
            "SELECT data, deadline, state, etag, token, secure_token, caller_token FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        if row is None or row[2] != "active":
            fail(404, "Sandbox not found")
        return row

    def _tombstone(self, sid):
        self._execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.db.commit()
        self.session_locks.pop(sid, None)

    def _live(self, sid, *, require_leader=True):
        with self._session_lock(sid):
            with self.lock:
                if require_leader:
                    self._require_leader()
                row = self._row(sid)
            persisted_image = json.loads(row[0]).get("image", {}).get("uri")
            if persisted_image != self.image:
                self._retire(sid)
                fail(409, "Sandbox uses a superseded runtime image")
            if row[1] <= time.time():
                self._retire(sid)
                fail(404, "Sandbox expired")
            response = self._request(sid, "/.management/getSession")
            if (
                response.status_code == 400
                and response.json().get("error", {}).get("code")
                == "SessionWithIdentifierNotFound"
            ):
                with self.lock:
                    self._tombstone(sid)
                fail(404, "Sandbox is no longer allocated")
            if response.status_code != 200:
                fail(503, "Cannot verify Azure session allocation")
            # Compare creation time, not ETag: ETag can change with activity.
            if response.json().get("createdAt") != row[3]:
                with self.lock:
                    self._tombstone(sid)
                fail(409, "Sandbox generation changed; refusing execution")
            return row

    def _retire(self, sid):
        with self._session_lock(sid):
            # Deny execution first; do not hold the database lock over Azure I/O.
            with self.lock:
                self._execute("UPDATE sessions SET state='retiring' WHERE id=?", (sid,))
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
                with self.lock:
                    self._tombstone(sid)
            else:
                fail(503, "Azure retirement not confirmed")

    def _janitor(self):
        while not self.shutdown.wait(5):
            with self.lock:
                if not self.leader_ready:
                    continue
                rows = self._execute(
                    "SELECT id FROM sessions WHERE state='retiring' OR (state IN ('active','allocating') AND deadline <= ?)",
                    (time.time(),),
                ).fetchall()
            for (sid,) in rows:
                try:
                    self._retire(sid)
                except Exception:
                    pass  # Retry retirement only; never allocate/retry user commands.

    def _recover(self):
        """Reconcile the durable journal with Azure before serving requests.

        The controller runs as one replica. Persistent storage is configured by
        the installer; if Azure cannot confirm an active session or retirement,
        construction fails and the API remains unavailable rather than serving
        an unverified or potentially stale execution endpoint.
        """
        with self.lock:
            rows = self._execute(
                "SELECT id, state, deadline FROM sessions ORDER BY id"
            ).fetchall()
        for sid, state, deadline in rows:
            if state == "retiring" or deadline <= time.time():
                self._retire(sid)
                continue
            if state == "active":
                try:
                    self._live(sid, require_leader=False)
                except HTTPException as exc:
                    # Missing or replaced allocations are fenced by _live;
                    # transport/auth failures keep startup fail-closed.
                    if exc.status_code not in (404, 409):
                        raise
                continue
            fail(503, "Persisted sandbox journal contains an unknown state")

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
        if request.timeout is None or not 60 <= request.timeout <= MAX_LEASE:
            fail(400, "An explicit lifetime of 60 seconds to 7 days is required")
        values = request.model_dump(exclude_none=True)
        allowed = {
            "image",
            "timeout",
            "metadata",
            "entrypoint",
            "resource_limits",
            "secure_access",
            "env",
            "extensions",
            "network_policy",
        }
        if any(
            value not in (None, {}, [], False) and key not in allowed
            for key, value in values.items()
        ):
            fail(400, "Request includes unsupported pool settings")
        if request.entrypoint not in (
            ["sleep", "infinity"],
            ["tail", "-f", "/dev/null"],
        ):
            fail(400, "Only the fixed pool keepalive entrypoint is supported")
        env = request.env or {}
        token = env.get("EXECD_ACCESS_TOKEN")
        if env and (
            set(env) != {"EXECD_ACCESS_TOKEN"}
            or not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
        ):
            fail(400, "Only a strong execd bootstrap credential is supported")
        extensions = request.extensions or {}
        if extensions and extensions != {"bootstrap.execd.isolation": "enable"}:
            fail(400, "Unsupported runtime extension")
        if bool(token) != bool(extensions):
            fail(
                400,
                "Execd isolation and its bootstrap credential must be supplied together",
            )
        if request.network_policy is not None:
            policy = request.network_policy.model_dump(by_alias=True, exclude_none=True)
            if policy != {"defaultAction": "deny", "egress": []}:
                fail(400, "This pool enforces deny-all network egress only")
        if request.resource_limits and request.resource_limits.root != {
            "cpu": "0.5",
            "memory": "1Gi",
        }:
            fail(400, "Pool resources are fixed at 0.5 CPU and 1Gi")
        with self.lock:
            self._require_leader()
            count = self._execute(
                "SELECT COUNT(*) FROM sessions WHERE state != 'deleted'"
            ).fetchone()[0]
            if count >= self.max_sessions:
                fail(429, "Proof pool capacity reached")
            sid = str(uuid4())
            caller_token = bool(token)
            token = token or secrets.token_urlsafe(32)
            secure_token = secrets.token_urlsafe(32) if request.secure_access else ""
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
            self._execute(
                "INSERT INTO sessions (id,data,deadline,state,etag,token,secure_token,caller_token) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    json.dumps(data),
                    now + request.timeout,
                    "allocating",
                    "",
                    token,
                    secure_token,
                    int(caller_token),
                ),
            )
            self.db.commit()
        with self._session_lock(sid):
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
                        "envs": {
                            "LD_LIBRARY_PATH": "/usr/local/lib",
                            "PATH": "/usr/local/bin:/usr/bin:/bin",
                            "HOME": "/workspace",
                        },
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
                with self.lock:
                    self._require_leader()
                    changed = self._execute(
                        "UPDATE sessions SET state='active',etag=? WHERE id=? AND state='allocating' AND deadline>?",
                        (metadata.json()["createdAt"], sid, time.time()),
                    ).rowcount
                    self.db.commit()
                    if changed != 1:
                        fail(503, "Sandbox allocation expired before activation")
            except Exception:
                try:
                    self._retire(sid)
                except Exception:
                    pass
                raise
        return CreateSandboxResponse.model_validate(data)

    def get_sandbox(self, sandbox_id):
        with self.lock:
            self._require_leader()
        return Sandbox.model_validate_json(self._live(sandbox_id)[0])

    def delete_sandbox(self, sandbox_id):
        with self.lock:
            self._require_leader()
            self._row(sandbox_id)
        self._retire(sandbox_id)

    def list_sandboxes(self, request):
        with self.lock:
            self._require_leader()
            ids = self._execute(
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
            if request.filter.state and item.status.state not in request.filter.state:
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
        if port not in (44772, 18080) or expires is not None:
            fail(400, "Only execd and the reserved egress endpoint are supported")
        # Upstream proxy dispatch always sets exactly one of these flags. Reject
        # before URL construction, so dot-segment normalization cannot turn an
        # egress request into an execd request. Plain discovery sets neither.
        if port == 18080 and (resolve_internal or use_proxy_host):
            fail(501, "Azure deny-all egress policy is immutable")
        with self._session_lock(sandbox_id):
            row = self._live(sandbox_id)
            # Loopback transport only. No Azure URL or identifier override leaks.
            # SDK 1.1.1 resolves egress even for command/file-only clients, so keep
            # a reserved, non-forwardable path for Azure's immutable deny-all policy.
            suffix = "/egress" if port == 18080 else ""
            return Endpoint(
                endpoint=f"127.0.0.1:8091/{sandbox_id}{suffix}",
                headers={"OpenSandbox-Secure-Access": row[5]} if row[5] else None,
            )

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
            # The upstream proxy streams bodyless SDK DELETE requests as an
            # empty chunked message. Accept only that exact bounded framing.
            if (
                handler.command != "DELETE"
                or (
                    hasattr(handler.headers, "get_all")
                    and len(handler.headers.get_all("Transfer-Encoding")) != 1
                )
                or handler.headers.get("Transfer-Encoding").lower() != "chunked"
                or handler.headers.get("Content-Length") is not None
                or handler.rfile.read(5) != b"0\r\n\r\n"
            ):
                fail(400, "Only empty chunked DELETE bodies are supported")
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 <= length <= MAX_BODY:
            fail(413, "Request too large")
        body = handler.rfile.read(length)
        with self._session_lock(sid):
            self._require_leader()
            row = self._live(sid)
            remaining = row[1] - time.time()
            if remaining < 1:
                fail(404, "Sandbox expired")
            if row[6] and not hmac.compare_digest(
                handler.headers.get("X-EXECD-ACCESS-TOKEN", ""), row[4]
            ):
                fail(401, "Missing or invalid execd credential")
            with self.lock:
                self._require_leader()
                row = self._row(sid)
                if row[6] and not hmac.compare_digest(
                    handler.headers.get("X-EXECD-ACCESS-TOKEN", ""), row[4]
                ):
                    fail(401, "Missing or invalid execd credential")
                token = row[4]
            headers = self._headers()
            headers["X-EXECD-ACCESS-TOKEN"] = token
            if handler.headers.get("Range"):
                byte_range = handler.headers["Range"]
                if not re.fullmatch(r"bytes=\d+-\d*", byte_range):
                    fail(400, "Unsupported byte range")
                headers["Range"] = byte_range
            if handler.headers.get("Content-Type"):
                headers["Content-Type"] = handler.headers["Content-Type"]
            params = pairs + [("identifier", sid), ("api-version", API_VERSION)]
            stream = self.client.stream(
                handler.command,
                self.pool + path,
                params=params,
                headers=headers,
                content=body,
                timeout=stream_timeout(remaining),
            )
            # Serialize only this sandbox until Azure acknowledges the request;
            # unrelated sessions remain available during cold allocation calls.
            response = stream.__enter__()
        try:
            # Buffer file/control replies before sending success, so size/deadline
            # failures cannot turn a partial download into an apparent HTTP 200.
            streaming = path == "/command" and handler.command == "POST"
            payload = bytearray()
            if not streaming:
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BODY or time.time() >= row[1]:
                        fail(413, "Response exceeds the transfer or lifetime limit")
                expected_length = response.headers.get("Content-Length")
                if (
                    path == "/files/download"
                    and expected_length is not None
                    and response.headers.get("Content-Encoding", "identity")
                    == "identity"
                    and len(payload) != int(expected_length)
                ):
                    fail(502, "Sandbox returned an incomplete transfer")
            handler.send_response(response.status_code)
            handler.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/octet-stream"),
            )
            if not streaming:
                handler.send_header("Content-Length", str(len(payload)))
                for header in ("Content-Range", "Accept-Ranges", "Content-Disposition"):
                    if header in response.headers:
                        handler.send_header(header, response.headers[header])
            handler.send_header("Connection", "close")
            handler.end_headers()
            if not streaming:
                handler.wfile.write(payload)
            else:
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY or time.time() >= row[1]:
                        # No execution_complete event is emitted on interruption.
                        handler.close_connection = True
                        return
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        finally:
            stream.__exit__(None, None, None)

    def _unsupported(self, *_args, **_kwargs):
        fail(501, "Operation is not supported by the Azure proof adapter")

    def renew_expiration(self, sandbox_id, request):
        deadline = request.expires_at.timestamp()
        with self._session_lock(sandbox_id):
            self._require_leader()
            row = self._live(sandbox_id)
            now = time.time()
            data = json.loads(row[0])
            created_at = datetime.fromisoformat(
                data["createdAt"].replace("Z", "+00:00")
            ).timestamp()
            if not now < deadline <= created_at + MAX_LEASE:
                fail(400, "Expiration must be within one hour of session creation")
            data["expiresAt"] = request.expires_at.isoformat()
            with self.lock:
                self._require_leader()
                self._execute(
                    "UPDATE sessions SET deadline=?,data=? WHERE id=?",
                    (deadline, json.dumps(data), sandbox_id),
                )
                self.db.commit()
            return RenewSandboxExpirationResponse(expiresAt=request.expires_at)

    pause_sandbox = resume_sandbox = patch_sandbox_metadata = _unsupported
    get_sandbox_log_diagnostics = get_sandbox_event_diagnostics = _unsupported
    get_sandbox_logs = get_sandbox_inspect = get_sandbox_events = _unsupported

    def close(self):
        self.shutdown.set()
        if hasattr(self, "bridge"):
            self.bridge.shutdown()
        if self.leadership_thread is not None:
            self.leadership_thread.join(timeout=5)
        self.client.close()
        self.credential.close()
        self.db.close()


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
