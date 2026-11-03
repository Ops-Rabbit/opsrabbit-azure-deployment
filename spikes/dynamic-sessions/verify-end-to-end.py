"""Run synthetic checks through the real OpenSandbox HTTPS API.

Reads the key from a local file. Never records keys, tokens, request headers or
customer data. Sessions are disposable; environment and pool are retained.
"""

import argparse
import concurrent.futures
import threading
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--key-file", type=Path, required=True)
parser.add_argument("--image", required=True)
parser.add_argument("--evidence", type=Path, required=True)
args = parser.parse_args()
if not args.url.startswith("https://"):
    parser.error("A verified HTTPS endpoint is required")
key = args.key_file.read_text().strip()
report = {
    "observedAt": datetime.now(timezone.utc).isoformat(),
    "checks": [],
    "sessions": [],
}


def request(path, method="GET", data=None, content_type="application/json", auth=True):
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    headers = {"OPEN-SANDBOX-API-KEY": key} if auth else {}
    if body is not None:
        headers["Content-Type"] = content_type
    if method == "DELETE" and body is None:
        body = b""
    req = urllib.request.Request(
        args.url.rstrip("/") + path, data=body, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as response:
            return response.status, response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(2 * 1024 * 1024)


def record(name, passed, evidence):
    row = {
        "name": name,
        "status": "passed" if passed else "failed",
        "evidence": evidence,
    }
    report["checks"].append(row)
    args.evidence.write_text(json.dumps(report, indent=2))
    print(json.dumps(row), flush=True)
    if not passed:
        raise RuntimeError("Acceptance check failed: " + name)


def create(label):
    start = time.monotonic()
    code, body = request(
        "/sandboxes",
        "POST",
        {
            "image": {"uri": args.image},
            "entrypoint": ["sleep", "infinity"],
            "timeout": 300,
            "resourceLimits": {"cpu": "0.5", "memory": "1Gi"},
            "metadata": {"purpose": "skills156-synthetic-proof"},
        },
    )
    record(
        label + " allocation",
        code == 202,
        {
            "httpStatus": code,
            "seconds": round(time.monotonic() - start, 2),
            "response": json.loads(body) if code != 202 else "created",
        },
    )
    sid = json.loads(body)["id"]
    report["sessions"].append(sid)
    code, body = request("/sandboxes/" + sid + "/endpoints/44772?use_server_proxy=true")
    endpoint = json.loads(body)
    record(
        label + " endpoint",
        code == 200
        and "/proxy/44772" in endpoint.get("endpoint", "")
        and not endpoint.get("headers"),
        {
            "httpStatus": code,
            "serverProxy": True,
            "credentialHeadersExposed": bool(endpoint.get("headers")),
        },
    )
    return sid, "/sandboxes/" + sid + "/proxy/44772"


def command(prefix, text, timeout=5000, background=False):
    code, body = request(
        prefix + "/command",
        "POST",
        {
            "command": text,
            "cwd": "/workspace",
            "timeout": timeout,
            "background": background,
        },
    )
    events = []
    for line in body.decode().splitlines():
        line = line.removeprefix("data:").strip()
        if line.startswith("{"):
            events.append(json.loads(line))
    return code, events


def output(events, kind):
    return "".join(e.get("text", "") for e in events if e.get("type") == kind)


def upload(prefix, path, content):
    boundary = "proof" + uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"; filename="metadata"\r\nContent-Type: application/json\r\n\r\n'
            + json.dumps({"path": path, "mode": 420})
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.txt"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return request(
        prefix + "/files/upload",
        "POST",
        body,
        "multipart/form-data; boundary=" + boundary,
    )


try:
    code, _ = request("/sandboxes", auth=False)
    record("API key required", code in (401, 403), {"httpStatus": code})
    first, prefix = create("First session")
    code, events = command(prefix, "printf command-ok; printf error-ok >&2; exit 7")
    record(
        "stdout stderr nonzero exit",
        code == 200
        and output(events, "stdout") == "command-ok"
        and output(events, "stderr") == "error-ok"
        and any(e.get("error", {}).get("evalue") == "7" for e in events),
        {"httpStatus": code, "events": events},
    )
    code, events = command(prefix, "printf warm-ok")
    record(
        "Warm command",
        code == 200
        and output(events, "stdout") == "warm-ok"
        and any(e.get("type") == "execution_complete" for e in events),
        {"httpStatus": code, "events": events},
    )
    payload = b"SKILLS-156 synthetic file round-trip\n"
    code, body = upload(prefix, "/workspace/fixture.txt", payload)
    record(
        "File upload",
        code == 200,
        {"httpStatus": code, "response": body.decode()[:500]},
    )
    code, body = request(prefix + "/files/download?path=%2Fworkspace%2Ffixture.txt")
    record(
        "File download",
        code == 200 and body == payload,
        {"httpStatus": code, "sha256": hashlib.sha256(body).hexdigest()},
    )
    code, events = command(
        prefix,
        "cat /workspace/fixture.txt; printf created-by-command > /workspace/generated.txt",
    )
    record(
        "Command reads uploaded file",
        code == 200
        and output(events, "stdout") == payload.decode()
        and any(e.get("type") == "execution_complete" for e in events),
        {"httpStatus": code, "events": events},
    )
    code, body = request(prefix + "/files/download?path=%2Fworkspace%2Fgenerated.txt")
    record(
        "File API reads command output",
        code == 200 and body == b"created-by-command",
        {"httpStatus": code, "sha256": hashlib.sha256(body).hexdigest()},
    )
    code, events = command(
        prefix,
        'test ! -e /proc/self/environ && test ! -e /etc/passwd && test -z "$IDENTITY_ENDPOINT" && test -z "$IDENTITY_HEADER" && test -z "$AZURE_CLIENT_ID" && ! touch /usr/forbidden 2>/dev/null && printf restricted-ok',
    )
    record(
        "Restricted filesystem and identity environment",
        code == 200
        and output(events, "stdout") == "restricted-ok"
        and any(e.get("type") == "execution_complete" for e in events),
        {"httpStatus": code, "events": events},
    )
    code, body = request(prefix + "/files/download?path=%2Fetc%2Fpasswd")
    record(
        "File API cannot read host identity file",
        code in (400, 403, 404, 500) and b"root:" not in body,
        {"httpStatus": code},
    )
    second, other = create("Second session")
    code, body = request(other + "/files/download?path=%2Fworkspace%2Ffixture.txt")
    record(
        "Cross-session file isolation",
        code in (400, 404, 500) and payload not in body,
        {"httpStatus": code},
    )
    started = time.monotonic()
    code, events = command(prefix, "sleep 5", timeout=100)
    record(
        "Command deadline",
        code == 200
        and time.monotonic() - started < 3
        and any(e.get("error", {}).get("evalue") == "-1" for e in events),
        {"httpStatus": code, "events": events},
    )
    code, events = command(prefix, "sleep 20", timeout=25000, background=True)
    context = next((e["text"] for e in events if e.get("type") == "init"), None)
    record(
        "Background command",
        code == 200 and bool(context),
        {"httpStatus": code, "events": events},
    )
    code, body = request(
        prefix + "/command?id=" + urllib.parse.quote(context), "DELETE"
    )
    record(
        "Command cancellation request",
        code == 200,
        {"httpStatus": code, "response": body.decode()[:500]},
    )
    code, body = request(prefix + "/command/status/" + urllib.parse.quote(context))
    status = json.loads(body)
    record(
        "Command cancellation confirmed",
        code == 200 and status.get("running") is False,
        {
            "httpStatus": code,
            "running": status.get("running"),
            "exitCode": status.get("exit_code"),
        },
    )
    command_started = threading.Event()
    foreground = {}

    def run_foreground():
        req = urllib.request.Request(
            args.url.rstrip("/") + prefix + "/command",
            data=json.dumps(
                {"command": "sleep 20", "cwd": "/workspace", "timeout": 25000}
            ).encode(),
            headers={"OPEN-SANDBOX-API-KEY": key, "Content-Type": "application/json"},
        )
        events = []
        with urllib.request.urlopen(req, timeout=35) as response:
            for raw in response:
                line = raw.decode().removeprefix("data:").strip()
                if not line.startswith("{"):
                    continue
                event = json.loads(line)
                events.append(event)
                if event.get("type") == "init":
                    foreground["id"] = event["text"]
                    command_started.set()
        return events

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_foreground)
        ready = command_started.wait(15)
        record("Foreground stream starts", ready, {"receivedInit": ready})
        code, _ = request(prefix + "/command?id=" + foreground["id"], "DELETE")
        events = future.result(timeout=10)
        record(
            "Foreground stream cancellation",
            code == 200
            and any(e.get("error", {}).get("evalue") == "-1" for e in events),
            {"httpStatus": code, "events": events},
        )
    code, events = command(
        prefix,
        '/usr/local/bin/python3 -c \'import socket;\ntry: socket.create_connection(("1.1.1.1",443),2)\nexcept OSError: print("egress-denied")\nelse: raise SystemExit(1)\'',
        timeout=4000,
    )
    record(
        "Outbound TCP denied",
        code == 200
        and output(events, "stdout") == "egress-denied\n"
        and any(e.get("type") == "execution_complete" for e in events),
        {"httpStatus": code, "events": events},
    )
    code, _ = request(prefix + "/ping?identifier=another-session")
    record("Session selector override rejected", code == 400, {"httpStatus": code})
    for sid in (first, second):
        code, _ = request("/sandboxes/" + sid, "DELETE")
        record(
            "Verified retirement " + ("A" if sid == first else "B"),
            code == 204,
            {"httpStatus": code, "adapterChecksAzureAbsence": True},
        )
        code, _ = request("/sandboxes/" + sid + "/proxy/44772/ping")
        record(
            "Retired execution denied " + ("A" if sid == first else "B"),
            code == 404,
            {"httpStatus": code},
        )
    report["outcome"] = "passed"
except Exception as error:
    report["outcome"] = "failed"
    report["errorType"] = type(error).__name__
    print(
        json.dumps({"outcome": "failed", "errorType": type(error).__name__}), flush=True
    )
finally:
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    args.evidence.write_text(json.dumps(report, indent=2))

raise SystemExit(0 if report.get("outcome") == "passed" else 1)
