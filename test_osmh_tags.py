"""Offline behavioral tests for opt-in isolation, repeat runs and Function configuration."""
import contextlib
from contextlib import ExitStack
import io
import json
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import onboard_osmh as app
import osmh_iam as iam
import osmh_runtime as runtime
import osmh_tags as tags
import oci
from function import func
from function.func import command
from reconcile_osmh import worker_arguments
from test_osmh_iam import client, args as iam_args, TENANCY, ROOT, CHILD, response, pages


class TagTests(unittest.TestCase):
    def setUp(self):
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def instance(self, **overrides):
        values = dict(id="ocid1.instance.oc1..one", display_name="one", compartment_id=ROOT,
                      lifecycle_state="RUNNING", image_id="image", shape="VM.Standard.E4.Flex",
                      defined_tags={}, freeform_tags={}, metadata={})
        values.update(overrides)
        return NS(**values)

    def test_namespace_and_rule_are_scope_stable_without_compartment_enumeration(self):
        namespace = tags.namespace_name(ROOT)
        rule = tags.tag_rule(namespace)
        self.assertIn("resource.type = 'instance'", rule)
        self.assertIn(f"tag.{namespace}.managedby.value = 'osmanagementhub'", rule)
        self.assertNotIn("compartment", rule)
        for invalid in ("a.b", "a'}", "", "name with spaces"):
            if invalid:
                with self.assertRaises(SystemExit):
                    tags.tag_rule(invalid)

    def test_freeform_wrong_namespace_and_wrong_value_do_not_opt_in(self):
        self.assertFalse(tags.is_opted_in(self.instance(freeform_tags={"managedby": "osmanagementhub"}), "NS"))
        self.assertFalse(tags.is_opted_in(self.instance(defined_tags={"Other": {"managedby": "osmanagementhub"}}), "NS"))
        self.assertFalse(tags.is_opted_in(self.instance(defined_tags={"NS": {"managedby": "another"}}), "NS"))
        self.assertTrue(tags.is_opted_in(self.instance(defined_tags={"NS": {"managedby": "osmanagementhub"}}), "NS"))

    def test_tag_merge_preserves_existing_keys_and_uses_etag(self):
        instance = self.instance(defined_tags={"NS": {"owner": "ops"}, "Finance": {"cost": "42"}})
        compute = Mock()
        compute.get_instance.return_value = response(instance)
        tags.apply_instance_tag(NS(dry_run=False), compute, instance, "NS", {ROOT})
        call = compute.update_instance.call_args
        self.assertEqual(call.kwargs["if_match"], "etag-before-change")
        self.assertEqual(call.args[1].defined_tags, {
            "NS": {"owner": "ops", "managedby": "osmanagementhub"}, "Finance": {"cost": "42"}})
        self.assertNotIn("managedby", instance.defined_tags["NS"])

    @patch.object(tags.time, "sleep")
    @patch.object(tags.time, "monotonic", side_effect=[0, 1])
    def test_new_defined_tag_validation_is_retried(self, _clock, sleep):
        error = oci.exceptions.ServiceError(
            400, "InvalidParameter", {}, "Failed to validate tags: TagNamespace osmh_ns does not exists")
        compute = Mock()
        compute.get_instance.return_value = response(self.instance())
        compute.update_instance.side_effect = [error, None]
        tags.apply_instance_tag(NS(dry_run=False, tag_propagation_timeout=180), compute,
                                self.instance(), "NS", {ROOT})
        self.assertEqual(compute.update_instance.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_dry_run_and_already_tagged_make_no_update(self):
        for dry_run, defined in [(True, {}), (False, {"NS": {"managedby": "osmanagementhub"}})]:
            compute = Mock()
            instance = self.instance(defined_tags=defined)
            compute.get_instance.return_value = response(instance)
            tags.apply_instance_tag(NS(dry_run=dry_run), compute, instance, "NS", {ROOT})
            compute.update_instance.assert_not_called()

    def test_changed_scope_state_oke_or_conflicting_value_stops_tagging(self):
        for change in [dict(compartment_id="outside"), dict(lifecycle_state="TERMINATED"),
                       dict(freeform_tags={"OKEclusterName": "cluster", "OKEnodePoolName": "pool"}),
                       dict(defined_tags={"NS": {"managedby": "different"}})]:
            compute = Mock()
            compute.get_instance.return_value = response(self.instance(**change))
            with self.assertRaises(SystemExit):
                tags.apply_instance_tag(NS(dry_run=False), compute, self.instance(), "NS", {ROOT})
            compute.update_instance.assert_not_called()

    @patch.object(tags, "list_call_get_all_results", side_effect=pages)
    def test_tag_namespace_dry_run_does_not_create_resources(self, _):
        identity = Mock()
        identity.list_tag_namespaces.return_value = response([])
        tags.ensure_tag_namespace(NS(compartment_id=ROOT, tag_namespace="NS", dry_run=True), identity)
        identity.create_tag_namespace.assert_not_called()
        identity.create_tag.assert_not_called()

    @patch.object(tags, "list_call_get_all_results", side_effect=pages)
    def test_existing_namespace_and_key_are_reused(self, _):
        identity = Mock()
        identity.list_tag_namespaces.return_value = response([NS(id="ns", name="NS", is_retired=False)])
        identity.list_tags.return_value = response([NS(name="managedby", is_retired=False,
                                                     validator=NS(values=["osmanagementhub"]))])
        identity.get_tag.return_value = response(NS(name="managedby", is_retired=False,
                                                   validator=NS(values=["osmanagementhub"])))
        tags.ensure_tag_namespace(NS(compartment_id=ROOT, tag_namespace="NS", dry_run=False), identity)
        identity.create_tag_namespace.assert_not_called()
        identity.create_tag.assert_not_called()

    @patch.object(tags, "list_call_get_all_results", side_effect=pages)
    def test_existing_tag_validator_rejects_incompatible_value(self, _):
        identity = Mock()
        identity.list_tag_namespaces.return_value = response([NS(id="ns", name="NS", is_retired=False)])
        identity.list_tags.return_value = response([NS(name="managedby")])
        identity.get_tag.return_value = response(NS(name="managedby", is_retired=False,
                                                   validator=NS(values=["another-service"])))
        with self.assertRaises(SystemExit):
            tags.ensure_tag_namespace(NS(compartment_id=ROOT, tag_namespace="NS", dry_run=False), identity)
        identity.create_tag.assert_not_called()

    @patch.object(iam, "list_call_get_all_results", side_effect=pages)
    def test_function_bootstrap_creates_tag_group_without_user_membership(self, _):
        identity = client(existing=False)
        options = iam_args(tag_namespace="NS", allow_empty_admin_group=True)
        iam.ensure_tree_iam(options, identity, TENANCY, [ROOT, CHILD], None)
        rule = identity.create_dynamic_group.call_args.args[0].matching_rule
        self.assertEqual(rule, tags.tag_rule("NS"))
        self.assertNotIn(CHILD, rule)
        identity.add_user_to_group.assert_not_called()
        identity.get_user.assert_not_called()
        self.assertTrue(any("dynamic-group osmh-tagged-" in s
                            for s in identity.create_policy.call_args.args[0].statements))

    @patch.object(iam, "list_call_get_all_results", side_effect=pages)
    def test_tag_group_does_not_change_when_new_compartment_appears(self, _):
        identity = client(existing=False)
        dg = NS(id="dg", name=f"osmh-tagged-{ROOT[-12:]}", matching_rule=tags.tag_rule("NS"),
                description=iam.DG_DESCRIPTION, lifecycle_state="ACTIVE")
        identity.list_dynamic_groups.return_value = response([dg])
        identity.get_dynamic_group.return_value = response(dg)
        iam.ensure_tree_iam(iam_args(tag_namespace="NS", allow_empty_admin_group=True),
                            identity, TENANCY, [ROOT, CHILD, "new-compartment"], None)
        identity.create_dynamic_group.assert_not_called()
        identity.update_dynamic_group.assert_not_called()

    def test_default_is_selection_and_worker_disables_cleanup(self):
        options = app.arguments([ROOT])
        self.assertEqual(options.workflow, "tag")
        self.assertTrue(options.skip_terminated_cleanup)
        options = app.arguments(worker_arguments([ROOT, "--auth", "resource_principal"]))
        self.assertEqual(options.workflow, "onboard-tagged")
        self.assertTrue(options.all_instances)
        self.assertTrue(options.skip_terminated_cleanup)
        self.assertEqual(options.registration_timeout, 0)
        self.assertTrue(options.allow_empty_admin_group)
        for flag in ("--cleanup-only", "--unregister-all", "--workflow=tag", "--instance-ids=x"):
            with self.assertRaises(SystemExit):
                worker_arguments([ROOT, flag])

    def test_function_payload_cannot_change_scope_or_auth(self):
        config = {"OSMH_COMPARTMENT_ID": ROOT, "OSMH_TAG_NAMESPACE": "NS", "OSMH_REGIONS": "all"}
        argv = command(config, {"dry_run": True})
        self.assertIn("--all-regions", argv)
        self.assertIn("resource_principal", argv)
        self.assertIn("--dry-run", argv)
        for payload in ({"compartment_id": CHILD}, {"dry_run": "false"}, []):
            with self.assertRaises(ValueError):
                command(config, payload)

    def test_function_handler_returns_reconciliation_output_tail(self):
        ctx = NS(Config=lambda: {"OSMH_COMPARTMENT_ID": ROOT, "OSMH_TAG_NAMESPACE": "NS", "OSMH_REGIONS": ""})
        process = Mock()
        process.stdout = iter(["line one\n", "line two\n"])
        process.wait.return_value = 0
        fake_response = types.SimpleNamespace(
            Response=lambda _ctx, response_data, headers: NS(response_data=response_data, headers=headers))
        fake_fdk = types.SimpleNamespace(response=fake_response)
        with patch.object(func.subprocess, "Popen", return_value=process), \
                patch.dict(sys.modules, {"fdk": fake_fdk}):
            result = func.handler(ctx, io.BytesIO(b"{}"))
        body = json.loads(result.response_data)
        self.assertEqual(body["status"], "reconciliation_pass_complete")
        self.assertEqual(body["output_tail"], ["line one", "line two"])

    @patch.object(runtime.oci.auth.signers, "get_resource_principals_signer")
    @patch.object(runtime.oci.config, "from_file")
    def test_function_auth_never_reads_local_credentials(self, from_file, signer_factory):
        signer = signer_factory.return_value
        signer.get_claim.return_value = TENANCY
        signer.region = "us-ashburn-1"
        options = app.arguments(worker_arguments([ROOT, "--auth", "resource_principal"]))
        config, kwargs = runtime.load_auth(options)
        from_file.assert_not_called()
        self.assertEqual(config["tenancy"], TENANCY)
        self.assertIs(kwargs["signer"], signer)

    def run_region_fixture(self, workflow, instances):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for cls in (app.oci.os_management_hub.OnboardingClient, app.oci.os_management_hub.ManagedInstanceClient,
                    app.oci.os_management_hub.ManagedInstanceGroupClient, app.oci.os_management_hub.SoftwareSourceClient,
                    app.oci.os_management_hub.WorkRequestClient):
            stack.enter_context(patch.object(app.oci.os_management_hub, cls.__name__))
        compute = stack.enter_context(patch.object(app.oci.core, "ComputeClient")).return_value
        stack.enter_context(patch.object(app.oci.container_engine, "ContainerEngineClient"))
        stack.enter_context(patch.object(app, "discover_compartments", return_value=[NS(id=ROOT, name="root")]))
        stack.enter_context(patch.object(app, "list_call_get_all_results", return_value=response(instances)))
        stack.enter_context(patch.object(app, "discover_oke_instance_ids", return_value={} ))
        managed = stack.enter_context(patch.object(app, "list_managed_in_compartments", return_value=[]))
        ensure_iam = stack.enter_context(patch.object(app, "ensure_tree_iam"))
        scan = stack.enter_context(patch.object(app, "scan_candidates", side_effect=lambda c, ins, *a: [
            (i, app.Platform("ORACLE_LINUX_9", "X86_64", "ORACLE")) for i in ins]))
        ensure_tag = stack.enter_context(patch.object(app, "ensure_tag_namespace", return_value="NS"))
        apply_tag = stack.enter_context(patch.object(app, "apply_instance_tag"))
        sources = stack.enter_context(patch.object(app, "ensure_oracle_linux_sources", return_value={}))
        profiles = stack.enter_context(patch.object(app, "find_or_create_profiles", return_value={}))
        options = app.arguments([ROOT, "--workflow", workflow, "--all", "--dry-run", "--tag-namespace", "NS",
                                 "--skip-terminated-cleanup"])
        return options, compute, managed, ensure_iam, scan, ensure_tag, apply_tag, sources

    def test_selection_phase_reads_osmh_inventory_but_does_not_create_iam(self):
        instance = self.instance()
        options, compute, managed, ensure_iam, scan, ensure_tag, apply_tag, sources = self.run_region_fixture("tag", [instance])
        app.run_region(options, {"tenancy": TENANCY, "region": "us-ashburn-1"}, {}, Mock())
        managed.assert_called_once()
        ensure_iam.assert_not_called()
        sources.assert_not_called()
        self.assertEqual(apply_tag.call_args.args[2].id, instance.id)

    def test_selection_phase_marks_existing_osmh_registration(self):
        instance = self.instance()
        options, compute, managed, ensure_iam, scan, ensure_tag, apply_tag, sources = self.run_region_fixture("tag", [instance])
        managed.return_value = [NS(id=instance.id, location="OCI_COMPUTE")]
        app.run_region(options, {"tenancy": TENANCY, "region": "us-ashburn-1"}, {}, Mock())
        self.assertEqual(scan.call_args.args[3], {instance.id: managed.return_value[0]})

    def test_unattended_scan_without_matching_tags_does_not_create_iam(self):
        options, compute, managed, ensure_iam, scan, ensure_tag, apply_tag, sources = self.run_region_fixture("onboard-tagged", [self.instance()])
        app.run_region(options, {"tenancy": TENANCY, "region": "us-ashburn-1"}, {}, Mock())
        self.assertEqual(scan.call_args.args[1], [])
        ensure_iam.assert_not_called()
        sources.assert_not_called()
        apply_tag.assert_not_called()

    def test_worker_passes_only_tagged_instances_to_onboarding(self):
        chosen = self.instance(defined_tags={"NS": {"managedby": "osmanagementhub"}})
        untagged = self.instance(id="ocid1.instance.oc1..untagged")
        options, compute, managed, ensure_iam, scan, ensure_tag, apply_tag, sources = self.run_region_fixture(
            "onboard-tagged", [chosen, untagged])
        ensure_iam.return_value = False
        platform = app.Platform("ORACLE_LINUX_9", "X86_64", "ORACLE")
        with patch.object(app, "find_or_create_profiles", return_value={platform: "profile"}), \
                patch.object(app, "enable_plugin", return_value=False) as enable, \
                patch.object(app, "ensure_groups"):
            app.run_region(options, {"tenancy": TENANCY, "region": "us-ashburn-1"}, {}, Mock())
        self.assertEqual(scan.call_args.args[1], [chosen])
        self.assertEqual(enable.call_args.args[2].id, chosen.id)
        apply_tag.assert_not_called()


if __name__ == "__main__":
    unittest.main()
