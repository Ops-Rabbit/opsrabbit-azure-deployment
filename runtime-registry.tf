# Dynamic Sessions image validation rejects repository-conditioned Reader
# grants. Keep its unconditional read grant on a registry reserved exclusively
# for sandbox runtime images, separate from application and controller images.
resource "azurerm_container_registry" "opensandbox_runtime" {
  name                          = local.opensandbox_runtime_registry_name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  sku                           = local.private_network_enabled ? "Premium" : "Basic"
  role_assignment_mode          = "AbacRepositoryPermissions"
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = !local.private_network_enabled
  network_rule_bypass_option    = "AzureServices"
  tags                          = var.tags

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_private_endpoint" "opensandbox_runtime_registry" {
  count = local.private_network_enabled ? 1 : 0

  name                = "pe-${local.opensandbox_runtime_registry_name}-registry"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.private_network.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-${local.opensandbox_runtime_registry_name}-registry"
    private_connection_resource_id = azurerm_container_registry.opensandbox_runtime.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "acr"
    private_dns_zone_ids = [var.private_network.acr_private_dns_zone_id]
  }

  tags = var.tags
}
