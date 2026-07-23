locals {
  private_network_enabled = var.network_mode == "private"
  public_origin           = var.dns_name_label == null ? null : "http://${var.dns_name_label}.${var.location}.azurecontainer.io:8080"
  application_origin      = local.private_network_enabled ? try(var.private_network.application_origin, null) : local.public_origin

  private_subnet_ids = local.private_network_enabled && var.private_network != null ? {
    aci              = var.private_network.aci_subnet_id
    postgresql       = var.private_network.postgresql_subnet_id
    private_endpoint = var.private_network.private_endpoint_subnet_id
  } : {}
  private_subnet_parts = {
    for name, id in local.private_subnet_ids : name => split("/", id)
  }

  backend_image = "${azurerm_container_registry.opsrabbit.login_server}/${var.backend_image_repository}@${var.backend_image_digest}"
  web_image     = "${azurerm_container_registry.opsrabbit.login_server}/${var.web_image_repository}@${var.web_image_digest}"

  backend_healthcheck_command = "node -e \"fetch('http://127.0.0.1:8384/health').then((res) => process.exit(res.ok ? 0 : 1)).catch(() => process.exit(1))\""

  postgresql_database_url = "postgresql://${replace(urlencode(var.postgresql_administrator_login), "+", "%20")}:${replace(urlencode(var.postgresql_administrator_password), "+", "%20")}@${azurerm_postgresql_flexible_server.opsrabbit.fqdn}:5432/${replace(urlencode(var.postgresql_database_name), "+", "%20")}?sslmode=require"
}
