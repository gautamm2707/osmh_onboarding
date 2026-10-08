# Terraform >= 1.14 locally. OCI is mocked; random_id uses the real provider.
# Separate state keys model independent stacks in the same compartment/region.
mock_provider "oci" {
  mock_data "oci_identity_region_subscriptions" {
    defaults = {
      region_subscriptions = [{ region_name = "us-ashburn-1", region_key = "IAD", is_home_region = true, state = "READY" }]
    }
  }
  mock_data "oci_objectstorage_namespace" { defaults = { namespace = "testnamespace" } }
}
mock_provider "oci" { alias = "home" }

variables {
  tenancy_ocid           = "ocid1.tenancy.oc1..testtenancy"
  compartment_ocid       = "ocid1.compartment.oc1..testscope"
  region                 = "us-ashburn-1"
  github_token_secret_id = "ocid1.vaultsecret.oc1.iad.test"
  invoke_after_deploy    = false
}

run "first_stack" {
  command   = apply
  state_key = "first"
  # Exercise the identity and infrastructure graph without running the build,
  # IAM propagation provisioners or initial Function invocation.
  plan_options {
    target = [oci_devops_project.build, oci_functions_application.worker, oci_core_vcn.function, oci_artifacts_container_repository.worker, oci_identity_dynamic_group.instances]
  }
  assert {
    condition = (
      length(output.deployment_id) == 32 &&
      strcontains(oci_devops_project.build[0].name, output.deployment_id) &&
      strcontains(oci_functions_application.worker[0].display_name, output.deployment_id) &&
      strcontains(oci_core_vcn.function[0].display_name, output.deployment_id) &&
      strcontains(oci_artifacts_container_repository.worker[0].display_name, output.deployment_id) &&
      strcontains(oci_identity_dynamic_group.instances[0].name, output.deployment_id)
    )
    error_message = "A fresh stack must use its own persisted deployment identity for infrastructure names."
  }
  assert {
    condition = (
      output.instance_opt_in_tag == "OSMH_${output.deployment_id}.managedby=osmanagementhub" &&
      strcontains(oci_identity_dynamic_group.instances[0].matching_rule, oci_identity_tag_namespace.opt_in[0].name) &&
      startswith(output.function_image, "iad.ocir.io/testnamespace/${oci_artifacts_container_repository.worker[0].display_name}:")
    )
    error_message = "Tagging, IAM and image references must use the actual namespace and repository names."
  }
}

run "second_stack_same_compartment" {
  command   = apply
  state_key = "second"
  plan_options {
    target = [oci_devops_project.build, oci_functions_application.worker, oci_core_vcn.function, oci_artifacts_container_repository.worker, oci_identity_dynamic_group.instances]
  }
  assert {
    condition = (
      output.deployment_id != run.first_stack.deployment_id &&
      output.instance_opt_in_tag != run.first_stack.instance_opt_in_tag &&
      output.function_image != run.first_stack.function_image
    )
    error_message = "Independent stacks in the same compartment and region must not share generated identities, namespaces or image repositories."
  }
}

run "retry_first_stack" {
  command   = plan
  state_key = "first"
  assert {
    condition = (
      output.deployment_id == run.first_stack.deployment_id &&
      output.instance_opt_in_tag == run.first_stack.instance_opt_in_tag &&
      output.function_image == run.first_stack.function_image &&
      oci_functions_function.worker.display_name == "onboard-tagged-instances-${run.first_stack.deployment_id}" &&
      oci_functions_function.worker.config.OSMH_TAG_NAMESPACE == oci_identity_tag_namespace.opt_in[0].name
    )
    error_message = "A retry of the same stack must retain the identity and resources saved in its state."
  }
}
