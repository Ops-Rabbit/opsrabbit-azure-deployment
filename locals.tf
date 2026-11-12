locals {
  private_network_enabled = var.network_mode == "private"
  application_enabled     = var.container_group_enabled != null ? var.container_group_enabled : var.application_enabled
  aci_enabled             = local.application_enabled && var.deployment_target == "aci"
  container_app_enabled   = local.application_enabled && var.deployment_target == "aca"
  opensandbox_enabled     = local.application_enabled
  resource_group_id       = "/subscriptions/${var.subscription_id}/resourceGroups/${var.resource_group_name}"
  opensandbox_runtime_registry_name = coalesce(
    var.opensandbox_runtime_registry_name,
    "${substr(lower(var.container_registry_name), 0, 35)}${substr(sha256(lower(var.container_registry_name)), 0, 8)}runtime",
  )
  opensandbox_runtime_image = var.opensandbox_runtime_image_digest == null ? null : "${azurerm_container_registry.opensandbox_runtime.login_server}/opsrabbit/opensandbox-runtime@${var.opensandbox_runtime_image_digest}"

  compute_subnet_id = local.private_network_enabled ? (
    var.deployment_target == "aca" ? try(var.private_network.container_apps_subnet_id, null) : try(var.private_network.aci_subnet_id, null)
  ) : null

  aci_public_origin = var.dns_name_label == null ? null : "http://${var.dns_name_label}.${var.location}.azurecontainer.io:8080"
  container_app_public_origin = var.deployment_target == "aca" ? try(
    "https://${var.container_app_name}.${azurerm_container_app_environment.opsrabbit[0].default_domain}",
    null,
  ) : null
  opensandbox_controller_origin = local.opensandbox_enabled ? try(
    "https://${var.opensandbox_controller_name}.${azurerm_container_app_environment.opsrabbit[0].default_domain}",
    null,
  ) : null
  public_origin      = var.deployment_target == "aca" ? local.container_app_public_origin : local.aci_public_origin
  application_origin = local.private_network_enabled ? try(var.private_network.application_origin, null) : local.public_origin

  private_subnet_ids = local.private_network_enabled && var.private_network != null ? {
    compute          = local.compute_subnet_id
    sessions         = (var.deployment_target == "aca" || local.application_enabled) ? var.private_network.container_apps_subnet_id : null
    postgresql       = var.private_network.postgresql_subnet_id
    private_endpoint = var.private_network.private_endpoint_subnet_id
  } : {}
  private_subnet_parts = {
    for name, id in local.private_subnet_ids : name => split("/", id) if id != null
  }

  backend_image_repository = var.backend_image_repository != null ? var.backend_image_repository : (
    var.deployment_target == "aca" ? "opsrabbit/backend" : "opsrabbit/backend-aci"
  )
  acr_repository_reader_action_guard      = "(!(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/content/read'}) AND !(ActionMatches{'Microsoft.ContainerRegistry/registries/repositories/metadata/read'}))"
  acr_application_reader_condition        = "(${local.acr_repository_reader_action_guard} OR (@Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase '${local.backend_image_repository}' OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase '${var.web_image_repository}'))"
  acr_opensandbox_server_reader_condition = "(${local.acr_repository_reader_action_guard} OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase 'opsrabbit/opensandbox-server')"
  backend_image                           = "${azurerm_container_registry.opsrabbit.login_server}/${local.backend_image_repository}@${var.backend_image_digest}"
  web_image                               = "${azurerm_container_registry.opsrabbit.login_server}/${var.web_image_repository}@${var.web_image_digest}"

  backend_healthcheck_command = "node -e \"fetch('http://127.0.0.1:8384/health').then((res) => process.exit(res.ok ? 0 : 1)).catch(() => process.exit(1))\""

  postgresql_database_url = "postgresql://${replace(urlencode(var.postgresql_administrator_login), "+", "%20")}:${replace(urlencode(var.postgresql_administrator_password), "+", "%20")}@${azurerm_postgresql_flexible_server.opsrabbit.fqdn}:5432/${replace(urlencode(var.postgresql_database_name), "+", "%20")}?sslmode=require"

  opensandbox_database_url = local.opensandbox_enabled ? "postgresql://${urlencode(postgresql_role.opensandbox[0].name)}:${replace(urlencode(random_password.opensandbox_database[0].result), "+", "%20")}@${azurerm_postgresql_flexible_server.opsrabbit.fqdn}:5432/${replace(urlencode(var.postgresql_database_name), "+", "%20")}?sslmode=require" : null

  opensandbox_bootstrap_json = local.opensandbox_enabled ? jsonencode({
    endpoint = local.opensandbox_controller_origin
    apiKey   = random_password.opensandbox_api_key[0].result
    image    = local.opensandbox_runtime_image
    resourceLimits = {
      cpu    = "0.5"
      memory = "1g"
    }
  }) : null

  opensandbox_bootstrap_revision = local.opensandbox_enabled ? sha256(jsonencode({
    server_digest = var.opensandbox_server_image_digest
    runtime_image = local.opensandbox_runtime_image
    max_sessions  = var.opensandbox_max_sessions
  })) : null

  opensandbox_bootstrap_command = local.opensandbox_enabled ? join(" && ", [
    "until nc -z \"$POSTGRES_HOST\" 5432; do sleep 2; done",
    "set -o pipefail",
    "node /opt/opsrabbit/apps/backend/dist/scripts/initialize-opensandbox.js --capabilities | node -e 'let s=\"\"; process.stdin.on(\"data\", c => s+=c); process.stdin.on(\"end\", () => { try { const c=JSON.parse(s); process.exit(c.activation===true && c.verification===true && c.install===true && c.resourceLimits===true && c.contractVersion===1 ? 0 : 1); } catch { process.exit(1); } });'",
    "node /opt/opsrabbit/apps/backend/dist/scripts/db-migrate.js",
    "printf '%s' \"$OPENSANDBOX_BOOTSTRAP_JSON\" | node /opt/opsrabbit/apps/backend/dist/scripts/initialize-opensandbox.js --install",
  ]) : null
}
