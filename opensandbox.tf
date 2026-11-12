resource "random_password" "opensandbox_api_key" {
  count   = local.opensandbox_enabled ? 1 : 0
  length  = 64
  special = false
}

resource "random_password" "opensandbox_database" {
  count   = local.opensandbox_enabled ? 1 : 0
  length  = 48
  special = false
}

resource "random_id" "opensandbox_credential_revision" {
  count       = local.opensandbox_enabled ? 1 : 0
  byte_length = 4
  keepers = {
    rotation = sha512(jsonencode([
      random_password.opensandbox_api_key[0].result,
      random_password.opensandbox_database[0].result,
    ]))
  }
}

resource "postgresql_role" "opensandbox" {
  count    = local.opensandbox_enabled ? 1 : 0
  name     = "opensandbox_controller"
  login    = true
  password = random_password.opensandbox_database[0].result

  depends_on = [
    azurerm_postgresql_flexible_server_database.opsrabbit,
    azurerm_postgresql_flexible_server_firewall_rule.azure_services,
    azurerm_postgresql_flexible_server_firewall_rule.terraform_runner,
  ]
}

resource "postgresql_schema" "opensandbox" {
  count    = local.opensandbox_enabled ? 1 : 0
  database = var.postgresql_database_name
  name     = "opensandbox"
  owner    = postgresql_role.opensandbox[0].name
  # The controller owns its journal table inside this dedicated schema.
  drop_cascade = true
}

resource "azurerm_user_assigned_identity" "opensandbox" {
  count               = local.opensandbox_enabled ? 1 : 0
  name                = "${var.identity_name}-sandbox"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_user_assigned_identity" "opensandbox_server_pull" {
  count               = local.opensandbox_enabled ? 1 : 0
  name                = "${var.identity_name}-sandbox-server-pull"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_user_assigned_identity" "opensandbox_runtime_pull" {
  count               = local.opensandbox_enabled ? 1 : 0
  name                = "${var.identity_name}-sandbox-runtime-pull"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_role_assignment" "opensandbox_acr_pull" {
  count                            = local.opensandbox_enabled ? 1 : 0
  scope                            = azurerm_container_registry.opsrabbit.id
  role_definition_name             = "Container Registry Repository Reader"
  principal_id                     = azurerm_user_assigned_identity.opensandbox_server_pull[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
  condition                        = local.acr_opensandbox_server_reader_condition
  condition_version                = "2.0"
}

resource "azurerm_role_assignment" "opensandbox_runtime_acr_pull" {
  count                            = local.opensandbox_enabled ? 1 : 0
  scope                            = azurerm_container_registry.opensandbox_runtime.id
  role_definition_name             = "Container Registry Repository Reader"
  principal_id                     = azurerm_user_assigned_identity.opensandbox_runtime_pull[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azapi_resource" "opensandbox_pool_image_pull" {
  count     = local.opensandbox_enabled ? 1 : 0
  type      = "Microsoft.App/sessionPools@2025-07-01"
  name      = var.opensandbox_pool_name
  parent_id = local.resource_group_id
  location  = var.location

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.opensandbox_runtime_pull[0].id]
  }

  body = {
    properties = {
      containerType      = "CustomContainer"
      poolManagementType = "Dynamic"
      environmentId      = azurerm_container_app_environment.opsrabbit[0].id
      customContainerTemplate = {
        containers = [{
          name  = "runtime"
          image = local.opensandbox_runtime_image
          resources = {
            cpu    = 0.5
            memory = "1Gi"
          }
        }]
        ingress = { targetPort = 8080 }
        registryCredentials = {
          identity = azurerm_user_assigned_identity.opensandbox_runtime_pull[0].id
          server   = azurerm_container_registry.opensandbox_runtime.login_server
        }
      }
      dynamicPoolConfiguration = {
        lifecycleConfiguration = {
          lifecycleType           = "Timed"
          cooldownPeriodInSeconds = 3600
        }
      }
      scaleConfiguration = {
        maxConcurrentSessions = var.opensandbox_max_sessions
        readySessionInstances = 1
      }
      sessionNetworkConfiguration = { status = "EgressDisabled" }
      managedIdentitySettings = [{
        # Azure canonicalizes this path segment on read-back. Match it without
        # ignoring changes to identity selection, lifecycle, or image values.
        identity  = replace(azurerm_user_assigned_identity.opensandbox_runtime_pull[0].id, "/[/]resourceGroups[/]/", "/resourcegroups/")
        lifecycle = "None"
      }]
    }
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true

    precondition {
      condition     = var.opensandbox_runtime_image_digest != null
      error_message = "Set opensandbox_runtime_image_digest to the reviewed immutable ACR runtime image before enabling the application."
    }
  }

  depends_on = [
    azurerm_role_assignment.opensandbox_runtime_acr_pull,
    azurerm_private_endpoint.opensandbox_runtime_registry,
    azapi_update_resource.acr_repository_mode,
    time_sleep.acr_role_propagation,
  ]
}

resource "azurerm_role_assignment" "opensandbox_session_executor" {
  count                            = local.opensandbox_enabled ? 1 : 0
  scope                            = azapi_resource.opensandbox_pool_image_pull[0].id
  role_definition_name             = "Azure ContainerApps Session Executor"
  principal_id                     = azurerm_user_assigned_identity.opensandbox[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Create the lifecycle settings in the same ARM request as the Container App.
# A follow-up patch would briefly expose the registry-only identity to app code.
resource "azapi_resource" "opensandbox" {
  count     = local.opensandbox_enabled ? 1 : 0
  type      = "Microsoft.App/containerApps@2025-07-01"
  name      = var.opensandbox_controller_name
  parent_id = local.resource_group_id
  location  = var.location

  identity {
    type = "UserAssigned"
    identity_ids = [
      azurerm_user_assigned_identity.opensandbox[0].id,
      azurerm_user_assigned_identity.opensandbox_server_pull[0].id,
    ]
  }

  body = {
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.opsrabbit[0].id
      workloadProfileName  = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        identitySettings = [
          {
            identity  = replace(azurerm_user_assigned_identity.opensandbox[0].id, "/[/]resourceGroups[/]/", "/resourcegroups/")
            lifecycle = "Main"
          },
          {
            identity  = replace(azurerm_user_assigned_identity.opensandbox_server_pull[0].id, "/[/]resourceGroups[/]/", "/resourcegroups/")
            lifecycle = "None"
          },
        ]
        ingress = {
          external      = true
          allowInsecure = false
          targetPort    = 8080
          # Keep transport managed, using Azure's canonical read-back casing.
          transport = "Auto"
          traffic = [{
            latestRevision = true
            weight         = 100
          }]
        }
        registries = [{
          server   = azurerm_container_registry.opsrabbit.login_server
          identity = azurerm_user_assigned_identity.opensandbox_server_pull[0].id
        }]
        secrets = [
          {
            name  = "opensandbox-api-key"
            value = random_password.opensandbox_api_key[0].result
          },
          {
            name  = "opensandbox-session-database-url"
            value = local.opensandbox_database_url
          },
        ]
      }
      template = {
        scale = {
          minReplicas = 1
          maxReplicas = 1
        }
        terminationGracePeriodSeconds = 60
        containers = [{
          name  = "opensandbox"
          image = "${azurerm_container_registry.opsrabbit.login_server}/opsrabbit/opensandbox-server@${var.opensandbox_server_image_digest}"
          resources = {
            cpu    = 1
            memory = "2Gi"
          }
          env = [
            { name = "OPENSANDBOX_API_KEY", secretRef = "opensandbox-api-key" },
            { name = "AZURE_SESSION_DATABASE_URL", secretRef = "opensandbox-session-database-url" },
            { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.opensandbox[0].client_id },
            { name = "AZURE_SESSION_IMAGE", value = local.opensandbox_runtime_image },
            { name = "AZURE_SESSION_ENDPOINT", value = azapi_resource.opensandbox_pool_image_pull[0].output.properties.poolManagementEndpoint },
            { name = "AZURE_SESSION_MAX_SESSIONS", value = tostring(var.opensandbox_max_sessions) },
            { name = "OPENSANDBOX_CREDENTIAL_GENERATION", value = random_id.opensandbox_credential_revision[0].hex },
          ]
        }]
      }
    }
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition     = var.opensandbox_server_image_digest != null
      error_message = "Set opensandbox_server_image_digest to the reviewed immutable ACR server image before enabling the application."
    }
  }

  depends_on = [
    azurerm_role_assignment.opensandbox_acr_pull,
    azurerm_role_assignment.opensandbox_runtime_acr_pull,
    azurerm_role_assignment.opensandbox_session_executor,
    azapi_update_resource.acr_repository_mode,
    postgresql_schema.opensandbox,
    time_sleep.acr_role_propagation,
  ]
}
