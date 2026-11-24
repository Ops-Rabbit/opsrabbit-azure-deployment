# AKS implementation verification

Validated locally on 20 November 2026 with Terraform 1.15.9, AzureRM 5.2.0,
Helm 4.3.0 and Python 3.14.

| Check | Result |
|---|---|
| Terraform formatting | Passed across the repository |
| AKS Terraform validation | Passed |
| AKS mocked plans | 6 passed |
| Existing ACI/ACA validation and mocked plans | Passed; 26 tests passed |
| TFLint | Passed recursively across both Terraform roots |
| Helm strict lint with example values | Passed |
| Rendered Helm contract and secret helper tests | 13 passed |
| Shell syntax and Git whitespace checks | Passed |

The existing root's validation/tests ran in an isolated temporary copy without
its local `backend.tf`, preserving the configured customer backend. AKS provider
initialization reused the installed provider binary and the committed lock
checksums. Secret helper tests used fake Terraform/kubectl executables, verified
temporary file permissions and cleanup, and made no cloud requests.

The Helm chart structure was adapted from the supplied public GCP reference at
`bb61c6f`; runtime contracts were checked against `gaurav-exp` at
`be2fdd33854d07361a30c99646e9d0d0021d52ec`.

A user-authorized live AKS evaluation deployment was subsequently completed.
It ran published OpsRabbit v1.8.2 at source revision
`e71dd8893d7eaeeb23c1c07c407692d3f48ad0a8`. Deployment exposed an additional
`pg_trgm` allowlist requirement in that release; the Terraform configuration now
allows both extensions and preserves PostgreSQL's Azure Storage service endpoint.

Live checks passed: both nodes and all application pods ready, trusted HTTPS
health response reporting v1.8.2, HTTP redirect, browser Create First Admin page,
256 matching migration hashes, and persistence across a backend restart in all
four subdirectories. Final Terraform plan reported no changes. Deployment inputs,
credentials and operational details are kept in the ignored `aks/.local/`
directory rather than public source. Initial deployment left application and
provider setup to the operator. After that setup, authenticated sanity tests
used the existing model provider and a bounded test agent; no license or provider
credentials are included in source. Disk backups and certificate renewal
scheduling remain operator responsibilities.

Additional pre-PR checks verified chat cancellation/recovery, in-flight reload,
anonymous API rejection, and per-command allow/deny behavior using synthetic
commands. Browser startup exposed a missing discovery link hidden by the PVC.
A non-clobbering initializer correction and two filesystem regression tests are
locally validated and deployed with user approval as Helm revision 3. Discovery
now succeeds, but the live browser retest reaches a separate Chromium
`No usable sandbox!` failure in the restricted AKS environment. No
sandbox-disabling flags or extra privileges were introduced; browser automation
remains an acceptance blocker.
Trivy reports the intentional writable backend root filesystem as HIGH and the
existing root installer's public storage policy as CRITICAL. These findings
remain unsuppressed and require review; they are not clean security gates.
