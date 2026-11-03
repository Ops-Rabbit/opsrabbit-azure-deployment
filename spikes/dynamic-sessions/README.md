# OpenSandbox on Azure Dynamic Sessions — feasibility proof

This proof runs the actual OpenSandbox lifecycle server on Azure Container Apps.
An `AzureSandboxService` implementation allocates custom-container Dynamic
Sessions. The upstream server proxy forwards execution requests through a
loopback bridge that adds Azure authentication and a per-session execd credential. The custom image runs the actual
upstream `execd` binary. There is no AKS cluster, VM, Docker daemon, or native
Azure-only replacement for the OpenSandbox API.

This directory is a bounded feasibility implementation. It does **not** change
customer Terraform defaults or complete mandatory installer integration.

## Supported contract

- One trusted installation controller/API key and one fixed image digest/pool.
- Create, list, read, endpoint discovery with `use_server_proxy=true`, and delete.
- Commands with stdout/stderr/exit status, file upload/download, command timeout
  and interruption. Requests and responses are bounded.
- Two concurrent sessions, fixed 0.5 CPU/1Gi shape, explicit 60–300 second lifetime.
  Azure rejected a zero-ready-instance pool configuration in this subscription;
  this proof retains one ready instance and does not claim scale-to-zero cold-start
  acceptance.
- Only execd port 44772 is exposed through the OpenSandbox proxy; the Azure
  container itself listens on port 8080.
- Absolute lifetime enforcement and cleanup retry while the controller is alive.
- Unknown, retired, expired or changed-generation sessions are rejected. Reads
  check Azure allocation before sending a request that could allocate a session.
- Explicit rejection of arbitrary images, registry credentials, environment
  injection, volumes, lifecycle hooks, per-session network rules, unsupported
  ports, snapshots, pause/resume, renewal and metadata mutation.

The required `sleep infinity` entrypoint describes the fixed long-running pool
contract; the image always starts restricted execd. The adapter rejects other
entrypoints instead of silently running an arbitrary caller-provided command.

## Trust boundaries

The controller uses a dedicated user-assigned managed identity with Session
Executor at the exact pool scope. A separate image-pull identity has only AcrPull
on the proof registry and pool identity lifecycle `None`. No controller key or
Azure access token is forwarded into the execution container or returned in
endpoint metadata.

The proof's HTTPS controller ingress requires its API key and is restricted to
the walkthrough client's public IP. The Azure pool gateway requires Entra
authentication. The pool has `EgressDisabled`. This is not proof of a private-only
customer network topology.

Bubblewrap starts **execd itself** in a restricted view. `/workspace` and a private
`/tmp` are writable; `/usr` and the execd binary are read-only. Host `/etc` and
`/proc` are absent. File APIs therefore share the command filesystem boundary.
Failure to establish the namespace terminates the container; there is no fallback.
The runtime starts with its initialization gate enabled. The controller binds the
session ID and a hash of a fresh execution credential before accepting commands.
The raw credential remains in private controller state and authenticated requests;
it is not placed in the workload environment or endpoint response. A newly
allocated replacement runtime rejects stale requests instead of executing them.
This minimal image is not the full OpsRabbit tool image, and browser execution is
unsupported.

## Local validation

```sh
python3 -m venv /tmp/azure-proof-lint
/tmp/azure-proof-lint/bin/pip install ruff==0.14.8
/tmp/azure-proof-lint/bin/ruff check spikes/dynamic-sessions
/tmp/azure-proof-lint/bin/ruff format --check spikes/dynamic-sessions

docker build --platform linux/amd64 -t opensandbox-azure-proof:test \
  -f spikes/dynamic-sessions/Dockerfile.server spikes/dynamic-sessions
docker run --rm --platform linux/amd64 --network none --entrypoint python \
  -v "$PWD/spikes/dynamic-sessions/test_azure_service.py:/tmp/test_azure_service.py:ro" \
  opensandbox-azure-proof:test /tmp/test_azure_service.py
python3 spikes/dynamic-sessions/test-server-startup.py opensandbox-azure-proof:test
```

CI runs the contract tests and real upstream startup/authentication checks without
Azure credentials or provisioning cloud resources.

## Live proof deployment

Use a dedicated test resource group and retain exact resource names in a private
inventory. Never put credentials or customer-specific resource IDs in this repo.

1. Create a Consumption Container Apps environment and ACR with registry admin
   credentials disabled.
2. Build/publish `Dockerfile.runtime` and `Dockerfile.server`; resolve both tags to
   immutable registry digests before deployment.
3. Create an image-pull identity and assign AcrPull at registry scope.
4. Create a `Microsoft.App/sessionPools` custom-container Dynamic pool using the
   environment, runtime digest, port 8080, 0.5 CPU/1Gi, two maximum sessions, one
   ready instance, 300-second timed idle cooldown, `EgressDisabled`, and image-pull
   identity lifecycle `None`. ARM API `2025-07-01` was used to deploy it; API
   `2026-07-01` exposes template rollout and readiness details.
5. Create a separate controller identity. Grant it Session Executor on this pool
   and AcrPull on the registry. Do not grant these permissions to sessions.
6. Deploy the server digest as a single-replica Container App, with HTTPS-only
   port 8080 ingress and an IP allowlist for the test client. Inject a strong
   generated `OPENSANDBOX_API_KEY` using an ACA secret, not plaintext checked-in
   configuration. Configure `AZURE_CLIENT_ID`, `AZURE_SESSION_IMAGE` (the same
   runtime digest), and `AZURE_SESSION_ENDPOINT` (exact HTTPS pool endpoint).
7. Wait for the **active** pool template to reference the expected image and show
   a ready instance, and for the controller revision to be healthy. An ARM
   resource marked Succeeded alone is insufficient.
8. Run the verification below. Keep the local key file private (mode 0600 in a
   mode-0700 directory). The runner uses Python's default TLS certificate checks.

```sh
python3 spikes/dynamic-sessions/verify-end-to-end.py \
  --url 'https://<controller-fqdn>' \
  --key-file '<private-directory>/server-api-key' \
  --image '<registry>/opensandbox-runtime@sha256:<digest>' \
  --evidence '<private-directory>/execution.json'
```

The runner uses only synthetic files. It retires the two successful test sessions
and checks that later execution is rejected. If a check fails, consult the saved
session inventory and retire those exact sessions through OpenSandbox, or allow
bounded expiry. Do not delete the environment/pool to clean up a fixture.

## Limits before customer installation

- Lifecycle state currently lives in SQLite inside one controller replica. On
  controller replacement, unknown sessions fail closed but Azure's idle cooldown
  may outlive their requested deadline. Durable state, recovery and a stronger
  lease fence are required before production. Do not scale replicas above one.
- Azure metadata-read plus dispatch is not atomic against external administrators.
  The runtime initialization gate and per-session credential prevent stale
  execution on replacement containers, but external interference may still cause
  a rejected request to allocate an idle replacement. Restrict executor access
  to the controller and finish lifecycle recovery before production.
- Tenant authorization belongs to the trusted caller in this proof. It is not
  acceptance of OpsRabbit multi-tenant authorization or upstream tenant mode.
- The minimal runtime, denied egress and fixed image cannot yet satisfy all
  OpsRabbit tool workloads. Bootstrap/product UI, customer networking, renewals,
  sustained concurrency, restart recovery and full installer tests are separate
  acceptance gates.
- Scans still report upstream/base-image High vulnerabilities. Record the exact
  scan results; do not present an execution pass as a production security pass.
- Building this proof uses pinned top-level Python packages, but a full hashed
  transitive dependency lock and release image pipeline remain necessary.

Keep this test setup running for the requested walkthrough. Individual sessions
remain ephemeral. Warm capacity, Container Apps and retained registry storage can
incur charges. Teardown requires a subsequent user instruction.
