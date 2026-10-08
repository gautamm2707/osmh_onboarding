# OCI DevOps builds inside OCI and delivers through a resource principal.
# Only the existing Vault secret OCID enters Terraform; its contents are never read here.
resource "oci_ons_notification_topic" "build" {
  count          = var.build_function_image ? 1 : 0
  depends_on     = [terraform_data.validate]
  compartment_id = local.target_compartment_id
  name           = "${local.name}-build"
}
resource "oci_devops_project" "build" {
  count          = var.build_function_image ? 1 : 0
  compartment_id = local.target_compartment_id
  name           = "${local.name}-build"
  notification_config { topic_id = oci_ons_notification_topic.build[0].id }
}
resource "oci_logging_log" "build" {
  count              = var.build_function_image ? 1 : 0
  display_name       = "devops-builds"
  log_group_id       = oci_logging_log_group.worker.id
  log_type           = "SERVICE"
  is_enabled         = true
  retention_duration = 30
  configuration {
    compartment_id = local.target_compartment_id
    source {
      category    = "all"
      resource    = oci_devops_project.build[0].id
      service     = "devops"
      source_type = "OCISERVICE"
    }
  }
}
resource "oci_devops_connection" "github" {
  count           = var.build_function_image ? 1 : 0
  project_id      = oci_devops_project.build[0].id
  connection_type = "GITHUB_ACCESS_TOKEN"
  access_token    = var.github_token_secret_id
  display_name    = "osmh-source"
}
resource "oci_devops_build_pipeline" "image" {
  count        = var.build_function_image ? 1 : 0
  project_id   = oci_devops_project.build[0].id
  display_name = "build-osmh-function"
}
resource "oci_artifacts_container_repository" "worker" {
  count          = var.build_function_image ? 1 : 0
  depends_on     = [terraform_data.validate]
  compartment_id = local.target_compartment_id
  display_name   = local.repository
  is_public      = false
}
resource "oci_identity_dynamic_group" "build" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-build"
  description    = "Only this OSMH build pipeline and GitHub connection"
  matching_rule  = "ANY {ALL {resource.type = 'devopsbuildpipeline', resource.id = '${oci_devops_build_pipeline.image[0].id}'}, ALL {resource.type = 'devopsconnection', resource.id = '${oci_devops_connection.github[0].id}'}}"
}
resource "oci_identity_policy" "build" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-build"
  description    = "Read one GitHub secret and deliver the OSMH image to one private repository"
  statements = [
    # OCI DevOps evaluates source retrieval as both the build-pipeline and
    # external-connection principals. These documented compartment-scoped
    # grants work for both authorization checks; target-resource conditions
    # are not consistently populated while DevOps fetches an external source.
    "Allow dynamic-group id ${oci_identity_dynamic_group.build[0].id} to read secret-family ${local.scope}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build[0].id} to manage devops-family ${local.scope}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build[0].id} to use ons-topics ${local.scope}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build[0].id} to manage repos ${local.scope}"
  ]
}
resource "oci_devops_deploy_artifact" "image" {
  count                      = var.build_function_image ? 1 : 0
  project_id                 = oci_devops_project.build[0].id
  display_name               = "osmh-function-image"
  deploy_artifact_type       = "DOCKER_IMAGE"
  argument_substitution_mode = "NONE"
  deploy_artifact_source {
    deploy_artifact_source_type = "OCIR"
    image_uri                   = local.built_image
  }
}
resource "oci_devops_build_pipeline_stage" "build" {
  count                              = var.build_function_image ? 1 : 0
  build_pipeline_id                  = oci_devops_build_pipeline.image[0].id
  display_name                       = "Build Linux AMD64 function"
  build_pipeline_stage_type          = "BUILD"
  image                              = "OL8_X86_64_STANDARD_10"
  build_spec_file                    = "build_spec.yaml"
  primary_build_source               = "osmh"
  stage_execution_timeout_in_seconds = 1800
  build_pipeline_stage_predecessor_collection {
    items { id = oci_devops_build_pipeline.image[0].id }
  }
  build_source_collection {
    items {
      connection_type = "GITHUB"
      connection_id   = oci_devops_connection.github[0].id
      repository_url  = var.source_repository_url
      branch          = var.source_branch
      name            = "osmh"
    }
  }
}
resource "oci_devops_build_pipeline_stage" "deliver" {
  count                     = var.build_function_image ? 1 : 0
  build_pipeline_id         = oci_devops_build_pipeline.image[0].id
  display_name              = "Deliver to private OCIR repository"
  build_pipeline_stage_type = "DELIVER_ARTIFACT"
  build_pipeline_stage_predecessor_collection {
    items { id = oci_devops_build_pipeline_stage.build[0].id }
  }
  deliver_artifact_collection {
    items {
      artifact_id   = oci_devops_deploy_artifact.image[0].id
      artifact_name = "osmh-function-image"
    }
  }
}
resource "terraform_data" "build_iam" {
  count      = var.build_function_image ? 1 : 0
  depends_on = [oci_identity_policy.build]
  provisioner "local-exec" {
    command = "sleep ${var.iam_wait_seconds}"
  }
  triggers_replace = { policy = sha256(jsonencode(oci_identity_policy.build[0].statements)) }
}
resource "terraform_data" "build_version" {
  count = var.build_function_image ? 1 : 0
  input = {
    commit     = var.source_commit
    revision   = var.build_revision
    repository = var.source_repository_url
    branch     = var.source_branch
  }
}
resource "oci_devops_build_run" "image" {
  count             = var.build_function_image ? 1 : 0
  depends_on        = [terraform_data.build_iam, oci_devops_build_pipeline_stage.deliver, oci_logging_log.build]
  build_pipeline_id = oci_devops_build_pipeline.image[0].id
  display_name      = "osmh-${local.image_tag}"
  commit_info {
    commit_hash       = var.source_commit
    repository_branch = var.source_branch
    repository_url    = var.source_repository_url
  }
  timeouts { create = "60m" }
  lifecycle {
    replace_triggered_by = [terraform_data.build_version[0]]
  }
}
