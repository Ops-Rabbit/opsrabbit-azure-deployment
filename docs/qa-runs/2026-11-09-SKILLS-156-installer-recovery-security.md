# SKILLS-156 installer, recovery, and image scan checks

Observed 2026-11-09 15:41 UTC. These are local code and unit tests only. The Azure
environment was torn down at the user's direction and was not recreated.

## Installer integration

- Terraform provisions the mandatory ACA controller and Dynamic Sessions pool
  when the application is enabled for either ACI or ACA.
- Private ACI deployments require a separate delegated Container Apps subnet.
- Before OpsRabbit starts, the installer init container runs migrations and the
  product's versioned `--install` contract.
- A fresh install verifies command execution, file round-trip, and confirmed
  sandbox cleanup before activation. A failure leaves settings untouched.
- Existing settings pass only if active, compatible with the configured endpoint
  and image, and verified with the saved encrypted key. Disabled/incompatible
  settings stop installation and report administrator reconciliation; they are
  not overwritten or counted as a successful mandatory installation.
- Existing settings are compared in full before and after the remote probe; a
  concurrent configuration edit or saved-key rotation fails reconciliation.
- Migration failure stops the ACA or ACI startup sequence before installer
  bootstrap. Controller admission capacity follows the Terraform pool limit.
- Sandbox leases are capped at one hour to match the Dynamic Sessions maximum
  idle cooldown; Azure can retire an idle session sooner than its requested expiry.
- ACR uses RBAC plus ABAC repository permissions. The app image identity is
  limited to its backend and web repositories; separate identities read only
  the OpenSandbox server and runtime repositories. The controller execution
  identity has no ACR data role and receives Session Executor only on its exact
  pool. ACA lifecycle settings keep the server pull identity unavailable to
  controller code; Dynamic Sessions keeps the runtime pull identity unavailable
  to sandbox code.
- Repository-scoped Reader assignments use ACR's documented
  `registries/repositories:name` attribute and are created before ACR switches
  from legacy role permissions to ABAC. A trigger-aware five-minute Terraform
  propagation barrier precedes the mode switch and image pulls. README documents
  the temporary registry-wide import role and repository-scoped Writer roles
  needed by customer operators. The old `AcrPull` assignment remains tracked for
  upgrade continuity; ACR ignores it after the switch.

## Durable recovery

The Azure adapter uses a restricted PostgreSQL role/schema for its session
journal. Terraform drops only that dedicated schema with `CASCADE`, allowing
the controller-owned journal table to be removed during destroy. On startup it
retires interrupted allocations and revalidates active sessions against Azure
before serving. Failed reconciliation remains fail-closed.
SQLite is retained only as a local-development fallback. The controller remains
single-replica; multi-replica coordination and browser execution are unsupported.

## Local checks

| Check | Result |
| --- | --- |
| PostgreSQL restart recovery, schema permissions, schema teardown, lost-leader recovery, overlapping-controller fence, and lock-order regression | PASS in hosted run 42651517438 |
| Adapter contract, concurrency, route allowlist, SQLite recovery, and fixed absolute renewal limit | PASS, 38/38 |
| Real OpenSandbox server startup/authentication | PASS in hosted CI run 42622842799 |
| Runtime gateway, file-path confinement, minimal mount view, and worker launch tests | PASS, 19/19 |
| Direct runtime command isolation and generated-file download | Run 42651517438 failed because the internal-network container did not expose a host port; test now uses its internal bridge address and awaits hosted rerun |
| Server image scan | PASS gate in hosted run 42651517438: 0 Critical, 47 High |
| Runtime image scan | PASS gate in hosted run 42651517438: 0 Critical, 47 High |
| Image-security acceptance | Both scan gates pass; security-owner disposition of 47 High findings per image remains outstanding |
| Focused ACA Terraform topology tests | PASS, 13/13 (current local candidate) |
| Terraform formatting and validation | PASS |
| Full Terraform test suite | PASS, 29/29 (including private ACA DNS, private ACI topology, ABAC roles, propagation wait, and atomic identity lifecycle settings) |
| Product OpenSandbox targeted tests | PASS, 143/143 |
| Product changed backend line coverage | initializer 100%; verifier 100%; execution 85.62% |
| Product TypeScript check and quality lint | PASS |
| Product installer and verifier tests on PR head `f01556073` | PASS, 65/65; backend TypeScript check and Biome pass (local Node 24.22.1 emits an engine-floor warning; required is 24.22.2) |
| Strict Semgrep scan of changed product source | PASS, 0 findings |
| Strict Semgrep scan of updated installer runtime source | Not rerun; Semgrep is unavailable in this shell |
| Ruff 0.14.8 lint and format for all spike code | PASS |
| Python bytecode compilation and image-build shell syntax | PASS |
| Direct runtime container command/file test | Pending hosted CI; Docker image store returns an I/O error locally |
| Independent autoreview | PASS at P1 threshold for this candidate; GitHub connector review must follow the next push |

The first final review caught an incorrect Azure role name and two protocol
edge cases in the runtime gateway. The role now uses Microsoft's documented
`Azure ContainerApps Session Executor` name and is asserted by the Terraform
tests. The gateway accepts only the SDK's empty chunked DELETE framing and
buffers bounded downloads so truncated bodies fail before a success response
is sent. A subsequent review also found that direct OpenSandbox command calls
were outside the workspace-only boundary and that Terraform could not drop the
controller journal schema after it gained a table. Both command and file execd
workers now launch inside the same proc-free Bubblewrap mount view. The dedicated
schema is explicitly dropped with cascade. A final review caught fractional
session limits that Azure and the Python controller cannot consume; validation
now requires a whole number and the Terraform suite rejects a fractional value.
Later review passes added private ACA DNS, a Docker test network that allows
host-side requests without external egress, and an ACA revision fingerprint for
OpenSandbox endpoint/image/session changes. The latest review also found direct
file API paths could escape `/workspace`; the gateway now validates and
canonicalizes query, JSON, and multipart paths, including symlink targets, and
rejects traversal before forwarding to execd. Its allowed routes match the
runtime gateway, including search, move, permissions, and replace operations.
The controller holds a PostgreSQL advisory lock across recovery and service to
prevent overlapping ACA revisions from reconciling the same journal. It releases
the in-process journal lock during recovery I/O, preserving the request path's
session-lock-then-journal-lock order and avoiding a revision-handover deadlock.
Startup recovery validates active rows before public readiness is enabled. It
monitors that advisory-lock connection and exits on database connection loss so
ACA can restart it and replay durable recovery. A concurrent request regression
test covers this lock ordering; it passed with the rest of the PostgreSQL suite
in hosted run 42651517438. The
installer probes backend image capabilities
before migration, and both ACA apps have revision markers for generated
credential rotations. The
SQLite fallback transitions interrupted allocations to retirement on restart.
Slow session allocation does not hold the controller database lock during Azure
network calls; per-session locks keep operations on one sandbox ordered. The
activation transaction checks the allocating state and lease deadline, so an
allocation that exceeds its lifetime is retired instead of returned as active.
The ACI init container also receives a non-secret revision fingerprint that
triggers re-verification when the session pool changes.

The file API validates and canonicalizes paths, then runs in a Bubblewrap mount
namespace that contains only the workspace, a read-only static execd binary,
and isolated temporary storage. The file worker does not mount `/usr`, `/etc`,
`/proc`, or `/dev`, so a symlink race cannot expose the runtime's system files.
The workspace temporary path is verified before Bubblewrap mounts a separate
tmpfs over it. Unit tests verify the mount arguments; container-level execution
remains pending the hosted Docker test.

The command worker also mounts `/usr/local` read-only. Its environment uses
`/usr/local/bin` and `/usr/local/lib` for Python and shared libraries; the
read-only bind makes those paths available without exposing them to writes.
The file worker continues to use its smaller mount view.

Autoreview caught a private ACI routing issue: environment-only ACA ingress was
not reachable from the separate ACI subnet. The controller now uses the
internal environment load balancer, and Terraform tests assert this path for
both private ACI and private ACA. A later review raised an unprivileged
`umount2` call; the design now avoids that operation and gives the file worker
only its static executable, the workspace, and isolated temporary storage. The
review also suggested a missing Consumption workload profile, but the shared
ACA environment already declares that profile in `container-apps.tf`; Terraform
validation and topology tests pass with it present.

The security review identified that controller execution and all image pulls
shared one managed identity with registry-wide `AcrPull`. The updated candidate
separates controller execution, server image pull, and session image pull;
conditions Reader roles to exact repositories; enables ACR ABAC only after
those roles are in place; waits five minutes for role propagation when the
grants change; and restricts the controller's image-pull identity to the
image-pull lifecycle in the same ARM create/update request. The legacy app
`AcrPull` grant remains tracked during and after migration; it becomes
ineffective when ACR switches to ABAC. Terraform assertions cover these
boundaries. Azure
documents that legacy ACR roles are ignored in ABAC mode, so README now covers
the migration and customer operator role requirements.

The first fresh autoreview caught two upgrade/security gaps. The existing
`AcrPull` assignment is now retained while repository Reader grants propagate
and ACR switches to ABAC; it becomes ineffective in ABAC mode. The OpenSandbox
controller now uses an AzAPI create/update request that includes both the
server-pull identity and its `None` lifecycle setting atomically, avoiding a
window where the controller could request that identity's token. Terraform
tests assert both migration sequencing inputs and lifecycle identity pairing.

The next review found two lifecycle gaps. ACI now receives a non-secret
bootstrap fingerprint so server/runtime digest or pool-capacity changes replace
the group and rerun verification. Allocation and retirement share a per-session
lock, and activation requires an unexpired lease and an `allocating` row; an
allocation that exceeds its requested lifetime is retired instead of returned.
Recovery also retires sessions whose persisted image differs from the configured
digest. Stream read timeouts now follow the remaining session lease, while
connect/write/pool timeouts remain bounded at 45 seconds.
The PostgreSQL active-session fixture now includes its pinned image URI so the
hosted recovery test exercises the intended same-image revalidation path.

Ruff lint and format, Python bytecode compilation, 38 adapter tests, 19 runtime
tests, and 29 Terraform tests pass locally. The adapter tests use the pinned
OpenSandbox 1.1.1 package in an isolated local Python environment. The real
server build/startup, PostgreSQL integration tests, and direct container test
require hosted execution because Docker Desktop's local image store returns
I/O errors, including when listing images. Hosted run 42651517438 passed
PostgreSQL recovery, server startup/authentication, and both image-scan gates
(47 High and zero Critical findings per image). Its direct runtime test failed
because the internal-network container did not expose a host port. The revised
smoke test connects directly to the container's address on the internal bridge,
retaining the no-egress network without publishing a host port; that retest is
pending. AppArmor, seccomp, and SYS_ADMIN overrides apply only to the synthetic
CI container.

Autoreview found and prompted fixes for the missing read-only `/usr/local`
mount, command-stream timeout being shorter than valid command durations, and
the hosted test's early port inspection. The command worker now mounts
`/usr/local` read-only; the gateway derives its stream timeout from the command
deadline up to the one-hour session cap; and the smoke test waits for port
publication. The current changes still require a fresh autoreview and GitHub
connector review.

The customer image helper was adjusted so it builds OCI layouts, scans those
exact layouts before publication, then copies the same digest and attestations
to ACR. Build-path resolution no longer depends on the caller's working
directory. The local OCI-layout export, Trivy scan, and Skopeo copy of the
generated image manifests completed successfully.

The tests do not constitute a new live Azure installation or deployment test.

## Image scan

The latest hosted server image scan (run 42622842799) reported zero Critical
and 47 High findings. The next hosted run will scan both images before the
runtime smoke test. The workflow prints High/Critical findings and fails on any
Critical finding. The High findings are not waived. Image-security acceptance
remains open until both final reports are reviewed and a security owner records
disposition for the findings.

No Azure images were published, Terraform was not applied to a subscription, and
the previous Azure setup was not recreated during this validation.
