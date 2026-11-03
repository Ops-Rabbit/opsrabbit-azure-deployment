"""Start the real patched upstream server and check routes without Azure access."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

image = sys.argv[1]
key = "synthetic-local-startup-test-key-not-a-real-secret"
container = subprocess.check_output(
    [
        "docker",
        "run",
        "-d",
        "--platform",
        "linux/amd64",
        "-p",
        "127.0.0.1::8080",
        "-e",
        "OPENSANDBOX_API_KEY=" + key,
        "-e",
        "AZURE_SESSION_IMAGE=example.invalid/runtime@sha256:" + "a" * 64,
        "-e",
        "AZURE_SESSION_ENDPOINT=https://proof.example.eastus2.azurecontainerapps.io",
        "-e",
        "AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000",
        image,
    ],
    text=True,
).strip()
try:
    port = json.loads(subprocess.check_output(["docker", "inspect", container]))[0][
        "NetworkSettings"
    ]["Ports"]["8080/tcp"][0]["HostPort"]
    base = "http://127.0.0.1:" + port
    for _ in range(30):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                assert response.status == 200
            break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    else:
        raise RuntimeError("Upstream server did not start")
    try:
        urllib.request.urlopen(base + "/sandboxes", timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("Unauthenticated lifecycle access was allowed")
    req = urllib.request.Request(
        base + "/sandboxes", headers={"OPEN-SANDBOX-API-KEY": key}
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200 and json.load(response)["items"] == []
    print("Real server startup, anonymous rejection, and authenticated list: PASS")
finally:
    subprocess.run(
        ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
    )
