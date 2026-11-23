"""Start the real patched upstream server and check routes without Azure access."""

import json
import http.client
import subprocess
import sys
import time

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
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
    # Local Apple Silicon Docker emulation can take over a minute for this
    # upstream Python server's first import; CI runners typically start sooner.
    for _ in range(120):
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            break
        except (OSError, http.client.HTTPException):
            time.sleep(1)
        finally:
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
    else:
        raise RuntimeError("Upstream server did not start")
    connection.request("GET", "/sandboxes")
    response = connection.getresponse()
    response.read()
    if response.status != 401:
        raise AssertionError("Unauthenticated lifecycle access was allowed")
    connection.close()
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
    connection.request("GET", "/sandboxes", headers={"OPEN-SANDBOX-API-KEY": key})
    response = connection.getresponse()
    assert response.status == 200 and json.loads(response.read())["items"] == []
    print("Real server startup, anonymous rejection, and authenticated list: PASS")
finally:
    if "connection" in locals():
        connection.close()
    subprocess.run(
        ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
    )
