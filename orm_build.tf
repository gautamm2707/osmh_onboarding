# OCI DevOps builds inside OCI and delivers through a resource principal.
# Terraform stores only the existing Vault secret OCID. The preflight reads the
# current token into process memory and never writes or prints it.
resource "oci_ons_notification_topic" "build" {
  count          = var.build_function_image ? 1 : 0
  depends_on     = [terraform_data.validate]
  compartment_id = local.target_compartment_id
  name           = "${local.name}-build"
  lifecycle { ignore_changes = [name] }
}
resource "oci_devops_project" "build" {
  count          = var.build_function_image ? 1 : 0
  compartment_id = local.target_compartment_id
  name           = "${local.name}-build"
  lifecycle { ignore_changes = [name] }
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
  display_name   = "${local.name}/worker"
  lifecycle { ignore_changes = [display_name] }
  is_public = false
}
moved {
  from = oci_identity_dynamic_group.build[0]
  to   = oci_identity_dynamic_group.build_pipeline[0]
}

moved {
  from = oci_identity_policy.build[0]
  to   = oci_identity_policy.build_pipeline[0]
}

resource "oci_identity_dynamic_group" "build_pipeline" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-build"
  lifecycle { ignore_changes = [name] }
  description   = "Only this OSMH build pipeline"
  matching_rule = "ALL {resource.type = 'devopsbuildpipeline', resource.id = '${oci_devops_build_pipeline.image[0].id}'}"
}

resource "oci_identity_dynamic_group" "connection" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-connection"
  lifecycle { ignore_changes = [name] }
  description   = "Only this OSMH GitHub connection"
  matching_rule = "ALL {resource.type = 'devopsconnection', resource.id = '${oci_devops_connection.github[0].id}'}"
}

resource "oci_identity_policy" "build_pipeline" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-build"
  lifecycle { ignore_changes = [name] }
  description = "Authorize the exact OSMH build pipeline and its regional delivery resources"
  statements = [
    # Source retrieval is evaluated before the managed build runner starts.
    # OCI's RelatedResourceNotAuthorizedOrNotFound guidance requires the
    # build-pipeline principal to manage devops-family at tenancy scope.
    "Allow dynamic-group id ${oci_identity_dynamic_group.build_pipeline[0].id} to manage devops-family in tenancy",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build_pipeline[0].id} to read secret-family in tenancy",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build_pipeline[0].id} to use ons-topics ${local.scope}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.build_pipeline[0].id} to manage repos ${local.scope}"
  ]
}

resource "oci_identity_policy" "connection" {
  provider       = oci.home
  count          = var.build_function_image ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-connection"
  lifecycle { ignore_changes = [name] }
  description = "Allow only the exact OSMH GitHub connection to read Vault secrets"
  statements = [
    # OCI DevOps does not consistently populate a specific secret target while
    # resolving external source files, so its documented failure guidance uses
    # secret-family at tenancy scope for the connection principal.
    "Allow dynamic-group id ${oci_identity_dynamic_group.connection[0].id} to read secret-family in tenancy"
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
  depends_on = [oci_identity_policy.build_pipeline, oci_identity_policy.connection]
  provisioner "local-exec" {
    command = "python3 \"${path.module}/validate_build_connection.py\" --connection-id \"$OSMH_CONNECTION_ID\" --secret-id \"$OSMH_SECRET_ID\" --repository-url \"$OSMH_REPOSITORY_URL\" --commit \"$OSMH_SOURCE_COMMIT\" --build-spec build_spec.yaml --region \"$OSMH_REGION\" --timeout-seconds \"$OSMH_TIMEOUT_SECONDS\""
    environment = {
      OSMH_CONNECTION_ID   = oci_devops_connection.github[0].id
      OSMH_SECRET_ID       = var.github_token_secret_id
      OSMH_REPOSITORY_URL  = var.source_repository_url
      OSMH_SOURCE_COMMIT   = var.source_commit
      OSMH_REGION          = var.region
      OSMH_TIMEOUT_SECONDS = tostring(var.iam_wait_seconds)
    }
  }
  triggers_replace = {
    validation_run        = plantimestamp()
    build_pipeline_policy = sha256(jsonencode(oci_identity_policy.build_pipeline[0].statements))
    connection_policy     = sha256(jsonencode(oci_identity_policy.connection[0].statements))
  }
}
resource "terraform_data" "build_run" {
  count      = var.build_function_image ? 1 : 0
  depends_on = [terraform_data.build_iam, oci_devops_build_pipeline_stage.deliver, oci_logging_log.build]
  triggers_replace = {
    pipeline   = oci_devops_build_pipeline.image[0].id
    commit     = var.source_commit
    revision   = var.build_revision
    repository = var.source_repository_url
    branch     = var.source_branch
    image      = local.built_image
  }
  provisioner "local-exec" {
    command = "python3 \"${path.module}/run_devops_build.py\" --pipeline-id \"$OSMH_PIPELINE_ID\" --display-name \"$OSMH_BUILD_NAME\" --repository-url \"$OSMH_REPOSITORY_URL\" --branch \"$OSMH_SOURCE_BRANCH\" --commit \"$OSMH_SOURCE_COMMIT\" --region \"$OSMH_REGION\" --timeout-seconds \"$OSMH_TIMEOUT_SECONDS\""
    environment = {
      OSMH_PIPELINE_ID     = oci_devops_build_pipeline.image[0].id
      OSMH_BUILD_NAME      = "osmh-${local.image_tag}"
      OSMH_REPOSITORY_URL  = var.source_repository_url
      OSMH_SOURCE_BRANCH   = var.source_branch
      OSMH_SOURCE_COMMIT   = var.source_commit
      OSMH_REGION          = var.region
      OSMH_TIMEOUT_SECONDS = tostring(var.build_timeout_seconds)
    }
  }
}
