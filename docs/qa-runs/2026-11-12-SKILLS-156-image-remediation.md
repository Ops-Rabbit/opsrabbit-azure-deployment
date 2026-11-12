# SKILLS-156 Azure image remediation — 12 November 2026

## Result

Both rebuilt images have zero High and zero Critical findings, with no
vulnerability exclusions. This supersedes the image-security baseline in the
9 November report. It does not claim zero findings at other severities or
prove a new live Azure installation.

The fresh Debian baseline in run [43160507719](https://github.com/Ops-Rabbit/opsrabbit-azure-deployment/actions/runs/43160507719)
still reported 47 High package findings (11 unique CVEs) per image, with no
fixed package versions listed for that distribution. Both Dockerfiles already
ran package upgrades. The final layers now use digest-pinned Python on Alpine
3.24, with its package updates applied. The server retains OpenSandbox 1.1.1,
installs the checksum-pinned upstream wheel and hash-locked transitive Python
dependencies, and retains the Azure adapter and startup entrypoint.

Alpine's [release policy](https://alpinelinux.org/releases/) currently supports
both main and community in 3.24. Track the community support window when
refreshing the runtime's Bubblewrap package.

The runtime retains workspace-only execution, non-root users and the static
execd binary. Copies of Alpine's binaries and libraries support the existing
restricted `/usr` view without deleting the original package files or metadata.
CI now retains JSON scan artifacts. CI and the customer publication helper
reject any High or Critical finding, including findings with no vendor fix.

## Verified implementation

Commit: `67211bf43cc90a94c7456c1ddea368b8ef2f112e`.
Hosted run: [43162771320](https://github.com/Ops-Rabbit/opsrabbit-azure-deployment/actions/runs/43162771320).

| Check | Result |
| --- | --- |
| Server image build and upstream patch compatibility | PASS |
| Adapter contracts | PASS, 38 tests |
| PostgreSQL recovery, permissions, leadership and concurrency | PASS |
| Actual server startup, anonymous rejection and authenticated list | PASS |
| Runtime worker unit tests | PASS, 19 tests |
| Go one-shot initialization regressions and runtime build | PASS |
| Direct restricted command and generated-file download | PASS |
| Bash arrays, Python CSV/JSON/SSL/SQLite imports and GNU file hashing inside the restricted view | PASS |
| Server High/Critical scan | PASS, 0 High / 0 Critical |
| Runtime High/Critical scan | PASS, 0 High / 0 Critical |
| Terraform CI | PASS, run 43162771283 |
| Independent review of the remediation changes with full startup context | PASS, no actionable P0/P1 findings |

## Scan provenance

The workflow artifact is `azure-image-security-67211bf43cc90a94c7456c1ddea368b8ef2f112e`.
These are Docker image configuration IDs from the tested local builds, not
published registry manifest digests. The customer publication helper must scan
its exact OCI layouts again before pushing them to ACR.

| Image | Scan observation (UTC) | Image configuration ID |
| --- | --- | --- |
| Runtime | 2026-11-12T05:41:47.460299587Z | `sha256:7146c0f34706a48bc3065f7e852dd9966a472c79229b10ea8c882fcc9b0573e7` |
| Server | 2026-11-12T05:41:54.81010641Z | `sha256:ae202976d44b27bc14a310535d091591ddbfb637307c7be2352d6a232e117538` |

JSON report SHA-256:

- `runtime-vulnerabilities.json`: `c5f0f57bed329f8450e9126515ec936e97441af79432c46ffac11ae113410613`
- `server-vulnerabilities.json`: `c101ebf19897727fb278ee9425d6134faa6598dff2817f7a4d367ed5fb4e1e72`

The runtime scan detected Alpine OS packages, Python packages and the Go binary;
the server scan detected Alpine and Python packages. Reports are filtered to
High/Critical severity. No finding was waived or hidden with ignore-unfixed.

## Remaining qualification

No Azure resources were provisioned, and these images were not published to an
ACR or deployed. Live customer-installer, chat and restart/revision checks must
be repeated with the final published digests. Alpine uses musl: arbitrary
glibc-only executables and browser automation remain unqualified. GitHub's
required approval and the connector review are separate from these test results.
