#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 2 || ! $1 =~ ^[a-z0-9]{5,50}$ || ! $2 =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]]; then
  echo "Usage: $0 <customer-acr-name> <unique-image-tag>" >&2
  exit 2
fi

registry_name="$1"
tag="$2"
registry="${registry_name}.azurecr.io"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
metadata_dir="$(mktemp -d)"
trap 'rm -rf "$metadata_dir"' EXIT

command -v az >/dev/null || { echo "Azure CLI is required." >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required to read BuildKit image metadata." >&2; exit 1; }
command -v trivy >/dev/null || { echo "Trivy is required for the High/Critical image gate." >&2; exit 1; }
command -v skopeo >/dev/null || { echo "Skopeo is required to publish the scanned OCI image and attestations." >&2; exit 1; }
docker buildx inspect >/dev/null

# Docker Desktop's global credential store is not necessarily available to
# Skopeo. Give the publisher its own short-lived ACR credentials, via stdin,
# and remove the private auth file with the other temporary build metadata.
authenticate_publisher() {
  az acr login --name "$registry_name" --expose-token --query accessToken --output tsv |
    skopeo login --authfile "${metadata_dir}/auth.json" \
      --username 00000000-0000-0000-0000-000000000000 \
      --password-stdin "$registry" >/dev/null
}

runtime_image="${registry}/opsrabbit/opensandbox-runtime:${tag}"
server_image="${registry}/opsrabbit/opensandbox-server:${tag}"
runtime_layout="${metadata_dir}/runtime-layout"
server_layout="${metadata_dir}/server-layout"

docker buildx build \
  --platform linux/amd64 \
  --sbom=true \
  --provenance=mode=max \
  --metadata-file "${metadata_dir}/runtime.json" \
  --output "type=oci,name=${runtime_image},dest=${runtime_layout},tar=false" \
  --file "${script_dir}/Dockerfile.runtime" \
  "$script_dir"

trivy image --input "$runtime_layout" --severity HIGH,CRITICAL --format table
trivy image --input "$runtime_layout" --exit-code 1 --severity HIGH,CRITICAL
authenticate_publisher
skopeo copy --authfile "${metadata_dir}/auth.json" --all --preserve-digests "oci:${runtime_layout}:${tag}" "docker://${runtime_image}"

docker buildx build \
  --platform linux/amd64 \
  --sbom=true \
  --provenance=mode=max \
  --metadata-file "${metadata_dir}/server.json" \
  --output "type=oci,name=${server_image},dest=${server_layout},tar=false" \
  --file "${script_dir}/Dockerfile.server" \
  "$script_dir"

trivy image --input "$server_layout" --severity HIGH,CRITICAL --format table
trivy image --input "$server_layout" --exit-code 1 --severity HIGH,CRITICAL
authenticate_publisher
skopeo copy --authfile "${metadata_dir}/auth.json" --all --preserve-digests "oci:${server_layout}:${tag}" "docker://${server_image}"

runtime_digest="$(jq -er '.["containerimage.digest"] | select(test("^sha256:[0-9a-f]{64}$"))' "${metadata_dir}/runtime.json")"
server_digest="$(jq -er '.["containerimage.digest"] | select(test("^sha256:[0-9a-f]{64}$"))' "${metadata_dir}/server.json")"

printf 'opensandbox_runtime_image_digest = "%s"\n' "$runtime_digest"
printf 'opensandbox_server_image_digest = "%s"\n' "$server_digest"
printf 'Runtime image: %s@%s\n' "$runtime_image" "$runtime_digest"
printf 'Server image: %s@%s\n' "$server_image" "$server_digest"
