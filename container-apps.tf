resource "azurerm_container_app_environment" "opsrabbit" {
  count = var.deployment_target == "aca" ? 1 : 0

  name                           = var.container_app_environment_name
  resource_group_name            = var.resource_group_name
  location                       = var.location
  infrastructure_subnet_id       = local.private_network_enabled ? local.compute_subnet_id : null
  internal_load_balancer_enabled = local.private_network_enabled ? true : null
  public_network_access          = local.private_network_enabled ? "Disabled" : "Enabled"

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition = !local.private_network_enabled || try(
        length(data.azurerm_subnet.private["compute"].address_prefixes) > 0 &&
        alltrue([
          for prefix in data.azurerm_subnet.private["compute"].address_prefixes :
          can(cidrnetmask(prefix)) && tonumber(split("/", prefix)[1]) <= 27
        ]),
        false,
      )
      error_message = "The private Azure Container Apps infrastructure subnet must have at least one IPv4 address prefix of /27 or larger."
    }

    precondition {
      condition = !local.private_network_enabled || try(anytrue([
        for delegation in data.azapi_resource.container_apps_subnet["compute"].output.properties.delegations :
        lower(delegation.properties.serviceName) == "microsoft.app/environments"
      ]), false)
      error_message = "The private Azure Container Apps infrastructure subnet must be dedicated and delegated to Microsoft.App/environments."
    }
  }

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_container_app_environment_storage" "application_data" {
  count = var.deployment_target == "aca" ? 1 : 0

  name                         = "opsrabbit-data"
  container_app_environment_id = azurerm_container_app_environment.opsrabbit[0].id
  account_name                 = azurerm_storage_account.opsrabbit.name
  share_name                   = azurerm_storage_share.application_data.name
  access_key                   = azurerm_storage_account.opsrabbit.primary_access_key
  access_mode                  = "ReadWrite"
}

resource "azurerm_container_app_environment_storage" "git_workspace" {
  count = var.deployment_target == "aca" ? 1 : 0

  name                         = "opsrabbit-git"
  container_app_environment_id = azurerm_container_app_environment.opsrabbit[0].id
  account_name                 = azurerm_storage_account.opsrabbit.name
  share_name                   = azurerm_storage_share.git_workspace.name
  access_key                   = azurerm_storage_account.opsrabbit.primary_access_key
  access_mode                  = "ReadWrite"
}

resource "random_id" "container_app_revision" {
  count = local.container_app_enabled ? 1 : 0

  byte_length = 4
  keepers = {
    rotation = sha512(jsonencode([
      var.postgresql_administrator_password,
      var.better_auth_secret,
      var.opsrabbit_encryption_key,
    ]))
  }
}

resource "azurerm_container_app" "opsrabbit" {
  count = local.container_app_enabled ? 1 : 0

  name                         = var.container_app_name
  container_app_environment_id = azurerm_container_app_environment.opsrabbit[0].id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aci_pull.id]
  }

  registry {
    server   = azurerm_container_registry.opsrabbit.login_server
    identity = azurerm_user_assigned_identity.aci_pull.id
  }

  secret {
    name  = "database-url"
    value = local.postgresql_database_url
  }

  secret {
    name  = "better-auth-secret"
    value = var.better_auth_secret
  }

  secret {
    name  = "opsrabbit-encryption-key"
    value = var.opsrabbit_encryption_key
  }

  ingress {
    external_enabled           = true
    allow_insecure_connections = false
    target_port                = 8080
    transport                  = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas                     = 1
    max_replicas                     = 1
    termination_grace_period_seconds = 120

    container {
      name   = "backend"
      image  = local.backend_image
      cpu    = var.backend_cpu
      memory = "${var.backend_memory_gb}Gi"

      command = ["/bin/bash", "-lc"]
      args = [
        "until nc -z \"$POSTGRES_HOST\" 5432; do sleep 2; done; test -w /home/opsbot/.opsrabbit && test -w /home/opsbot/git && exec /usr/local/bin/docker-entrypoint.sh node /opt/opsrabbit/apps/backend/dist/index.js",
      ]

      env {
        name  = "NODE_ENV"
        value = "production"
      }

      env {
        name  = "POSTGRES_HOST"
        value = azurerm_postgresql_flexible_server.opsrabbit.fqdn
      }

      env {
        name  = "OPSRABBIT_NODE_HOST"
        value = "0.0.0.0"
      }

      env {
        name  = "OPSRABBIT_NODE_PORT"
        value = "8384"
      }

      env {
        name  = "OPSRABBIT_WEB_ORIGIN"
        value = local.application_origin
      }

      env {
        name  = "OPSRABBIT_NODE_BASE_URL"
        value = "${local.application_origin}/api"
      }

      env {
        name  = "OPSRABBIT_NODE_DATA_DIR"
        value = "/home/opsbot/.opsrabbit"
      }

      env {
        name  = "OPSRABBIT_NODE_WORKSPACE_DIR"
        value = "/home/opsbot/git"
      }

      env {
        name        = "OPSRABBIT_NODE_DATABASE_URL"
        secret_name = "database-url"
      }

      env {
        name        = "BETTER_AUTH_SECRET"
        secret_name = "better-auth-secret"
      }

      env {
        name        = "OPSRABBIT_NODE_ENCRYPTION_KEY"
        secret_name = "opsrabbit-encryption-key"
      }

      # Secret values are application-scoped in ACA and do not create a new
      # revision on their own. This non-secret generation marker makes a secret
      # rotation a template change while Azure generates the unique suffix.
      env {
        name  = "OPSRABBIT_SECRET_GENERATION"
        value = random_id.container_app_revision[0].hex
      }

      volume_mounts {
        name = "opsrabbitdata"
        path = "/home/opsbot/.opsrabbit"
      }

      volume_mounts {
        name = "opsrabbitgit"
        path = "/home/opsbot/git"
      }

      startup_probe {
        transport               = "HTTP"
        port                    = 8384
        path                    = "/health"
        initial_delay           = 5
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 30
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 8384
        path                    = "/health"
        initial_delay           = 20
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 30
        success_count_threshold = 1
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8384
        path                    = "/health"
        initial_delay           = 60
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 5
      }
    }

    container {
      name   = "web"
      image  = local.web_image
      cpu    = var.web_cpu
      memory = "${var.container_app_web_memory_gb}Gi"

      command = ["/bin/sh", "-c"]
      args = [
        "sed -i 's/listen 80;/listen 8080;/g; s/listen \\[::\\]:80;/listen [::]:8080;/g' /etc/nginx/templates/http.conf.template; exec /docker-entrypoint.sh nginx -g 'daemon off;'",
      ]

      env {
        name  = "VITE_API_URL"
        value = "/api"
      }

      env {
        name  = "WEB_API_UPSTREAM"
        value = "http://127.0.0.1:8384"
      }

      env {
        name  = "WEB_TLS_MODE"
        value = "http"
      }

      env {
        name  = "WEB_SERVER_NAME"
        value = "_"
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/"
        initial_delay           = 10
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 18
        success_count_threshold = 1
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/"
        initial_delay           = 60
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 5
      }
    }

    volume {
      name          = "opsrabbitdata"
      storage_name  = azurerm_container_app_environment_storage.application_data[0].name
      storage_type  = "AzureFile"
      mount_options = "uid=1000,gid=1000,dir_mode=0700,file_mode=0700,mfsymlinks,nobrl"
    }

    volume {
      name          = "opsrabbitgit"
      storage_name  = azurerm_container_app_environment_storage.git_workspace[0].name
      storage_type  = "AzureFile"
      mount_options = "uid=1000,gid=1000,dir_mode=0700,file_mode=0700,mfsymlinks,nobrl"
    }
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition = (
        var.backend_cpu > 0 && var.web_cpu > 0 &&
        floor((var.backend_cpu + var.web_cpu) * 4) == (var.backend_cpu + var.web_cpu) * 4 &&
        var.backend_cpu + var.web_cpu <= 4 &&
        var.backend_memory_gb + var.container_app_web_memory_gb == (var.backend_cpu + var.web_cpu) * 2
      )
      error_message = "ACA Consumption resources must total 0.25-4 vCPU in 0.25 increments with memory equal to twice the vCPU count."
    }
  }

  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_postgresql_flexible_server_firewall_rule.azure_services,
    azurerm_private_endpoint.container_registry,
    azurerm_container_app_environment_storage.application_data,
    azurerm_container_app_environment_storage.git_workspace,
    postgresql_extension.vector,
  ]
}
