# Resource Manager entry point. The CLI deployment remains in deployment/.
terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 8.29.0, < 9.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.2"
    }
  }
}

# Resource Manager supplies the job's credentials. No local OCI profile or keys.
provider "oci" {
  region = var.region
}

provider "oci" {
  alias  = "home"
  region = local.home_region
}

data "oci_identity_region_subscriptions" "tenancy" {
  tenancy_id = var.tenancy_ocid
}

data "oci_objectstorage_namespace" "tenancy" {
  compartment_id = var.tenancy_ocid
}

# Generate once per state, including before the first OCI create. No keepers:
# changing inputs or retrying a failed Apply must not rotate the deployment ID.
resource "random_id" "deployment" {
  byte_length = 16
}

# Generated names are assigned at creation only (see ignore_changes on names).
# This also preserves names already owned by v1.7 and earlier stack states.
# References to repositories and namespaces must use their actual stored names.
locals {
  target_compartment_id = var.target_compartment_ocid != "" ? var.target_compartment_ocid : var.compartment_ocid
  home_region           = one([for r in data.oci_identity_region_subscriptions.tenancy.region_subscriptions : r.region_name if r.is_home_region])
  region_key            = try(lower(one([for r in data.oci_identity_region_subscriptions.tenancy.region_subscriptions : r.region_key if r.region_name == var.region && r.state == "READY"])), "invalid")
  suffix                = random_id.deployment.hex
  name                  = "osmh-rm-${local.suffix}-${var.region}"
  scope                 = local.target_compartment_id == var.tenancy_ocid ? "in tenancy" : "in compartment id ${local.target_compartment_id}"
  namespace             = var.use_existing_tag_namespace ? var.tag_namespace : oci_identity_tag_namespace.opt_in[0].name
  new_namespace         = "${substr(var.tag_namespace, 0, 67)}_${local.suffix}"
  repository            = var.build_function_image ? oci_artifacts_container_repository.worker[0].display_name : try(regex("^[^/]+/[^/]+/(.+):[^:]+$", var.existing_image)[0], "invalid")
  image_tag             = "${substr(var.source_commit, 0, 12)}-${var.build_revision}"
  built_image           = "${local.region_key}.ocir.io/${data.oci_objectstorage_namespace.tenancy.namespace}/${local.repository}:${local.image_tag}"
  image                 = var.build_function_image ? local.built_image : var.existing_image
  application_id        = local.create_application ? oci_functions_application.worker[0].id : var.application_id
  create_application    = var.create_function_application == null ? var.application_id == "" : var.create_function_application
  create_new_network    = local.create_application && var.create_network
  existing_subnets      = !local.create_application || var.create_network ? [] : (var.existing_subnet_id != "" ? [var.existing_subnet_id] : var.subnet_ids)
  selected_ids          = sort(distinct(var.selected_instance_ids))
  tag_namespace_id      = var.use_existing_tag_namespace ? var.existing_tag_namespace_id : oci_identity_tag_namespace.opt_in[0].id
  schedule_parts        = split(":", var.schedule_time_utc)
  schedule_cron         = try("${tonumber(local.schedule_parts[1])} ${tonumber(local.schedule_parts[0])} * * *", "invalid")
  worker_condition      = "request.principal.type = 'fnfunc', request.principal.id = '${oci_functions_function.worker.id}'"
}

resource "terraform_data" "validate" {
  input = { region = var.region, compartment = local.target_compartment_id }
  lifecycle {
    precondition {
      condition     = contains([for r in data.oci_identity_region_subscriptions.tenancy.region_subscriptions : r.region_name if r.state == "READY"], var.region)
      error_message = "Choose a READY subscribed OCI region."
    }
    precondition {
      condition     = local.create_application ? (var.create_network || length(local.existing_subnets) > 0) : var.application_id != ""
      error_message = "Choose a new network or an existing subnet for a new application; supply an application OCID when reusing an application."
    }
    precondition {
      condition     = !local.create_application || var.create_network || var.existing_subnet_id == "" || var.existing_vcn_id != ""
      error_message = "Choose the VCN that contains the selected existing subnet."
    }
    precondition {
      condition     = length(local.selected_ids) == 0 || var.workload_regions == "" || var.workload_regions == "all" || contains(split(",", var.workload_regions), var.region)
      error_message = "Workload regions must include the selected instances' deployment region."
    }
    precondition {
      condition     = !(var.onboard_all_instances && length(local.selected_ids) > 0)
      error_message = "Choose Onboard all eligible instances or individual Compute instances, not both."
    }
    precondition {
      condition     = !var.build_function_image || (can(regex("^[0-9a-f]{40}$", var.source_commit)) && can(regex("^ocid1\\.vaultsecret\\.", var.github_token_secret_id)))
      error_message = "Cloud builds require a full Git commit SHA and the OCID of a GitHub token stored in OCI Vault."
    }
    precondition {
      condition     = var.build_function_image || (can(regex("^[a-z0-9.-]+/[a-z0-9_-]+/[a-z0-9._/-]+:[A-Za-z0-9_.-]+$", var.existing_image)) && !endswith(var.existing_image, ":latest") && (startswith(var.existing_image, "${local.region_key}.ocir.io/${data.oci_objectstorage_namespace.tenancy.namespace}/") || startswith(var.existing_image, "${var.region}.ocir.io/${data.oci_objectstorage_namespace.tenancy.namespace}/")))
      error_message = "Supply a versioned Linux AMD64 image from this tenancy's selected-region OCIR repository; latest is not accepted."
    }
    precondition {
      condition     = !var.use_existing_tag_namespace || can(regex("^ocid1\\.tagnamespace\\.", var.existing_tag_namespace_id))
      error_message = "Supply the existing namespace OCID when reusing a namespace."
    }
  }
}
