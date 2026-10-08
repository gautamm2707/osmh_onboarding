# Run with Terraform >= 1.7. All OCI responses are mocked; no credentials or cloud writes.
mock_provider "oci" {
  mock_data "oci_identity_region_subscriptions" {
    defaults = {
      region_subscriptions = [
        { region_name = "us-ashburn-1", region_key = "IAD", is_home_region = true, state = "READY" },
        { region_name = "us-phoenix-1", region_key = "PHX", is_home_region = false, state = "READY" }
      ]
    }
  }
  mock_data "oci_objectstorage_namespace" {
    defaults = { namespace = "testnamespace" }
  }
  mock_data "oci_functions_application" {
    defaults = { compartment_id = "ocid1.compartment.oc1..testscope", shape = "GENERIC_X86", state = "ACTIVE" }
  }
  mock_data "oci_core_instance" {
    defaults = { compartment_id = "ocid1.compartment.oc1..testscope", state = "RUNNING" }
  }
  mock_data "oci_core_subnet" {
    defaults = { compartment_id = "ocid1.compartment.oc1..testscope", state = "AVAILABLE", availability_domain = "", vcn_id = "ocid1.vcn.oc1.iad.existing" }
  }

}
mock_provider "oci" {
  alias = "home"
  mock_data "oci_identity_tag_namespaces" {
    defaults = { tag_namespaces = [{ id = "ocid1.tagnamespace.oc1..existing", name = "OSMH", is_retired = false, state = "ACTIVE" }] }
  }
  mock_data "oci_identity_tag" {
    defaults = {
      name       = "managedby"
      state      = "ACTIVE"
      is_retired = false
      validator  = [{ validator_type = "ENUM", values = ["osmanagementhub"] }]
    }
  }
}

variables {
  tenancy_ocid         = "ocid1.tenancy.oc1..testtenancy"
  compartment_ocid     = "ocid1.compartment.oc1..testscope"
  region               = "us-ashburn-1"
  build_function_image = false
  existing_image       = "iad.ocir.io/testnamespace/osmh/worker:v1"
}
run "existing_image_deployment" {
  command = plan
  assert {
    condition     = length(oci_devops_build_run.image) == 0 && length(oci_devops_connection.github) == 0
    error_message = "Existing-image mode must not create builds or GitHub connections."
  }
  assert {
    condition     = oci_functions_function.worker.config.OSMH_SKIP_IAM == "true"
    error_message = "The worker must not manage IAM."
  }
  assert {
    condition     = length(oci_identity_dynamic_group.instances) == 1 && length(oci_core_subnet.function) == 1
    error_message = "Default deployment must provision instance access and private networking."
  }
}
run "target_compartment_is_independent_of_stack_compartment" {
  command = plan
  variables {
    target_compartment_ocid = "ocid1.compartment.oc1..workloads"
  }
  assert {
    condition     = oci_functions_application.worker[0].compartment_id == "ocid1.compartment.oc1..workloads" && oci_core_vcn.function[0].compartment_id == "ocid1.compartment.oc1..workloads"
    error_message = "Deployment resources must use the target compartment rather than the stack storage compartment."
  }
}
run "cloud_build_deployment" {
  command = plan
  variables {
    build_function_image   = true
    source_commit          = "0123456789abcdef0123456789abcdef01234567"
    github_token_secret_id = "ocid1.vaultsecret.oc1.iad.test"
  }
  assert {
    condition     = length(oci_devops_build_run.image) == 1 && oci_artifacts_container_repository.worker[0].is_public == false && oci_devops_build_pipeline_stage.build[0].image == "OL8_X86_64_STANDARD_10"
    error_message = "Cloud building must create one OL8 build and a private image repository."
  }
  assert {
    condition     = endswith(output.function_image, ":0123456789ab-1")
    error_message = "Image version must identify the source commit and build revision."
  }
}
run "deploy_in_non_home_region" {
  command = plan
  variables {
    region         = "us-phoenix-1"
    existing_image = "phx.ocir.io/testnamespace/osmh/worker:v1"
  }
  assert {
    condition     = oci_functions_function.worker.config.OSMH_REGIONS == "us-phoenix-1"
    error_message = "Default workload scope must follow the deployment region."
  }
}
run "reject_unresolved_region_placeholder" {
  command = plan
  variables { region = "$${session.region}" }
  expect_failures = [var.region]
}
run "reject_wrong_registry_tenancy" {
  command = plan
  variables { existing_image = "iad.ocir.io/othertenancy/osmh/worker:v1" }
  expect_failures = [terraform_data.validate]
}
run "reject_unpinned_cloud_source" {
  command = plan
  variables {
    build_function_image   = true
    source_commit          = ""
    github_token_secret_id = "ocid1.vaultsecret.oc1.iad.test"
  }
  expect_failures = [terraform_data.validate]
}
run "reuse_namespace_without_owning_it" {
  command = plan
  variables {
    use_existing_tag_namespace = true
    existing_tag_namespace_id  = "ocid1.tagnamespace.oc1..existing"
  }
  assert {
    condition     = length(oci_identity_tag_namespace.opt_in) == 0 && length(oci_identity_tag.opt_in) == 0
    error_message = "Reused namespaces and keys must not be created or managed by this stack."
  }
}
run "ignore_hidden_existing_network_when_creating_new" {
  command = plan
  variables { existing_subnet_id = "ocid1.subnet.oc1.iad.stale" }
  assert {
    condition     = length(data.oci_core_subnet.existing) == 0 && length(oci_core_vcn.function) == 1
    error_message = "Hidden controls must not override the new-network choice."
  }
}
run "reuse_existing_application" {
  command = plan
  variables {
    application_id              = "ocid1.fnapp.oc1.iad.existing"
    create_function_application = false
    create_network              = true # Hidden stale default must not create a network.
    logging_enabled             = false
  }
  assert {
    condition     = length(oci_functions_application.worker) == 0 && length(oci_core_vcn.function) == 0 && length(oci_logging_log.worker) == 0
    error_message = "Reusing an application must not create an application, network, or disabled log."
  }
}

run "reuse_existing_network" {
  command = plan
  variables {
    create_function_application = true
    create_network              = false
    existing_vcn_id             = "ocid1.vcn.oc1.iad.existing"
    existing_subnet_id          = "ocid1.subnet.oc1.iad.existing"
  }
  assert {
    condition     = length(oci_core_vcn.function) == 0 && oci_functions_application.worker[0].subnet_ids == tolist(["ocid1.subnet.oc1.iad.existing"])
    error_message = "The new application must use the selected existing subnet."
  }
}
run "reject_subnet_from_another_vcn" {
  command = plan
  variables {
    create_network     = false
    existing_vcn_id    = "ocid1.vcn.oc1.iad.other"
    existing_subnet_id = "ocid1.subnet.oc1.iad.existing"
  }
  expect_failures = [data.oci_core_subnet.existing]
}
run "selected_instances_are_bound_to_initial_invocation" {
  command = plan
  variables {
    selected_instance_ids = ["ocid1.instance.oc1.iad.second", "ocid1.instance.oc1.iad.first"]
    invoke_after_deploy   = false
    schedule_time_utc     = "05:15"
  }
  assert {
    condition     = oci_functions_invoke_function.initial[0].invoke_function_body == jsonencode({ onboard_instance_ids = ["ocid1.instance.oc1.iad.first", "ocid1.instance.oc1.iad.second"] }) && oci_functions_function.worker.config.OSMH_SELECTION_SHA256 == sha256(jsonencode(["ocid1.instance.oc1.iad.first", "ocid1.instance.oc1.iad.second"]))
    error_message = "The invocation and authorization digest must cover the same exact selection."
  }
  assert {
    condition     = oci_resource_scheduler_schedule.nightly.recurrence_details == "15 5 * * *"
    error_message = "The schedule must use the selected UTC time."
  }
}
run "reject_instance_in_other_compartment" {
  command = plan
  variables { selected_instance_ids = ["ocid1.instance.oc1.iad.wrong"] }
  override_data {
    target = data.oci_core_instance.selected["ocid1.instance.oc1.iad.wrong"]
    values = { compartment_id = "ocid1.compartment.oc1..other", state = "RUNNING" }
  }
  expect_failures = [data.oci_core_instance.selected]
}
run "reject_stopped_instance" {
  command = plan
  variables { selected_instance_ids = ["ocid1.instance.oc1.iad.stopped"] }
  override_data {
    target = data.oci_core_instance.selected["ocid1.instance.oc1.iad.stopped"]
    values = { compartment_id = "ocid1.compartment.oc1..testscope", state = "STOPPED" }
  }
  expect_failures = [data.oci_core_instance.selected]
}
run "reject_invalid_utc_time" {
  command = plan
  variables { schedule_time_utc = "24:00" }
  expect_failures = [var.schedule_time_utc]
}
