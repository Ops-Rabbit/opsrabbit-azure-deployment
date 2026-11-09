"""Exercise direct command and file APIs in the pinned runtime container."""

import hashlib
import http.client
import json
import subprocess
import sys
import time
import uuid


def request(port, path, method="GET", body=None, headers=None, timeout=15):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def main(image):
    network = "skills156-runtime-" + uuid.uuid4().hex[:12]
    container = None
    try:
        subprocess.run(
            ["docker", "network", "create", "--internal", network],
            check=True,
            capture_output=True,
            text=True,
        )
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "-d",
                "--network",
                network,
                # Exercise Bubblewrap mount isolation on the ephemeral,
                # read-only-token CI runner; this is test-container-only.
                "--security-opt",
                "apparmor=unconfined",
                "--security-opt",
                "seccomp=unconfined",
                "--cap-add",
                "SYS_ADMIN",
                "-p",
                "127.0.0.1::8080",
                image,
            ],
            text=True,
        ).strip()
        deadline = time.monotonic() + 30
        mappings = []
        inspected = None
        while time.monotonic() < deadline:
            inspected = json.loads(
                subprocess.check_output(["docker", "inspect", container])
            )[0]
            mappings = inspected["NetworkSettings"]["Ports"].get("8080/tcp") or []
            if mappings:
                break
            state = inspected["State"]
            if not state["Running"]:
                logs = subprocess.check_output(["docker", "logs", container], text=True)
                raise RuntimeError(
                    "runtime container exited before publishing its port "
                    f"(exit={state['ExitCode']}, error={state['Error']!r}, "
                    f"oom={state['OOMKilled']}): {logs[-2000:]}"
                )
            time.sleep(0.1)
        if not mappings:
            raise RuntimeError(
                "runtime container did not publish its port within 30 seconds: "
                + json.dumps(inspected["State"], sort_keys=True)
            )
        port = int(mappings[0]["HostPort"])
        for _ in range(120):
            try:
                status, _ = request(port, "/ping", timeout=3)
                if status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("runtime gateway did not become ready")

        token = "synthetic-local-isolation-test-token"
        binding = {
            "sandboxId": "local-isolation-test",
            "generation": 1,
            "envs": {
                "LD_LIBRARY_PATH": "/usr/local/lib",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/workspace",
            },
            "accessTokenHash": "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        }
        status, _ = request(
            port,
            "/internal/init",
            "POST",
            json.dumps(binding),
            {"Content-Type": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"one-shot runtime binding failed with HTTP {status}")

        command = (
            "test ! -e /proc/self/environ && test ! -e /etc/passwd && "
            'test -z "$IDENTITY_ENDPOINT" && ! touch /usr/forbidden 2>/dev/null && '
            "printf generated-by-command > /workspace/generated.txt && printf isolation-ok"
        )
        status, body = request(
            port,
            "/command",
            "POST",
            json.dumps(
                {
                    "command": command,
                    "cwd": "/workspace",
                    "timeout": 5000,
                    "background": False,
                }
            ),
            {
                "Content-Type": "application/json",
                "X-EXECD-ACCESS-TOKEN": token,
            },
        )
        response = body.decode(errors="replace")
        if (
            status != 200
            or "isolation-ok" not in response
            or "execution_complete" not in response
        ):
            raise RuntimeError(f"isolated command failed with HTTP {status}")

        status, body = request(
            port,
            "/files/download?path=%2Fworkspace%2Fgenerated.txt",
            headers={"X-EXECD-ACCESS-TOKEN": token},
        )
        if status != 200 or body != b"generated-by-command":
            raise RuntimeError(
                f"command output file download failed with HTTP {status}"
            )
        print("Direct command isolation and generated-file download: PASS")
    finally:
        if container is not None:
            subprocess.run(
                ["docker", "rm", "-f", container],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["docker", "network", "rm", network],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: test-runtime-container.py <image>")
    main(sys.argv[1])
