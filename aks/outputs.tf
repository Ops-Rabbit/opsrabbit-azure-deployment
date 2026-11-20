output "resource_group_name" {
  description = "Resource group containing the cluster and database."
  value       = azurerm_resource_group.opsrabbit.name
}

output "cluster_name" {
  description = "AKS cluster for az aks get-credentials."
  value       = azurerm_kubernetes_cluster.opsrabbit.name
}

output "container_registry_name" {
  description = "ACR destination for approved release imports."
  value       = azurerm_container_registry.opsrabbit.name
}

output "container_registry_login_server" {
  description = "Private ACR image registry hostname."
  value       = azurerm_container_registry.opsrabbit.login_server
}

output "postgresql_host" {
  description = "Private PostgreSQL host, resolved inside the VNet."
  value       = azurerm_postgresql_flexible_server.opsrabbit.fqdn
}

output "database_url" {
  description = "Application connection string for the external Kubernetes Secret; contains credentials."
  sensitive   = true
  value       = "postgresql://${replace(urlencode(var.postgresql_administrator_login), "+", "%20")}:${replace(urlencode(var.postgresql_administrator_password), "+", "%20")}@${azurerm_postgresql_flexible_server.opsrabbit.fqdn}:5432/${replace(urlencode(var.postgresql_database_name), "+", "%20")}?sslmode=require"
}
