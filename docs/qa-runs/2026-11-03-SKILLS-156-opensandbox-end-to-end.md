# SKILLS-156: OpenSandbox command/file execution on Azure

## Result

The bounded Azure adapter proof passed **33 live acceptance checks** through the real OpenSandbox service and runtime. OpenSandbox runs on Azure Container Apps and allocates custom-container Dynamic Sessions. No AKS cluster or separate VM is used.

This completes the command/file feasibility proof. Customer installer/bootstrap integration, durable recovery, full product-image acceptance and production security approval remain outstanding. SKILLS-156 stays In Progress.

## Observations

Main API execution: 2026-11-03T06:26:38.771806+00:00 to 2026-11-03T06:27:04.831567+00:00.

| Check | Status | Evidence |
| --- | --- | --- |
| API key required | PASS | HTTP 401 |
| First session allocation | PASS | HTTP 202; 3.73 seconds after pool readiness. |
| First session endpoint | PASS | HTTP 200 |
| stdout stderr nonzero exit | PASS | Expected stdout/stderr and exit code 7 were preserved. |
| Warm command | PASS | Expected stdout and terminal execution_complete event. |
| File upload | PASS | Multipart upload accepted by upstream execd. |
| File download | PASS | Downloaded bytes matched the uploaded fixture. |
| Command reads uploaded file | PASS | Command read uploaded bytes and created a second file. |
| File API reads command output | PASS | Downloaded the file written by the command. |
| Restricted filesystem and identity environment | PASS | Host identity/proc paths absent, /usr write denied, Azure identity environment absent. |
| File API cannot read host identity file | PASS | HTTP 404 |
| Second session allocation | PASS | HTTP 202; 1.43 seconds after pool readiness. |
| Second session endpoint | PASS | HTTP 200 |
| Cross-session file isolation | PASS | The second session could not read the first session fixture (404). |
| Command deadline | PASS | 100 ms deadline killed the long command; exit -1. |
| Background command | PASS | HTTP 200 |
| Command cancellation request | PASS | HTTP 200 |
| Command cancellation confirmed | PASS | HTTP 200 |
| Foreground stream starts | PASS | {'receivedInit': True} |
| Foreground stream cancellation | PASS | Stream started, interruption succeeded and terminated the command. |
| Outbound TCP denied | PASS | Python ran successfully; outbound TCP to the test destination was denied. |
| Session selector override rejected | PASS | HTTP 400 |
| Verified retirement A | PASS | HTTP 204 |
| Retired execution denied A | PASS | HTTP 404 |
| Verified retirement B | PASS | HTTP 204 |
| Retired execution denied B | PASS | HTTP 404 |

Expiry and credential execution: 2026-11-03T06:27:13.332935+00:00 to 2026-11-03T06:28:23.777647+00:00.

| Check | Status | Evidence |
| --- | --- | --- |
| 60-second lease created | PASS | HTTP 202 |
| Missing runtime token | PASS | HTTP 401 |
| Incorrect runtime token | PASS | HTTP 401 |
| Runtime rebind blocked at public proxy | PASS | HTTP 400 |
| Expired lifecycle read rejected | PASS | HTTP 404 |
| Expired execution rejected | PASS | HTTP 404 |
| Physical Azure retirement after expiry | PASS | Independent Azure getSession returned SessionWithIdentifierNotFound. |

Additional gateway observation at 2026-11-03T06:14:03.023328+00:00: Azure stripped the Entra Authorization header before forwarding to the runtime. The diagnostic returned booleans only, never credential values, and its session was retired. This was a separate observation, not part of the 33-check run.

## Code and artifacts

Implementation commit: `95b1b11` (with later documentation-only updates).

- OpenSandbox server base: `sha256:bcd5f264101262cebfa7e1ff9cd0424403e42f31a6596e4364cf90d8cb5c936b`.
- Upstream execd: `sha256:cda2bc35409bbf57561599b316cafb899598f7dce671332e7ceda14eb1cfeded`; binary reports commit `b7d7697750b45be2ccc876da645c8880a0cf69fc`.
- Deployed Azure server: `sha256:9e1767502b170288fc07addb218792cbc6684a56b3faca377ee3bed3773999d5`.
- Deployed runtime: `sha256:1cbde69379e5ec876df8d6919ec8416f7a65e7552b56a9fe1845acf5015dbe99`.

The server module and runtime launcher's image hashes were compared with committed source and matched. Real upstream startup/authentication, 16 adapter tests, Ruff checks, Terraform validation, all 26 mocked Terraform tests and TFLint passed. Both GitHub CI jobs passed on the implementation commit.

Earlier failed runs are retained in the private evidence bundle. A multipart metadata filename in the test harness and the workload environment applied during runtime initialization were corrected, then the complete suite was rerun successfully. Failed-run sessions were explicitly retired.

## Security and remaining limits

Final scans report 0 Critical / 65 High package findings for the runtime, and 0 Critical / 45 High for the server. These are package findings, not unique CVE counts. High base-image/upstream findings remain; some execd Go fixes require rebuilding the upstream binary. Execution success is not production security approval.

The proof uses a fixed image, denied egress, one trusted controller/API key, one controller replica, and bounded 60–300 second sessions. It does not prove OpsRabbit tenant authorization, browser tooling, private-only networking, arbitrary images or full product tool compatibility. Controller state is ephemeral; replacement fails unknown IDs closed, but physical cleanup may rely on Azure idle expiry. Durable recovery remains required before release.

Azure rejected zero ready instances for this pool configuration. One ready instance is retained. The measured allocations occurred after pool readiness; uncached image-pull/scale-to-zero cold-start acceptance is not claimed.

## Retention and walkthrough

The Container Apps environment, controller, session pool, two scoped identities and image registry remain deployed for the requested walkthrough. Only disposable test sessions were retired. One ready runtime instance and one controller replica are retained; compute/storage can incur charges. No teardown is scheduled by this work.

Reproduction instructions are in `spikes/dynamic-sessions/README.md`. Actual resource names, endpoint, private-key location, replay helper and raw timestamped evidence are kept in the private local walkthrough bundle, outside this public repository. No credentials or customer data are included here.

Draft review PR: https://github.com/Ops-Rabbit/opsrabbit-azure-deployment/pull/20

Jira: https://opsrabbit.atlassian.net/browse/SKILLS-156

Connector review was requested. No completed review had been returned at handoff; the PR remains draft and unmerged.
