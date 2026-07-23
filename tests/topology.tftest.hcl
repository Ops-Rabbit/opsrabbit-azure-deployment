mock_provider "azurerm" {}
mock_provider "postgresql" {}

variables {
  subscription_id                   = "00000000-0000-0000-0000-000000000001"
  location                          = "eastus2"
  resource_group_name               = "rg-opsrabbit-test"
  container_registry_name           = "opsrabbittestacr"
  storage_account_name              = "opsrabbitteststorage"
  postgresql_server_name            = "pg-opsrabbit-test"
  postgresql_administrator_password = "test postgres+password"
  terraform_runner_public_ip        = "203.0.113.10"
  identity_name                     = "id-opsrabbit-aci-test"
  container_group_name              = "aci-opsrabbit-test"
  dns_name_label                    = "opsrabbit-test"
  backend_image_digest              = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  web_image_digest                  = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  better_auth_secret                = "test-better-auth-secret-at-least-32-characters"
  opsrabbit_encryption_key          = "test-opsrabbit-encryption-key-at-least-32-characters"
}

override_resource {
  target          = azurerm_resource_group.opsrabbit[0]
  override_during = plan
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test"
  }
}

override_resource {
  target          = azurerm_container_registry.opsrabbit
  override_during = plan
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.ContainerRegistry/registries/opsrabbittestacr"
    login_server = "opsrabbittestacr.azurecr.io"
  }
}

override_resource {
  target          = azurerm_user_assigned_identity.aci_pull
  override_during = plan
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-opsrabbit-aci-test"
    client_id    = "00000000-0000-0000-0000-000000000003"
    principal_id = "00000000-0000-0000-0000-000000000002"
  }
}

override_resource {
  target          = azurerm_storage_account.opsrabbit
  override_during = plan
  values = {
    id                 = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.Storage/storageAccounts/opsrabbitteststorage"
    primary_access_key = "dGVzdC1zdG9yYWdlLWtleQ=="
  }
}

override_resource {
  target          = azurerm_postgresql_flexible_server.opsrabbit
  override_during = plan
  values = {
    id   = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg-opsrabbit-test"
    fqdn = "pg-opsrabbit-test.postgres.database.azure.com"
  }
}

run "bootstrap_without_container_group" {
  command = plan

  variables {
    container_group_enabled = false
  }

  assert {
    condition     = length(azurerm_container_group.opsrabbit) == 0
    error_message = "The bootstrap plan must not create ACI before images are imported."
  }

  assert {
    condition     = var.network_mode == "public" && length(azurerm_resource_group.opsrabbit) == 1 && length(data.azurerm_resource_group.existing) == 0
    error_message = "The default deployment must keep public networking and create its resource group."
  }

  assert {
    condition     = azurerm_container_registry.opsrabbit.sku == "Basic" && azurerm_container_registry.opsrabbit.public_network_access_enabled
    error_message = "The default public deployment must retain Basic ACR with public network access."
  }

  assert {
    condition     = azurerm_storage_account.opsrabbit.public_network_access_enabled && length(azurerm_storage_account.opsrabbit.network_rules) == 0
    error_message = "The default public deployment must retain unrestricted Azure Files network access."
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.opsrabbit.version == "16"
    error_message = "The deployment must use PostgreSQL 16."
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.opsrabbit.sku_name == "B_Standard_B1ms"
    error_message = "The default PostgreSQL SKU must match the verified deployment."
  }

  assert {
    condition     = azurerm_postgresql_flexible_server_configuration.extensions.value == "VECTOR"
    error_message = "The Azure PostgreSQL server must allow the vector extension."
  }

  assert {
    condition     = azurerm_postgresql_flexible_server.opsrabbit.public_network_access_enabled && length(azurerm_postgresql_flexible_server_firewall_rule.azure_services) == 1 && length(azurerm_postgresql_flexible_server_firewall_rule.terraform_runner) == 1
    error_message = "Public PostgreSQL must retain its two restricted firewall rules."
  }

  assert {
    condition     = length(azurerm_private_endpoint.container_registry) == 0
    error_message = "The public deployment must not create an ACR private endpoint."
  }

  assert {
    condition     = postgresql_extension.vector.name == "vector"
    error_message = "Terraform must install vector in the OpsRabbit database."
  }

  assert {
    condition     = azurerm_storage_share.application_data.quota == 10 && azurerm_storage_share.git_workspace.quota == 10
    error_message = "Both persistent shares must default to the verified 10 GiB quota."
  }
}

run "complete_application_topology" {
  command = plan

  override_resource {
    target          = azurerm_container_group.opsrabbit[0]
    override_during = plan
    values = {
      fqdn = "opsrabbit-test.eastus2.azurecontainer.io"
    }
  }

  variables {
    container_group_enabled = true
  }

  assert {
    condition     = length(azurerm_container_group.opsrabbit) == 1
    error_message = "Enabling the application must create exactly one ACI group."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].ip_address_type == "Public" && azurerm_container_group.opsrabbit[0].dns_name_label == "opsrabbit-test" && azurerm_container_group.opsrabbit[0].subnet_ids == null
    error_message = "The default application topology must retain its public ACI address and DNS label."
  }

  assert {
    condition     = length(azurerm_container_group.opsrabbit[0].container) == 2
    error_message = "The ACI group must contain only the backend and web containers."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[0].name == "backend" && azurerm_container_group.opsrabbit[0].container[0].cpu == 2 && azurerm_container_group.opsrabbit[0].container[0].memory == 4
    error_message = "The backend container must use the verified name and default sizing."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[0].image == "opsrabbittestacr.azurecr.io/opsrabbit/backend-aci@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    error_message = "The backend must use the immutable ACR digest."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[0].secure_environment_variables["BETTER_AUTH_SECRET"] == var.better_auth_secret
    error_message = "The backend must receive Better Auth through secure environment variables."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[0].secure_environment_variables["OPSRABBIT_NODE_DATABASE_URL"] == "postgresql://opsrabbitadmin:test%20postgres%2Bpassword@pg-opsrabbit-test.postgres.database.azure.com:5432/opsrabbit_neo?sslmode=require"
    error_message = "The database URL must safely encode password characters."
  }

  assert {
    condition     = join("\n", azurerm_container_group.opsrabbit[0].container[0].readiness_probe[0].exec) == join("\n", ["/bin/sh", "-c", local.backend_healthcheck_command]) && join("\n", azurerm_container_group.opsrabbit[0].container[0].liveness_probe[0].exec) == join("\n", ["/bin/sh", "-c", local.backend_healthcheck_command])
    error_message = "The backend probes must execute the in-container health request."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[1].name == "web" && azurerm_container_group.opsrabbit[0].container[1].cpu == 0.5 && azurerm_container_group.opsrabbit[0].container[1].memory == 0.5
    error_message = "The web container must use the verified name and default sizing."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[1].image == "opsrabbittestacr.azurecr.io/opsrabbit/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    error_message = "The web application must use the immutable ACR digest."
  }

  assert {
    condition     = output.opsrabbit_url == "http://opsrabbit-test.eastus2.azurecontainer.io:8080"
    error_message = "The public OpsRabbit output must match the ACI DNS label and port."
  }
}

run "use_existing_resource_group" {
  command = plan

  variables {
    create_resource_group   = false
    container_group_enabled = false
  }

  assert {
    condition     = length(azurerm_resource_group.opsrabbit) == 0 && length(data.azurerm_resource_group.existing) == 1
    error_message = "Existing-resource-group mode must look up the group without managing it."
  }

  assert {
    condition     = output.resource_group_name == "rg-opsrabbit-test"
    error_message = "All resources must continue to use the supplied existing resource-group name."
  }
}

run "private_application_topology" {
  command = plan

  override_data {
    target = data.azurerm_subnet.private["aci"]
    values = {
      service_endpoints = ["Microsoft.Storage"]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.2.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["aci"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["postgresql"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["private_endpoint"]
    values = {
      location = "eastus2"
    }
  }

  override_resource {
    target          = azurerm_container_group.opsrabbit[0]
    override_during = plan
    values = {
      ip_address = "10.20.1.4"
    }
  }

  variables {
    create_resource_group      = false
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    container_group_enabled    = true
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = ["10.20.0.4"]
    }
  }

  assert {
    condition     = length(azurerm_resource_group.opsrabbit) == 0 && length(data.azurerm_resource_group.existing) == 1
    error_message = "Private mode must work inside an existing customer resource group."
  }

  assert {
    condition     = azurerm_container_registry.opsrabbit.sku == "Premium" && !azurerm_container_registry.opsrabbit.public_network_access_enabled && azurerm_container_registry.opsrabbit.network_rule_bypass_option == "AzureServices"
    error_message = "Private mode must use a network-restricted Premium ACR while retaining trusted-service image import and pulls."
  }

  assert {
    condition     = azurerm_storage_account.opsrabbit.public_network_access_enabled && azurerm_storage_account.opsrabbit.network_rules[0].default_action == "Deny" && azurerm_storage_account.opsrabbit.network_rules[0].bypass == toset(["None"]) && contains(azurerm_storage_account.opsrabbit.network_rules[0].virtual_network_subnet_ids, var.private_network.aci_subnet_id)
    error_message = "Private mode must deny general Azure Files access and allow only the ACI service-endpoint subnet."
  }

  assert {
    condition     = !azurerm_postgresql_flexible_server.opsrabbit.public_network_access_enabled && azurerm_postgresql_flexible_server.opsrabbit.delegated_subnet_id == var.private_network.postgresql_subnet_id && azurerm_postgresql_flexible_server.opsrabbit.private_dns_zone_id == var.private_network.postgresql_private_dns_zone_id
    error_message = "Private PostgreSQL must use the supplied delegated subnet and private DNS zone."
  }

  assert {
    condition     = length(azurerm_postgresql_flexible_server_firewall_rule.azure_services) == 0 && length(azurerm_postgresql_flexible_server_firewall_rule.terraform_runner) == 0
    error_message = "Private PostgreSQL must not create public firewall rules."
  }

  assert {
    condition     = length(azurerm_private_endpoint.container_registry) == 1 && azurerm_private_endpoint.container_registry[0].subnet_id == var.private_network.private_endpoint_subnet_id && azurerm_private_endpoint.container_registry[0].private_service_connection[0].subresource_names == tolist(["registry"])
    error_message = "Private mode must create the ACR registry endpoint in the supplied private-endpoint subnet."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].ip_address_type == "Private" && azurerm_container_group.opsrabbit[0].dns_name_label == null && contains(azurerm_container_group.opsrabbit[0].subnet_ids, var.private_network.aci_subnet_id)
    error_message = "Private ACI must use a private IP in the supplied delegated subnet without a public DNS label."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].dns_config[0].nameservers == tolist(["10.20.0.4"])
    error_message = "Private ACI must receive the explicitly supplied customer DNS servers."
  }

  assert {
    condition     = azurerm_container_group.opsrabbit[0].container[0].environment_variables["OPSRABBIT_WEB_ORIGIN"] == var.private_network.application_origin && azurerm_container_group.opsrabbit[0].container[0].environment_variables["OPSRABBIT_NODE_BASE_URL"] == "${var.private_network.application_origin}/api"
    error_message = "Private mode must configure OpsRabbit with the customer-approved internal origin."
  }

  assert {
    condition     = output.container_group_fqdn == null && output.container_group_ip_address == "10.20.1.4" && output.opsrabbit_url == var.private_network.application_origin
    error_message = "Private outputs must expose the private IP and internal application URL without claiming a public FQDN."
  }
}

run "reject_invalid_backend_digest" {
  command = plan

  variables {
    backend_image_digest = "latest"
  }

  expect_failures = [
    var.backend_image_digest,
  ]
}

run "reject_non_ipv4_runner_address" {
  command = plan

  variables {
    terraform_runner_public_ip = "2001:db8::1"
  }

  expect_failures = [
    var.terraform_runner_public_ip,
  ]
}

run "reject_display_name_location" {
  command = plan

  variables {
    location = "East US 2"
  }

  expect_failures = [
    var.location,
  ]
}

run "reject_unknown_network_mode" {
  command = plan

  variables {
    network_mode = "internal"
  }

  expect_failures = [
    var.network_mode,
  ]
}

run "reject_private_mode_without_network" {
  command = plan

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network            = null
  }

  expect_failures = [
    var.private_network,
  ]
}

run "reject_private_subnet_from_another_subscription" {
  command = plan

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000099/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    var.private_network,
  ]
}

run "reject_malformed_private_application_origin" {
  command = plan

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com?redirect=public"
      dns_servers                    = []
    }
  }

  expect_failures = [
    var.private_network,
  ]
}

run "reject_private_network_in_another_region" {
  command = plan

  override_data {
    target = data.azurerm_subnet.private["aci"]
    values = {
      service_endpoints = ["Microsoft.Storage"]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.2.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["aci"]
    values = {
      location = "westus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["postgresql"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["private_endpoint"]
    values = {
      location = "eastus2"
    }
  }

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    azurerm_container_registry.opsrabbit,
  ]
}

run "reject_private_aci_subnet_without_storage_endpoint" {
  command = plan

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.2.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["aci"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["postgresql"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["private_endpoint"]
    values = {
      location = "eastus2"
    }
  }

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    azurerm_container_registry.opsrabbit,
  ]
}

run "reject_private_postgresql_subnet_smaller_than_slash_28" {
  command = plan

  override_data {
    target = data.azurerm_subnet.private["aci"]
    values = {
      service_endpoints = ["Microsoft.Storage"]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.2.0/29"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["aci"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["postgresql"]
    values = {
      location = "eastus2"
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["private_endpoint"]
    values = {
      location = "eastus2"
    }
  }

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    azurerm_container_registry.opsrabbit,
  ]
}

run "reject_postgresql_private_dns_zone_in_another_subscription" {
  command = plan

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000099/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    var.private_network,
  ]
}

run "reject_postgresql_private_dns_zone_named_after_server" {
  command = plan

  variables {
    network_mode               = "private"
    dns_name_label             = null
    terraform_runner_public_ip = null
    private_network = {
      aci_subnet_id                  = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-aci"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/pg-opsrabbit-test.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    var.private_network,
  ]
}
