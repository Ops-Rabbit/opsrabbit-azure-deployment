resource "azurerm_resource_group" "opsrabbit" {
  name     = "rg-${var.name_prefix}-aks"
  location = var.location
  tags     = var.tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_virtual_network" "opsrabbit" {
  name                = "vnet-${var.name_prefix}"
  location            = var.location
  resource_group_name = azurerm_resource_group.opsrabbit.name
  address_space       = ["10.42.0.0/16"]
  tags                = var.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.opsrabbit.name
  virtual_network_name = azurerm_virtual_network.opsrabbit.name
  address_prefixes     = ["10.42.0.0/22"]
}

resource "azurerm_subnet" "postgresql" {
  name                 = "postgresql"
  resource_group_name  = azurerm_resource_group.opsrabbit.name
  virtual_network_name = azurerm_virtual_network.opsrabbit.name
  address_prefixes     = ["10.42.4.0/24"]
  # Flexible Server adds this endpoint for its storage dependencies. Declare it
  # explicitly so later Terraform applies preserve the Azure-managed setting.
  service_endpoint {
    service = "Microsoft.Storage"
  }
  delegation {
    name = "postgresql"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "postgresql" {
  name                = "${var.name_prefix}.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.opsrabbit.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgresql" {
  name                = "postgresql"
  private_dns_zone_id = azurerm_private_dns_zone.postgresql.id
  virtual_network_id  = azurerm_virtual_network.opsrabbit.id
  tags                = var.tags
}

resource "azurerm_postgresql_flexible_server" "opsrabbit" {
  name                          = var.postgresql_server_name
  resource_group_name           = azurerm_resource_group.opsrabbit.name
  location                      = var.location
  version                       = "16"
  administrator_login           = var.postgresql_administrator_login
  administrator_password        = var.postgresql_administrator_password
  sku_name                      = var.postgresql_sku_name
  storage_mb                    = 32768
  auto_grow_enabled             = true
  backup_retention_days         = 14
  public_network_access_enabled = false
  delegated_subnet_id           = azurerm_subnet.postgresql.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgresql.id
  tags                          = var.tags
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone]
  }
  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgresql]
}

resource "azurerm_postgresql_flexible_server_database" "opsrabbit" {
  name      = var.postgresql_database_name
  server_id = azurerm_postgresql_flexible_server.opsrabbit.id
  lifecycle {
    prevent_destroy = true
  }
}

# Startup migrations create the extension from inside the VNet, as in the GCP
# deployment. Terraform only uses ARM; no database connection from the runner.
resource "azurerm_postgresql_flexible_server_configuration" "vector" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.opsrabbit.id
  value     = "VECTOR,PG_TRGM"
}

resource "azurerm_container_registry" "opsrabbit" {
  name                          = var.container_registry_name
  resource_group_name           = azurerm_resource_group.opsrabbit.name
  location                      = var.location
  sku                           = "Premium"
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = false
  network_rule_bypass_option    = "AzureServices"
  tags                          = var.tags
}

resource "azurerm_private_dns_zone" "acr" {
  name                = "privatelink.azurecr.io"
  resource_group_name = azurerm_resource_group.opsrabbit.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  name                = "acr"
  private_dns_zone_id = azurerm_private_dns_zone.acr.id
  virtual_network_id  = azurerm_virtual_network.opsrabbit.id
  tags                = var.tags
}

resource "azurerm_private_endpoint" "acr" {
  name                = "pe-${var.name_prefix}-acr"
  resource_group_name = azurerm_resource_group.opsrabbit.name
  location            = var.location
  subnet_id           = azurerm_subnet.aks.id
  private_service_connection {
    name                           = "acr"
    private_connection_resource_id = azurerm_container_registry.opsrabbit.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "acr"
    private_dns_zone_ids = [azurerm_private_dns_zone.acr.id]
  }
  tags = var.tags
}

resource "azurerm_user_assigned_identity" "aks" {
  name                = "id-${var.name_prefix}-aks"
  resource_group_name = azurerm_resource_group.opsrabbit.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "kubelet" {
  name                = "id-${var.name_prefix}-kubelet"
  resource_group_name = azurerm_resource_group.opsrabbit.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "kubelet_operator" {
  scope                            = azurerm_user_assigned_identity.kubelet.id
  role_definition_name             = "Managed Identity Operator"
  principal_id                     = azurerm_user_assigned_identity.aks.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "network" {
  scope                            = azurerm_virtual_network.opsrabbit.id
  role_definition_name             = "Network Contributor"
  principal_id                     = azurerm_user_assigned_identity.aks.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_kubernetes_cluster" "opsrabbit" {
  name                              = "aks-${var.name_prefix}"
  resource_group_name               = azurerm_resource_group.opsrabbit.name
  location                          = var.location
  dns_prefix                        = var.name_prefix
  kubernetes_version                = var.kubernetes_version
  private_cluster_enabled           = var.private_cluster_enabled
  local_account_disabled            = true
  role_based_access_control_enabled = true
  automatic_upgrade_channel         = "patch"
  node_os_upgrade_channel           = "NodeImage"
  sku_tier                          = "Standard"
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  azure_active_directory_role_based_access_control {
    tenant_id              = data.azurerm_client_config.current.tenant_id
    admin_group_object_ids = var.admin_group_object_ids
    azure_rbac_enabled     = true
  }
  dynamic "api_server_access_profile" {
    for_each = var.private_cluster_enabled ? [] : [1]
    content {
      authorized_ip_ranges = var.api_server_authorized_ip_ranges
    }
  }
  node_provisioning_profile {
    mode = "Manual"
  }
  default_node_pool {
    name                        = "system"
    node_count                  = var.node_count
    vm_size                     = var.node_vm_size
    vnet_subnet_id              = azurerm_subnet.aks.id
    os_disk_type                = "Managed"
    os_disk_size_gb             = 128
    temporary_name_for_rotation = "rotation"
    upgrade_settings {
      max_surge = "1"
    }
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks.id]
  }
  kubelet_identity {
    client_id                 = azurerm_user_assigned_identity.kubelet.client_id
    object_id                 = azurerm_user_assigned_identity.kubelet.principal_id
    user_assigned_identity_id = azurerm_user_assigned_identity.kubelet.id
  }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "azure"
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.43.0.0/16"
    dns_service_ip      = "10.43.0.10"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
  }
  storage_profile {
    disk_driver_enabled = true
  }
  tags       = var.tags
  depends_on = [azurerm_role_assignment.network, azurerm_role_assignment.kubelet_operator, azurerm_role_assignment.acr_pull]
}

# Image pulls use the kubelet identity, not the control plane or pod identity.
resource "azurerm_role_assignment" "acr_pull" {
  scope                            = azurerm_container_registry.opsrabbit.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.kubelet.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "cluster_admin" {
  for_each = var.admin_principal_object_ids

  scope                = azurerm_kubernetes_cluster.opsrabbit.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = each.value
}
