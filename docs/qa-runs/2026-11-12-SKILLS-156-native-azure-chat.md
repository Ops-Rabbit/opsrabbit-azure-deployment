# SKILLS-156 Azure installer and chat acceptance — 12 November 2026

## Scope

Synthetic-data QA on Azure Container Apps with custom-container Dynamic Sessions.
No AKS cluster or VM. The customer README bootstrap/application sequence was
used; OpenSandbox verification ran before application startup. These observations
describe the deployment before the user's later pause and completed teardown.
The earlier walkthrough-retention instruction is superseded.

## Last deployed provenance — historical

| Component | Source | Immutable digest |
| --- | --- | --- |
| OpenSandbox controller | `fab51a342e62077f8c1a1955a48846173a16af4b` | `sha256:8a109ce76098e9963b1f6064be6d4d2f414544cff0f1f8974a1b04edbf91b9ec` |
| Workspace runtime | `42684578f9974ae9972995d46b4b012fde95a2df` | `sha256:6a303da47ec5bedf75494193e690c0caefad92bb3f462fc8e224863b444db93b` |
| QA backend | code `d4271677f`; tested PR head `00bcb7c0a` adds documentation/tests | `sha256:2cdb6881662794996c9132447843431b1373c5ae6eabedab25fd5f59ae2e3fa4` |
| QA web | `c5df56cae` | `sha256:124b06bd3423c139387b49dbdf36fc2a18ad42fdaf8c8729635c814b493f68c5` |

Controller and runtime OCI images passed High/Critical scans before publication;
registry digests and provenance attestations were verified. Temporary publisher
roles and authentication files were removed. The runtime identity can pull only
from the separate runtime-only registry; its identity is not available to code
executed inside a session.

The controller update applied at **2026-11-12T15:46:28.598904+00:00**.
It changed the controller digest and derived application bootstrap revision,
without changing the backend/runtime images, stored credentials or session pool.
The new application's bootstrap container exited 0. A changing operator public
IP required replacing the existing single-IP database firewall rule before
regenerating the full plan; no broader address range was allowed.

## Historical validation

The exact controller/runtime above passed **32 live API checks** from
2026-11-12T15:47:51Z to 15:50:02Z, including disconnected parent/child
cancellation, command/file execution, filesystem isolation and denied egress,
followed by **9 controller-restart checks**. Azure replaced the controller replica; the same sandbox retained its file bytes
and executed another command. All allocations owned by these probes were
confirmed retired, with later execution returning 404. HTTPS HTML and health
returned 200 with `ok=true` and valid TLS on the new application revision.

The actual Chrome chat attempt **failed before command execution**: the product
requested a lease exceeding the controller's 3600-second maximum. Bootstrap had
verified a one-hour policy but saved the default 24-hour idle policy. This proves
that bootstrap/API success alone did not qualify the chat path. A second review
found that renewing a full hour on every tool call would also exceed the absolute
lifetime and replace the sandbox, losing temporary files between tools.

A generic bootstrap lifecycle policy and conservative valid-lease reuse are being
implemented and must pass tests/review and final-image chat acceptance. The QA
administrator configuration was explicitly reconciled to idle one hour/max-age
one day, preserving its saved key, network denial and workspace-only isolation.
That correction is diagnostic; it is not evidence of automatic fresh installation.
Generated chat-file download and anonymous access denial remain unverified.

- Product [CI 43230843349](https://github.com/applied-ai-consulting/gaurav-exp/actions/runs/43230843349): passed all seven jobs for `00bcb7c0a`.
- Installer [Terraform CI](https://github.com/Ops-Rabbit/opsrabbit-azure-deployment/actions/runs/43248788114) and [sandbox CI](https://github.com/Ops-Rabbit/opsrabbit-azure-deployment/actions/runs/43248788868): passed `fab51a3`.
- Controller contract tests: 70 passed; real PostgreSQL recovery suite: 10 passed.
- At that observation, the product connector review was clean for the committed
  candidate and the installer connector review had not returned. Neither review
  status nor the historical CI results qualify the current uncommitted changes.

## Disconnect fix and review

The earlier image allowed a disconnected foreground command and its detached
child to make delayed writes. That execution failed acceptance and its test
allocations were retired. The fix watches the downstream connection, preserves
the execution ID from the trusted event stream and sends a bounded exact-command
cancellation only after allocation ownership/generation validation. It does not
retire a whole session or cancel background commands.

The historical disconnect-fix review found no remaining P0/P1 issues in that scope. Autoreview's queued-success
finding was checked against the pinned upstream runtime: it marks a command
finished before emitting successful completion, and a subsequent exact-ID
interrupt rejects the finished command without signalling it. An unobserved
completion can still cause a redundant DELETE/error; only observed terminal
events suppress cancellation. The upstream running/PID snapshot-to-signal
operation is not atomic. This existing upstream limitation remains documented;
this patch does not claim to eliminate every cancellation race.

## Release boundaries

- The backend has **15 Critical and 378 High findings (142 unique advisories)**.
  The user approved only the exact listed digest for synthetic-data QA. This is
  not customer-release image-security approval.
- Browser automation is unsupported. This runtime qualifies command/file
  execution; arbitrary glibc-only binaries are not qualified.
- The controller is single-replica with PostgreSQL recovery. This is not a
  multi-controller or private-only network acceptance result.
- Product PR #1037 stays open for user-owned merging. Installer review approval
  is separate from CI, image scans and this QA deployment.

## Completed teardown and resumed local work

The private teardown resume record reports inventory/state verification completed
at **2026-11-12T17:15:43.394312+00:00**. Only the two image registries remained in
the application resource group; separate Terraform-state storage was retained.
The application, controller and session pool were torn down. No fresh live
inventory check was performed for this documentation update.

Terraform state retains registries and random credentials. Private PostgreSQL,
Azure Files, configuration and state backups were retained; archive-index and
checksum checks passed, but a full restore has not been rehearsed. Restoration
must be planned before any separately authorized deployment, using reviewed
installer source rather than the teardown-only configuration.

At the pause, the installer lifecycle edits had passed 32 Terraform tests,
70 controller tests, validation and TFLint; product lifecycle/reuse edits were
still untested. Both worktrees and private patch/untracked archives were retained.
Those results do not cover subsequent pool-identity regression cases or other
resumed edits. The current task authorizes local fixes/tests/reviews only;
the coordinating task schedules verification. No build, deployment, merge or
fresh cloud check was performed in this installer editing pass.

The historical images and CI are not the new candidate. Final source review,
scheduled tests and any later image qualification remain required. The previous
backend security exception is limited to the exact historical synthetic-QA
digest, and does not transfer to a new build. Current chat/file acceptance is
still outstanding.

## Evidence

Historical deployment evidence is under
`~/.codex/skills156-evidence/deploy-2026-11-12/`. The teardown and pause status
above comes from `~/.codex/skills156-evidence/teardown-2026-11-12/RESUME.md`.
It includes exact CI observations, immutable image metadata/scans/publication,
review disposition and Terraform apply results. Reports omit credentials,
cookies, tokens, license contents, provider secrets and customer data.
