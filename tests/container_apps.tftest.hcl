mock_provider "azurerm" {}
mock_provider "azapi" {}
mock_provider "postgresql" {}
mock_provider "random" {}

variables {
  subscription_id                   = "00000000-0000-0000-0000-000000000001"
  location                          = "eastus2"
  resource_group_name               = "rg-opsrabbit-test"
  container_registry_name           = "opsrabbittestacr"
  storage_account_name              = "opsrabbitteststorage"
  postgresql_server_name            = "pg-opsrabbit-test"
  postgresql_administrator_password = join("", [for _ in range(16) : "p"])
  terraform_runner_public_ip        = "203.0.113.10"
  identity_name                     = "id-opsrabbit-pull-test"
  deployment_target                 = "aca"
  dns_name_label                    = null
  container_app_environment_name    = "cae-opsrabbit-test"
  container_app_name                = "ca-opsrabbit-test"
  backend_image_digest              = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  web_image_digest                  = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  better_auth_secret                = join("", [for _ in range(32) : "a"])
  opsrabbit_encryption_key          = join("", [for _ in range(32) : "e"])
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
    id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-opsrabbit-pull-test"
    client_id    = "00000000-0000-0000-0000-000000000003"
    principal_id = "00000000-0000-0000-0000-000000000002"
  }
}

override_resource {
  target          = azurerm_storage_account.opsrabbit
  override_during = plan
  values = {
    id                 = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.Storage/storageAccounts/opsrabbitteststorage"
    primary_access_key = join("", [for _ in range(32) : "k"])
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

override_resource {
  target          = azurerm_container_app_environment.opsrabbit[0]
  override_during = plan
  values = {
    id             = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-opsrabbit-test/providers/Microsoft.App/managedEnvironments/cae-opsrabbit-test"
    default_domain = "test.eastus2.azurecontainerapps.io"
  }
}

override_resource {
  target          = azurerm_container_app.opsrabbit[0]
  override_during = plan
  values = {
    latest_revision_fqdn = "ca-opsrabbit-test--revision.test.eastus2.azurecontainerapps.io"
    ingress = {
      fqdn = "ca-opsrabbit-test.test.eastus2.azurecontainerapps.io"
    }
  }
}

override_resource {
  target          = random_id.container_app_revision[0]
  override_during = plan
  values = {
    hex = "abcd1234"
  }
}

run "bootstrap_aca_without_application" {
  command = plan

  assert {
    condition     = length(azurerm_container_app_environment.opsrabbit) == 1 && length(azurerm_container_app.opsrabbit) == 0
    error_message = "ACA bootstrap must create its environment without creating the application workload."
  }

  assert {
    condition     = length(azurerm_container_group.opsrabbit) == 0
    error_message = "Selecting ACA must not create an ACI container group."
  }

  assert {
    condition     = length(azurerm_container_app_environment_storage.application_data) == 1 && length(azurerm_container_app_environment_storage.git_workspace) == 1
    error_message = "ACA bootstrap must register both persistent Azure Files shares with the environment."
  }

  assert {
    condition     = local.backend_image_repository == "opsrabbit/backend" && output.opsrabbit_url == null
    error_message = "ACA must default to the standard backend image and withhold the URL until the application is enabled."
  }
}

run "public_aca_ignores_unused_legacy_private_network" {
  command = plan

  variables {
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

  assert {
    condition     = length(azurerm_container_app_environment.opsrabbit) == 1 && azurerm_container_app_environment.opsrabbit[0].infrastructure_subnet_id == null
    error_message = "Public ACA must ignore retained private-network settings that are not selected by network_mode."
  }
}

run "accept_custom_backend_repository_with_repeated_hyphens" {
  command = plan

  variables {
    backend_image_repository = "team/backend--aca"
  }

  assert {
    condition     = output.backend_image_repository == "team/backend--aca"
    error_message = "Valid custom OCI repository paths with repeated hyphens must remain supported."
  }
}

run "complete_public_aca_topology" {
  command = plan

  variables {
    application_enabled = true
  }

  assert {
    condition     = length(azurerm_container_app.opsrabbit) == 1 && length(azurerm_container_group.opsrabbit) == 0
    error_message = "Enabling ACA must create one Container App and no ACI group."
  }

  assert {
    condition     = azurerm_container_app_environment.opsrabbit[0].public_network_access == "Enabled" && !coalesce(azurerm_container_app_environment.opsrabbit[0].internal_load_balancer_enabled, false) && azurerm_container_app_environment.opsrabbit[0].infrastructure_subnet_id == null
    error_message = "Public ACA must use a public managed environment without a customer subnet."
  }

  assert {
    condition = length([
      for profile in azurerm_container_app_environment.opsrabbit[0].workload_profile : profile
      if profile.name == "Consumption" && profile.workload_profile_type == "Consumption"
    ]) == 1
    error_message = "ACA must use a workload-profile environment with the Consumption profile."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].revision_mode == "Single" && azurerm_container_app.opsrabbit[0].workload_profile_name == "Consumption" && azurerm_container_app.opsrabbit[0].template[0].min_replicas == 1 && azurerm_container_app.opsrabbit[0].template[0].max_replicas == 1
    error_message = "The initial ACA deployment must use one stable replica and single revision mode."
  }

  assert {
    condition = (
      azurerm_container_app.opsrabbit[0].template[0].revision_suffix != random_id.container_app_revision[0].hex &&
      length(random_id.container_app_revision[0].keepers) == 1 &&
      length([
        for setting in azurerm_container_app.opsrabbit[0].template[0].container[0].env : setting
        if setting.name == "OPSRABBIT_SECRET_GENERATION" && setting.value == random_id.container_app_revision[0].hex
      ]) == 1
    )
    error_message = "ACA must let Azure generate unique suffixes and turn application-secret changes into new revisions."
  }

  assert {
    condition     = length(azurerm_container_app.opsrabbit[0].template[0].container) == 2
    error_message = "The Container App must contain only the backend and web containers."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].template[0].container[0].image == "opsrabbittestacr.azurecr.io/opsrabbit/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" && azurerm_container_app.opsrabbit[0].template[0].container[0].memory == "4Gi"
    error_message = "ACA must use the immutable standard backend image with the requested memory."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].template[0].container[1].image == "opsrabbittestacr.azurecr.io/opsrabbit/web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" && azurerm_container_app.opsrabbit[0].template[0].container[1].memory == "1Gi"
    error_message = "ACA must use the immutable web image and its valid Consumption memory allocation."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].template[0].container[1].env[1].value == "http://127.0.0.1:8384"
    error_message = "The colocated web container must proxy API traffic to the backend over localhost."
  }

  assert {
    condition     = strcontains(azurerm_container_app.opsrabbit[0].template[0].container[0].args[0], "test -w /home/opsbot/.opsrabbit && test -w /home/opsbot/git && exec") && strcontains(azurerm_container_app.opsrabbit[0].template[0].container[0].args[0], "node /opt/opsrabbit/apps/backend/dist/index.js") && strcontains(azurerm_container_app.opsrabbit[0].template[0].container[1].args[0], "nginx -g 'daemon off;'")
    error_message = "ACA startup wrappers must enforce writable persistent volumes and preserve both foreground server commands."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].ingress[0].external_enabled && !azurerm_container_app.opsrabbit[0].ingress[0].allow_insecure_connections && azurerm_container_app.opsrabbit[0].ingress[0].target_port == 8080
    error_message = "ACA ingress must expose only the web container over managed HTTPS."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].template[0].volume[0].mount_options == "uid=1000,gid=1000,dir_mode=0700,file_mode=0700,mfsymlinks,nobrl" && azurerm_container_app.opsrabbit[0].template[0].volume[1].mount_options == "uid=1000,gid=1000,dir_mode=0700,file_mode=0700,mfsymlinks,nobrl"
    error_message = "ACA Azure Files mounts must map both shares privately to the non-root OpsRabbit user."
  }

  assert {
    condition     = output.container_app_name == "ca-opsrabbit-test" && output.container_app_fqdn == "ca-opsrabbit-test.test.eastus2.azurecontainerapps.io" && output.opsrabbit_url == "https://ca-opsrabbit-test.test.eastus2.azurecontainerapps.io"
    error_message = "ACA outputs must expose the selected app and managed HTTPS URL."
  }
}

run "complete_private_aca_topology" {
  command = plan

  override_data {
    target = data.azapi_resource.container_apps_subnet["compute"]
    values = {
      output = {
        properties = {
          delegations = [{
            properties = {
              serviceName = "Microsoft.App/environments"
            }
          }]
        }
      }
    }
  }

  override_data {
    target = data.azurerm_subnet.private["compute"]
    values = {
      address_prefixes = ["10.20.0.0/27"]
      service_endpoint = [{
        network_identifier = ""
        service            = "Microsoft.Storage"
      }]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.1.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["compute"]
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
    create_resource_group      = false
    network_mode               = "private"
    terraform_runner_public_ip = null
    application_enabled        = true
    private_network = {
      container_apps_subnet_id       = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-container-apps"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  assert {
    condition     = azurerm_container_app_environment.opsrabbit[0].infrastructure_subnet_id == var.private_network.container_apps_subnet_id && azurerm_container_app_environment.opsrabbit[0].internal_load_balancer_enabled && azurerm_container_app_environment.opsrabbit[0].public_network_access == "Disabled"
    error_message = "Private ACA must use the supplied infrastructure subnet and an internal managed environment."
  }

  assert {
    condition     = contains(azurerm_storage_account.opsrabbit.network_rules[0].virtual_network_subnet_ids, var.private_network.container_apps_subnet_id)
    error_message = "Private Azure Files access must be restricted to the ACA infrastructure subnet."
  }

  assert {
    condition     = azurerm_container_app.opsrabbit[0].template[0].container[0].env[4].value == var.private_network.application_origin && azurerm_container_app.opsrabbit[0].template[0].container[0].env[5].value == "${var.private_network.application_origin}/api"
    error_message = "Private ACA must configure OpsRabbit with the customer-approved internal origin."
  }

  assert {
    condition     = output.container_group_name == null && output.container_app_name == "ca-opsrabbit-test" && output.opsrabbit_url == var.private_network.application_origin
    error_message = "Private ACA outputs must expose only the Container App and approved internal URL."
  }
}

run "reject_private_aca_without_aca_subnet" {
  command = plan

  variables {
    network_mode               = "private"
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
    var.private_network,
  ]
}

run "reject_private_aca_subnet_smaller_than_slash_27" {
  command = plan

  override_data {
    target = data.azapi_resource.container_apps_subnet["compute"]
    values = {
      output = {
        properties = {
          delegations = [{
            properties = {
              serviceName = "Microsoft.App/environments"
            }
          }]
        }
      }
    }
  }

  override_data {
    target = data.azurerm_subnet.private["compute"]
    values = {
      address_prefixes = ["10.20.0.0/28"]
      service_endpoint = [{
        network_identifier = ""
        service            = "Microsoft.Storage"
      }]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.1.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["compute"]
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
    terraform_runner_public_ip = null
    private_network = {
      container_apps_subnet_id       = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-container-apps"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    azurerm_container_app_environment.opsrabbit[0],
  ]
}

run "reject_private_aca_subnet_without_delegation" {
  command = plan

  override_data {
    target = data.azapi_resource.container_apps_subnet["compute"]
    values = {
      output = {
        properties = {
          delegations = []
        }
      }
    }
  }

  override_data {
    target = data.azurerm_subnet.private["compute"]
    values = {
      address_prefixes = ["10.20.0.0/27"]
      service_endpoint = [{
        network_identifier = ""
        service            = "Microsoft.Storage"
      }]
    }
  }

  override_data {
    target = data.azurerm_subnet.private["postgresql"]
    values = {
      address_prefixes = ["10.20.1.0/28"]
    }
  }

  override_data {
    target = data.azurerm_virtual_network.private["compute"]
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
    terraform_runner_public_ip = null
    private_network = {
      container_apps_subnet_id       = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-container-apps"
      postgresql_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-postgresql"
      private_endpoint_subnet_id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-customer/subnets/snet-private-endpoints"
      acr_private_dns_zone_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.azurecr.io"
      postgresql_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/opsrabbit.postgres.database.azure.com"
      application_origin             = "https://opsrabbit.internal.example.com"
      dns_servers                    = []
    }
  }

  expect_failures = [
    azurerm_container_app_environment.opsrabbit[0],
  ]
}

run "reject_invalid_aca_consumption_allocation" {
  command = plan

  variables {
    application_enabled         = true
    container_app_web_memory_gb = 0.5
  }

  expect_failures = [
    azurerm_container_app.opsrabbit[0],
  ]
}

run "reject_unknown_deployment_target" {
  command = plan

  variables {
    deployment_target = "aks"
  }

  expect_failures = [
    var.deployment_target,
  ]
}
