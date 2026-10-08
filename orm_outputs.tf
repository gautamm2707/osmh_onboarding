output "function_id" { value = oci_functions_function.worker.id }
output "schedule_id" { value = oci_resource_scheduler_schedule.nightly.id }
output "function_image" { value = local.image }
output "log_group_id" { value = oci_logging_log_group.worker.id }
output "build_pipeline_id" { value = try(oci_devops_build_pipeline.image[0].id, null) }
output "build_run_id" { value = try(oci_devops_build_run.image[0].id, null) }
output "tag_namespace_id" {
  value = local.tag_namespace_id
}
output "instance_opt_in_tag" { value = "${local.namespace}.managedby=osmanagementhub" }
output "deployment_id" {
  description = "Stable per-stack identifier used for newly created resource names. Preserve this stack's state for retries and updates."
  value       = random_id.deployment.hex
}
output "function_subnet_ids" { value = !local.create_application ? data.oci_functions_application.existing[0].subnet_ids : (local.create_new_network ? [oci_core_subnet.function[0].id] : local.existing_subnets) }
output "schedule_utc" { value = "${local.schedule_cron} (${var.schedule_time_utc} UTC daily)" }
output "next_steps" {
  value = "Apply starts a detached job to tag selected instances and reconcile onboarding. To opt in more instances later, apply the defined tag ${local.namespace}.managedby=osmanagementhub to supported running non-OKE instances in the selected compartment tree/workload regions. Invoke the Function in detached mode or wait for the daily schedule. Inspect OSMH registration and Function logs; Apply success is not guest registration confirmation."
}

output "selected_instance_ids" { value = local.selected_ids }
