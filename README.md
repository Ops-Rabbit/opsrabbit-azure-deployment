# OpsRabbit Azure Terraform

This package creates the Azure resources for a fresh OpsRabbit deployment. It
supports public or private networking and can create a dedicated resource group
or use an existing customer-owned group.

- Azure Container Registry, using Basic for public mode or Premium with a
  private endpoint for private mode
- user-assigned image-pull identity and `AcrPull` permission
- Azure Files shares for application data and Git workspaces, restricted to the
  selected compute subnet in private mode
- Azure Database for PostgreSQL Flexible Server 16
- the PostgreSQL `vector` extension
- either one Azure Container Instances group or one Azure Container App
  containing the ready-to-run OpsRabbit backend and web images

OpsRabbit builds the images and publishes them in OpsRabbit Amazon ECR. The
customer does not build or modify images.

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

The package was validated on 29 July 2026 with:

| Component | Version |
|---|---:|
| Terraform CLI | `1.15.8` |
| AzureRM provider | `4.81.0` |
| PostgreSQL provider | `1.27.0` |
| AzAPI provider | `2.11.0` |
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
| `outputs.tf` | Deployment addresses and resource names |
| `terraform.tfvars.example` | Secret-free customer input example |
| `backend.tf.example` | Remote Azure state-backend example |

## Prerequisites

- Azure subscription
- permission to create resources, register the listed Azure providers, and
  create the ACR role assignment
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
PostgreSQL, and ACR. It
creates an ACR private endpoint and restricts Azure Files to the ACI subnet
through the Microsoft.Storage service endpoint required by ACI volume mounts.
It uses customer-owned subnets and private DNS zones.

The customer must prepare:

- all three supplied subnets in the deployment subscription and Azure region
- an ACI-only subnet delegated to
  `Microsoft.ContainerInstance/containerGroups`
- an operational NAT Gateway on the ACI subnet, with at least one public IP
  address or public IP prefix, for supported outbound connectivity
- the `Microsoft.Storage` service endpoint enabled on the ACI subnet
- when `deployment_target = "aca"`, a dedicated `/27` or larger infrastructure
  subnet delegated to `Microsoft.App/environments`, supplied as
  `container_apps_subnet_id` instead of `aci_subnet_id`
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

The bootstrap apply creates ACR, PostgreSQL, `vector`, Azure Files, the managed
identity, and the ACA environment when selected. It does not create the
application workload.

In private mode, confirm the records created during bootstrap now resolve to
private addresses from the Terraform runner:

```bash
nslookup "$(terraform output -raw container_registry_login_server)"
nslookup "$(terraform output -raw postgresql_host)"
```

Do not continue to image verification when either lookup fails or returns only
a public address.

## 5. Import the finished images from OpsRabbit ECR

The customer runs these commands using the short-lived ECR token supplied by
OpsRabbit. The customer does not need an AWS account or AWS CLI.

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

## 6. Deploy the application

Set the following value in `terraform.tfvars`:

```hcl
application_enabled = true
```

Then review and apply:

```bash
terraform plan -out=deployment.tfplan
terraform show deployment.tfplan
terraform apply deployment.tfplan
rm -f deployment.tfplan
```

## 7. Verify the deployment

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

## Upgrades

1. Receive the next approved release manifest and ECR access method.
2. Import both new images into ACR by immutable digest.
3. Verify the destination digests.
4. Update only `backend_image_digest` and `web_image_digest`.
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
