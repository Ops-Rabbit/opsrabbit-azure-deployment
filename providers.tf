provider "azurerm" {
  subscription_id = var.subscription_id

  resource_provider_registrations = "none"
  resource_providers_to_register = concat(
    [
      "Microsoft.Authorization",
      "Microsoft.ContainerRegistry",
      "Microsoft.DBforPostgreSQL",
      "Microsoft.ManagedIdentity",
      "Microsoft.Storage",
    ],
    var.deployment_target == "aca" ? ["Microsoft.App"] : ["Microsoft.ContainerInstance"],
    var.network_mode == "private" ? ["Microsoft.Network"] : [],
  )

  features {}
}

provider "postgresql" {
  host             = "${var.postgresql_server_name}.postgres.database.azure.com"
  port             = 5432
  database         = "postgres"
  username         = var.postgresql_administrator_login
  password         = var.postgresql_administrator_password
  sslmode          = "require"
  connect_timeout  = 30
  superuser        = false
  expected_version = "16.0"
}
