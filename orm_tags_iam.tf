# Reuse existing namespaces and keys without importing them into this state. If
# the namespace exists but managedby does not, this stack creates and protects
# only the missing key.
resource "oci_identity_tag_namespace" "opt_in" {
  provider       = oci.home
  count          = local.reuse_tag_namespace ? 0 : 1
  depends_on     = [terraform_data.validate]
  compartment_id = local.target_compartment_id
  name           = local.new_namespace
  description    = "Opt in selected Compute instances to OSMH reconciliation"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [name]
  }
}
resource "oci_identity_tag" "opt_in" {
  provider         = oci.home
  count            = local.reuse_tag_namespace ? 0 : 1
  tag_namespace_id = oci_identity_tag_namespace.opt_in[0].id
  name             = "managedby"
  description      = "OSMH opt-in: osmanagementhub"
  validator {
    validator_type = "ENUM"
    values         = ["osmanagementhub"]
  }
  lifecycle { prevent_destroy = true }
}
data "oci_identity_tag_namespaces" "existing" {
  provider                = oci.home
  compartment_id          = var.tenancy_ocid
  include_subcompartments = true
  state                   = "ACTIVE"
}
data "oci_identity_tags" "existing" {
  provider         = oci.home
  count            = local.reuse_tag_namespace ? 1 : 0
  tag_namespace_id = local.existing_namespace_id
}
data "oci_identity_tag" "existing" {
  provider         = oci.home
  count            = local.reuse_tag_definition ? 1 : 0
  tag_namespace_id = local.existing_namespace_id
  tag_name         = "managedby"
  lifecycle {
    postcondition {
      condition     = self.state == "ACTIVE" && !self.is_retired && (try(self.validator[0].validator_type, "DEFAULT") != "ENUM" || try(contains(self.validator[0].values, "osmanagementhub"), false))
      error_message = "The existing managedby key must be active and permit osmanagementhub."
    }
  }
}
resource "oci_identity_tag" "existing_missing" {
  provider         = oci.home
  count            = local.reuse_tag_namespace && !local.reuse_tag_definition ? 1 : 0
  tag_namespace_id = local.existing_namespace_id
  name             = "managedby"
  description      = "OSMH opt-in: osmanagementhub"
  validator {
    validator_type = "ENUM"
    values         = ["osmanagementhub"]
  }
  lifecycle { prevent_destroy = true }
}

# A tag default affects resources created after it becomes ACTIVE and is
# inherited by child compartments. Existing instances are handled separately by
# the initial all-instances invocation.
data "oci_identity_tag_defaults" "existing_opt_in" {
  provider       = oci.home
  count          = local.reuse_tag_definition ? 1 : 0
  compartment_id = local.target_compartment_id
  state          = "ACTIVE"
}
resource "oci_identity_tag_default" "new_opt_in" {
  provider          = oci.home
  count             = local.reuse_tag_definition ? 0 : 1
  compartment_id    = local.target_compartment_id
  tag_definition_id = local.tag_definition_id
  value             = "osmanagementhub"
  is_required       = false
}
resource "oci_identity_tag_default" "existing_opt_in" {
  provider          = oci.home
  count             = local.reuse_tag_definition && length(local.existing_tag_defaults) == 0 ? 1 : 0
  compartment_id    = local.target_compartment_id
  tag_definition_id = local.tag_definition_id
  value             = "osmanagementhub"
  is_required       = false
}
resource "terraform_data" "validate_existing_tag_default" {
  count = local.reuse_tag_definition && length(local.existing_tag_defaults) > 0 ? 1 : 0
  lifecycle {
    precondition {
      condition     = alltrue([for item in local.existing_tag_defaults : item.value == "osmanagementhub"])
      error_message = "The selected compartment already has this tag default with another value; change it to osmanagementhub before applying this stack."
    }
  }
}
resource "oci_identity_dynamic_group" "instances" {
  provider       = oci.home
  count          = var.create_instance_iam ? 1 : 0
  depends_on     = [terraform_data.validate, oci_identity_tag.opt_in, data.oci_identity_tag.existing, oci_identity_tag.existing_missing]
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-instances"
  lifecycle { ignore_changes = [name] }
  description   = "Compute instances explicitly opted into OSMH; permissions are scoped by policy"
  matching_rule = "ALL {resource.type = 'instance', tag.${local.namespace}.managedby.value = 'osmanagementhub'}"
}
resource "oci_identity_policy" "instances" {
  provider       = oci.home
  count          = var.create_instance_iam ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${local.name}-instances"
  lifecycle { ignore_changes = [name] }
  description = "OSMH agent permissions for opted-in instances in the selected compartment tree"
  statements = [
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to {OSMH_MANAGED_INSTANCE_ACCESS} ${local.scope} where request.principal.id = target.managed-instance.id",
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to use metrics ${local.scope} where target.metrics.namespace = 'oracle_appmgmt'",
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to {MGMT_AGENT_DEPLOY_PLUGIN_CREATE, MGMT_AGENT_INSPECT, MGMT_AGENT_READ} ${local.scope}",
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to {APPMGMT_MONITORED_INSTANCE_READ, APPMGMT_MONITORED_INSTANCE_ACTIVATE} ${local.scope} where request.instance.id = target.monitored-instance.id",
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to {INSTANCE_READ, INSTANCE_UPDATE} ${local.scope} where request.instance.id = target.instance.id",
    "Allow dynamic-group id ${oci_identity_dynamic_group.instances[0].id} to {APPMGMT_WORK_REQUEST_READ, INSTANCE_AGENT_PLUGIN_INSPECT} ${local.scope}",
    "Allow any-user to read instances ${local.scope} where request.principal.type = 'osmh-dynamic-sets'",
    "Allow any-user to inspect management-agents ${local.scope} where request.principal.type = 'osmh-dynamic-sets'"
  ]
}
