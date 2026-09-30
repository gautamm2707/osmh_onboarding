"""Offline tests of tree IAM setup; never connect to an OCI tenancy."""

import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import oci
import osmh_iam as iam


ROOT = "ocid1.compartment.oc1..root123456789012"
CHILD = "ocid1.compartment.oc1..child"
USER = "ocid1.user.oc1..caller"
TENANCY = "ocid1.tenancy.oc1..test"
ADMIN_NAME = "osmh-admins-123456789012"
DG_NAME = "osmh-instances-123456789012"
POLICY_NAME = "osmh-automation-123456789012"


def args(**overrides):
    result = dict(compartment_id=ROOT, dry_run=False, skip_iam=False,
                  admin_group=None, instance_dynamic_group=None,
                  operator_group=None, identity_domain=None)
    result.update(overrides)
    return NS(**result)


def response(data):
    return NS(data=data, headers={"etag": "etag-before-change"})


def pages(fn, *a, **kw):
    return fn(*a, **kw)


def group(**overrides):
    result = dict(id="ocid1.group.oc1..admin", name=ADMIN_NAME,
                  description=iam.GROUP_DESCRIPTION, lifecycle_state="ACTIVE")
    result.update(overrides)
    return NS(**result)


def dynamic_group(**overrides):
    result = dict(id="ocid1.dynamicgroup.oc1..instances", name=DG_NAME,
                  description=iam.DG_DESCRIPTION, lifecycle_state="ACTIVE",
                  matching_rule=iam.compartment_rule([ROOT, CHILD]))
    result.update(overrides)
    return NS(**result)


def policy(**overrides):
    result = dict(id="ocid1.policy.oc1..policy", name=POLICY_NAME,
                  description=iam.POLICY_DESCRIPTION,
                  statements=iam.policy_statements(ROOT, TENANCY,
                      f"group {ADMIN_NAME}",
                      f"dynamic-group {DG_NAME}"))
    result.update(overrides)
    return NS(**result)


def client(existing=True):
    identity = Mock()
    identity.list_groups.return_value = response([group()] if existing else [])
    identity.list_dynamic_groups.return_value = response([dynamic_group()] if existing else [])
    identity.list_policies.return_value = response([policy()] if existing else [])
    identity.get_group.return_value = response(group())
    identity.get_dynamic_group.return_value = response(dynamic_group())
    identity.get_policy.return_value = response(policy())
    identity.get_user.return_value = response(NS(id=USER))
    identity.list_user_group_memberships.return_value = response([NS(lifecycle_state="ACTIVE")] if existing else [])
    identity.create_group.return_value = response(group())
    identity.create_dynamic_group.return_value = response(dynamic_group())
    return identity


@patch.object(iam, "list_call_get_all_results", side_effect=pages)
class TreeIamTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        output_context = contextlib.redirect_stdout(self.output)
        output_context.__enter__()
        self.addCleanup(output_context.__exit__, None, None, None)

    def assert_no_writes(self, identity):
        for name, _, _ in identity.mock_calls:
            self.assertFalse(name.startswith(("create_", "update_", "delete_", "add_")), name)

    def test_automatic_group_dynamic_group_policy_and_membership(self, _):
        identity = client(existing=False)
        self.assertTrue(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD, CHILD], USER))
        dg = identity.create_dynamic_group.call_args.args[0]
        self.assertEqual(iam.simple_rule_compartments(dg.matching_rule), {ROOT, CHILD})
        self.assertEqual(dg.name, DG_NAME)
        self.assertEqual(identity.create_group.call_args.args[0].name, ADMIN_NAME)
        membership = identity.add_user_to_group.call_args.args[0]
        self.assertEqual((membership.user_id, membership.group_id), (USER, group().id))
        statements = identity.create_policy.call_args.args[0].statements
        self.assertTrue(any(f"dynamic-group {DG_NAME}" in s for s in statements))
        self.assertTrue(any(f"group {ADMIN_NAME}" in s for s in statements))
        self.assertFalse(any("group id ocid1." in s for s in statements))
        self.assertTrue(all(CHILD not in s for s in statements))
        names = [name for name, _, _ in identity.mock_calls]
        self.assertLess(names.index("create_dynamic_group"), names.index("add_user_to_group"))

    def test_dry_run_prints_complete_plan_without_mutations(self, _):
        identity = client(existing=False)
        self.assertFalse(iam.ensure_tree_iam(args(dry_run=True), identity, TENANCY, [ROOT, CHILD], USER))
        self.assert_no_writes(identity)
        output = self.output.getvalue()
        self.assertIn(CHILD, output)
        self.assertIn("add configured API user", output)
        self.assertIn(f"Allow group {ADMIN_NAME}", output)
        self.assertIn(f"Allow dynamic-group {DG_NAME}", output)
        self.assertNotIn("dry-run-id", output)

    def test_idempotent_existing_setup(self, _):
        identity = client()
        self.assertFalse(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        self.assert_no_writes(identity)

    def test_old_id_template_is_migrated_once_preserving_custom_grants(self, _):
        identity = client()
        old = iam.policy_statements(ROOT, TENANCY, f"group id {group().id}",
                                    f"dynamic-group id {dynamic_group().id}")
        custom = f"Allow group id {group().id} to read buckets in tenancy"
        identity.get_policy.return_value = response(policy(statements=old + [custom]))
        self.assertTrue(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        written = identity.update_policy.call_args.args[1].statements
        self.assertIn(custom, written)
        self.assertEqual(set(written), set(policy().statements + [custom]))
        self.assertEqual(identity.update_policy.call_args.kwargs["if_match"], "etag-before-change")
        identity.get_policy.return_value = response(policy(statements=written))
        identity.reset_mock()
        self.assertFalse(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        self.assert_no_writes(identity)

    def test_migration_dry_run_shows_replacements_without_writes(self, _):
        identity = client()
        old = iam.policy_statements(ROOT, TENANCY, f"group id {group().id}",
                                    f"dynamic-group id {dynamic_group().id}")
        identity.get_policy.return_value = response(policy(statements=old))
        self.assertFalse(iam.ensure_tree_iam(args(dry_run=True), identity, TENANCY, [ROOT, CHILD], USER))
        self.assert_no_writes(identity)
        self.assertIn("replace: Allow group id", self.output.getvalue())
        self.assertIn(f"with: Allow group {ADMIN_NAME}", self.output.getvalue())

    def test_migration_deduplicates_old_and_named_templates(self, _):
        identity = client()
        old = iam.policy_statements(ROOT, TENANCY, f"group id {group().id}",
                                    f"dynamic-group id {dynamic_group().id}")
        identity.get_policy.return_value = response(policy(statements=old + policy().statements))
        iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        self.assertEqual(identity.update_policy.call_args.args[1].statements, policy().statements)

    def test_names_are_quoted_and_domain_qualified(self, _):
        self.assertEqual(iam._subject("group", group(name="OSMH Admins"), group().id, "Finance Domain"),
                         "group 'Finance Domain'/'OSMH Admins'")
        self.assertEqual(iam._subject("dynamic-group", dynamic_group(), "Default/" + DG_NAME),
                         f"dynamic-group {DG_NAME}")
        with self.assertRaisesRegex(SystemExit, "safely represented"):
            iam._subject("group", group(name="bad'name"), group().id)

    def test_ocid_override_without_verifiable_default_domain_stops_before_writes(self, _):
        identity = client()
        identity.list_groups.return_value = response([])
        with self.assertRaisesRegex(SystemExit, "identity domain"):
            iam.ensure_tree_iam(args(admin_group=group().id), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_nondefault_ocid_overrides_render_domain_names(self, _):
        identity = client()
        iam.ensure_tree_iam(args(admin_group=group().id, instance_dynamic_group=dynamic_group().id,
                                 identity_domain="Finance"), identity, TENANCY, [ROOT, CHILD], USER)
        statements = identity.update_policy.call_args.args[1].statements
        self.assertTrue(any(f"group Finance/{ADMIN_NAME} to" in s for s in statements))
        self.assertTrue(any(f"dynamic-group Finance/{DG_NAME} to" in s for s in statements))

    def test_owned_dynamic_group_expands_with_etag(self, _):
        identity = client()
        identity.get_dynamic_group.return_value = response(dynamic_group(
            matching_rule=f"ALL {{instance.compartment.id = '{ROOT}'}}"))
        self.assertTrue(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        call = identity.update_dynamic_group.call_args
        self.assertEqual(call.kwargs["if_match"], "etag-before-change")
        self.assertEqual(iam.simple_rule_compartments(call.args[1].matching_rule), {ROOT, CHILD})

    def test_external_group_does_not_get_user_membership_or_rule_update(self, _):
        identity = client()
        self.assertFalse(iam.ensure_tree_iam(
            args(admin_group=group().id, instance_dynamic_group=dynamic_group().id),
            identity, TENANCY, [ROOT, CHILD], USER))
        self.assert_no_writes(identity)
        identity.get_user.assert_not_called()
        identity.list_user_group_memberships.assert_not_called()

    def test_external_group_with_partial_rule_is_rejected_without_writes(self, _):
        identity = client()
        identity.get_dynamic_group.return_value = response(dynamic_group(
            matching_rule=f"ALL {{instance.compartment.id = '{ROOT}'}}"))
        with self.assertRaisesRegex(SystemExit, "all 2 scanned compartments"):
            iam.ensure_tree_iam(args(instance_dynamic_group=DG_NAME), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_restrictive_all_rule_cannot_be_mistaken_for_any_rule(self, _):
        identity = client()
        identity.get_dynamic_group.return_value = response(dynamic_group(
            matching_rule=f"ALL {{instance.compartment.id = '{ROOT}', instance.compartment.id = '{CHILD}'}}"))
        with self.assertRaisesRegex(SystemExit, "customized"):
            iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_quota_error_does_not_create_admin_group_or_membership(self, _):
        identity = client(existing=False)
        identity.create_dynamic_group.side_effect = oci.exceptions.ServiceError(
            400, "IdcsConversionError", {}, "You have reached the object limit of DynamicResourceGroups.")
        with self.assertRaisesRegex(SystemExit, "quota/limit"):
            iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        identity.create_group.assert_not_called()
        identity.add_user_to_group.assert_not_called()
        identity.create_policy.assert_not_called()

    def test_group_name_collision_does_not_grant_access(self, _):
        identity = client()
        identity.list_groups.return_value = response([group(description="manually created")])
        with self.assertRaisesRegex(SystemExit, "not marked as script-owned"):
            iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_policy_name_collision_does_not_change_anything(self, _):
        identity = client()
        identity.get_policy.return_value = response(policy(description="unrelated policy"))
        with self.assertRaisesRegex(SystemExit, "not marked as script-owned"):
            iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_existing_policy_preserves_unrelated_statements(self, _):
        identity = client()
        unrelated = "Allow group old-admins to read buckets in tenancy"
        identity.get_policy.return_value = response(policy(statements=[unrelated]))
        self.assertTrue(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        call = identity.update_policy.call_args
        self.assertEqual(call.args[1].statements[0], unrelated)
        self.assertEqual(call.kwargs["if_match"], "etag-before-change")

    def test_skip_iam_does_not_even_read_identity_resources(self, _):
        identity = client()
        self.assertFalse(iam.ensure_tree_iam(args(skip_iam=True), identity, TENANCY, [ROOT, CHILD], USER))
        self.assertEqual(identity.mock_calls, [])

    def test_default_domain_name_is_accepted_and_nondefault_name_is_rejected(self, _):
        identity = client()
        self.assertFalse(iam.ensure_tree_iam(args(identity_domain="Default"), identity, TENANCY, [ROOT, CHILD], USER))
        with self.assertRaisesRegex(SystemExit, "default identity domain"):
            iam.ensure_tree_iam(args(identity_domain="Finance"), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)

    def test_creating_automatic_group_waits_before_membership(self, _):
        identity = client(existing=False)
        identity.create_group.return_value = response(group(lifecycle_state="CREATING"))
        with patch.object(iam.oci, "wait_until", return_value=response(group())) as waiter:
            self.assertTrue(iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER))
        waiter.assert_called_once()
        identity.add_user_to_group.assert_called_once()

    def test_failed_bootstrap_identifies_missing_authority(self, _):
        identity = client(existing=False)
        identity.list_groups.side_effect = oci.exceptions.ServiceError(403, "NotAuthorized", {}, "denied")
        with self.assertRaisesRegex(SystemExit, "bootstrap access"):
            iam.ensure_tree_iam(args(), identity, TENANCY, [ROOT, CHILD], USER)
        self.assert_no_writes(identity)


if __name__ == "__main__":
    unittest.main()
