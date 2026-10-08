# Reused namespaces and keys are read-only. Do not let two states own the same tag.
resource "oci_identity_tag_namespace" "opt_in" {
  provider       = oci.home
  count          = var.use_existing_tag_namespace ? 0 : 1
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
  count            = var.use_existing_tag_namespace ? 0 : 1
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
  count                   = var.use_existing_tag_namespace ? 1 : 0
  compartment_id          = var.tenancy_ocid
  include_subcompartments = true
  state                   = "ACTIVE"
  lifecycle {
    postcondition {
      condition     = length([for ns in self.tag_namespaces : ns.id if ns.id == var.existing_tag_namespace_id && ns.name == var.tag_namespace && !ns.is_retired && ns.state == "ACTIVE"]) == 1
      error_message = "The existing namespace must be ACTIVE, visible in this tenancy, and match the supplied name and OCID."
    }
  }
}
data "oci_identity_tag" "existing" {
  provider         = oci.home
  count            = var.use_existing_tag_namespace ? 1 : 0
  tag_namespace_id = var.existing_tag_namespace_id
  depends_on       = [data.oci_identity_tag_namespaces.existing]
  tag_name         = "managedby"
  lifecycle {
    postcondition {
      condition     = self.state == "ACTIVE" && !self.is_retired && (try(self.validator[0].validator_type, "DEFAULT") != "ENUM" || try(contains(self.validator[0].values, "osmanagementhub"), false))
      error_message = "The existing managedby key must be active and permit osmanagementhub."
    }
  }
}
resource "oci_identity_dynamic_group" "instances" {
  provider       = oci.home
  count          = var.create_instance_iam ? 1 : 0
  depends_on     = [terraform_data.validate, oci_identity_tag.opt_in, data.oci_identity_tag.existing]
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
