# SKILLS-156 installer, recovery, and image scan checks

Observed 2026-11-09. These are local code and container tests only. The Azure
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
| PostgreSQL restart recovery, schema permissions, and lost-leader recovery | Five integration cases pending hosted CI; local Postgres container could not start because Docker image storage returns I/O errors |
| PostgreSQL schema cascade teardown regression | Added; pending hosted CI because local Docker image storage returns I/O errors |
| Overlapping-controller recovery fence | Added PostgreSQL advisory-lock test; pending hosted CI because local Docker image storage returns I/O errors |
| Adapter contract, concurrency, route allowlist, and SQLite recovery tests | PASS, 37/37 |
| Real OpenSandbox server startup/authentication | PASS in hosted CI run 42622842799 |
| Runtime gateway, file-path confinement, minimal mount view, and worker launch tests | PASS, 19/19 |
| Direct runtime command isolation and generated-file download | First hosted attempt exposed the runner's user-namespace restriction; test-only seccomp override added, rerun pending |
| Server image scan | PASS gate in hosted run 42622842799: 0 Critical, 47 High |
| Runtime image scan | Pending: CI stopped at direct runtime test before reaching this scan |
| Image-security acceptance | Open pending runtime scan and security-owner disposition of server's 47 High findings |
| Focused ACA Terraform topology tests | PASS, 10/10 (previous run) |
| Terraform formatting and validation | PASS |
| Full Terraform test suite | PASS, 27/27 (including private ACA DNS and private ACI bootstrap topology) |
| Product OpenSandbox targeted tests | PASS, 143/143 |
| Product changed backend line coverage | initializer 100%; verifier 100%; execution 85.62% |
| Product TypeScript check and quality lint | PASS |
| Strict Semgrep scan of changed product source | PASS, 0 findings |
| Strict Semgrep scan of updated installer runtime source | Not rerun; Semgrep is unavailable in this shell |
| Ruff 0.14.8 lint and format for all spike code | PASS |
| Python bytecode compilation and image-build shell syntax | PASS |
| Direct runtime container command/file test | Pending hosted CI; Docker image store returns an I/O error locally |
| Independent code reviews and autoreview | Autoreview clean for P0–P2 at 2026-11-09 13:23 UTC; GitHub connector review of the latest pushed candidate pending |

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
prevent overlapping ACA revisions from reconciling the same journal. Startup
recovery validates active rows before public readiness is enabled. It monitors
that lock connection
and exits on database connection loss so ACA can restart it and replay durable
recovery. The installer probes backend image capabilities before migration, and
both ACA apps have revision markers for generated credential rotations. The
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

Ruff lint and format, Python bytecode compilation, 37 adapter tests, 19 runtime
tests, and 27 Terraform tests pass. The real server build/startup, PostgreSQL
tests, and direct container test are in hosted CI because Docker Desktop's local
image store returns I/O errors, including when listing images. Hosted run
42622842799 passed adapter, PostgreSQL recovery, server startup, and server
image scanning, then failed the direct runtime test because the GitHub runner's
outer seccomp policy denied Bubblewrap's user-namespace syscall. The smoke test
now uses a test-only seccomp override and reports container exit state; no
deployment security setting changed. The runtime image scan was not reached.

Autoreview found and prompted fixes for the missing read-only `/usr/local`
mount, command-stream timeout being shorter than valid command durations, and
the hosted test's early port inspection. The command worker now mounts
`/usr/local` read-only; the gateway derives its stream timeout from the command
deadline up to the one-hour session cap; and the smoke test waits for port
publication. Latest follow-up autoreview completed clean for P0–P2 at
2026-11-09 13:23 UTC. GitHub connector review of the latest pushed candidate is
pending.

The customer image helper was adjusted so it builds OCI layouts, scans those
exact layouts before publication, then copies the same digest and attestations
to ACR. Build-path resolution no longer depends on the caller's working
directory. The local OCI-layout export, Trivy scan, and Skopeo copy of the
generated image manifests completed successfully.

The tests do not constitute a new live Azure installation or deployment test.

## Image scan

The latest hosted server image scan (run 42622842799) reported zero Critical
and 47 High findings. The run failed before scanning the runtime image because
the runner blocked Bubblewrap's user namespace; that test-only runner setting
has now been adjusted, and the next run will scan both images. The hosted proof
workflow prints High/Critical findings and fails on any Critical finding. The
High findings are not waived. Image-security acceptance remains open pending
the runtime scan and security-owner disposition of the 47 High findings.

No Azure images were published, Terraform was not applied to a subscription, and
the previous Azure setup was not recreated during this validation.
