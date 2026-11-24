"""Fixed routing to a mandatory command launcher and confined file worker.

The lifecycle controller alone binds both daemons. No cloud identity, raw token,
or caller-controlled route is stored here. Only the trusted command supervisor
has procfs; every command payload enters a fresh no-proc child. Raw workers
bind loopback and the command children have a private network namespace.
"""

import http.client
import json
import math
import os
import posixpath
import select
import socket
import subprocess
import signal
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 33 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 120
MAX_COMMAND_STREAM_TIMEOUT = 3600
RUNTIME_CONTRACT = "workspace-command-files-v1"
PROTECTED_NAMES = frozenset(
    (
        ".opsrabbit-system",
        ".opsrabbit-empty",
        ".opsrabbit-skills",
        ".opsrabbit-browser-runtime",
    )
)
command = files = None


def cancel_on_disconnect(client, upstream, finished):
    """Cancel execd even when a foreground payload produces no more output."""
    while not finished.is_set():
        try:
            readable, _, _ = select.select([client], [], [], 0.1)
            if not readable:
                continue
            if client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                # No pipelined request is consumed by this HTTP/1.0 handler.
                finished.wait(0.1)
                continue
        except BlockingIOError:
            continue
        except (OSError, ValueError):
            pass
        try:
            upstream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return


def confined_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("file paths must stay inside /workspace")
    candidate = value if value.startswith("/") else posixpath.join("/workspace", value)
    normalized = posixpath.normpath(candidate)
    if posixpath.commonpath(("/workspace", normalized)) != "/workspace":
        raise ValueError("file paths must stay inside /workspace")
    if normalized.split("/")[2:3] and normalized.split("/")[2] in PROTECTED_NAMES:
        raise ValueError("file path is reserved for the runtime")
    resolved = os.path.realpath(normalized)
    if posixpath.commonpath(("/workspace", resolved)) != "/workspace":
        raise ValueError("file paths must stay inside /workspace")
    if resolved.split("/")[2:3] and resolved.split("/")[2] in PROTECTED_NAMES:
        raise ValueError("file path is reserved for the runtime")


def initialize_workers(body, headers):
    """Advertise the immutable contract only after BOTH bindings are ready."""
    request = json.loads(body)
    allowed = {"sandboxId", "generation", "envs", "accessTokenHash"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("unsupported runtime initialization")
    baseline = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LD_LIBRARY_PATH": "/usr/local/lib",
        "HOME": "/workspace",
    }
    envs = request.get("envs", {})
    if not isinstance(envs, dict) or any(
        k not in baseline or baseline[k] != v for k, v in envs.items()
    ):
        raise ValueError("unsupported runtime initialization environment")
    expected = (request.get("sandboxId"), request.get("generation"))
    reply = None
    for port in (8081, 8082):
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=DEFAULT_REQUEST_TIMEOUT
        )
        try:
            connection.request("POST", "/internal/init", body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_BODY + 1)
            if response.status != 200 or len(payload) > MAX_BODY:
                raise RuntimeError("runtime binding failed")
            reply = json.loads(payload)
            if (
                not isinstance(reply, dict)
                or reply.get("status") != "initialized"
                or (reply.get("sandboxId"), reply.get("generation")) != expected
            ):
                raise RuntimeError("runtime binding mismatch")
        finally:
            connection.close()
    # Check readiness after both bindings, never advertise on partial success.
    for port in (8081, 8082):
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=DEFAULT_REQUEST_TIMEOUT
        )
        try:
            connection.request("GET", "/ready", headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_BODY + 1)
            if response.status != 200 or len(payload) > MAX_BODY:
                raise RuntimeError("runtime worker is not ready")
            ready = json.loads(payload)
            if (
                not isinstance(ready, dict)
                or ready.get("initialized") is not True
                or (ready.get("sandboxId"), ready.get("generation")) != expected
            ):
                raise RuntimeError("runtime readiness mismatch")
        finally:
            connection.close()
    if command.poll() is not None or files.poll() is not None:
        raise RuntimeError("runtime worker exited")
    return {**reply, "runtimeContract": RUNTIME_CONTRACT}


def validate_file_request(method, path, query, content_type, body):
    query_paths = {
        ("GET", "/files/info"),
        ("GET", "/files/search"),
        ("GET", "/files/download"),
        ("GET", "/directories/list"),
        ("DELETE", "/files"),
        ("DELETE", "/directories"),
    }
    if (method, path) in query_paths:
        values = parse_qs(query, keep_blank_values=True).get("path", [])
        if not values:
            raise ValueError("file path is required")
        for value in values:
            confined_path(value)
        return

    json_path_maps = {
        ("POST", "/files/permissions"),
        ("POST", "/files/replace"),
        ("POST", "/directories"),
    }
    if (method, path) in json_path_maps:
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("file path body must be JSON")
        values = json.loads(body)
        if not isinstance(values, dict) or not values:
            raise ValueError("file path body must be a non-empty object")
        for value in values:
            confined_path(value)
        return

    if method == "POST" and path == "/files/mv":
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("file move body must be JSON")
        values = json.loads(body)
        if not isinstance(values, list) or not values:
            raise ValueError("file move body must be a non-empty list")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("file move entries must be objects")
            confined_path(item.get("src"))
            confined_path(item.get("dest"))
        return

    if method == "POST" and path == "/files/upload":
        if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
            raise ValueError("file upload must use multipart form data")
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
                "latin-1"
            )
            + body
        )
        metadata_found = False
        if message.is_multipart():
            for part in message.iter_parts():
                if part.get_param("name", header="content-disposition") == "metadata":
                    metadata = json.loads(part.get_content())
                    if not isinstance(metadata, dict):
                        raise ValueError("file upload metadata must be an object")
                    confined_path(metadata.get("path"))
                    metadata_found = True
        if not metadata_found:
            raise ValueError("file upload path metadata is required")


def request_timeout(method, path, body):
    if method != "POST" or path != "/command":
        return DEFAULT_REQUEST_TIMEOUT
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return DEFAULT_REQUEST_TIMEOUT
    if not isinstance(payload, dict):
        return DEFAULT_REQUEST_TIMEOUT
    timeout_ms = payload.get("timeout")
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, (int, float))
        or timeout_ms <= 0
    ):
        return MAX_COMMAND_STREAM_TIMEOUT
    return min(
        MAX_COMMAND_STREAM_TIMEOUT,
        max(DEFAULT_REQUEST_TIMEOUT, math.ceil(timeout_ms / 1000) + 30),
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def forward(self):
        path = urlsplit(self.path).path
        if path in ("/ping", "/ready", "/internal/init"):
            ports = [8081, 8082]
        elif path == "/command" or path.startswith("/command/status/"):
            ports = [8081]
        elif path.startswith("/files") or path.startswith("/directories"):
            ports = [8082]
        else:
            self.send_error(404)
            return
        if command.poll() is not None or files.poll() is not None:
            self.send_error(503)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 <= length <= MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            headers = {
                key: self.headers[key]
                for key in ("Content-Type", "X-EXECD-ACCESS-TOKEN", "Range")
                if key in self.headers
            }
            if path == "/internal/init":
                if self.command != "POST":
                    self.send_error(405)
                    return
                try:
                    initialized = initialize_workers(body, headers)
                except (ValueError, UnicodeDecodeError):
                    self.send_error(400, "invalid runtime initialization")
                    return
                except RuntimeError:
                    self.send_error(503, "runtime initialization failed")
                    return
                payload = json.dumps(initialized).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                validate_file_request(
                    self.command,
                    path,
                    urlsplit(self.path).query,
                    self.headers.get("Content-Type", ""),
                    body,
                )
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "file paths must stay inside /workspace")
                return
            for index, port in enumerate(ports):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=request_timeout(self.command, path, body)
                )
                finished = threading.Event()
                disconnect_watcher = None
                try:
                    connection.request(
                        self.command, self.path, body=body, headers=headers
                    )
                    if self.command == "POST" and path == "/command":
                        disconnect_watcher = threading.Thread(
                            target=cancel_on_disconnect,
                            args=(self.connection, connection.sock, finished),
                            daemon=True,
                        )
                        disconnect_watcher.start()
                    response = connection.getresponse()
                    if index < len(ports) - 1:
                        reply = response.read(MAX_BODY + 1)
                        if response.status != 200 or len(reply) > MAX_BODY:
                            self.send_error(503)
                            return
                        continue
                    self.send_response(response.status)
                    for key in (
                        "Content-Type",
                        "Content-Length",
                        "Content-Range",
                        "Accept-Ranges",
                    ):
                        if response.getheader(key):
                            self.send_header(key, response.getheader(key))
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    finished.set()
                    if disconnect_watcher is not None:
                        disconnect_watcher.join(timeout=1)
                    connection.close()
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True

    do_GET = do_POST = do_DELETE = do_PUT = forward


def wait_for_workers(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in (command, files)):
            raise RuntimeError("runtime worker exited during startup")
        live = 0
        for port in (8081, 8082):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", "/ping")
                response = connection.getresponse()
                if response.status == 200:
                    live += 1
                response.read(1024)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        if live == 2:
            return
        time.sleep(0.1)
    raise RuntimeError("runtime workers did not start within 30 seconds")


def supervise(server):
    while all(child.poll() is None for child in (command, files)):
        time.sleep(0.2)
    server.shutdown()


def terminate(_signum, _frame):
    raise SystemExit(0)


def main():
    global command, files
    command = files = None
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        worker_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LD_LIBRARY_PATH": "/usr/local/lib",
            "HOME": "/workspace",
        }
        command = subprocess.Popen(
            ["/usr/local/bin/python", "-I", "-S", "/opt/runtime-start.py", "command"],
            env=worker_env,
            close_fds=True,
        )
        files = subprocess.Popen(
            ["/usr/local/bin/python", "-I", "-S", "/opt/runtime-start.py", "files"],
            env=worker_env,
            close_fds=True,
        )
        wait_for_workers()
        server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
        threading.Thread(target=supervise, args=(server,), daemon=True).start()
        server.serve_forever()
    finally:
        # Further termination signals must not interrupt bounded child cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        children = [child for child in (command, files) if child is not None]
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
