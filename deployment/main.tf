terraform {
  required_version = ">= 1.5.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 7.0.0, < 9.0.0"
    }
  }
}

provider "oci" {
  region              = var.region
  config_file_profile = var.profile
  auth                = var.auth
}

provider "oci" {
  alias               = "home"
  region              = var.home_region
  config_file_profile = var.profile
  auth                = var.auth
}

locals {
  suffix         = substr(var.compartment_id, length(var.compartment_id) - 12, 12)
  name           = "osmh-tags-${local.suffix}-${var.region}"
  scope          = var.compartment_id == var.tenancy_id ? "in tenancy" : "in compartment id ${var.compartment_id}"
  namespace      = var.tag_namespace == "" ? "OSMH" : var.tag_namespace
  repository     = regex("^[^/]+/[^/]+/(.+):[^:]+$", var.image)[0]
  application_id = var.application_id != "" ? var.application_id : oci_functions_application.worker[0].id
  # Bootstrap is assigned to this exact function resource principal, never to opted-in Compute nodes.
  worker_condition = "request.principal.type = 'fnfunc', request.principal.id = '${oci_functions_function.worker.id}'"
}

resource "oci_functions_application" "worker" {
  count          = var.application_id == "" ? 1 : 0
  compartment_id = var.compartment_id
  display_name   = local.name
  subnet_ids     = var.create_network ? [oci_core_subnet.function[0].id] : var.subnet_ids
  shape          = "GENERIC_X86"
  lifecycle {
    precondition {
      condition     = var.application_id != "" || var.create_network != (length(var.subnet_ids) > 0)
      error_message = "Choose exactly one of create_network=true, existing subnet_ids, or application_id."
    }
    precondition {
      condition     = var.application_id == "" || (!var.create_network && length(var.subnet_ids) == 0)
      error_message = "Do not pass subnet_ids/create_network when reusing an existing Function application."
    }
  }
}

resource "oci_functions_function" "worker" {
  depends_on                       = [oci_identity_policy.image_access]
  application_id                   = local.application_id
  display_name                     = "onboard-tagged-instances"
  image                            = var.image
  memory_in_mbs                    = 1024
  timeout_in_seconds               = 300
  detached_mode_timeout_in_seconds = 3600
  config = {
    OSMH_COMPARTMENT_ID      = var.compartment_id
    OSMH_TAG_NAMESPACE       = local.namespace
    OSMH_REGIONS             = var.workload_regions
    OSMH_SKIP_IAM            = var.bootstrap_iam ? "false" : "true"
    OSMH_REPOSITORY_FAMILIES = var.repository_families
    OSMH_GROUP_PREFIX        = var.group_prefix
  }
}

resource "oci_identity_policy" "image_access" {
  provider       = oci.home
  compartment_id = var.tenancy_id
  name           = "${local.name}-image"
  description    = "Functions service access to this deployment's image repository"
  statements = [
    "Allow service FaaS to read repos in tenancy where target.repo.name = '${local.repository}'"
  ]
}

resource "oci_identity_policy" "worker" {
  provider       = oci.home
  compartment_id = var.tenancy_id
  name           = "${local.name}-function"
  description    = "Bootstrap and operational access for the exact OSMH function principal"
  statements = concat([
    "Allow any-user to inspect tenancies in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read compartments in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read instance-images in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read instances ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to {INSTANCE_UPDATE} ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to read cluster-family ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to use tag-namespaces ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to manage osmh-family in tenancy where all {${local.worker_condition}}"
    ], var.bootstrap_iam ? [
    "Allow any-user to {GROUP_INSPECT, GROUP_CREATE} in tenancy where all {${local.worker_condition}}",
    "Allow any-user to {DYNAMIC_GROUP_INSPECT, DYNAMIC_GROUP_CREATE} in tenancy where all {${local.worker_condition}}",
    "Allow any-user to {POLICY_READ, POLICY_CREATE, POLICY_UPDATE} in tenancy where all {${local.worker_condition}}"
  ] : [])
}

resource "oci_resource_scheduler_schedule" "nightly" {
  compartment_id     = var.compartment_id
  display_name       = "${local.name}-2200-ist"
  description        = "Daily tagged-instance OSMH reconciliation: 22:00 Asia/Kolkata / 16:30 UTC"
  action             = "START_RESOURCE"
  recurrence_type    = "CRON"
  recurrence_details = "30 16 * * *"
  state              = var.schedule_enabled ? "ACTIVE" : "INACTIVE"
  resources {
    id = oci_functions_function.worker.id
  }
}

resource "oci_identity_policy" "scheduler" {
  provider       = oci.home
  compartment_id = var.tenancy_id
  name           = "${local.name}-schedule"
  description    = "Permit only this schedule to invoke the OSMH function"
  statements = [
    "Allow any-user to use functions-family ${local.scope} where all {request.principal.type = 'resourceschedule', request.principal.id = '${oci_resource_scheduler_schedule.nightly.id}', target.function.id = '${oci_functions_function.worker.id}'}"
  ]
}

resource "oci_logging_log_group" "worker" {
  compartment_id = var.compartment_id
  display_name   = local.name
}

resource "oci_logging_log" "worker" {
  count              = var.logging_enabled ? 1 : 0
  display_name       = "function-invocations"
  log_group_id       = oci_logging_log_group.worker.id
  log_type           = "SERVICE"
  is_enabled         = true
  retention_duration = 30
  configuration {
    compartment_id = var.compartment_id
    source {
      category    = "invoke"
      resource    = local.application_id
      service     = "functions"
      source_type = "OCISERVICE"
    }
  }
}

output "function_id" { value = oci_functions_function.worker.id }
output "schedule_id" { value = oci_resource_scheduler_schedule.nightly.id }
output "tag_namespace" { value = local.namespace }
output "log_group_id" { value = oci_logging_log_group.worker.id }
output "schedule_utc" { value = "30 16 * * * (22:00 IST daily)" }
