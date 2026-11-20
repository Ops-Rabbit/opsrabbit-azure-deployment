variable "subscription_id" {
  description = "Azure subscription to provision into."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for dedicated AKS resources; use a new prefix for each installation."
  type        = string
  default     = "opsrabbit"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name_prefix))
    error_message = "Use 3-24 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "location" {
  description = "Azure region supporting AKS and PostgreSQL Flexible Server."
  type        = string
  default     = "eastus2"
}

variable "container_registry_name" {
  description = "Globally unique ACR name."
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9]{5,50}$", var.container_registry_name))
    error_message = "ACR names require 5-50 alphanumeric characters."
  }
}

variable "postgresql_server_name" {
  description = "Globally unique PostgreSQL server name."
  type        = string
}

variable "postgresql_administrator_login" {
  description = "Database owner used by OpsRabbit startup migrations."
  type        = string
  default     = "opsrabbitadmin"
}

variable "postgresql_administrator_password" {
  description = "Database administrator password; supply via TF_VAR, never commit."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.postgresql_administrator_password) >= 16 && length(var.postgresql_administrator_password) <= 128
    error_message = "Supply a 16-128 character password meeting Azure PostgreSQL complexity rules."
  }
}

variable "postgresql_database_name" {
  description = "Application database."
  type        = string
  default     = "opsrabbit"
}

variable "postgresql_sku_name" {
  description = "PostgreSQL Flexible Server SKU."
  type        = string
  default     = "B_Standard_B2ms"
}

variable "admin_group_object_ids" {
  description = "Entra group object IDs granted AKS administrator access. Include the installing operator."
  type        = set(string)
  default     = []
  validation {
    condition = (length(var.admin_group_object_ids) + length(var.admin_principal_object_ids) > 0) && alltrue([
      for id in var.admin_group_object_ids : can(regex("^[0-9a-fA-F-]{36}$", id))
    ])
    error_message = "Provide at least one Entra administrator group or Azure RBAC administrator principal object ID."
  }
}

variable "admin_principal_object_ids" {
  description = "Entra principal object IDs assigned Azure Kubernetes Service RBAC Cluster Admin directly, for installations without an administrator group."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.admin_principal_object_ids : can(regex("^[0-9a-fA-F-]{36}$", id))])
    error_message = "Administrator principal object IDs must be UUIDs."
  }
}

variable "private_cluster_enabled" {
  description = "Keep the Kubernetes API private; Helm/kubectl then require VNet connectivity."
  type        = bool
  default     = true
}

variable "api_server_authorized_ip_ranges" {
  description = "IPv4 CIDRs permitted to access a public Kubernetes API. Required for public clusters."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([
      for cidr in var.api_server_authorized_ip_ranges : can(cidrnetmask(cidr)) && !endswith(cidr, "/0")
    ]) && (var.private_cluster_enabled || length(var.api_server_authorized_ip_ranges) > 0)
    error_message = "A public cluster requires explicit IPv4 API allowlist CIDRs; /0 is prohibited."
  }
}

variable "kubernetes_version" {
  description = "Optional supported regional AKS version; null selects Azure's current default. Patch auto-upgrades remain enabled."
  type        = string
  default     = null
}

variable "node_count" {
  description = "Number of AKS system nodes. Application remains a single backend replica."
  type        = number
  default     = 2
  validation {
    condition     = var.node_count >= 2 && floor(var.node_count) == var.node_count
    error_message = "Provision at least two nodes for system workloads and maintenance."
  }
}

variable "node_vm_size" {
  description = "Linux amd64 node size; allow room for the backend, web and AKS system pods."
  type        = string
  default     = "Standard_D4s_v5"
}

variable "tags" {
  description = "Tags applied to Azure resources."
  type        = map(string)
  default     = { application = "opsrabbit" }
}
