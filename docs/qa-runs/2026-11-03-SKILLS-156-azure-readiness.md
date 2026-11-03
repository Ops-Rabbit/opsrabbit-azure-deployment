# SKILLS-156 Azure Dynamic Sessions readiness test
> Historical readiness snapshot. The later [OpenSandbox end-to-end report](2026-11-03-SKILLS-156-opensandbox-end-to-end.md) records the completed adapter proof.

## Outcome

Azure is ready for the next OpenSandbox integration test. The custom-container session executed a fixed Bubblewrap isolation probe successfully. This is **not yet an end-to-end OpenSandbox or OpsRabbit acceptance pass**.

| Check | Result | Evidence |
| --- | --- | --- |
| Container Apps environment and session pool | PASS | Both report Succeeded; image pull failures and crashes were zero in the checked active template. |
| Authenticated session allocation and health | PASS | HTTP 200; health response ok=true. Observed 2026-11-03T05:33:10.470488+00:00. |
| Basic user/PID/mount namespace feasibility | PASS | Probe exitCode=0, stdout=isolation-ok; /proc/self/environ and /etc/passwd absent in the restricted command view. This is not the full product filesystem test suite. |
| Anonymous access | PASS | HTTP 403 Unauthenticated. Observed 2026-11-03T05:36:19.062729+00:00. |
| Session metadata and list | PASS | Authenticated getSession/listSessions returned HTTP 200 before retirement. |
| Explicit test-session retirement | PASS | stopSession returned 200, getSession returned SessionWithIdentifierNotFound (HTTP 400), and listSessions returned count=0. Observed 2026-11-03T05:36:34.544276+00:00. |
| No AKS or VM in the proof resource group | PASS | Inventory contains an ACA environment, ACR, image-pull identity and session pool. |
| Image-pull identity restricted to initialization | VERIFIED CONFIGURATION | Pool identity lifecycle=None; no runtime identity exposure tested beyond configuration inspection. |
| Egress restriction | VERIFIED CONFIGURATION | Pool uses EgressDisabled; a live network-denial test is still outstanding. |
| Real OpenSandbox create/command/file/retire path | NOT RUN | Upstream 1.1.1 accepts Docker/Kubernetes only. A real Azure SandboxService adapter and trusted execution transport remain to be implemented and tested. |
| OpsRabbit product path, tenant isolation, full workspace policy | NOT RUN | Requires the adapter, actual runtime image, and product-level acceptance. |

## What the result means

The earlier provisioning and allocation errors cleared after Azure warmed the pool. Do not treat that as proof of a bounded cold-start time or general reliability. The successful diagnostic used a small probe image, not the large OpsRabbit sandbox image or execd runtime.

The Azure session gateway requires an Entra token and an identifier query parameter. The future OpenSandbox adapter must keep that token in trusted control-plane code, enforce sandbox ownership and absolute expiry, and prevent retired identifiers from allocating replacement sessions. Pool idle expiry alone is not equivalent to OpenSandbox lifecycle semantics.

## Setup retained for walkthrough

The environment, image registry, image-pull identity and session pool remain deployed. Only the disposable diagnostic session was stopped to verify retirement. The pool remains configured for one ready instance and at most two concurrent sessions. No AKS cluster or separate VM was created. Do not tear down the retained setup until the user requests it. Warm compute and retained registry storage can incur charges.

The private local evidence bundle contains observed timestamps, inventory, sanitized HTTP results and the local probe runner; no access tokens are stored. The probe runner uses the local Azure CLI session.

## Walkthrough order

1. Inspect the environment, pool image digest, warm capacity, denied egress configuration and identity lifecycle.
2. Allocate a fresh synthetic session and run /health and the fixed /probe diagnostic through Azure's authenticated gateway.
3. Show the unauthenticated 403 result and the recorded retirement receipt.
4. Explain the remaining adapter boundary before presenting any claim of complete OpenSandbox integration.
