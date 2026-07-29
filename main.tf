resource "azurerm_resource_group" "opsrabbit" {
  count = var.create_resource_group ? 1 : 0

  name     = var.resource_group_name
  location = var.location
  tags     = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

moved {
  from = azurerm_resource_group.opsrabbit
  to   = azurerm_resource_group.opsrabbit[0]
}

data "azurerm_resource_group" "existing" {
  count = var.create_resource_group ? 0 : 1

  name = var.resource_group_name
}

data "azurerm_subnet" "private" {
  for_each = local.private_subnet_parts

  name                 = each.value[10]
  virtual_network_name = each.value[8]
  resource_group_name  = each.value[4]
}

data "azurerm_virtual_network" "private" {
  for_each = local.private_subnet_parts

  name                = each.value[8]
  resource_group_name = each.value[4]
}

data "azapi_resource" "container_apps_subnet" {
  for_each = local.private_network_enabled && var.deployment_target == "aca" && local.compute_subnet_id != null ? {
    compute = local.compute_subnet_id
  } : {}

  type                   = "Microsoft.Network/virtualNetworks/subnets@2024-05-01"
  resource_id            = each.value
  response_export_values = ["properties.delegations"]
}

resource "azurerm_container_registry" "opsrabbit" {
  name                = var.container_registry_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = local.private_network_enabled ? "Premium" : "Basic"
  admin_enabled       = false

  public_network_access_enabled = !local.private_network_enabled
  anonymous_pull_enabled        = false
  network_rule_bypass_option    = "AzureServices"

  tags = var.tags

  lifecycle {
    precondition {
      condition = !local.private_network_enabled || alltrue([
        for network in data.azurerm_virtual_network.private :
        lower(network.location) == lower(var.location)
      ])
      error_message = "All private_network subnets must belong to virtual networks in ${var.location}."
    }

    precondition {
      condition     = !local.private_network_enabled || contains(data.azurerm_subnet.private["compute"].service_endpoints, "Microsoft.Storage")
      error_message = "The private compute subnet must enable the Microsoft.Storage service endpoint for Azure Files volume mounts."
    }

    precondition {
      condition = !local.private_network_enabled || try(
        length(data.azurerm_subnet.private["postgresql"].address_prefixes) > 0 &&
        alltrue([
          for prefix in data.azurerm_subnet.private["postgresql"].address_prefixes :
          can(cidrnetmask(prefix)) && tonumber(split("/", prefix)[1]) <= 28
        ]),
        false
      )
      error_message = "The private PostgreSQL subnet must have at least one IPv4 address prefix of /28 or larger."
    }
  }

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_user_assigned_identity" "aci_pull" {
  name                = var.identity_name
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                            = azurerm_container_registry.opsrabbit.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.aci_pull.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_storage_account" "opsrabbit" {
  name                = var.storage_account_name
  resource_group_name = var.resource_group_name
  location            = var.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  public_network_access_enabled   = true
  shared_access_key_enabled       = true
  allow_nested_items_to_be_public = false

  dynamic "network_rules" {
    for_each = local.private_network_enabled ? [local.compute_subnet_id] : []

    content {
      default_action             = "Deny"
      bypass                     = ["None"]
      virtual_network_subnet_ids = [network_rules.value]
    }
  }

  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_storage_share" "application_data" {
  # storage_account_id selects the Azure Resource Manager API, so share
  # lifecycle does not require Terraform runner access to the Files endpoint.
  name               = "opsrabbit-data"
  storage_account_id = azurerm_storage_account.opsrabbit.id
  quota              = var.application_data_share_quota_gb
  access_tier        = "TransactionOptimized"

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_share" "git_workspace" {
  name               = "opsrabbit-git"
  storage_account_id = azurerm_storage_account.opsrabbit.id
  quota              = var.git_workspace_share_quota_gb
  access_tier        = "TransactionOptimized"

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server" "opsrabbit" {
  name                = var.postgresql_server_name
  resource_group_name = var.resource_group_name
  location            = var.location
  version             = "16"

  administrator_login    = var.postgresql_administrator_login
  administrator_password = var.postgresql_administrator_password

  authentication {
    active_directory_auth_enabled = false
    password_auth_enabled         = true
  }

  sku_name                      = var.postgresql_sku_name
  storage_mb                    = var.postgresql_storage_mb
  auto_grow_enabled             = false
  backup_retention_days         = var.postgresql_backup_retention_days
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = !local.private_network_enabled
  delegated_subnet_id           = local.private_network_enabled ? try(var.private_network.postgresql_subnet_id, null) : null
  private_dns_zone_id           = local.private_network_enabled ? try(var.private_network.postgresql_private_dns_zone_id, null) : null

  tags = var.tags

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone]
  }

  depends_on = [
    azurerm_resource_group.opsrabbit,
    data.azurerm_resource_group.existing,
  ]
}

resource "azurerm_postgresql_flexible_server_database" "opsrabbit" {
  name      = var.postgresql_database_name
  server_id = azurerm_postgresql_flexible_server.opsrabbit.id

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.opsrabbit.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  count = local.private_network_enabled ? 0 : 1

  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.opsrabbit.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

moved {
  from = azurerm_postgresql_flexible_server_firewall_rule.azure_services
  to   = azurerm_postgresql_flexible_server_firewall_rule.azure_services[0]
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "terraform_runner" {
  count = local.private_network_enabled ? 0 : 1

  name             = "TerraformRunner"
  server_id        = azurerm_postgresql_flexible_server.opsrabbit.id
  start_ip_address = var.terraform_runner_public_ip
  end_ip_address   = var.terraform_runner_public_ip
}

moved {
  from = azurerm_postgresql_flexible_server_firewall_rule.terraform_runner
  to   = azurerm_postgresql_flexible_server_firewall_rule.terraform_runner[0]
}

resource "postgresql_extension" "vector" {
  name           = "vector"
  database       = var.postgresql_database_name
  schema         = "public"
  create_cascade = false
  drop_cascade   = false

  depends_on = [
    azurerm_postgresql_flexible_server_configuration.extensions,
    azurerm_postgresql_flexible_server_database.opsrabbit,
    azurerm_postgresql_flexible_server_firewall_rule.terraform_runner,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_private_endpoint" "container_registry" {
  count = local.private_network_enabled ? 1 : 0

  name                = "pe-${var.container_registry_name}-registry"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.private_network.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-${var.container_registry_name}-registry"
    private_connection_resource_id = azurerm_container_registry.opsrabbit.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "acr"
    private_dns_zone_ids = [var.private_network.acr_private_dns_zone_id]
  }

  tags = var.tags
}

resource "azurerm_container_group" "opsrabbit" {
  count = local.aci_enabled ? 1 : 0

  name                = var.container_group_name
  resource_group_name = var.resource_group_name
  location            = var.location
  os_type             = "Linux"
  sku                 = "Standard"
  restart_policy      = "Always"

  ip_address_type = local.private_network_enabled ? "Private" : "Public"
  dns_name_label  = local.private_network_enabled ? null : var.dns_name_label
  subnet_ids      = local.private_network_enabled ? [var.private_network.aci_subnet_id] : null

  exposed_port {
    port     = 8080
    protocol = "TCP"
  }

  dynamic "dns_config" {
    for_each = local.private_network_enabled && length(var.private_network.dns_servers) > 0 ? [var.private_network.dns_servers] : []

    content {
      nameservers = dns_config.value
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aci_pull.id]
  }

  image_registry_credential {
    server                    = azurerm_container_registry.opsrabbit.login_server
    user_assigned_identity_id = azurerm_user_assigned_identity.aci_pull.id
  }

  container {
    name   = "backend"
    image  = local.backend_image
    cpu    = var.backend_cpu
    memory = var.backend_memory_gb

    commands = [
      "/bin/bash",
      "-lc",
      "until nc -z \"$POSTGRES_HOST\" 5432; do sleep 2; done; runuser -u opsbot -- test -w /home/opsbot/.opsrabbit; runuser -u opsbot -- test -w /home/opsbot/git; exec runuser --preserve-environment -u opsbot -- /usr/local/bin/docker-entrypoint.sh",
    ]

    environment_variables = {
      NODE_ENV                     = "production"
      POSTGRES_HOST                = azurerm_postgresql_flexible_server.opsrabbit.fqdn
      OPSRABBIT_NODE_HOST          = "0.0.0.0"
      OPSRABBIT_NODE_PORT          = "8384"
      OPSRABBIT_WEB_ORIGIN         = local.application_origin
      OPSRABBIT_NODE_BASE_URL      = "${local.application_origin}/api"
      OPSRABBIT_NODE_DATA_DIR      = "/home/opsbot/.opsrabbit"
      OPSRABBIT_NODE_WORKSPACE_DIR = "/home/opsbot/git"
    }

    secure_environment_variables = {
      OPSRABBIT_NODE_DATABASE_URL   = local.postgresql_database_url
      BETTER_AUTH_SECRET            = var.better_auth_secret
      OPSRABBIT_NODE_ENCRYPTION_KEY = var.opsrabbit_encryption_key
    }

    ports {
      port     = 8384
      protocol = "TCP"
    }

    volume {
      name                 = "opsrabbitdata"
      mount_path           = "/home/opsbot/.opsrabbit"
      read_only            = false
      share_name           = azurerm_storage_share.application_data.name
      storage_account_name = azurerm_storage_account.opsrabbit.name
      storage_account_key  = azurerm_storage_account.opsrabbit.primary_access_key
    }

    volume {
      name                 = "opsrabbitgit"
      mount_path           = "/home/opsbot/git"
      read_only            = false
      share_name           = azurerm_storage_share.git_workspace.name
      storage_account_name = azurerm_storage_account.opsrabbit.name
      storage_account_key  = azurerm_storage_account.opsrabbit.primary_access_key
    }

    readiness_probe {
      initial_delay_seconds = 20
      period_seconds        = 10
      timeout_seconds       = 5
      failure_threshold     = 30
      success_threshold     = 1

      exec = ["/bin/sh", "-c", local.backend_healthcheck_command]
    }

    liveness_probe {
      initial_delay_seconds = 240
      period_seconds        = 30
      timeout_seconds       = 5
      failure_threshold     = 5
      success_threshold     = 1

      exec = ["/bin/sh", "-c", local.backend_healthcheck_command]
    }
  }

  container {
    name   = "web"
    image  = local.web_image
    cpu    = var.web_cpu
    memory = var.web_memory_gb

    commands = [
      "/bin/sh",
      "-c",
      "sed -i \"s/listen 80;/listen 8080;/g; s/listen \\\\[::\\\\]:80;/listen [::]:8080;/g\" /etc/nginx/templates/http.conf.template; exec /docker-entrypoint.sh",
    ]

    environment_variables = {
      VITE_API_URL     = "/api"
      WEB_API_UPSTREAM = "http://127.0.0.1:8384"
      WEB_TLS_MODE     = "http"
      WEB_SERVER_NAME  = "_"
    }

    ports {
      port     = 8080
      protocol = "TCP"
    }

    readiness_probe {
      initial_delay_seconds = 10
      period_seconds        = 10
      timeout_seconds       = 5
      failure_threshold     = 18
      success_threshold     = 1

      http_get {
        path   = "/"
        port   = 8080
        scheme = "http"
      }
    }

    liveness_probe {
      initial_delay_seconds = 60
      period_seconds        = 30
      timeout_seconds       = 5
      failure_threshold     = 5
      success_threshold     = 1

      http_get {
        path   = "/"
        port   = 8080
        scheme = "http"
      }
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_postgresql_flexible_server_firewall_rule.azure_services,
    azurerm_private_endpoint.container_registry,
    postgresql_extension.vector,
  ]
}
