output "function_id" { value = oci_functions_function.worker.id }
output "schedule_id" { value = oci_resource_scheduler_schedule.nightly.id }
output "function_image" { value = local.image }
output "log_group_id" { value = oci_logging_log_group.worker.id }
output "build_pipeline_id" { value = try(oci_devops_build_pipeline.image[0].id, null) }
output "build_run_id" {
  description = "Terraform build execution checkpoint. OCI DevOps build run OCIDs are printed in the Apply log."
  value       = try(terraform_data.build_run[0].id, null)
}
output "tag_namespace_id" {
  value = local.tag_namespace_id
}
output "tag_default_id" {
  value = try(oci_identity_tag_default.new_opt_in[0].id, oci_identity_tag_default.existing_opt_in[0].id, data.oci_identity_tag_defaults.existing_opt_in[0].tag_defaults[0].id, null)
}
output "instance_opt_in_tag" { value = "${local.namespace}.managedby=osmanagementhub" }
output "deployment_id" {
  description = "Stable per-stack identifier used for newly created resource names. Preserve this stack's state for retries and updates."
  value       = random_id.deployment.hex
}
output "function_subnet_ids" { value = !local.create_application ? data.oci_functions_application.existing[0].subnet_ids : (local.create_new_network ? [oci_core_subnet.function[0].id] : local.existing_subnets) }
output "schedule_utc" { value = "${local.schedule_cron} (${var.schedule_time_utc} UTC daily)" }
output "next_steps" {
  value = "Apply starts a detached job to tag the chosen eligible instances and reconcile onboarding. The compartment tag default applies ${local.namespace}.managedby=osmanagementhub to future resources in the selected compartment tree, and scheduled runs evaluate tagged Compute instances. Inspect OSMH registration and Function logs; Apply success is not guest registration confirmation."
}

output "selected_instance_ids" { value = local.selected_ids }
