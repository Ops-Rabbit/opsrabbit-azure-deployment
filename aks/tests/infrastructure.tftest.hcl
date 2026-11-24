mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id = "00000000-0000-0000-0000-000000000001"
    }
  }
  mock_resource "azurerm_resource_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks"
    }
  }
  mock_resource "azurerm_virtual_network" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.Network/virtualNetworks/vnet-opsrabbit"
    }
  }
  mock_resource "azurerm_subnet" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.Network/virtualNetworks/vnet-opsrabbit/subnets/mock"
    }
  }
  mock_resource "azurerm_private_dns_zone" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.Network/privateDnsZones/test.postgres.database.azure.com"
    }
  }
  mock_resource "azurerm_user_assigned_identity" {
    defaults = {
      id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.ManagedIdentity/userAssignedIdentities/aks"
      principal_id = "00000000-0000-0000-0000-000000000002"
    }
  }
  mock_resource "azurerm_container_registry" {
    defaults = {
      id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.ContainerRegistry/registries/opsrabbittestacr"
      login_server = "opsrabbittestacr.azurecr.io"
    }
  }
  mock_resource "azurerm_postgresql_flexible_server" {
    defaults = {
      id   = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.DBforPostgreSQL/flexibleServers/opsrabbit-test"
      fqdn = "opsrabbit-test.postgres.database.azure.com"
    }
  }
  mock_resource "azurerm_kubernetes_cluster" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.ContainerService/managedClusters/aks-opsrabbit"
      kubelet_identity = [{
        object_id                 = "00000000-0000-0000-0000-000000000003"
        client_id                 = "00000000-0000-0000-0000-000000000004"
        user_assigned_identity_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-aks/providers/Microsoft.ManagedIdentity/userAssignedIdentities/kubelet"
      }]
    }
  }
}

variables {
  subscription_id                   = "00000000-0000-0000-0000-000000000001"
  container_registry_name           = "opsrabbittestacr"
  postgresql_server_name            = "opsrabbit-test"
  postgresql_administrator_password = "test password+with@symbols#42"
  admin_group_object_ids            = ["00000000-0000-0000-0000-000000000005"]
}

run "private_cluster_and_dependencies" {
  command = plan
  assert {
    condition = (
      azurerm_kubernetes_cluster.opsrabbit.private_cluster_enabled &&
      azurerm_kubernetes_cluster.opsrabbit.local_account_disabled &&
      azurerm_kubernetes_cluster.opsrabbit.azure_active_directory_role_based_access_control[0].azure_rbac_enabled
    )
    error_message = "AKS must default to a private API with Entra RBAC and no local admin credentials."
  }
  assert {
    condition = (
      !azurerm_postgresql_flexible_server.opsrabbit.public_network_access_enabled &&
      azurerm_postgresql_flexible_server.opsrabbit.delegated_subnet_id == azurerm_subnet.postgresql.id &&
      azurerm_postgresql_flexible_server_configuration.vector.value == "VECTOR,PG_TRGM" &&
      one(azurerm_subnet.postgresql.service_endpoint).service == "Microsoft.Storage" &&
      azurerm_postgresql_flexible_server.opsrabbit.backup_retention_days == 14
    )
    error_message = "PostgreSQL must remain private with pgvector allowed and backups enabled."
  }
  assert {
    condition = (
      !azurerm_container_registry.opsrabbit.admin_enabled &&
      !azurerm_container_registry.opsrabbit.public_network_access_enabled &&
      azurerm_container_registry.opsrabbit.sku == "Premium" &&
      azurerm_role_assignment.acr_pull.principal_id == azurerm_kubernetes_cluster.opsrabbit.kubelet_identity[0].object_id &&
      azurerm_role_assignment.acr_pull.role_definition_name == "AcrPull"
    )
    error_message = "Private ACR pulls must use the kubelet identity."
  }
  assert {
    condition     = strcontains(output.database_url, "test%20password%2Bwith%40symbols%2342@") && endswith(output.database_url, "?sslmode=require")
    error_message = "Database credentials must be URL encoded and TLS required."
  }
}

run "allowlisted_public_api" {
  command = plan
  variables {
    private_cluster_enabled         = false
    api_server_authorized_ip_ranges = ["203.0.113.10/32"]
  }
  assert {
    condition     = toset(azurerm_kubernetes_cluster.opsrabbit.api_server_access_profile[0].authorized_ip_ranges) == toset(["203.0.113.10/32"])
    error_message = "Public API access must preserve the operator allowlist."
  }
}

run "reject_unrestricted_public_api" {
  command = plan
  variables {
    private_cluster_enabled         = false
    api_server_authorized_ip_ranges = ["0.0.0.0/0"]
  }
  expect_failures = [var.api_server_authorized_ip_ranges]
}

run "reject_public_api_without_allowlist" {
  command = plan
  variables {
    private_cluster_enabled = false
  }
  expect_failures = [var.api_server_authorized_ip_ranges]
}

run "reject_missing_administrators" {
  command = plan
  variables {
    admin_group_object_ids = []
  }
  expect_failures = [var.admin_group_object_ids]
}

run "direct_operator_rbac_access" {
  command = plan
  variables {
    admin_group_object_ids     = []
    admin_principal_object_ids = ["00000000-0000-0000-0000-000000000006"]
  }
  assert {
    condition     = azurerm_role_assignment.cluster_admin["00000000-0000-0000-0000-000000000006"].role_definition_name == "Azure Kubernetes Service RBAC Cluster Admin"
    error_message = "Operators without an Entra group must receive explicit Azure RBAC cluster access."
  }
}
