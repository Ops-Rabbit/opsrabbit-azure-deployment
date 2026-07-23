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
  description = "Immutable backend image reference used by ACI."
  value       = local.backend_image
}

output "web_image" {
  description = "Immutable web image reference used by ACI."
  value       = local.web_image
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
  value       = var.container_group_enabled && !local.private_network_enabled ? azurerm_container_group.opsrabbit[0].fqdn : null
}

output "container_group_ip_address" {
  description = "ACI public or private IP address, or null until container_group_enabled is true."
  value       = var.container_group_enabled ? azurerm_container_group.opsrabbit[0].ip_address : null
}

output "container_group_name" {
  description = "ACI container group name, or null until container_group_enabled is true."
  value       = var.container_group_enabled ? azurerm_container_group.opsrabbit[0].name : null
}

output "opsrabbit_url" {
  description = "OpsRabbit URL, or null until container_group_enabled is true."
  value       = var.container_group_enabled ? local.application_origin : null
}
