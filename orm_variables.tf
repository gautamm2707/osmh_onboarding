variable "tenancy_ocid" {
  type        = string
  description = "Automatically supplied by OCI Resource Manager. This configuration targets commercial OCI (oc1)."
  validation {
    condition     = can(regex("^ocid1\\.tenancy\\.oc1\\.", var.tenancy_ocid))
    error_message = "This stack currently supports commercial OCI tenancies (oc1)."
  }
}
variable "compartment_ocid" {
  type        = string
  description = "Automatically supplied by OCI Resource Manager: the compartment that stores the stack."
  validation {
    condition     = can(regex("^ocid1\\.(compartment|tenancy)\\.", var.compartment_ocid))
    error_message = "Use a compartment or tenancy OCID."
  }
}
variable "target_compartment_ocid" {
  type        = string
  default     = ""
  description = "Compartment where resources are deployed and the root of the workload compartment tree to scan. Empty preserves compatibility by using compartment_ocid."
  validation {
    condition     = var.target_compartment_ocid == "" || can(regex("^ocid1\\.(compartment|tenancy)\\.", var.target_compartment_ocid))
    error_message = "Use a compartment or tenancy OCID."
  }
}
variable "region" {
  type        = string
  description = "OCI region where the Function and supporting resources are deployed."
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z0-9-]+-[0-9]+$", var.region))
    error_message = "Select a valid OCI region such as us-ashburn-1."
  }
}
variable "workload_regions" {
  type        = string
  default     = ""
  description = "Empty: deployment region. CSV: selected regions. all: every READY subscribed region."
  validation {
    condition     = var.workload_regions == "" || var.workload_regions == "all" || can(regex("^[a-z][a-z0-9-]*(,[a-z][a-z0-9-]*)*$", var.workload_regions))
    error_message = "Use region names separated by commas without spaces, all, or an empty string."
  }
}
variable "build_function_image" {
  type    = bool
  default = true
}
variable "source_repository_url" {
  type    = string
  default = "https://github.com/gautamm2707/osmh_onboarding.git"
  validation {
    condition     = can(regex("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\\.git)?$", var.source_repository_url))
    error_message = "Use an HTTPS GitHub repository URL without credentials or query parameters."
  }
}
variable "source_branch" {
  type    = string
  default = "main"
}
variable "source_commit" {
  type        = string
  default     = "5cc748857a4a7029d035d9c3a3b8c0539b8f49e4"
  description = "Published 40-character commit SHA containing function/Dockerfile and build_spec.yaml. The Resource Manager form pins and hides this value."
}
variable "github_token_secret_id" {
  type        = string
  default     = ""
  description = "OCID of an existing OCI Vault secret containing a GitHub PAT; never enter the token itself. Required for cloud builds."
}
variable "build_revision" {
  type        = string
  default     = "1"
  description = "Increment to request a new build and image tag for the same source commit."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$", var.build_revision))
    error_message = "Use 1-32 letters, numbers, dots, hyphens or underscores, beginning with a letter or number."
  }
}
variable "existing_image" {
  type        = string
  default     = ""
  description = "Full deployment-region OCIR image URI when cloud building is disabled; use a unique version tag."
}
variable "create_network" {
  type    = bool
  default = true
}
variable "subnet_ids" {
  type    = list(string)
  default = []
}
variable "application_id" {
  type        = string
  default     = ""
  description = "Optional existing GENERIC_X86 Function application in the stack compartment and deployment region."
  validation {
    condition     = var.application_id == "" || can(regex("^ocid1\\.fnapp\\.", var.application_id))
    error_message = "Use a Function application OCID."
  }
}
variable "tag_namespace" {
  type        = string
  default     = "OSMH"
  description = "New namespace prefix (first 67 characters plus a per-stack suffix), or the exact name when reusing a namespace. Existing managed namespace names are preserved on upgrades."
  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,99}$", var.tag_namespace))
    error_message = "Use a namespace starting with a letter, followed by letters, digits or underscores (max 100)."
  }
}
variable "use_existing_tag_namespace" {
  type    = bool
  default = false
}
variable "existing_tag_namespace_id" {
  type    = string
  default = ""
}
variable "create_instance_iam" {
  type        = bool
  default     = true
  description = "Create a tag-based Compute dynamic group and OSMH access policy. Disable only if an administrator has already provided equivalent access."
}
variable "repository_families" {
  type    = string
  default = "uek,ksplice,mysql,oci"
  validation {
    condition     = var.repository_families == "none" || alltrue([for item in split(",", var.repository_families) : contains(["uek", "ksplice", "mysql", "oci"], item)])
    error_message = "Use a CSV of uek,ksplice,mysql,oci or none."
  }
}
variable "group_prefix" {
  type    = string
  default = "osmh"
}
variable "schedule_enabled" {
  type    = bool
  default = true
}
variable "logging_enabled" {
  type        = bool
  default     = true
  description = "Create the application's invocation log. Disable when reusing an application that already has one."
}
variable "invoke_after_deploy" {
  type    = bool
  default = true
}
variable "iam_wait_seconds" {
  type        = number
  default     = 3600
  description = "Maximum time to poll OCI DevOps connection validation while IAM and dynamic-group changes propagate. Validation proceeds immediately when the connection is ready."
  validation {
    condition     = var.iam_wait_seconds >= 60 && var.iam_wait_seconds <= 3600 && floor(var.iam_wait_seconds) == var.iam_wait_seconds
    error_message = "Use an integer from 60 to 3600 seconds."
  }
}

variable "runtime_iam_wait_seconds" {
  type        = number
  default     = 120
  description = "Short initial wait before the detached Function invocation. Increase only if Function logs show that newly created operational IAM policies have not propagated."
  validation {
    condition     = var.runtime_iam_wait_seconds >= 0 && var.runtime_iam_wait_seconds <= 3600 && floor(var.runtime_iam_wait_seconds) == var.runtime_iam_wait_seconds
    error_message = "Use an integer from 0 to 3600 seconds."
  }
}

variable "build_timeout_seconds" {
  type        = number
  default     = 7200
  description = "Maximum total time for OCI DevOps build attempts, including retries for transient source and IAM propagation failures."
  validation {
    condition     = var.build_timeout_seconds >= 1800 && var.build_timeout_seconds <= 10800 && floor(var.build_timeout_seconds) == var.build_timeout_seconds
    error_message = "Use an integer from 1800 to 10800 seconds."
  }
}

variable "selected_instance_ids" {
  type        = list(string)
  default     = []
  description = "Existing Compute instances in the selected compartment and region to opt in during Apply. Removing a selection does not remove its tag or registration."
  validation {
    condition     = alltrue([for id in var.selected_instance_ids : can(regex("^ocid1\\.instance\\.[A-Za-z0-9._-]+$", id))])
    error_message = "Select valid Compute instance OCIDs."
  }
}
variable "create_function_application" {
  type        = bool
  default     = null
  description = "Create an application or reuse application_id. Null preserves the earlier automatic choice for existing callers."
}
variable "existing_vcn_id" {
  type    = string
  default = ""
}
variable "existing_subnet_id" {
  type    = string
  default = ""
}
variable "schedule_time_utc" {
  type        = string
  default     = "16:30"
  description = "Daily start time in UTC, HH:MM (24-hour clock)."
  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.schedule_time_utc))
    error_message = "Use a valid 24-hour UTC time, for example 16:30."
  }
}
