# OpsRabbit Azure Terraform

This package creates the Azure resources for a fresh OpsRabbit deployment. It
supports public or private networking and can create a dedicated resource group
or use an existing customer-owned group.

- Azure Container Registry, using Basic for public mode or Premium with a
  private endpoint for private mode
- separate controller and image-pull identities; a dedicated ACR for sandbox runtimes
- Azure Files shares for application data and Git workspaces, restricted to the
  selected compute subnet in private mode
- Azure Database for PostgreSQL Flexible Server 16
- the PostgreSQL `vector` extension and `pg_trgm` allowlist required by application migrations
- either one Azure Container Instances group or one Azure Container App
  containing the ready-to-run OpsRabbit backend and web images
- an Azure Container Apps custom-container Dynamic Sessions pool and a separate
  OpenSandbox controller for every application installation, including ACI
- a restricted PostgreSQL role and schema for durable OpenSandbox session
  recovery, plus a fresh API key stored as an ACA secret and encrypted in the
  OpsRabbit configuration store

OpsRabbit publishes its application images in its release registry. The two
Azure OpenSandbox adapter images are built from this repository and pushed to
the customer's ACR by the provided script.

The first Azure integration confines both direct OpenSandbox command requests
and file transfers to `/workspace`, including requests that bypass OpsRabbit.
Browser automation is not supported by this Azure runtime yet.

## Security modes

Private networking is recommended for production and customer environments. It
keeps the selected container platform, PostgreSQL, and ACR off public endpoints and requires a
customer-managed internal entry point.

Public networking remains the default for backward compatibility and controlled
evaluation. It gives the selected container platform a public address, keeps authenticated Azure Files
publicly reachable, and uses a public PostgreSQL endpoint restricted by
firewall rules. Public mode is not the recommended production security
baseline.

Both modes place credentials in Terraform state. Use encrypted remote state,
restrict state access, and follow the secret-handling requirements below.

## Tested toolchain

The package was validated on 23 August 2026 with:

| Component | Version |
|---|---:|
| Terraform CLI | `1.15.9` |
| AzureRM provider | `5.2.0` |
| PostgreSQL provider | `1.27.0` |
| AzAPI provider | `2.12.0` |
| Random provider | `3.9.0` |

The exact provider versions are pinned in `versions.tf` and checksummed in
`.terraform.lock.hcl`.

## Files

| File | Purpose |
|---|---|
| `versions.tf` | Terraform and provider version constraints |
| `providers.tf` | Azure and PostgreSQL provider configuration |
| `variables.tf` | Required customer inputs and validation |
| `main.tf` | Azure and PostgreSQL resources |
| `container-apps.tf` | Azure Container Apps resources |
| `opensandbox.tf` | Dynamic Sessions pool, separate executor and image-pull identities, controller and durable journal role |
| `spikes/dynamic-sessions/` | Pinned Azure adapter/runtime images, contract tests and customer build helper |
| `outputs.tf` | Deployment addresses and resource names |
| `terraform.tfvars.example` | Secret-free customer input example |
| `backend.tf.example` | Remote Azure state-backend example |

## Prerequisites

- Azure subscription
- permission to create resources, register the listed Azure providers, and
  create conditional ACR role assignments
- when customer-owned network resources are supplied, permission to read the
  three virtual networks and subnets, join each subnet, and attach the ACR
  private endpoint to the supplied private DNS zone
- when customer-owned private DNS zones are supplied, permission to read them
  and create the required private-endpoint zone association
- Azure CLI authenticated to the target tenant and subscription, with the
  `containerapp` extension when deploying ACA
- Terraform `1.15.x`
- approved OpsRabbit release manifest
- short-lived read access to the OpsRabbit ECR repositories, supplied securely
  by OpsRabbit
- for public mode, a stable public IPv4 address for the Terraform runner
- for private mode, a Terraform runner connected to the customer network and
  the private-network foundations described below
- encrypted, access-controlled Azure storage for Terraform state

## State and secrets

Terraform state contains:

- the PostgreSQL administrator password
- `BETTER_AUTH_SECRET`
- `OPSRABBIT_NODE_ENCRYPTION_KEY`
- the Azure Files storage account key used by the selected container platform

Configure an encrypted remote backend with tightly restricted access before the
first real apply. Do not use local state for a customer deployment. Do not commit
`backend.tf`, `terraform.tfvars`, plan files, or state files.

The Better Auth secret and OpsRabbit encryption key must remain stable across
upgrades. Replacing the encryption key can make stored application credentials
unreadable.

## 1. Configure remote state

The state resource group, storage account, and blob container must already
exist. Copy the example and enter the customer-approved values:

```bash
cp backend.tf.example backend.tf
```

## 2. Configure non-secret inputs

```bash
cp terraform.tfvars.example terraform.tfvars
```

Replace every placeholder. Use the immutable image digests from the approved
OpsRabbit release manifest.

Check PostgreSQL sizes for the selected region before the bootstrap apply:

```bash
az postgres flexible-server list-skus --location "<azure-region>" --output json
```

Set `postgresql_sku_name` to an appropriate available size. The default is
`B_Standard_B1ms`, but Azure can reject a listed size with `SkuNotAvailable`
when provisioning. If that happens, choose another available size, review its
cost, and create a new Terraform plan. Preserve resources already created by
the bootstrap apply; do not destroy the installation to retry database creation.

### Container platform

Azure Container Instances remains the default:

```hcl
deployment_target = "aci"
```

To deploy the standard non-root backend image on Azure Container Apps instead:

```hcl
deployment_target              = "aca"
dns_name_label                 = null
container_app_environment_name = "cae-opsrabbit"
container_app_name             = "ca-opsrabbit"
```

When `backend_image_repository` is omitted, Terraform selects
`opsrabbit/backend-aci` for ACI and `opsrabbit/backend` for ACA.

OpenSandbox always runs in Azure Container Apps because Dynamic Sessions is an
ACA service. Terraform also creates a single-replica controller app with
authenticated HTTPS ingress and a generated 64-character API key. In a private
ACA environment, the environment's internal load balancer keeps the controller
VNet-accessible to both private ACA and private ACI workloads without a public
endpoint. In a public environment, the controller ingress is public and
protected by the API key. The controller's execution identity receives only
`Azure ContainerApps Session Executor` on the exact pool. Separate identities
pull the OpenSandbox server and runtime images. The server identity can read
only its repository in the application ACR. The runtime identity can read only
a second ACR reserved exclusively for sandbox runtime images; it has no grant
on the application ACR. Their identity tokens are unavailable to the controller
process and sandbox workload. The session pool denies egress and exposes no
managed identity to workload containers.

The separate runtime ACR is required because the validated Dynamic Sessions
image-pull path rejected repository-conditioned Reader grants. Its Reader role
has no repository condition, so **do not store application images, customer
images, or other unrelated artifacts in this registry**. The registry itself is
the access boundary. It adds a second Basic ACR in public mode, or a second
Premium ACR and private endpoint in private mode. Set
`opensandbox_runtime_registry_name` to choose its globally unique name; when
omitted, Terraform derives a distinct name from `container_registry_name`.

Terraform enables the application ACR's RBAC plus ABAC repository mode after
it assigns the application/server identities' repository-scoped Reader roles. Legacy `AcrPull`,
`AcrPush`, and `AcrDelete` assignments stop granting data access in that mode.
For an existing customer-managed registry, migrate any external identity roles
before changing the mode. Azure's [ACR ABAC migration guidance](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-abac-repository-permissions)
describes the impact. ACR authorization changes can also require fresh
credentials; refresh cached credentials **after** the mode switch has
completed. Terraform waits five minutes after creating or changing the grants
before switching ACR authorization mode and starting image pulls, so the
assignments can propagate to the registry data plane. The legacy `AcrPull`
assignment remains managed for upgrade continuity; ACR ignores that role after
the switch, while the repository-conditioned Reader grants provide data access.

Before database migration or bootstrap, the init container checks that the
selected backend image advertises verified-install contract version 1. It stops
before changing application settings if the image is older or incompatible.

### Resource-group ownership

The default creates and manages a dedicated resource group:

```hcl
create_resource_group = true
resource_group_name   = "rg-opsrabbit-customer"
```

To place OpsRabbit in an existing customer-owned resource group:

```hcl
create_resource_group = false
resource_group_name   = "rg-existing-customer-workloads"
```

In this mode Terraform verifies that the group exists but does not take
ownership of it. Destroying the OpsRabbit stack therefore does not delete the
shared resource group or unrelated resources inside it.

Choose this setting before the first deployment. Changing an already-managed
resource group to existing-resource-group mode is not an adoption procedure.

### Public networking

Public mode is the default and preserves the original deployment topology:

```hcl
network_mode               = "public"
dns_name_label             = "<globally-unique-aci-dns-label>"
terraform_runner_public_ip = "<stable-runner-public-ipv4>"
```

ACI receives a public regional Azure hostname. PostgreSQL remains on its public
endpoint but accepts only the Azure-services rule and the single Terraform
runner address. Azure Files and ACR retain authenticated public endpoints.

### Private networking

Private mode disables public network access for the selected container platform,
the OpenSandbox controller, PostgreSQL, and ACR. It
creates an ACR private endpoint and restricts Azure Files to the ACI subnet
through the Microsoft.Storage service endpoint required by ACI volume mounts.
It uses customer-owned subnets and private DNS zones.

The customer must prepare:

- the selected compute subnet, PostgreSQL subnet, private-endpoint subnet, and
  OpenSandbox ACA subnet in the deployment subscription and Azure region
- an ACI-only subnet delegated to
  `Microsoft.ContainerInstance/containerGroups`
- an operational NAT Gateway on the ACI subnet, with at least one public IP
  address or public IP prefix, for supported outbound connectivity
- the `Microsoft.Storage` service endpoint enabled on the ACI subnet
- a dedicated `/27` or larger infrastructure subnet delegated to
  `Microsoft.App/environments`, supplied as `container_apps_subnet_id`; for ACA
  it is also the application subnet, while ACI needs this additional subnet
- a PostgreSQL-only `/28` or larger subnet delegated to
  `Microsoft.DBforPostgreSQL/flexibleServers`
- a separate subnet that allows the ACR private endpoint
- the `privatelink.azurecr.io` private DNS zone
- a PostgreSQL private DNS zone in the deployment subscription, ending in
  `.postgres.database.azure.com` and not beginning with the PostgreSQL server
  name
- links from both private DNS zones to the required virtual networks; the ACR
  zone must also resolve from the Terraform runner's network
- VPN, ExpressRoute, peering, or another approved path for users and the
  Terraform runner to reach the private network
- a stable customer-managed internal entry point, such as an internal
  Application Gateway or equivalent reverse proxy, that terminates TLS when
  HTTPS is used and forwards HTTP to the current ACI private IP on TCP `8080`
- customer-managed network rules that allow the internal entry point or direct
  clients to reach the ACI subnet on TCP `8080`

Configure:

```hcl
network_mode               = "private"
dns_name_label             = null
terraform_runner_public_ip = null

private_network = {
  aci_subnet_id                  = "/subscriptions/<subscription>/resourceGroups/<network-rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<aci-subnet>" # use container_apps_subnet_id for ACA
  postgresql_subnet_id           = "/subscriptions/<subscription>/resourceGroups/<network-rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<postgresql-subnet>"
  private_endpoint_subnet_id     = "/subscriptions/<subscription>/resourceGroups/<network-rg>/providers/Microsoft.Network/virtualNetworks/<vnet>/subnets/<private-endpoint-subnet>"
  acr_private_dns_zone_id        = "/subscriptions/<subscription>/resourceGroups/<network-rg>/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
  postgresql_private_dns_zone_id = "/subscriptions/<subscription>/resourceGroups/<network-rg>/providers/Microsoft.Network/privateDnsZones/<customer>.postgres.database.azure.com"
  application_origin             = "https://opsrabbit.internal.example.com"
  dns_servers                    = ["10.0.0.4"]
}
```

`dns_servers` is optional when Azure-provided DNS resolves the linked private
zones. Supply the customer's DNS-server IPv4 addresses when ACI must use custom
DNS; ACI does not automatically inherit custom virtual-network DNS settings.

The Terraform runner must be able to resolve and connect to the private
PostgreSQL and ACR endpoints after Azure creates their DNS records. The
PostgreSQL provider installs `vector` directly in the database, and the
required ACR digest-verification commands use the private registry data
endpoint. Terraform manages the two shares through the Azure Resource Manager
API because the resources use `storage_account_id`; the runner does not need
access to the Azure Files data endpoint.

Terraform looks up the supplied subnets before planning workload resources. It
rejects subnets in another subscription or region and rejects an ACI subnet
without the `Microsoft.Storage` service endpoint. It also rejects a PostgreSQL
subnet smaller than `/28`, a PostgreSQL private DNS zone in another
subscription, and a zone beginning with the PostgreSQL server name. Terraform
cannot prove that the customer-managed subnet delegations, NAT Gateway, routing,
DNS links, VPN, or ExpressRoute path work end to end.

Before the first private plan, set the two delegated subnet IDs and verify their
customer-managed configuration:

```bash
export ACI_SUBNET_ID="<private_network.aci_subnet_id>"
export POSTGRESQL_SUBNET_ID="<private_network.postgresql_subnet_id>"

test "$(az network vnet subnet show \
  --ids "$ACI_SUBNET_ID" \
  --query "contains(delegations[].serviceName, 'Microsoft.ContainerInstance/containerGroups')" \
  --output tsv)" = "true"

test "$(az network vnet subnet show \
  --ids "$ACI_SUBNET_ID" \
  --query "contains(serviceEndpoints[].service, 'Microsoft.Storage')" \
  --output tsv)" = "true"

export ACI_NAT_GATEWAY_ID="$(az network vnet subnet show \
  --ids "$ACI_SUBNET_ID" \
  --query natGateway.id \
  --output tsv)"
test -n "$ACI_NAT_GATEWAY_ID"
test "$(az network nat gateway show \
  --ids "$ACI_NAT_GATEWAY_ID" \
  --query sku.name \
  --output tsv)" = "Standard"

export ACI_NAT_PUBLIC_IP_ID="$(az network nat gateway show \
  --ids "$ACI_NAT_GATEWAY_ID" \
  --query 'publicIpAddresses[0].id' \
  --output tsv)"
export ACI_NAT_PUBLIC_PREFIX_ID="$(az network nat gateway show \
  --ids "$ACI_NAT_GATEWAY_ID" \
  --query 'publicIpPrefixes[0].id' \
  --output tsv)"
test -n "${ACI_NAT_PUBLIC_IP_ID}${ACI_NAT_PUBLIC_PREFIX_ID}"

test "$(az network vnet subnet show \
  --ids "$POSTGRESQL_SUBNET_ID" \
  --query "contains(delegations[].serviceName, 'Microsoft.DBforPostgreSQL/flexibleServers')" \
  --output tsv)" = "true"

az network vnet subnet show \
  --ids "$POSTGRESQL_SUBNET_ID" \
  --query addressPrefixes \
  --output tsv
```

Separately confirm that:

- every displayed PostgreSQL subnet prefix is `/28` or larger
- the ACR private DNS zone is linked to both the ACI virtual network and the
  Terraform runner's network
- the PostgreSQL private DNS zone is linked to every virtual network that needs
  database resolution, including the Terraform runner's network
- the ACI subnet can route to the ACR private endpoint and PostgreSQL subnet
- customer DNS servers, when supplied, forward the Azure private zones
- the Terraform runner is connected to the private network and its DNS
  resolvers are configured to use the linked zones

The ACR and PostgreSQL records do not exist before the bootstrap apply creates
the resources. At this stage, verify the zone links, DNS forwarding, and network
routes; do not expect the final service hostnames to resolve yet.

Do not continue when any preflight check fails.

Private mode uses Premium ACR because ACR private endpoints are not available
on Basic. Trusted Azure-services access remains enabled so the documented
server-side ECR import and managed-identity ACI image pull continue to work.

For ACI, the backend digest must identify the ready-to-run ACI image that starts
as root for Azure Files mount setup and then launches OpsRabbit as `opsbot`. For
ACA, use the standard non-root backend image. The customer does not build either
image.

Keep this value disabled for the first apply:

```hcl
application_enabled = false
```

## 3. Supply secrets without a variable file

```bash
read -r -s -p "PostgreSQL administrator password: " \
  TF_VAR_postgresql_administrator_password
printf '\n'
export TF_VAR_postgresql_administrator_password

read -r -s -p "Better Auth secret: " TF_VAR_better_auth_secret
printf '\n'
export TF_VAR_better_auth_secret

read -r -s -p "OpsRabbit encryption key: " TF_VAR_opsrabbit_encryption_key
printf '\n'
export TF_VAR_opsrabbit_encryption_key
```

Use the customer-approved secret store as the source of these values for every
future plan or apply.

## 4. Initialize and review

```bash
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=bootstrap.tfplan
terraform show bootstrap.tfplan
terraform apply bootstrap.tfplan
rm -f bootstrap.tfplan
```

The bootstrap apply creates both ACRs, PostgreSQL, `vector`, Azure Files, the managed
identity, and the ACA environment when selected. It does not create the
application workload.

In private mode, confirm the records created during bootstrap now resolve to
private addresses from the Terraform runner:

```bash
nslookup "$(terraform output -raw container_registry_login_server)"
nslookup "$(terraform output -raw opensandbox_runtime_registry_login_server)"
nslookup "$(terraform output -raw postgresql_host)"
```

Do not continue to image verification when either lookup fails or returns only
a public address.

## 5. Import the finished images from OpsRabbit ECR

The operator running the import needs the Azure role `Container Registry Data
Importer and Data Reader` on the customer ACR. Grant it only for the import,
then remove it after both images are verified. The role is registry-wide; it is
not assigned to any application or sandbox identity. The customer runs these
commands using the short-lived ECR token supplied by OpsRabbit. The customer
does not need an AWS account or AWS CLI.

Set values from the release manifest:

```bash
export RELEASE="<approved-release>"
export OPSRABBIT_ECR_REGISTRY="<opsrabbit-ecr-registry>"
export BACKEND_ECR_REPOSITORY="<backend-aci-repository-for-ACI-or-backend-repository-for-ACA>"
export WEB_ECR_REPOSITORY="<web-repository>"
export BACKEND_IMAGE_DIGEST="<backend-sha256-digest>"
export WEB_IMAGE_DIGEST="<web-sha256-digest>"

export CUSTOMER_ACR_NAME="$(terraform output -raw container_registry_name)"
export BACKEND_ACR_REPOSITORY="$(terraform output -raw backend_image_repository)"
export SOURCE_REGISTRY_USER="AWS"

read -r -s -p "OpsRabbit ECR access token: " SOURCE_REGISTRY_PASSWORD
printf '\n'
```

Import directly between registries:

```bash
az acr import \
  --name "$CUSTOMER_ACR_NAME" \
  --source "${OPSRABBIT_ECR_REGISTRY}/${BACKEND_ECR_REPOSITORY}@${BACKEND_IMAGE_DIGEST}" \
  --image "${BACKEND_ACR_REPOSITORY}:${RELEASE}" \
  --username "$SOURCE_REGISTRY_USER" \
  --password "$SOURCE_REGISTRY_PASSWORD"

az acr import \
  --name "$CUSTOMER_ACR_NAME" \
  --source "${OPSRABBIT_ECR_REGISTRY}/${WEB_ECR_REPOSITORY}@${WEB_IMAGE_DIGEST}" \
  --image "opsrabbit/web:${RELEASE}" \
  --username "$SOURCE_REGISTRY_USER" \
  --password "$SOURCE_REGISTRY_PASSWORD"

unset SOURCE_REGISTRY_PASSWORD
```

Verify that ACR retained the approved digests:

```bash
test "$(az acr repository show \
  --name "$CUSTOMER_ACR_NAME" \
  --image "${BACKEND_ACR_REPOSITORY}:${RELEASE}" \
  --query digest --output tsv)" = "$BACKEND_IMAGE_DIGEST"

test "$(az acr repository show \
  --name "$CUSTOMER_ACR_NAME" \
  --image "opsrabbit/web:${RELEASE}" \
  --query digest --output tsv)" = "$WEB_IMAGE_DIGEST"
```

Do not continue if either digest differs.

In private mode, the import is still server-side and does not require the
Terraform runner to pull and push image layers. Do not disable ACR trusted
Azure-services access; a network-restricted registry needs that exception for
`az acr import`. The two `az acr repository show` checks are data-plane
operations, so the Terraform runner must resolve and route to the ACR private
endpoint even though it does not transfer image layers.

## 6. Build and scan the Azure OpenSandbox images

These images contain the Azure Dynamic Sessions adapter and the restricted
workspace runtime. Build them from this checkout after the bootstrap apply:

```bash
chmod +x spikes/dynamic-sessions/build-azure-images.sh
spikes/dynamic-sessions/build-azure-images.sh \
  "$(terraform output -raw container_registry_name)" \
  "$(terraform output -raw opensandbox_runtime_registry_name)" \
  "<unique-build-tag>"
```

The helper attaches BuildKit SBOM and provenance attestations, pushes amd64
images to their separate customer ACRs, prints immutable digests for Terraform, scans both
images with Trivy before publishing, and fails on High or Critical findings,
including findings without a vendor fix. Skopeo is required to publish the
scanned OCI layouts with their attestations. The helper authenticates Skopeo
directly with a short-lived ACR token through standard input. Its private
temporary auth file is removed on exit, so publication does not depend on
Docker Desktop's credential store. Both final image layers use
digest-pinned Python on Alpine 3.24; the OpenSandbox server dependency tree is
hash-locked. Review the exact commit's JSON image scan artifacts in CI and
qualify the final image digests on Azure before customer release. The minimal
runtime supports command/file execution; arbitrary glibc-only binaries and
browser automation are not qualified. Terraform state
contains the generated OpenSandbox API key and restricted database password,
so use encrypted remote state with limited access.

The operator building/publishing these images needs temporary `Container
Registry Repository Writer` grants: one on the application ACR conditioned to
`opsrabbit/opensandbox-server`, and one on the runtime ACR conditioned to
`opsrabbit/opensandbox-runtime`. Remove those publishing grants after verifying
the destination digests. Do not grant write access to either deployed identity.
The server gets repository-scoped Reader access in the application ACR; the
session pool gets Reader access only on the dedicated runtime ACR. The helper
rejects identical registry names so it cannot silently mix the two image types.

Set the two printed digest values in `terraform.tfvars`:

```hcl
opensandbox_server_image_digest  = "sha256:<server-digest>"
opensandbox_runtime_image_digest = "sha256:<runtime-digest>"
```

Keep image repositories and tags immutable. Never use a mutable `latest` tag
in Terraform.

## 7. Deploy the application

Set the following value in `terraform.tfvars`:

```hcl
application_enabled = true
```

This apply creates the session pool, controller, dedicated identities and
PostgreSQL journal schema. Before the OpsRabbit web/backend workload starts, an
installer init container runs migrations and invokes the product's
`--install` bootstrap. The product verifies command execution, file round-trip
and confirmed cleanup through the real OpenSandbox path before saving the
encrypted API key and enabling workspace-only command/file execution. If
verification fails, the application container does not start. Existing active
settings are preserved only when they match the deployed endpoint and image,
use the required workspace-only/session policy, and pass verification with the
saved encrypted key. Disabled or incompatible settings stop installation and
require administrator reconciliation; the installer never overwrites them.

Changes to the controller endpoint, server or runtime image digest, session
limit, or generated credentials create new ACA revisions. The application
revision reruns the installer bootstrap; the controller revision reloads its
credentials. For ACI, a non-secret bootstrap revision fingerprint replaces the
container group and reruns verification when the controller/runtime digest or
session limit changes. During a single-revision controller rollout, the new
process waits for the PostgreSQL leader lock before reconciling sessions.
Requests can briefly return HTTP 503 while the old controller stops and the new
one recovers the journal.

Then review and apply:

```bash
terraform plan -out=deployment.tfplan
terraform show deployment.tfplan
terraform apply deployment.tfplan
rm -f deployment.tfplan
```

If an Azure operation fails partway through the apply, keep the state and the
resources that were created successfully. Resolve the reported error, then
generate and review a fresh plan before retrying. Do not reuse a plan that was
already partially applied. The application name and hostname remain null if
you refresh state before the application has been created.

## 8. Verify the deployment

```bash
terraform output

if [ "$(terraform output -raw deployment_target)" = "aca" ]; then
  az containerapp show \
    --resource-group "$(terraform output -raw resource_group_name)" \
    --name "$(terraform output -raw container_app_name)" \
    --query '{name:name,revision:properties.latestReadyRevisionName,fqdn:properties.configuration.ingress.fqdn}' \
    --output table
else
  az container show \
    --resource-group "$(terraform output -raw resource_group_name)" \
    --name "$(terraform output -raw container_group_name)" \
    --query 'containers[].{name:name,state:instanceView.currentState.state,restarts:instanceView.restartCount}' \
    --output table
fi
```

For public mode:

```bash
curl --fail --show-error \
  "$(terraform output -raw opsrabbit_url)/api/health"
```

For private mode, do not treat the ACI private IP as a permanent application
address. An ACI update can replace the container group and assign another IP.
After every deployment or replacement, first reconcile the customer-managed
internal gateway backend with the current Terraform output. For the documented
HTTPS origin, terminate TLS at that gateway and forward HTTP to
`<container_group_ip_address>:8080`; ACI does not provide a TLS listener. If DNS
points directly to ACI, use an origin such as
`http://opsrabbit.internal.example.com:8080`, and automation must update the DNS
record and wait for resolution before the deployment is considered healthy.

From a machine connected to the customer network, confirm that the internal
application name resolves to the reconciled private entry point and that the
current ACI address is private:

```bash
export ACI_PRIVATE_IP="$(terraform output -raw container_group_ip_address)"
printf 'Reconcile the internal gateway HTTP backend to %s:8080\n' "$ACI_PRIVATE_IP"

nslookup "$(terraform output -raw opsrabbit_url | sed -E 's#^https?://([^/:]+).*$#\1#')"

curl --fail --show-error \
  "$(terraform output -raw opsrabbit_url)/api/health"
```

For private ACA, configure customer DNS and the internal entry point for the
customer-approved `private_network.application_origin`; the Container Apps
environment itself remains inaccessible from the public internet.

Expected health response:

```json
{"ok":true,"service":"opsrabbit-node-backend","role":"all"}
```

## Moving from the single-registry preview

Keep existing workloads and state. Before applying the new runtime image
location, create only the new registry (and its endpoint in private mode):

```bash
terraform plan -target=azurerm_container_registry.opensandbox_runtime \
  -target=azurerm_private_endpoint.opensandbox_runtime_registry \
  -out=runtime-registry.tfplan
terraform show runtime-registry.tfplan
terraform apply runtime-registry.tfplan
```

This targeted bootstrap is only for migrating the earlier preview. Publish or
copy the already verified runtime image and attestations to the dedicated
registry, then verify its digest. Generate and review a normal, untargeted plan
before continuing. It must remove the runtime identity's grant on the
application registry and grant read access only on the new registry. Never
move unrelated application images into the runtime registry.

For an existing active OpsRabbit installation, reconcile its sandbox image
setting through the administrator workflow as part of the migration. Bootstrap
stops on a conflicting active configuration instead of silently overwriting it.
Do not set `application_enabled=false` to migrate a running installation; that
would plan removal of its workloads.

## Upgrades

1. Receive the next approved release manifest and ECR access method.
2. Import both application images into ACR by immutable digest.
3. Verify the destination digests.
4. Update `backend_image_digest` and `web_image_digest` for OpsRabbit releases;
   update the OpenSandbox image digests only when a reviewed adapter/runtime
   rebuild is intended.
5. Review `terraform plan`.
6. Apply and repeat the health and persistence checks.

Do not change the PostgreSQL database, storage account, shares, Better Auth
secret, or OpsRabbit encryption key during a routine image upgrade.

Do not change `network_mode` during a routine upgrade. Moving an existing
deployment between public and private networking can replace the selected
container platform and PostgreSQL.
The stateful-resource protection intentionally blocks that conversion. Treat it
as a separately planned migration with backups, restore testing, DNS changes,
and a cutover plan.

## Destruction protection

The Terraform-created resource group, PostgreSQL server, database, `vector`
extension, storage account, and file shares use Terraform `prevent_destroy`.
This stops an ordinary plan from deleting durable application state or an
entire managed resource group.

When `create_resource_group` is false, the existing resource group is a
read-only lookup and is never part of Terraform destruction.

Intentional removal requires a separately reviewed change that removes those
guards after backups and customer approval. Do not work around the protection
with direct Azure deletion.

## Security

Report suspected vulnerabilities privately through this repository's GitHub
Security page. Do not include credentials, customer identifiers, or deployment
details in public issues. See [SECURITY.md](SECURITY.md).

## Contributing

Contributions are welcome through pull requests. See
[CONTRIBUTING.md](CONTRIBUTING.md) for validation and security requirements.

## License

The source code in this repository is licensed under the
[Apache License 2.0](LICENSE). The license does not grant rights to OpsRabbit
container images, services, names, logos, or other trademarks.
