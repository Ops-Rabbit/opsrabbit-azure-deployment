# SKILLS-156 local readiness — 20 November 2026

This execution covers source fixes, local tests and PR review. No Azure
installation was deployed, and neither PR was merged. Historical Azure results
are not acceptance evidence for this candidate.

## Corrections

- Bootstrap verifies and saves the same one-hour idle/one-day maximum-age
  policy. Both ACI and ACA bootstrap fingerprints include the session-pool
  identity, so replacing a pool reruns verification.
- The product reuses a valid sandbox lease across upload, command and read,
  retains cleanup ownership during uncertain expiry, and explains incompatible
  saved settings before probing. Existing administrator settings stay intact.
- The runtime passes its command envelope through an unlinked private descriptor
  instead of one large command-line argument. Stdin is preserved, and descriptors
  close on successful launch, failure and cancellation. Individual final arguments
  and environment entries still obey Linux execution limits.
- Active commands and file transfers observe committed lease changes. A separate
  expiry watchdog does not wait for journal locks or queries. Output waits at
  most 0.5 seconds for fresh confirmation; quiet streams fail closed after one
  second without confirmation or at expiry, whichever comes first. One refresher
  serves the controller. A stuck driver can retain that single daemon/connection
  until it returns or the process exits; it cannot delay expiry or create an
  accumulating set of blocked refresh workers.

## Local results

| Check | Result | Evidence/limit |
| --- | --- | --- |
| Terraform mocked plans | PASS | 34 tests; validation, formatting and TFLint also pass |
| Controller contract | PASS | 80 tests, including real loopback HTTP renewal, cancellation, blocked journal/lock and expiry |
| PostgreSQL controller recovery | PASS | 10 tests against a disposable database |
| Runtime Python tests | PASS | 46 tests |
| Runtime Go tests and builds | PASS | Both AMD64 and ARM64 canonical Dockerfile builds pass the one-shot initialization and workspace-command suites |
| Native container execution | PASS | Command/file contract, aggregate envelope above 128 KiB, generated-file download, isolation and cleanup |
| Local AMD64 container execution | BLOCKED | This ARM Mac's emulation rejects the seccomp filter; isolation was not disabled. Hosted native AMD64 testing remains required |
| AMD64 runtime image scan | PASS | Zero High/Critical findings in the newly built local image |
| Python lint/format | PASS | CI-pinned Ruff 0.14.8 |
| Product focused tests | PASS | 440 tests; changed-file coverage 95.39–100% |
| Product PostgreSQL tests | PASS | 45 initializer/workspace-access tests |
| Product type/lint/security | PASS | Full strict pinned Semgrep: 526 rules, 4,338 targets, zero findings/errors; existing default skips unchanged |
| Product connector review | PASS | Current product commit `c84f6d3b7`; no major issues reported |
| New hosted checks | PENDING | Product paid CI needs exact-run approval; installer checks run after push |
| Actual Azure chat and restore | NOT RUN | Requires separately authorized deployment and live acceptance |

The renewal tests fail against the old controller: downloads stop early, command
streams lose their completion event, and a quiet HTTP body retains the obsolete
read timeout. The corrected controller passes all three assertions. Product
regressions likewise reproduce the prior lifetime/reuse and saved-limit failures.

## Review disposition

Independent review findings are addressed. Structured AutoReview completed the
full installer scope and returned one P1 claiming `closeCommandExtraFiles` was
undefined. That claim is rejected: the pinned upstream implementation defines
it in `components/execd/pkg/runtime/isolated_session.go`, and both canonical
image builds compile and execute the affected Go tests successfully. The review
artifact remains recorded as findings with this adjudication, not as a clean
reviewer result. An earlier provider-registration claim was also rejected:
`opensandbox_enabled` is exactly `application_enabled`, which already controls
Microsoft.App registration. Earlier index-only findings were corrected before
this final staged review.

Current-head connector review and hosted installer image/test gates remain
required. No previous backend image exception transfers to a new image. Local
runtime checks are not an OpsRabbit chat pass or customer-release approval.
