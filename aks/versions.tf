terraform {
  required_version = "= 1.15.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.14.0"
    }
  }
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  resource_providers_to_register = [
    "Microsoft.Authorization", "Microsoft.ContainerService",
    "Microsoft.ContainerRegistry", "Microsoft.DBforPostgreSQL",
    "Microsoft.ManagedIdentity", "Microsoft.Network", "Microsoft.Compute",
    "Microsoft.Storage",
  ]
  features {}
}

data "azurerm_client_config" "current" {}
