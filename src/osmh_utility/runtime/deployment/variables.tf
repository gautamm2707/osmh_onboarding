variable "tenancy_id" { type = string }
variable "compartment_id" { type = string }
variable "region" { type = string }
variable "home_region" { type = string }
variable "profile" {
  type    = string
  default = "DEFAULT"
}
variable "auth" {
  type    = string
  default = "APIKey"
  validation {
    condition     = contains(["APIKey", "SecurityToken"], var.auth)
    error_message = "Use APIKey or SecurityToken for deployment. The deployed function uses a resource principal."
  }
}
variable "subnet_ids" {
  type    = list(string)
  default = []
}
variable "create_network" {
  type    = bool
  default = false
}
variable "application_id" {
  type        = string
  default     = ""
  description = "Existing OCI Functions application OCID. When set, no application or network is created."
  validation {
    condition     = var.application_id == "" || can(regex("^ocid1\\.fnapp\\.", var.application_id))
    error_message = "application_id must be an OCI Functions application OCID."
  }
}
variable "repository_families" {
  type    = string
  default = "uek,ksplice,mysql,oci"
}
variable "group_prefix" {
  type    = string
  default = "osmh"
}
variable "image" {
  type        = string
  description = "Versioned Linux AMD64 image in OCIR in the function's region"
  validation {
    condition     = can(regex("^[a-z0-9.-]+/[a-z0-9_-]+/[a-z0-9._/-]+:[A-Za-z0-9_.-]+$", var.image)) && !endswith(var.image, ":latest")
    error_message = "Use a full OCIR host/namespace/repository:version image path, not latest."
  }
}
variable "tag_namespace" {
  type    = string
  default = ""
}
variable "workload_regions" {
  type        = string
  default     = ""
  description = "Empty = function region, CSV = selected regions, all = every READY subscription"
}
variable "bootstrap_iam" {
  type        = bool
  default     = true
  description = "Allow function to create OSMH IAM resources. Can be disabled after first successful setup."
}
variable "schedule_enabled" {
  type    = bool
  default = true
}
variable "logging_enabled" {
  type        = bool
  default     = false
  description = "Create a Functions invocation service log for the application. Disabled by default because reused apps may already have one."
}
