# SKILLS-156: OpsRabbit chat acceptance on Azure

## Scope and completion

Deploy an isolated OpsRabbit application for the retained Azure proof, configure its existing cloud-neutral OpenSandbox settings, and verify real chat command and attachment/file execution. Keep all application and sandbox infrastructure running for inspection. This does not claim completion of the customer installer or production security acceptance.

## Approach

Use the existing Terraform ACA application path with separate names and private local state. Import the published matching backend/web release by immutable digest. Preserve the existing proof controller/pool. Extend the adapter only where it can genuinely enforce OpsRabbit's requested contract: fixed image/resources, denied network egress, isolated execd credentials, bounded leases, authenticated proxy and confirmed retirement. Reject unsupported semantics rather than dropping them. Maintain workspace-only isolation and leave browser tools unsupported.

Use the existing application configuration APIs and encrypted secret store. Do not add a cloud-specific product provider. User performs first-admin enrollment, license installation and approved model-provider setup when needed. Test only synthetic content in a dedicated chat/agent.

## Access and data lifecycle

Sandbox settings remain deployment-admin control plane; existing product tenant/session authorization remains authoritative. Each runtime is a child of its sandbox/session and must not receive controller identity, provider credentials, or application secrets. Only trusted lifecycle requests may initialize it. Local credentials and Terraform state stay in a mode-0700 directory outside Git. Reports contain synthetic evidence and artifact identifiers only. Application records and uploaded test files remain for inspection; disposable sandbox sessions expire or are retired. Retained database/storage/backups may contain test records and incur charges.

## Validation

Run adapter contract and startup tests serially, including invalid settings and credentials. Validate deployed image provenance and application health. Use the product verification path before activation where supported. Verify chat tool-call evidence, stdout, actual uploaded bytes, command-generated download bytes, session isolation and retirement. Record failures and required user intervention without marking partial results PASS.

## Limits carried forward

The installer now provisions the Azure Dynamic Sessions pool and OpenSandbox
controller, stores the adapter journal in a restricted PostgreSQL schema, and
uses the product's versioned verify-before-enable bootstrap contract. Recovery
tests cover interrupted allocation retirement and active-session revalidation
against Azure. The controller remains single-replica; multi-replica coordination
and browser execution are unsupported. Current local image scans show zero
Critical and 47 High findings per image with no fixed versions. Security
acceptance is outstanding and must not be inferred from the Critical-only build
gate. No new cloud deployment was performed during this code-validation pass.
