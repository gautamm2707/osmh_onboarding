resource "oci_functions_application" "worker" {
  depends_on     = [terraform_data.validate, data.oci_core_subnet.existing]
  count          = local.create_application ? 1 : 0
  compartment_id = local.target_compartment_id
  display_name   = local.name
  subnet_ids     = local.create_new_network ? [oci_core_subnet.function[0].id] : local.existing_subnets
  shape          = "GENERIC_X86"

}

resource "oci_functions_function" "worker" {
  depends_on                       = [terraform_data.validate, data.oci_functions_application.existing, oci_identity_policy.image_access, oci_devops_build_run.image, data.oci_core_instance.selected]
  application_id                   = local.application_id
  display_name                     = "onboard-tagged-instances"
  image                            = local.image
  memory_in_mbs                    = 1024
  timeout_in_seconds               = 300
  detached_mode_timeout_in_seconds = 3600
  config = {
    OSMH_COMPARTMENT_ID      = local.target_compartment_id
    OSMH_TAG_NAMESPACE       = local.namespace
    OSMH_REGIONS             = var.workload_regions == "" ? var.region : var.workload_regions
    OSMH_SELECTION_REGION    = var.region
    OSMH_SELECTION_SHA256    = sha256(jsonencode(local.selected_ids))
    OSMH_SKIP_IAM            = "true"
    OSMH_REPOSITORY_FAMILIES = var.repository_families
    OSMH_GROUP_PREFIX        = var.group_prefix
  }
}

resource "oci_identity_policy" "image_access" {
  provider       = oci.home
  depends_on     = [terraform_data.validate]
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-image"
  description    = "Functions service access to this deployment's image repository"
  statements = [
    "Allow service FaaS to read repos in tenancy where target.repo.name = '${local.repository}'"
  ]
}

resource "oci_identity_policy" "worker" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-function"
  description    = "Operational access for the exact OSMH function; IAM is owned by the stack"
  statements = [
    "Allow any-user to inspect tenancies in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read compartments in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read instance-images in tenancy where all {${local.worker_condition}}",
    "Allow any-user to read instances ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to {INSTANCE_UPDATE} ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to read cluster-family ${local.scope} where all {${local.worker_condition}}",
    "Allow any-user to use tag-namespaces in tenancy where all {${local.worker_condition}, target.tag-namespace.id = '${local.tag_namespace_id}'}",
    "Allow any-user to manage osmh-family in tenancy where all {${local.worker_condition}}"

  ]
}

resource "oci_resource_scheduler_schedule" "nightly" {
  compartment_id     = local.target_compartment_id
  display_name       = "${local.name}-daily"
  description        = "Daily tagged-instance OSMH reconciliation at ${var.schedule_time_utc} UTC"
  action             = "START_RESOURCE"
  recurrence_type    = "CRON"
  recurrence_details = local.schedule_cron
  state              = var.schedule_enabled ? "ACTIVE" : "INACTIVE"
  resources {
    id = oci_functions_function.worker.id
  }
}

resource "oci_identity_policy" "scheduler" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-schedule"
  description    = "Permit only this schedule to invoke the OSMH function"
  statements = [
    "Allow any-user to use functions-family ${local.scope} where all {request.principal.type = 'resourceschedule', request.principal.id = '${oci_resource_scheduler_schedule.nightly.id}', target.function.id = '${oci_functions_function.worker.id}'}"
  ]
}

resource "oci_logging_log_group" "worker" {
  depends_on     = [terraform_data.validate]
  compartment_id = local.target_compartment_id
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
    compartment_id = local.target_compartment_id
    source {
      category    = "invoke"
      resource    = local.application_id
      service     = "functions"
      source_type = "OCISERVICE"
    }
  }
}


# Apply starts an asynchronous reconciliation; it does not wait for guest registration.
resource "terraform_data" "runtime_iam" {
  depends_on = [oci_identity_policy.worker, oci_identity_policy.instances, oci_identity_policy.scheduler]
  provisioner "local-exec" {
    command = "sleep ${var.iam_wait_seconds}"
  }
  triggers_replace = {
    worker_policy   = sha256(jsonencode(oci_identity_policy.worker.statements))
    instance_policy = sha256(jsonencode(oci_identity_policy.instances[*].statements))
  }
}
resource "oci_functions_invoke_function" "initial" {
  count                = (var.invoke_after_deploy || length(local.selected_ids) > 0) ? 1 : 0
  depends_on           = [terraform_data.runtime_iam, oci_logging_log.worker, oci_identity_tag.opt_in, data.oci_identity_tag.existing]
  function_id          = oci_functions_function.worker.id
  fn_invoke_type       = "detached"
  invoke_function_body = length(local.selected_ids) > 0 ? jsonencode({ onboard_instance_ids = local.selected_ids }) : "{}"
  lifecycle {
    replace_triggered_by = [oci_functions_function.worker]
  }
}

data "oci_functions_application" "existing" {
  count          = local.create_application ? 0 : 1
  application_id = var.application_id
  lifecycle {
    postcondition {
      condition     = self.compartment_id == local.target_compartment_id && self.shape == "GENERIC_X86" && self.state == "ACTIVE"
      error_message = "The existing application must be ACTIVE, GENERIC_X86, and in the selected compartment and region."
    }
  }
}

# Existing Compute instances remain outside Terraform ownership.
data "oci_core_instance" "selected" {
  for_each    = toset(local.selected_ids)
  instance_id = each.value
  lifecycle {
    postcondition {
      condition     = self.compartment_id == local.target_compartment_id && self.state == "RUNNING"
      error_message = "Select RUNNING instances from the selected compartment and region. The worker also checks OS support and excludes OKE nodes before tagging."
    }
  }
}
