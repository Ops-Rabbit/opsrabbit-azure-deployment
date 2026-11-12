"""Synthetic acceptance through the deployed OpenSandbox HTTPS API.

No cloud calls on import. Evidence contains only check results and synthetic IDs,
never response bodies, command output, headers, credentials or customer data.
Only allocations returned by this invocation are eligible for final cleanup.
"""

import argparse
import hashlib
import http.client
import json
import queue
import re
import runpy
import signal
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

CONTRACT = "workspace-command-files-v1"
MAX_BODY = 2 * 1024 * 1024
MAX_LINE = 128 * 1024
SUITE_SECONDS = 600
# The shared container fixture attempts writes three seconds after its handshake.
# Cancel before that window, then observe beyond it without waiting for the
# command's own timeout. Late/absent handshakes fail instead of proving cleanup.
SURVIVAL_SECONDS = 3
CANCEL_SECONDS = 2.5
HANDSHAKE_SECONDS = 15
OBSERVE_SECONDS = 5
LONG_COMMAND_MS = 30000
PROTECTED = (
    ".opsrabbit-system",
    ".opsrabbit-empty",
    ".opsrabbit-skills",
    ".opsrabbit-browser-runtime",
)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("probe deadline exceeded")
    return seconds


def require(condition):
    if not condition:
        raise RuntimeError("synthetic acceptance assertion failed")


def output(events, kind):
    values = [event.get("text", "") for event in events if event["type"] == kind]
    require(all(isinstance(value, str) for value in values))
    return "".join(values)


def assert_result(events, succeeds=True):
    errors = [event for event in events if event["type"] == "error"]
    completed = [event for event in events if event["type"] == "execution_complete"]
    if succeeds:
        require(bool(completed) and not errors)
    else:
        require(bool(errors) and not completed)


def execution_id(events):
    ids = [event.get("text") for event in events if event["type"] == "init"]
    require(len(ids) == 1 and isinstance(ids[0], str) and bool(ids[0]))
    return ids[0]


def endpoint_matches(value, origin, path):
    """Accept the server's scheme-less form, but bind the whole proxy URL."""
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) <= 32 for character in value)
        or any(character in value for character in ("\\", "?", "#"))
    ):
        return False
    if value.startswith("//"):
        value = origin.scheme + ":" + value
    elif "://" not in value:
        value = origin.scheme + "://" + value
    try:
        endpoint = urllib.parse.urlsplit(value)
        return (
            endpoint.scheme == origin.scheme == "https"
            and endpoint.username is None
            and endpoint.password is None
            and bool(endpoint.hostname)
            and endpoint.hostname.encode("idna").lower()
            == origin.hostname.encode("idna").lower()
            and (endpoint.port if endpoint.port is not None else 443)
            == (origin.port if origin.port is not None else 443)
            and endpoint.path == origin.path.rstrip("/") + path
        )
    except (ValueError, UnicodeError):
        return False


def resolve_addresses(host, port, deadline):
    """Bound getaddrinfo without letting an abandoned worker open sockets."""
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as error:
            result.put((False, error))
        else:
            result.put((True, addresses))

    remaining(deadline)
    worker = threading.Thread(target=resolve, daemon=True, name="probe-resolver")
    worker.start()
    try:
        success, value = result.get(timeout=remaining(deadline))
    except queue.Empty:
        raise TimeoutError("probe DNS deadline exceeded") from None
    remaining(deadline)
    if not success:
        raise value
    require(bool(value))
    return value


class Response:
    """One non-redirecting HTTPS request with a total deadline and body cap."""

    def __init__(self, origin, path, method, body, headers, deadline):
        self.deadline = deadline
        context = ssl.create_default_context()
        self.connection = http.client.HTTPSConnection(
            origin.hostname,
            origin.port or 443,
            timeout=remaining(deadline),
            context=context,
        )
        self.response = None
        self.transport = None
        self.timer = None
        self.count = 0
        self._transport_lock = threading.Lock()
        self._disconnected = False
        try:
            # Start before DNS/TCP/TLS. The DNS worker only returns addresses;
            # all sockets stay owned by this thread and are closed on failure.
            self.timer = threading.Timer(remaining(deadline), self.disconnect)
            self.timer.daemon = True
            self.timer.start()
            self.connect(context)
            self.transport.settimeout(remaining(deadline))
            self.connection.request(
                method, origin.path.rstrip("/") + path, body=body, headers=headers
            )
            self.transport.settimeout(remaining(deadline))
            self.response = self.connection.getresponse()
            self.status = self.response.status
        except BaseException:
            self.close()
            raise

    def attach_transport(self, transport):
        with self._transport_lock:
            if self._disconnected:
                transport.close()
                raise TimeoutError("probe connection deadline exceeded")
            self.transport = transport

    def connect(self, context):
        addresses = resolve_addresses(
            self.connection.host, self.connection.port, self.deadline
        )
        last_error = None
        for family, kind, protocol, _canonical, address in addresses:
            remaining(self.deadline)
            raw = socket.socket(family, kind, protocol)
            secure = None
            try:
                self.attach_transport(raw)
                raw.settimeout(remaining(self.deadline))
                # getaddrinfo returned a numeric address: connect cannot invoke
                # hostname resolution again. Keep the original host for TLS
                # SNI/certificate verification and the HTTP Host header.
                raw.connect(address)
                raw.settimeout(remaining(self.deadline))
                secure = context.wrap_socket(
                    raw,
                    server_hostname=self.connection.host,
                    do_handshake_on_connect=False,
                )
                self.attach_transport(secure)
                secure.settimeout(remaining(self.deadline))
                secure.do_handshake()
                remaining(self.deadline)
                self.connection.sock = secure
                return
            except OSError as error:
                last_error = error
            except BaseException:
                if secure is not None:
                    secure.close()
                raw.close()
                raise
            if secure is not None:
                secure.close()
            raw.close()
        raise last_error or OSError("probe connection failed")

    def chunk(self):
        if self.response.isclosed() or self.response.length == 0:
            require(self.response.length in (None, 0))
            return b""
        self.transport.settimeout(remaining(self.deadline))
        chunk = self.response.read1(min(65536, MAX_BODY + 1 - self.count))
        self.count += len(chunk)
        require(self.count <= MAX_BODY)
        if not chunk:
            require(self.response.length in (None, 0))
        return chunk

    def body(self):
        chunks = []
        while chunk := self.chunk():
            chunks.append(chunk)
        return b"".join(chunks)

    def events(self, parse_event):
        pending = b""
        while chunk := self.chunk():
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                require(len(line) <= MAX_LINE)
                event = parse_event(line)
                if event is not None:
                    yield event
            require(len(pending) <= MAX_LINE)
        if pending:
            event = parse_event(pending)
            if event is not None:
                yield event

    def disconnect(self):
        # Explicit shutdown also disconnects a response whose HTTPConnection has
        # detached its socket after seeing Connection: close.
        with self._transport_lock:
            self._disconnected = True
            transport = self.transport
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
        self.disconnect()
        if self.response is not None:
            self.response.close()
        self.connection.close()
        if self.transport is not None:
            self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Probe:
    def __init__(self, args):
        self.args = args
        self.origin = urllib.parse.urlsplit(args.url)
        require(
            self.origin.scheme == "https"
            and bool(self.origin.hostname)
            and not self.origin.username
            and not self.origin.password
            and not self.origin.query
            and not self.origin.fragment
        )
        require(re.search(r"@sha256:[a-f0-9]{64}$", args.image) is not None)
        self.key = args.key_file.read_text().strip()
        require(bool(self.key) and "\n" not in self.key and "\r" not in self.key)
        fixture_path = Path(__file__).with_name("test-runtime-container.py")
        # Load definitions, not main(): no Docker or test execution. Sharing the
        # exact adversarial generator avoids a weaker live-only payload.
        fixture = runpy.run_path(str(fixture_path))
        self.adversarial_argv = fixture["adversarial_lifecycle_argv"]
        self.parse_event = fixture["parse_event"]
        self.deadline = time.monotonic() + SUITE_SECONDS
        self.owned = []
        self.report = {
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "imageDigest": args.image.rsplit("@", 1)[1],
            "fixtureSha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            "checks": [],
            "sessions": self.owned,
            "outcome": "running",
        }

    def save(self):
        self.args.evidence.write_text(json.dumps(self.report, indent=2))

    def record(self, name, passed, **evidence):
        row = {
            "name": name,
            "status": "passed" if passed else "failed",
            "evidence": evidence,
        }
        self.report["checks"].append(row)
        self.save()
        print(json.dumps(row), flush=True)
        require(passed)

    def open(
        self,
        path,
        method="GET",
        data=None,
        content_type="application/json",
        auth=True,
        deadline=None,
        cleanup=False,
    ):
        deadline = deadline if deadline is not None else time.monotonic() + 70
        if not cleanup:
            deadline = min(deadline, self.deadline)
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
        require(body is None or len(body) <= MAX_BODY)
        headers = {"OPEN-SANDBOX-API-KEY": self.key} if auth else {}
        if body is not None:
            headers["Content-Type"] = content_type
        if method == "DELETE" and body is None:
            body = b""
        return Response(self.origin, path, method, body, headers, deadline)

    def request(self, *args, **kwargs):
        with self.open(*args, **kwargs) as response:
            # Only this fixed non-secret receipt is retained from headers.
            receipt = response.response.getheader("X-OpsRabbit-Workload-Deleted")
            return response.status, response.body(), receipt

    def identity(self, sid, label):
        code, body, _ = self.request("/sandboxes/" + sid)
        info = json.loads(body)
        self.record(
            label,
            code == 200
            and isinstance(info, dict)
            and info.get("id") == sid
            and info.get("image", {}).get("uri") == self.args.image
            and info.get("metadata", {}).get("opsrabbit.runtime.contract") == CONTRACT,
            httpStatus=code,
        )

    def create(self, label):
        start = time.monotonic()
        code, body, _ = self.request(
            "/sandboxes",
            "POST",
            {
                "image": {"uri": self.args.image},
                "entrypoint": ["sleep", "infinity"],
                "timeout": SUITE_SECONDS + 300,
                "resourceLimits": {"cpu": "0.5", "memory": "1Gi"},
                "metadata": {"purpose": "skills156-synthetic-proof"},
            },
        )
        require(code == 202)
        allocation = json.loads(body)
        sid = allocation.get("id")
        require(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid))
        # Register ownership before any identity/endpoint assertion can fail.
        require(sid not in self.owned)
        self.owned.append(sid)
        self.save()
        self.record(
            label + " allocation",
            True,
            httpStatus=code,
            seconds=round(time.monotonic() - start, 2),
        )
        self.identity(sid, label + " exact native lifecycle GET")
        prefix = "/sandboxes/" + sid + "/proxy/44772"
        code, body, _ = self.request(
            "/sandboxes/" + sid + "/endpoints/44772?use_server_proxy=true"
        )
        endpoint = json.loads(body)
        self.record(
            label + " endpoint",
            code == 200
            and endpoint_matches(endpoint.get("endpoint"), self.origin, prefix)
            and not endpoint.get("headers"),
            httpStatus=code,
        )
        return sid, prefix

    def command(
        self,
        prefix,
        text=None,
        *,
        argv=None,
        cwd="/workspace",
        envs=None,
        timeout=5000,
        background=False,
        succeeds=True,
        deadline=None,
    ):
        require((text is None) != (argv is None))
        payload = {
            "cwd": cwd,
            "envs": envs or {},
            "timeout": timeout,
            "background": background,
            **({"command": text} if argv is None else {"argv": argv}),
        }
        with self.open(
            prefix + "/command", "POST", payload, deadline=deadline
        ) as response:
            require(response.status == 200)
            events = list(response.events(self.parse_event))
        execution_id(events)
        if background and succeeds:
            # Background launch acknowledgment is not foreground completion.
            # Still parse the entire bounded stream and reject every error.
            require(not any(event["type"] == "error" for event in events))
        else:
            assert_result(events, succeeds)
        return events

    def upload(self, prefix, path, content):
        boundary = "proof" + uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"; filename="metadata"\r\nContent-Type: application/json\r\n\r\n'
                + json.dumps({"path": path, "mode": 0o644})
                + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.txt"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.request(
            prefix + "/files/upload",
            "POST",
            body,
            "multipart/form-data; boundary=" + boundary,
        )

    def download(self, prefix, path, **kwargs):
        return self.request(
            prefix + "/files/download?" + urllib.parse.urlencode({"path": path}),
            **kwargs,
        )

    def check_file(self, prefix, path, expected, **kwargs):
        code, body, _ = self.download(prefix, path, **kwargs)
        require(code == 200 and body == expected)

    def wait_ready(self, prefix, marker, deadline):
        while True:
            code, body, _ = self.download(
                prefix, "/workspace/" + marker + "-ready", deadline=deadline
            )
            if code == 200:
                require(body == b"ready")
                return
            require(code == 404)
            time.sleep(min(0.1, remaining(deadline)))

    def stopped(self, prefix, context, deadline):
        while True:
            code, body, _ = self.request(
                prefix + "/command/status/" + urllib.parse.quote(context, safe=""),
                deadline=deadline,
            )
            require(code == 200)
            state = json.loads(body)
            if state.get("running") is False:
                # A normal exit racing the cancellation is not a cleanup proof.
                require(state.get("exit_code", state.get("exitCode")) not in (None, 0))
                return
            require(state.get("running") is True)
            time.sleep(min(0.1, remaining(deadline)))

    def observe(self, prefix, marker, started, expected):
        # Observer command runs in the same live allocation, after both delayed
        # writes would have happened. No deletion/lease expiry may mask escape.
        delay = max(0, started + OBSERVE_SECONDS - time.monotonic())
        self.command(
            prefix,
            argv=["/usr/local/bin/python", "-c", f"import time; time.sleep({delay!r})"],
            timeout=10000,
        )
        self.check_file(prefix, "/workspace/" + marker + "-ready", b"ready")
        for suffix in ("-survived", "-survived-child"):
            code, body, _ = self.download(prefix, "/workspace/" + marker + suffix)
            require((code == 200 and body == b"bad") if expected else code == 404)
        self.check_file(
            prefix, "/workspace/fixture.txt", b"SKILLS-156 synthetic file round-trip\n"
        )
        events = self.command(prefix, "printf workspace-still-usable")
        require(output(events, "stdout") == "workspace-still-usable")

    def normal_exit(self, prefix):
        marker = "proof-normal-exit-" + uuid.uuid4().hex
        self.command(prefix, argv=self.adversarial_argv(marker, exit_after_fork=True))
        # Startup can exceed the entire observation window. Anchor after the
        # successful command completes so delayed descendant writes cannot fall
        # beyond the window merely because startup was slow.
        completed_at = time.monotonic()
        self.observe(prefix, marker, completed_at, expected=False)
        self.record("Normal exit reaps detached descendant", True)

    def cancellation(self, prefix, mode):
        require(
            mode in ("timeout", "foreground-delete", "background-delete", "disconnect")
        )
        marker = "proof-" + mode + "-" + uuid.uuid4().hex
        argv = self.adversarial_argv(marker)
        started = time.monotonic()
        handshake_deadline = min(self.deadline, started + HANDSHAKE_SECONDS)
        events = []
        if mode == "background-delete":
            events = self.command(
                prefix,
                argv=argv,
                background=True,
                timeout=LONG_COMMAND_MS,
                deadline=handshake_deadline,
            )
            context = execution_id(events)
            self.wait_ready(prefix, marker, handshake_deadline)
            ready_at = time.monotonic()
            cutoff = min(self.deadline, ready_at + CANCEL_SECONDS)
            code, _, _ = self.request(
                prefix + "/command?id=" + urllib.parse.quote(context, safe=""),
                "DELETE",
                deadline=cutoff,
            )
            require(code == 200)
        else:
            payload = {
                "argv": argv,
                "cwd": "/workspace",
                "background": False,
                "timeout": 1500 if mode == "timeout" else LONG_COMMAND_MS,
            }
            with self.open(
                prefix + "/command", "POST", payload, deadline=handshake_deadline
            ) as response:
                require(response.status == 200)
                iterator = response.events(self.parse_event)
                for event in iterator:
                    events.append(event)
                    require(event["type"] not in ("error", "execution_complete"))
                    if marker + "-ready\n" in output(events, "stdout"):
                        break
                else:
                    raise RuntimeError("adversarial handshake missing")
                context = execution_id(events)
                ready_at = time.monotonic()
                cutoff = min(self.deadline, ready_at + CANCEL_SECONDS)
                response.deadline = cutoff
                if mode == "foreground-delete":
                    code, _, _ = self.request(
                        prefix + "/command?id=" + urllib.parse.quote(context, safe=""),
                        "DELETE",
                        deadline=cutoff,
                    )
                    require(code == 200)
                if mode != "disconnect":
                    for event in iterator:
                        events.append(event)
                        if event["type"] in ("error", "execution_complete"):
                            break
                    assert_result(events, succeeds=False)
                # disconnect deliberately closes the HTTPS socket here, with no
                # DELETE or short server timeout that could hide propagation bugs.
        require(time.monotonic() < cutoff)
        # Reject normal exits and any delayed writes, including when network
        # latency caused the cancellation to arrive too late for this fixture.
        self.stopped(prefix, context, min(self.deadline, ready_at + SURVIVAL_SECONDS))
        self.observe(prefix, marker, ready_at, expected=False)
        require(time.monotonic() - started < LONG_COMMAND_MS / 1000)
        self.record(
            "Adversarial " + mode,
            True,
            seconds=round(time.monotonic() - started, 2),
            handshake=True,
            parentWrites=False,
            descendantWrites=False,
        )

    def namespace_checks(self, prefix):
        events = self.command(
            prefix,
            argv=[
                "/usr/local/bin/python",
                "-c",
                """
import ctypes, errno, os, platform, threading
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
arch = platform.machine()
clone, unshare, setns = {'x86_64': (56,272,308), 'aarch64': (220,97,268)}[arch]
def denied(number, expected, *args):
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(a) for a in args])
    assert result == -1 and ctypes.get_errno() == expected
denied(unshare, errno.EPERM, 0)
denied(setns, errno.EPERM, -1, 0)
for number in ((101,311,521,540) if arch == 'x86_64' else (117,271)):
    denied(number, errno.EPERM, -1, 0, 0, 0, 0, 0)
denied(435, errno.ENOSYS, 0, 0)
for flag in (0x80,0x20000,0x2000000,0x4000000,0x8000000,0x10000000,0x20000000,0x40000000):
    denied(clone, errno.EPERM, flag | 17, 0, 0, 0, 0)
if arch == 'x86_64': denied(0x40000000 | unshare, errno.ENOSYS, 0)
done = []
thread = threading.Thread(target=lambda: done.append(True))
thread.start(); thread.join(); assert done == [True]
pid = os.fork()
if pid == 0: os._exit(0)
assert os.waitpid(pid,0)[1] == 0
print(arch + ':namespace-filter-ok')
""",
            ],
        )
        architecture = output(events, "stdout").strip()
        require(
            architecture
            in ("aarch64:namespace-filter-ok", "x86_64:namespace-filter-ok")
        )
        self.record(
            "Native syscall denial and ordinary fork/thread",
            True,
            architecture=architecture.split(":")[0],
        )

    def run(self):
        # An unauthenticated synthetic lookup checks auth without listing or
        # selecting any allocation owned by another run.
        code, _, _ = self.request("/sandboxes/" + str(uuid.uuid4()), auth=False)
        self.record("API key required", code in (401, 403), httpStatus=code)
        first, prefix = self.create("First session")
        events = self.command(
            prefix, "printf command-ok; printf error-ok >&2; exit 7", succeeds=False
        )
        self.record(
            "stdout stderr nonzero exit",
            output(events, "stdout") == "command-ok"
            and output(events, "stderr") == "error-ok"
            and any(e.get("error", {}).get("evalue") == "7" for e in events),
        )
        events = self.command(prefix, "printf warm-ok")
        self.record("Warm command", output(events, "stdout") == "warm-ok")
        payload = b"SKILLS-156 synthetic file round-trip\n"
        code, _, _ = self.upload(prefix, "/workspace/fixture.txt", payload)
        self.record("File upload", code == 200, httpStatus=code)
        self.check_file(prefix, "/workspace/fixture.txt", payload)
        self.record("File download", True, sha256=hashlib.sha256(payload).hexdigest())
        events = self.command(
            prefix,
            "cat /workspace/fixture.txt; printf created-by-command > /workspace/generated.txt",
        )
        self.record(
            "Command reads uploaded file", output(events, "stdout") == payload.decode()
        )
        self.check_file(prefix, "/workspace/generated.txt", b"created-by-command")
        self.record("File API reads command output", True)
        events = self.command(
            prefix,
            'test ! -e /proc/self/environ && test ! -e /etc/passwd && test -z "$IDENTITY_ENDPOINT" && test -z "$IDENTITY_HEADER" && test -z "$AZURE_CLIENT_ID" && ! touch /usr/forbidden 2>/dev/null && printf restricted-ok',
        )
        self.record(
            "Restricted filesystem and identity environment",
            output(events, "stdout") == "restricted-ok",
        )
        self.command(
            prefix,
            "mkdir -p /workspace/sub; printf 'test ! -e /proc/self/environ || exit 91\\n' > /workspace/startup",
        )
        events = self.command(
            prefix,
            argv=[
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                'test "$PWD" = /workspace/sub && test "$CANARY" = literal && test "$PATH" = /usr/local/bin:/usr/bin:/bin && test "$1" = \'$(id)\' && printf argv-ok',
                "probe",
                "$(id)",
            ],
            cwd="/workspace/sub",
            envs={
                "CANARY": "literal",
                "PATH": "/workspace",
                "BASH_ENV": "/workspace/startup",
            },
        )
        self.record(
            "Native argv cwd environment and literal arguments",
            output(events, "stdout") == "argv-ok",
        )
        self.command(
            prefix,
            "ln -s /proc /workspace/proc-link; ln -s /workspace/.opsrabbit-system /workspace/reserved-link",
        )
        for path in (
            "/etc/passwd",
            "/workspace/../etc/passwd",
            "/workspace/proc-link/self/environ",
            "/workspace/reserved-link/canary",
            *("/workspace/" + name + "/canary" for name in PROTECTED),
        ):
            code, _, _ = self.download(prefix, path)
            require(code == 400)
            code, _, _ = self.upload(prefix, path, b"synthetic-denied-write")
            require(code == 400)
        self.record("File guards reject outside reserved and symlink paths", True)
        self.command(prefix, "test ! -e self/environ", cwd="/workspace/proc-link")
        code, _, _ = self.upload(
            prefix, "/workspace/.opsrabbit-runtime/stdin", b"stdin-ok"
        )
        require(code == 200)
        events = self.command(prefix, "cat /workspace/.opsrabbit-runtime/stdin")
        self.record(
            "Product stdin path remains usable", output(events, "stdout") == "stdin-ok"
        )
        _, other = self.create("Second session")
        code, _, _ = self.download(other, "/workspace/fixture.txt")
        self.record("Cross-session file isolation", code == 404, httpStatus=code)
        self.namespace_checks(prefix)
        marker = "proof-control-" + uuid.uuid4().hex
        started = time.monotonic()
        events = self.command(prefix, argv=self.adversarial_argv(marker), timeout=15000)
        require(marker + "-ready\n" in output(events, "stdout"))
        self.observe(prefix, marker, started, expected=True)
        self.record(
            "Uncancelled adversarial positive control",
            True,
            parentWrites=True,
            descendantWrites=True,
        )
        for mode in ("timeout", "foreground-delete", "background-delete", "disconnect"):
            self.cancellation(prefix, mode)
        self.normal_exit(prefix)
        self.identity(first, "Same allocation and native identity after cancellation")
        events = self.command(
            prefix,
            argv=[
                "/usr/local/bin/python",
                "-c",
                'import socket\ntry: socket.create_connection(("1.1.1.1",443),2)\nexcept OSError: print("egress-denied")\nelse: raise SystemExit(1)',
            ],
            timeout=4000,
        )
        self.record(
            "Outbound TCP denied", output(events, "stdout") == "egress-denied\n"
        )
        code, _, _ = self.request(prefix + "/ping?identifier=another-session")
        self.record("Session selector override rejected", code == 400, httpStatus=code)

    def cleanup(self):
        # Never discover IDs from list inventory, input files or previous reports.
        # Attempt every owned allocation even after a check/cleanup failure.
        for index, sid in enumerate(self.owned):
            try:
                deadline = time.monotonic() + 90
                code, _, receipt = self.request(
                    "/sandboxes/" + sid + "?wait_for_termination=true",
                    "DELETE",
                    cleanup=True,
                    deadline=deadline,
                )
                self.record(
                    "Confirmed retirement " + str(index + 1),
                    code == 204 and receipt == "true",
                    httpStatus=code,
                    workloadDeleted=receipt == "true",
                )
                code, _, _ = self.request(
                    "/sandboxes/" + sid + "/proxy/44772/ping",
                    cleanup=True,
                    deadline=deadline,
                )
                self.record(
                    "Retired execution denied " + str(index + 1),
                    code == 404,
                    httpStatus=code,
                )
            except Exception as error:
                self.report["outcome"] = "failed"
                self.report["checks"].append(
                    {
                        "name": "Cleanup " + str(index + 1),
                        "status": "failed",
                        "evidence": {"errorType": type(error).__name__},
                    }
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    probe = None

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        probe = Probe(args)
        probe.save()
        probe.run()
        probe.report["outcome"] = "passed"
    except BaseException as error:
        if probe is not None:
            probe.report["outcome"] = "failed"
            probe.report["errorType"] = type(error).__name__
        print(
            json.dumps({"outcome": "failed", "errorType": type(error).__name__}),
            flush=True,
        )
    finally:
        if probe is not None:
            # A second stop signal must not interrupt bounded targeted cleanup.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            probe.cleanup()
            probe.report["finishedAt"] = datetime.now(timezone.utc).isoformat()
            probe.save()
    return 0 if probe is not None and probe.report["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
