# SKILLS-156: mandatory OpenSandbox on Azure Dynamic Sessions

## Scope

Prove the real OpenSandbox server/API can use custom-container Dynamic Sessions. No AKS, VM, native Azure-only product replacement or weakened workspace-only isolation. Installer integration follows only after proof. New installations require sandbox verification; existing administrator settings must not be silently overwritten.

## Source findings

Azure installer main: a4328af8c3cf00c8b60ad92ccb3d0bccfd516eea. ACI and ACA supported; AKS explicitly rejected. OpenSandbox release-1.1.1 resolves to 531700ca18a7fa5fab6a2f25d6dc68a76eddab12. Its runtime config and factory accept only Docker and Kubernetes, so an Azure service adapter and endpoint/auth translation are required. Dynamic Sessions routes an opaque identifier query parameter and requires a trusted Entra bearer token; tokens must not reach untrusted execution code.

## Proof gates

1. Provision an isolated low-concurrency custom session pool and image without touching existing workloads.
2. Prove the actual runtime image can enforce the workspace-only filesystem policy.
3. Implement a bounded OpenSandbox SandboxService adapter, preserving upstream API authentication and execution protocols; fail explicitly for unsupported operations.
4. Prove create, command output/exit codes, file round-trip, cold/warm paths, cancellation, cross-session isolation and confirmed deletion through the OpenSandbox API/client.
5. Record actual results and blockers. Keep the test setup running for the requested walkthrough; do not tear down without a subsequent user instruction. Preserve code/evidence. No success claim for a direct Azure-only smoke test.

## Security and lifecycle

Separate controller and image-pull identities. No cloud credentials in session environment or model-visible output. Deny egress initially; no customer data or model calls. Use ephemeral synthetic files, bounded lifetimes/concurrency, scoped RBAC, and exact-resource cleanup. Confirm retirement without allocating the same session again. Pool cooldown is not equivalent to a fixed OpenSandbox TTL; expired identifiers must fail closed instead of silently allocating replacement sessions. Product-path proof must preserve caller tenant/session authorization.

## Validation

Local adapter/protocol tests, lint/security review, and live Azure observations with exact source/image references. Live deployment uses a new task-owned resource group and does not provision application database/storage. Full customer installer tests and licensed UI validation remain subsequent work.

## Adapter gaps identified before live testing

| OpenSandbox contract | Azure mapping / required work |
| --- | --- |
| Runtime selection | Extend config validation and factory with a genuine SandboxService implementation; upstream accepts only Docker/Kubernetes. |
| Image per create request | Session pool fixes the image; require an approved image-to-pool mapping and reject other images. |
| Execution endpoint | Existing server proxy builds HTTP endpoints; Azure pool requires HTTPS and an identifier query parameter on each request. Add a trusted transport mapping, not a token exposed in client endpoint metadata. |
| API credentials | Control-plane managed identity acquires Entra tokens; session containers get neither executor identity nor lifecycle API keys. |
| Session identity | Server-generated opaque IDs, owner mapping and fail-closed tombstones; Azure can otherwise allocate a new container for a retired identifier. |
| TTL/renewal | Pool inactivity timeout differs from absolute requested expiry; enforce the requested deadline and do not keep it alive through health polling. |
| Deletion receipt | Verify stop/status semantics independently; HTTP 200 from stop alone must not be misrepresented as confirmed workload deletion. |
| Filesystem | Live test Bubblewrap user namespace/mount capabilities before carrying over the workspace-only command/file implementation. |
| Network policy | Azure pool EgressDisabled is a starting point, not proof that arbitrary per-sandbox OpenSandbox egress rules are supported. Reject unsupported policies. |

Microsoft reference sample reviewed at a76d4eb3dab30f9d02dcff6aa4063ba87343d9d5. Its shared identity/resource-access pattern is not copied: the probe identity is image-pull-only and unavailable during session execution. No Azure OpenAI/model resources are needed for this proof.

## Walkthrough retention

The user explicitly requested on 2026-11-03 that the setup remain running after testing. Do not delete the environment, registry, identity or session pool. Individual sessions are ephemeral under the configured 300-second idle cooldown and can be recreated for the walkthrough. Retained infrastructure and one warm session can incur charges.
