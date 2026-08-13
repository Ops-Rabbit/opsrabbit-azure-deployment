terraform {
  required_version = "~> 1.15.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "4.81.0"
    }

    azapi = {
      source  = "Azure/azapi"
      version = "2.12.0"
    }

    postgresql = {
      source  = "cyrilgdn/postgresql"
      version = "1.27.0"
    }

    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
    }
  }
}
