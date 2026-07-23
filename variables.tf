variable "subscription_id" {
  description = "Azure subscription that will own the OpsRabbit resources."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be an Azure subscription UUID."
  }
}

variable "location" {
  description = "Canonical Azure region name used by all resources, such as eastus2."
  type        = string
  default     = "eastus2"

  validation {
    condition     = can(regex("^[a-z0-9]+$", var.location))
    error_message = "location must use the canonical lowercase Azure name without spaces, such as eastus2 rather than East US 2."
  }
}

variable "create_resource_group" {
  description = "Create and manage the OpsRabbit resource group. Set false to use an existing customer-owned resource group."
  type        = bool
  default     = true
}

variable "resource_group_name" {
  description = "Resource group created for the OpsRabbit deployment, or an existing resource group when create_resource_group is false."
  type        = string

  validation {
    condition     = length(var.resource_group_name) >= 1 && length(var.resource_group_name) <= 90
    error_message = "resource_group_name must contain between 1 and 90 characters."
  }
}

variable "network_mode" {
  description = "OpsRabbit network exposure. Public preserves the default public endpoints; private uses customer-supplied private networking."
  type        = string
  default     = "public"

  validation {
    condition     = contains(["public", "private"], var.network_mode)
    error_message = "network_mode must be either public or private."
  }
}

variable "private_network" {
  description = "Customer-owned subnets, private DNS zones, and internal origin required when network_mode is private."
  type = object({
    aci_subnet_id                  = string
    postgresql_subnet_id           = string
    private_endpoint_subnet_id     = string
    acr_private_dns_zone_id        = string
    postgresql_private_dns_zone_id = string
    application_origin             = string
    dns_servers                    = optional(list(string), [])
  })
  default  = null
  nullable = true

  validation {
    condition     = var.network_mode != "private" || var.private_network != null
    error_message = "private_network must be supplied when network_mode is private."
  }

  validation {
    condition = var.private_network == null || alltrue([
      for subnet_id in [
        var.private_network.aci_subnet_id,
        var.private_network.postgresql_subnet_id,
        var.private_network.private_endpoint_subnet_id,
      ] : can(regex("(?i)^/subscriptions/[0-9a-f-]+/resourceGroups/[^/]+/providers/Microsoft\\.Network/virtualNetworks/[^/]+/subnets/[^/]+$", subnet_id))
    ])
    error_message = "Each private_network subnet must be a complete Azure subnet resource ID."
  }

  validation {
    condition = var.private_network == null || alltrue([
      for subnet_id in [
        var.private_network.aci_subnet_id,
        var.private_network.postgresql_subnet_id,
        var.private_network.private_endpoint_subnet_id,
      ] : try(lower(split("/", subnet_id)[2]) == lower(var.subscription_id), false)
    ])
    error_message = "All private_network subnets must be in subscription_id."
  }

  validation {
    condition = var.private_network == null || length(toset([
      lower(var.private_network.aci_subnet_id),
      lower(var.private_network.postgresql_subnet_id),
      lower(var.private_network.private_endpoint_subnet_id),
    ])) == 3
    error_message = "ACI, PostgreSQL, and private endpoints must use three distinct subnets."
  }

  validation {
    condition     = var.private_network == null || endswith(lower(var.private_network.acr_private_dns_zone_id), "/providers/microsoft.network/privatednszones/privatelink.azurecr.io")
    error_message = "acr_private_dns_zone_id must identify the privatelink.azurecr.io private DNS zone."
  }

  validation {
    condition     = var.private_network == null || can(regex("(?i)/providers/Microsoft\\.Network/privateDnsZones/[^/]+\\.postgres\\.database\\.azure\\.com$", var.private_network.postgresql_private_dns_zone_id))
    error_message = "postgresql_private_dns_zone_id must identify a private DNS zone ending in .postgres.database.azure.com."
  }

  validation {
    condition     = var.private_network == null || try(lower(split("/", var.private_network.postgresql_private_dns_zone_id)[2]) == lower(var.subscription_id), false)
    error_message = "postgresql_private_dns_zone_id must be in subscription_id."
  }

  validation {
    condition     = var.private_network == null || try(lower(split(".", element(reverse(split("/", var.private_network.postgresql_private_dns_zone_id)), 0))[0]) != lower(var.postgresql_server_name), false)
    error_message = "The PostgreSQL private DNS zone name cannot begin with postgresql_server_name."
  }

  validation {
    condition = var.private_network == null || (
      can(regex("^https?://(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*(?::[0-9]{1,5})?$", var.private_network.application_origin)) &&
      try(tonumber(regex(":([0-9]+)$", var.private_network.application_origin)[0]) >= 1 && tonumber(regex(":([0-9]+)$", var.private_network.application_origin)[0]) <= 65535, true)
    )
    error_message = "private_network.application_origin must be an http or https origin containing only a valid hostname and optional port, without credentials, whitespace, a path, query, fragment, or trailing slash."
  }

  validation {
    condition = var.private_network == null || alltrue([
      for server in var.private_network.dns_servers :
      can(cidrnetmask("${server}/32")) && !strcontains(server, "/")
    ])
    error_message = "Every private_network.dns_servers entry must be one IPv4 address without a CIDR suffix."
  }
}

variable "container_registry_name" {
  description = "Globally unique Azure Container Registry name."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9]{5,50}$", var.container_registry_name))
    error_message = "container_registry_name must contain 5-50 alphanumeric characters."
  }
}

variable "storage_account_name" {
  description = "Globally unique Azure Storage account name."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "storage_account_name must contain 3-24 lowercase letters or numbers."
  }
}

variable "postgresql_server_name" {
  description = "Globally unique PostgreSQL Flexible Server name."
  type        = string

  validation {
    condition     = length(var.postgresql_server_name) >= 3 && length(var.postgresql_server_name) <= 63 && can(regex("^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", var.postgresql_server_name))
    error_message = "postgresql_server_name must contain 3-63 lowercase letters, numbers, or hyphens and cannot start or end with a hyphen."
  }
}

variable "postgresql_database_name" {
  description = "OpsRabbit PostgreSQL database name."
  type        = string
  default     = "opsrabbit_neo"

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_-]{0,62}$", var.postgresql_database_name))
    error_message = "postgresql_database_name must be a valid PostgreSQL identifier of at most 63 characters."
  }
}

variable "postgresql_administrator_login" {
  description = "PostgreSQL Flexible Server administrator login."
  type        = string
  default     = "opsrabbitadmin"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.postgresql_administrator_login))
    error_message = "postgresql_administrator_login must start with a letter and contain only letters, numbers, or underscores."
  }
}

variable "postgresql_administrator_password" {
  description = "PostgreSQL administrator password. Supply with TF_VAR_postgresql_administrator_password."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.postgresql_administrator_password) >= 8 && length(var.postgresql_administrator_password) <= 128
    error_message = "postgresql_administrator_password must contain 8-128 characters."
  }
}

variable "postgresql_sku_name" {
  description = "PostgreSQL Flexible Server SKU."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgresql_storage_mb" {
  description = "PostgreSQL provisioned storage in MiB."
  type        = number
  default     = 32768

  validation {
    condition     = contains([32768, 65536, 131072, 262144, 524288, 1048576, 2097152, 4193280, 4194304, 8388608, 16777216, 33553408], var.postgresql_storage_mb)
    error_message = "postgresql_storage_mb must be a storage size supported by Azure PostgreSQL Flexible Server."
  }
}

variable "postgresql_backup_retention_days" {
  description = "PostgreSQL backup retention in days."
  type        = number
  default     = 7

  validation {
    condition     = var.postgresql_backup_retention_days >= 7 && var.postgresql_backup_retention_days <= 35
    error_message = "postgresql_backup_retention_days must be between 7 and 35."
  }
}

variable "terraform_runner_public_ip" {
  description = "Stable public IPv4 address allowed to manage the PostgreSQL vector extension in public mode."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.network_mode != "public" || (var.terraform_runner_public_ip != null && can(cidrnetmask("${var.terraform_runner_public_ip}/32")) && !strcontains(var.terraform_runner_public_ip, "/"))
    error_message = "terraform_runner_public_ip must be one IPv4 address without a CIDR suffix when network_mode is public."
  }
}

variable "identity_name" {
  description = "User-assigned identity used by ACI to pull from ACR."
  type        = string
}

variable "container_group_name" {
  description = "ACI container group name."
  type        = string
}

variable "dns_name_label" {
  description = "Globally unique ACI DNS label within the selected region. Required only in public mode."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.network_mode != "public" || (var.dns_name_label != null && length(var.dns_name_label) >= 3 && length(var.dns_name_label) <= 63 && can(regex("^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", var.dns_name_label)))
    error_message = "dns_name_label must contain 3-63 lowercase letters, numbers, or hyphens and cannot start or end with a hyphen when network_mode is public."
  }
}

variable "container_group_enabled" {
  description = "Create the ACI group after both release images have been imported into ACR."
  type        = bool
  default     = false
}

variable "backend_image_repository" {
  description = "Backend repository name inside the customer ACR."
  type        = string
  default     = "opsrabbit/backend-aci"
}

variable "backend_image_digest" {
  description = "Immutable backend image digest supplied in the OpsRabbit release manifest."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.backend_image_digest))
    error_message = "backend_image_digest must be a lowercase sha256 digest."
  }
}

variable "web_image_repository" {
  description = "Web repository name inside the customer ACR."
  type        = string
  default     = "opsrabbit/web"
}

variable "web_image_digest" {
  description = "Immutable web image digest supplied in the OpsRabbit release manifest."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.web_image_digest))
    error_message = "web_image_digest must be a lowercase sha256 digest."
  }
}

variable "better_auth_secret" {
  description = "Stable Better Auth secret. Supply with TF_VAR_better_auth_secret."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.better_auth_secret) >= 32
    error_message = "better_auth_secret must contain at least 32 characters."
  }
}

variable "opsrabbit_encryption_key" {
  description = "Stable OpsRabbit encryption key. Supply with TF_VAR_opsrabbit_encryption_key."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.opsrabbit_encryption_key) >= 32
    error_message = "opsrabbit_encryption_key must contain at least 32 characters."
  }
}

variable "application_data_share_quota_gb" {
  description = "Quota for the persistent OpsRabbit application-data share."
  type        = number
  default     = 10

  validation {
    condition     = var.application_data_share_quota_gb >= 1 && var.application_data_share_quota_gb <= 5120
    error_message = "application_data_share_quota_gb must be between 1 and 5120."
  }
}

variable "git_workspace_share_quota_gb" {
  description = "Quota for the persistent Git-workspace share."
  type        = number
  default     = 10

  validation {
    condition     = var.git_workspace_share_quota_gb >= 1 && var.git_workspace_share_quota_gb <= 5120
    error_message = "git_workspace_share_quota_gb must be between 1 and 5120."
  }
}

variable "backend_cpu" {
  description = "Backend requested vCPU."
  type        = number
  default     = 2
}

variable "backend_memory_gb" {
  description = "Backend requested memory in GiB."
  type        = number
  default     = 4
}

variable "web_cpu" {
  description = "Web requested vCPU."
  type        = number
  default     = 0.5
}

variable "web_memory_gb" {
  description = "Web requested memory in GiB."
  type        = number
  default     = 0.5
}

variable "tags" {
  description = "Tags applied to customer resources."
  type        = map(string)
  default = {
    application = "opsrabbit"
    managed-by  = "terraform"
  }
}
