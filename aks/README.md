# OpsRabbit on Azure Kubernetes Service

This is a separate Terraform root and Helm chart for a **fresh installation**.
Run Terraform from `aks/`; use a distinct backend state key. Do not apply it over
the root Container Apps/Container Instances state or change an existing
deployment's state to this configuration. Moving existing data requires a
separate migration and restore plan.

## Architecture

| Component | Resource |
|---|---|
| Compute | AKS Standard tier, two Linux amd64 nodes by default, Azure CNI Overlay |
| Cluster access | Private Kubernetes API by default, Entra groups and Azure RBAC; local accounts disabled |
| Image registry | Premium ACR with private endpoint and kubelet `AcrPull`; no registry passwords |
| Database | Private PostgreSQL Flexible Server 16, 14-day backups, `VECTOR` and `PG_TRGM` allowed |
| Application | One backend with Recreate updates; two stateless nginx web replicas |
| Endpoint | Internal Azure LoadBalancer by default; optional public load balancer |
| TLS | Web image terminates HTTPS with an existing TLS Secret; HTTP redirects to HTTPS |
| Persistence | 100 GiB Azure Disk CSI PVC, ext4, retained on uninstall |
| Secrets | Existing Kubernetes Secret; no application credentials in Helm values/history |

The backend PVC mounts `.opsrabbit`, `git`, `.agent-browser` and `.codex` as
subdirectories under `/home/opsbot`. It preserves bundled home contents,
including the product's `node_modules` symlink. A non-root init container
creates these directories on the CSI volume and restores the shipped browser
discovery link/config when missing. Existing browser profiles and configuration
are preserved. The backend runs as UID/GID 1000;
the web runs as nginx UID/GID 101 on ports 8080/8443. Match the approved images'
runtime users if their image contract changes.

The backend is reachable only through a ClusterIP Service, with a NetworkPolicy
allowing traffic from this release's web pods. Web nginx retains the product's
same-origin API, webhook and WebSocket routing. There is no ingress controller
dependency. Application startup migrations create the `vector` and `pg_trgm` extensions;
Terraform only allowlists it, so the Terraform runner does not need a database
connection. The application currently uses the database owner for migrations
and runtime, consistent with the other cloud installers.

## Prerequisites

- Terraform **1.15.9**, Azure CLI, Helm **4.3.0**, kubectl and kubelogin.
- An Azure subscription and permission to register providers, provision these
  resources and assign roles. The installer needs permission to create a
  cluster-scoped StorageClass as well as namespace workloads.
- At least one Entra administrator group containing the installing operator,
  or `admin_principal_object_ids` granting your operator account Azure RBAC
  Cluster Admin directly when no administrator group is available.
- An approved matching backend/web release manifest with **linux/amd64** images
  and short-lived ECR pull credentials. Use the standard backend image, rather
  than the root installer’s `backend-aci` variant.
- A runner with routing and DNS access to the new VNet for private Kubernetes
  API access and ACR digest verification. Use a jump host, VPN or peering with
  private DNS forwarding. There is no automatic creation of runner connectivity.
- A DNS hostname and a trusted TLS certificate/key covering that hostname.
- An existing encrypted Azure state backend with restricted access.

The dedicated VNet uses `10.42.0.0/16`, pod CIDR `10.244.0.0/16` and service CIDR
`10.43.0.0/16`. Check for conflicts before peering with existing networks; adjust
the configuration before the first apply if needed. Kubernetes versions and VM
quotas vary by region; optionally set `kubernetes_version` to a supported version.

## 1. Provision Azure infrastructure

From this directory:

```bash
cp backend.tf.example backend.tf
cp terraform.tfvars.example terraform.tfvars
# Edit both files for your subscription, state backend, unique names and groups.
az login
az account set --subscription '<subscription-id>'
read -r -s -p 'PostgreSQL password: ' TF_VAR_postgresql_administrator_password
printf '\n'
export TF_VAR_postgresql_administrator_password
terraform init -input=false -lockfile=readonly
terraform plan -out=aks.tfplan
terraform apply aks.tfplan
rm -f aks.tfplan
```

Keep the password in your secret manager for future plans. It is sensitive but
is stored in Terraform state. Do not print `database_url`, enable shell tracing,
or archive state/plan files in build artifacts.

For an optional public API set `private_cluster_enabled = false` and supply
`api_server_authorized_ip_ranges` with your operator/CI public IPv4 CIDRs. The
configuration rejects an empty allowlist or `/0`. This does not expose the
application or PostgreSQL, and ACR still requires private network access.

## 2. Import approved images

Use the [root image import procedure](../README.md#5-import-the-finished-images-from-opsrabbit-ecr)
from this `aks/` directory. Set `CUSTOMER_ACR_NAME` from
`terraform output -raw container_registry_name` and set
`BACKEND_ACR_REPOSITORY=opsrabbit/backend` explicitly (the AKS root does not have
a `backend_image_repository` output). Choose the standard backend ECR repository
from the approved manifest. Import web into `opsrabbit/web`.

Verify both destination digests equal the approved release digests before
installation. ACR imports run server-side using its trusted Azure services
exception; registry digest checks require the runner to reach the private ACR
endpoint. Allow time for Azure identity role propagation if initial pulls fail.

## 3. Configure Kubernetes and secrets

```bash
az aks get-credentials \
  --resource-group "$(terraform output -raw resource_group_name)" \
  --name "$(terraform output -raw cluster_name)"
kubelogin convert-kubeconfig -l azurecli
export KUBE_CONTEXT="$(kubectl config current-context)"
kubectl --context "$KUBE_CONTEXT" create namespace opsrabbit

# Retrieve existing stable secrets from your secret manager for upgrades.
# For a fresh installation only, generate and securely save these values:
export BETTER_AUTH_SECRET="$(openssl rand -hex 32)"
export OPSRABBIT_NODE_ENCRYPTION_KEY="$(openssl rand -hex 32)"
./scripts/sync-runtime-secret.sh opsrabbit opsrabbit-runtime
unset BETTER_AUTH_SECRET OPSRABBIT_NODE_ENCRYPTION_KEY

kubectl --context "$KUBE_CONTEXT" -n opsrabbit create secret tls opsrabbit-tls \
  --cert=/secure/path/fullchain.pem --key=/secure/path/privkey.pem
```

The helper uses protected temporary files and server-side apply, avoiding
secret values in command arguments, stdout, Helm history and the client-side
apply annotation. It reads the sensitive database URL from the current AKS
Terraform state. You may instead create the same Secret through an approved
external secret controller, with keys `DATABASE_URL`, `BETTER_AUTH_SECRET` and
`OPSRABBIT_NODE_ENCRYPTION_KEY`.

## 4. Install OpsRabbit

```bash
cp values.example.yaml values.local.yaml
# Edit applicationOrigin and both immutable ACR image references.
# For public HTTPS set service.internal=false; optionally restrict client CIDRs.
helm lint ./chart -f values.local.yaml --strict
helm upgrade --install opsrabbit ./chart \
  --kube-context "$KUBE_CONTEXT" --namespace opsrabbit \
  --values values.local.yaml --wait --timeout 15m
kubectl --context "$KUBE_CONTEXT" -n opsrabbit get svc opsrabbit-opsrabbit-web
```

Point the hostname's DNS A record at the service's assigned IP, using private
DNS for an internal service. Internal clients need VNet connectivity. TLS is
required in both modes. An internal load balancer may still use a public DNS
name with split DNS, as long as the certificate matches. `applicationOrigin`
must be exactly `https://hostname`, without a path, port or trailing slash.
Neither certificate issuance/renewal nor DNS records are provisioned by default.
For an external ACME HTTP-01 client, `tls.acmeChallengeConfigMap` optionally mounts
its challenge files into every web replica at `/.well-known/acme-challenge/`.
The client must manage that ConfigMap, issue/renew the certificate, update the TLS
Secret and restart the web Deployment. Azure public DNS labels may be requested
through the literal `service.beta.kubernetes.io/azure-dns-label-name` key
in `service.annotations`.

## 5. Validate the running application

```bash
kubectl --context "$KUBE_CONTEXT" -n opsrabbit rollout status deployment/opsrabbit-opsrabbit-backend
kubectl --context "$KUBE_CONTEXT" -n opsrabbit rollout status deployment/opsrabbit-opsrabbit-web
kubectl --context "$KUBE_CONTEXT" -n opsrabbit get pods,pvc,svc
kubectl --context "$KUBE_CONTEXT" -n opsrabbit logs deployment/opsrabbit-opsrabbit-backend --tail=100
curl --fail --show-error https://opsrabbit.example.com/api/health
curl --head http://opsrabbit.example.com
```

Verify a redirect to HTTPS, sign-in, API calls and a streaming/WebSocket
conversation in the browser. Confirm database migrations completed without
extension errors. Create a disposable workspace file through the application,
restart the backend, then verify the file and application settings survive.
Review logs before sharing them because product logs can contain customer data.

The installation supports the core web/backend application. Features that
manage sibling Docker containers or require a host Docker socket need separate
Kubernetes integrations; no privileged container, host socket or automatic
cluster administrator credential is given to the backend. Model/cloud/provider
credentials are configured through the product after installation.

## Browser runtime compatibility

The initializer restores the browser discovery link hidden by the persistent
mount. In the restricted AKS evaluation environment, this made Chrome discoverable,
but Chrome then exited with `No usable sandbox!` before opening a page. Headless
browser automation is therefore not yet validated for that environment. A secure,
compatible browser execution configuration is required before accepting browser
features. The tested chart retains its non-root users, dropped capabilities,
RuntimeDefault seccomp and disabled privilege escalation; no browser sandbox
bypass is enabled by the installer.

## Upgrades, recovery and removal

- Save a PostgreSQL restore point and an Azure Disk snapshot/backup before
  upgrades. PostgreSQL backups are enabled; **disk backups are not scheduled**
  by this package. Retention alone is not a backup.
- Update both image digests from the same approved release, then repeat the
  Helm upgrade. Backend updates use Recreate to avoid concurrent workers and
  ReadWriteOnce disk attach conflicts; expect downtime during updates/drains.
- Preserve authentication and encryption keys across upgrades/restores. After
  secret rotation, rerun the helper and restart the backend. For certificate
  renewal, update the TLS Secret and restart the web Deployment to reload nginx.
- `helm rollback opsrabbit <revision> -n opsrabbit` restores manifests/images,
  but cannot reverse database migrations. Follow the release's database rollback
  procedure; restore the database and disk together when necessary.
- `helm uninstall opsrabbit -n opsrabbit` retains the chart-created PVC and
  StorageClass. Reinstall into the same namespace with
  `persistence.existingClaim=opsrabbit-opsrabbit`. A recovered external PVC must
  contain the four expected subdirectories with UID/GID 1000 write access.
- Terraform protects the resource group and PostgreSQL database/server from
  destruction. Deleting AKS or its managed node resource group can still delete
  managed disks. Back up and export recovery information before infrastructure
  removal. Never use namespace deletion as a retention strategy: it deletes PVCs.

## Local checks

```bash
terraform init -backend=false -input=false -lockfile=readonly
terraform fmt -check -recursive
terraform validate
terraform test -no-color
tflint
helm lint chart -f values.example.yaml --strict
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/sync-runtime-secret.sh
```

Mocked plans and rendered chart checks do not provision Azure. CI runs both the
original deployment checks and these AKS checks.

## References

The chart's helpers and workload structure are adapted from the
[OpsRabbit GCP Helm chart](https://github.com/Ops-Rabbit/opsrabbit-gcp-deployment/tree/bb61c6f/charts/opsrabbit).
Runtime paths, startup behavior, nginx TLS templates and environment variables
were checked against the local `gaurav-exp` product source at
`be2fdd33854d07361a30c99646e9d0d0021d52ec`.

- [Azure CNI Overlay](https://learn.microsoft.com/azure/aks/azure-cni-overlay)
- [AKS managed identities](https://learn.microsoft.com/azure/aks/managed-identity-overview)
- [Azure Disk CSI and retention](https://learn.microsoft.com/azure/aks/azure-disk-volume)
- [PostgreSQL extension allowlist](https://learn.microsoft.com/azure/postgresql/extensions/how-to-allow-extensions)
