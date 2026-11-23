# OpenSandbox on Azure Dynamic Sessions — technical guide

OpenSandbox runs on Azure Container Apps and creates isolated command/file
workspaces using Azure Container Apps Dynamic Sessions.
An `AzureSandboxService` implementation allocates custom-container Dynamic
Sessions. The upstream server proxy forwards execution requests through a
loopback bridge that adds Azure authentication and a per-session execd credential. The custom image runs the actual
upstream `execd` binary. There is no AKS cluster, VM, Docker daemon, or native
Azure-only replacement for the OpenSandbox API.

For installation, upgrades and troubleshooting, follow the [customer README](../../README.md).
This guide covers the adapter, security boundaries, recovery and contributor tests.
OpenSandbox is required for every enabled application deployment; browser
execution is unsupported by this runtime.

## Supported contract

- One trusted installation controller/API key and one fixed image digest/pool.
- Create, list, read, endpoint discovery with `use_server_proxy=true`, and delete.
- Commands with stdout/stderr/exit status, file upload/download, command timeout
  and interruption. Requests and responses are bounded.
- Configurable concurrent sessions, fixed 0.5 CPU/1Gi shape, explicit 60–3600
  second maximum lifetime. Azure may delete an idle sandbox at the 3600-second
  cooldown before its requested absolute expiry.
  The installer retains one ready instance. Scale-to-zero cold starts are not
  qualified.
- Only execd port 44772 is exposed through the OpenSandbox proxy; the Azure
  container itself listens on port 8080.
- Absolute lifetime enforcement and cleanup retry while the controller is alive.
- Unknown, retired, expired or changed-generation sessions are rejected. Reads
  check Azure allocation before sending a request that could allocate a session.
- Explicit rejection of arbitrary images, registry credentials, environment
  injection, volumes, lifecycle hooks, per-session network rules, unsupported
  ports, snapshots, pause/resume, metadata mutation, and renewal beyond the
  absolute one-hour lifetime cap.

The required `sleep infinity` entrypoint describes the fixed long-running pool
contract; the image always starts restricted execd. The adapter rejects other
entrypoints instead of silently running an arbitrary caller-provided command.

## Trust boundaries

The controller uses a dedicated user-assigned managed identity with Session
Executor at the exact pool scope. A separate image-pull identity has only
`Container Registry Repository Reader` on a dedicated runtime-only ACR, and
pool identity lifecycle `None`. The customer installer separates this registry
from the application/controller ACR because live Dynamic Sessions image pulls
rejected repository-conditioned Reader grants. Store only runtime images in
that dedicated registry. No controller key or
Azure access token is forwarded into the execution container or returned in
endpoint metadata.

In public mode, the controller has public HTTPS ingress with API-key authentication
and no IP allowlist. Private mode uses the internal ACA environment. The Azure
pool gateway requires Entra authentication, and the pool has `EgressDisabled`.
Public-mode tests do not qualify a customer's private network topology.

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

The customer image publication helper also requires Skopeo; it scans the OCI
layout before copying the image and attestations to ACR.
Both CI and publication reject High and Critical vulnerabilities, including
findings without a vendor fix. No vulnerability exclusions are applied.

The final server and runtime layers use digest-pinned Python on Alpine 3.24.
The server installs the checksum-pinned OpenSandbox 1.1.1 wheel and the hashed
transitive lock in `requirements-server.txt`; narrow Azure patches still fail
the build if the expected upstream interfaces change. To refresh the lock:

```sh
uv pip compile spikes/dynamic-sessions/requirements-server.in \
  --python-version 3.12 --python-platform x86_64-unknown-linux-musl \
  --generate-hashes --output-file spikes/dynamic-sessions/requirements-server.txt
```

Alpine uses musl rather than glibc. The runtime includes Bash, GNU coreutils,
Python and the static execd binary. Arbitrary glibc-only executables are not
qualified for this image. The command worker's restricted `/usr` view includes
copies of Alpine's `/bin` and `/lib` files; the original package files and
package database remain intact for scanning and maintenance.

CI runs the contract tests and real upstream startup/authentication checks without
Azure credentials or provisioning cloud resources.

## Live verification

Deploy using the [customer README](../../README.md), then verify the exact
controller endpoint and runtime image digest from that installation. Confirm the
active session-pool template uses that digest, has a ready instance and has no
image-pull failures. An ARM resource marked Succeeded alone is insufficient.

Keep the API key in a mode-0600 file inside a private mode-0700 directory, never
in command arguments, logs or the repository. The runner validates TLS and uses
only synthetic data. Run from the repository root:

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

## Durable controller recovery

The controller stores its session journal in a dedicated PostgreSQL schema and
uses a restricted role that can create and modify objects in that schema, but
cannot create objects in `public`. SQLite remains only as a local development
fallback. On startup the controller fences any interrupted allocation as
retiring, confirms deletion with Azure, and revalidates each active session
against the exact pool generation before serving requests. A PostgreSQL session
advisory lock fences overlapping Container Apps revisions; only its holder may
recover or mutate the journal. A new revision can start while the old one drains,
but returns 503 until it acquires the lock and completes recovery. If Azure
cannot confirm recovery, the controller stays unavailable and retries. It never
replays a command or silently allocates a replacement session for an old
identifier. Keep each deployment to one configured replica; the lock prevents
overlapping writers, but this implementation does not provide multi-replica
availability. The controller checks its lock-owning database connection; if
PostgreSQL drops that connection, it exits so Container Apps restarts it and
startup recovery reconciles the durable journal. Per-session locks preserve
ordering for one sandbox while slow allocation and Azure calls leave unrelated
sandbox requests available.

The customer installer creates the schema and role, injects the connection
string through the Container App secret mechanism, waits for the pool/controller
to become ready, then invokes the product's verify-before-enable bootstrap from
an init container. Existing administrator settings are preserved. Application
startup is blocked if the real OpenSandbox command, file round-trip, or confirmed
cleanup check fails.

## Limitations and release checks

- Azure metadata-read plus dispatch is not atomic against external administrators.
  The runtime initialization gate and per-session credential prevent stale
  execution on replacement containers, but external interference may still cause
  a rejected request to allocate an idle replacement. Restrict executor access
  to the controller and keep each pool dedicated to this deployment.
- Tenant authorization belongs to OpsRabbit as the trusted caller. Adapter tests
  do not establish OpsRabbit multi-tenant authorization or upstream tenant mode.
- The minimal runtime, denied egress and fixed image cannot satisfy all
  OpsRabbit tool workloads. Browser execution, sustained concurrency,
  customer-network acceptance and multi-replica coordination remain unsupported.
- Runtime and controller images must pass the High/Critical gate. Review the
  exact commit's JSON scan artifacts from the Azure OpenSandbox proof workflow
  before release. Scan application images separately; a synthetic-QA exception
  is not customer-release approval. A clean scan does not replace live Azure
  qualification of a changed runtime base.
- A centrally managed release image pipeline remains necessary; the current
  helper builds, scans and publishes to the customer's ACR.
