output "resource_group_name" {
  description = "OpsRabbit resource group."
  value       = var.resource_group_name
}

output "container_registry_name" {
  description = "Customer ACR name used for server-side image import."
  value       = azurerm_container_registry.opsrabbit.name
}

output "container_registry_login_server" {
  description = "Customer ACR login server."
  value       = azurerm_container_registry.opsrabbit.login_server
}

output "backend_image" {
  description = "Immutable backend image reference used by the selected container platform."
  value       = local.backend_image
}

output "backend_image_repository" {
  description = "Resolved backend repository inside the customer ACR."
  value       = local.backend_image_repository
}

output "web_image" {
  description = "Immutable web image reference used by the selected container platform."
  value       = local.web_image
}

output "deployment_target" {
  description = "Selected Azure container platform."
  value       = var.deployment_target
}

output "postgresql_host" {
  description = "PostgreSQL Flexible Server hostname."
  value       = azurerm_postgresql_flexible_server.opsrabbit.fqdn
}

output "postgresql_database_name" {
  description = "OpsRabbit database name."
  value       = azurerm_postgresql_flexible_server_database.opsrabbit.name
}

output "application_data_share_name" {
  description = "Persistent OpsRabbit application-data share."
  value       = azurerm_storage_share.application_data.name
}

output "git_workspace_share_name" {
  description = "Persistent OpsRabbit Git-workspace share."
  value       = azurerm_storage_share.git_workspace.name
}

output "container_group_fqdn" {
  description = "Public ACI FQDN, or null for private networking and before ACI is enabled."
  value       = local.aci_enabled && !local.private_network_enabled ? azurerm_container_group.opsrabbit[0].fqdn : null
}

output "container_group_ip_address" {
  description = "ACI public or private IP address, or null until application_enabled is true."
  value       = local.aci_enabled ? azurerm_container_group.opsrabbit[0].ip_address : null
}

output "container_group_name" {
  description = "ACI container group name, or null until application_enabled is true."
  value       = local.aci_enabled ? azurerm_container_group.opsrabbit[0].name : null
}

output "container_app_environment_name" {
  description = "Azure Container Apps environment name, or null when ACA is not selected."
  value       = var.deployment_target == "aca" ? azurerm_container_app_environment.opsrabbit[0].name : null
}

output "container_app_name" {
  description = "Azure Container App name, or null until the ACA workload is enabled."
  value       = local.container_app_enabled ? azurerm_container_app.opsrabbit[0].name : null
}

output "container_app_fqdn" {
  description = "Azure Container App ingress FQDN, or null until the ACA workload is enabled."
  value       = local.container_app_enabled ? azurerm_container_app.opsrabbit[0].ingress[0].fqdn : null
}

output "opsrabbit_url" {
  description = "OpsRabbit URL, or null until the selected application workload is enabled."
  value       = local.application_enabled ? local.application_origin : null
}
