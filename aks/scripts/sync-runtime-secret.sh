#!/usr/bin/env bash
# Keep credentials out of Helm release history, process arguments and stdout.
set -euo pipefail
set +x
umask 077

: "${KUBE_CONTEXT:?Set KUBE_CONTEXT to the intended AKS context}"
: "${BETTER_AUTH_SECRET:?Supply the stable application authentication secret}"
: "${OPSRABBIT_NODE_ENCRYPTION_KEY:?Supply the stable application encryption key}"
if (( ${#BETTER_AUTH_SECRET} < 32 || ${#OPSRABBIT_NODE_ENCRYPTION_KEY} < 32 )); then
  echo "Both application secrets must contain at least 32 characters." >&2
  exit 1
fi

namespace="${1:-opsrabbit}"
secret_name="${2:-opsrabbit-runtime}"
aks_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$(mktemp -d)"
trap 'rm -rf "$secret_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

terraform -chdir="$aks_dir" output -raw database_url > "$secret_dir/DATABASE_URL"
test -s "$secret_dir/DATABASE_URL"
printf '%s' "$BETTER_AUTH_SECRET" > "$secret_dir/BETTER_AUTH_SECRET"
printf '%s' "$OPSRABBIT_NODE_ENCRYPTION_KEY" > "$secret_dir/OPSRABBIT_NODE_ENCRYPTION_KEY"
kubectl --context "$KUBE_CONTEXT" -n "$namespace" create secret generic "$secret_name" \
  --from-file="$secret_dir/DATABASE_URL" \
  --from-file="$secret_dir/BETTER_AUTH_SECRET" \
  --from-file="$secret_dir/OPSRABBIT_NODE_ENCRYPTION_KEY" \
  --dry-run=client -o yaml |
  kubectl --context "$KUBE_CONTEXT" -n "$namespace" apply \
    --server-side --field-manager=opsrabbit-secrets -f -

echo "Runtime Secret synchronized. Restart the backend after rotating credentials."
