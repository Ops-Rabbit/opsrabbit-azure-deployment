locals {
  private_network_enabled = var.network_mode == "private"
  application_enabled     = var.container_group_enabled != null ? var.container_group_enabled : var.application_enabled
  aci_enabled             = local.application_enabled && var.deployment_target == "aci"
  container_app_enabled   = local.application_enabled && var.deployment_target == "aca"

  compute_subnet_id = local.private_network_enabled ? (
    var.deployment_target == "aca" ? try(var.private_network.container_apps_subnet_id, null) : try(var.private_network.aci_subnet_id, null)
  ) : null

  aci_public_origin = var.dns_name_label == null ? null : "http://${var.dns_name_label}.${var.location}.azurecontainer.io:8080"
  container_app_public_origin = var.deployment_target == "aca" ? try(
    "https://${var.container_app_name}.${azurerm_container_app_environment.opsrabbit[0].default_domain}",
    null,
  ) : null
  public_origin      = var.deployment_target == "aca" ? local.container_app_public_origin : local.aci_public_origin
  application_origin = local.private_network_enabled ? try(var.private_network.application_origin, null) : local.public_origin

  private_subnet_ids = local.private_network_enabled && var.private_network != null ? {
    compute          = local.compute_subnet_id
    postgresql       = var.private_network.postgresql_subnet_id
    private_endpoint = var.private_network.private_endpoint_subnet_id
  } : {}
  private_subnet_parts = {
    for name, id in local.private_subnet_ids : name => split("/", id)
  }

  backend_image_repository = var.backend_image_repository != null ? var.backend_image_repository : (
    var.deployment_target == "aca" ? "opsrabbit/backend" : "opsrabbit/backend-aci"
  )
  backend_image = "${azurerm_container_registry.opsrabbit.login_server}/${local.backend_image_repository}@${var.backend_image_digest}"
  web_image     = "${azurerm_container_registry.opsrabbit.login_server}/${var.web_image_repository}@${var.web_image_digest}"

  backend_healthcheck_command = "node -e \"fetch('http://127.0.0.1:8384/health').then((res) => process.exit(res.ok ? 0 : 1)).catch(() => process.exit(1))\""

  postgresql_database_url = "postgresql://${replace(urlencode(var.postgresql_administrator_login), "+", "%20")}:${replace(urlencode(var.postgresql_administrator_password), "+", "%20")}@${azurerm_postgresql_flexible_server.opsrabbit.fqdn}:5432/${replace(urlencode(var.postgresql_database_name), "+", "%20")}?sslmode=require"
}
