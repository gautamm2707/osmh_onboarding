"""Offline coverage for recursive discovery, instance selection and registration."""

import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, call, patch

import oci
import onboard_osmh as app


def options(**overrides):
    values = dict(compartment_id="parent", all_instances=False, instance_ids=None,
                  interactive=False, skip_terminated_cleanup=False,
                  registration_timeout=60, dry_run=False)
    values.update(overrides)
    return NS(**values)


def instance(identifier, *, compartment="parent", state="RUNNING", image="ol9-image"):
    return NS(id=identifier, display_name=identifier, compartment_id=compartment,
              lifecycle_state=state, image_id=image, shape="VM.Standard.E4.Flex")


def response(data):
    return NS(data=data, status=200, headers={})


def pages(method, *args, **kwargs):
    return method(*args, **kwargs)


class TreeFlowTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.ol9 = app.Platform("ORACLE_LINUX_9", "X86_64", "ORACLE")

    def targets(self):
        return [(instance("one"), self.ol9),
                (instance("two", compartment="child"), self.ol9),
                (instance("three", compartment="child"), self.ol9)]

    def test_scan_counts_oke_separately_from_stopped_and_unsupported(self):
        compute = Mock()
        compute.get_image.side_effect = lambda identifier: response(NS(
            operating_system="Ubuntu" if identifier == "ubuntu" else "Oracle Linux",
            operating_system_version="16.04" if identifier == "ubuntu" else "9"))
        records = [instance("ordinary"), instance("oke"),
                   instance("stopped", state="STOPPED"),
                   instance("terminated", state="TERMINATED"),
                   instance("unsupported", image="ubuntu")]

        result = app.scan_candidates(compute, records, {"oke": "node pool membership"}, {})

        self.assertEqual([record.id for record, _ in result], ["ordinary"])
        self.assertIn("5 Compute instances; 1 OKE excluded; 4 non-OKE", self.output.getvalue())
        self.assertIn("Eligible for selection: 1; non-running: 2; unsupported/unverified: 1",
                      self.output.getvalue())
        self.assertIn("EXCLUDE OKE: oke", self.output.getvalue())
        self.assertEqual(compute.get_image.call_count, 2)

    def test_image_lookup_is_cached_across_compartments(self):
        compute = Mock()
        compute.get_image.return_value = response(NS(
            operating_system="Oracle Linux", operating_system_version="9.6"))
        records = [instance("root-compute"), instance("child-compute", compartment="child")]

        result = app.scan_candidates(compute, records, {}, {})

        self.assertEqual({record.id for record, _ in result}, {"root-compute", "child-compute"})
        compute.get_image.assert_called_once_with("ol9-image")

    def test_compartment_paths_distinguish_repeated_leaf_names(self):
        labels = app.compartment_labels([
            NS(id="root", name="Example (root)"),
            NS(id="prod", name="Prod", compartment_id="root"),
            NS(id="dev", name="Dev", compartment_id="root"),
            NS(id="a", name="Apps", compartment_id="prod"),
            NS(id="b", name="Apps", compartment_id="dev")])
        self.assertEqual(labels["a"], "Example (root) / Prod / Apps")
        self.assertEqual(labels["b"], "Example (root) / Dev / Apps")
        self.assertEqual(labels["root"], "Example (root)")

    def test_skipped_and_oke_rows_show_compartment(self):
        compute = Mock()
        compute.get_image.return_value = response(NS(operating_system="Ubuntu", operating_system_version="24"))
        app.scan_candidates(compute, [instance("oke"), instance("stopped", state="STOPPED"),
                                     instance("unsupported")], {"oke": "node pool"}, {},
                            {"parent": "Example (root) / Apps"})
        for line in self.output.getvalue().splitlines():
            if line.startswith(("SKIP", "EXCLUDE")):
                self.assertIn("Compartment: Example (root) / Apps", line)

    def test_selection_rows_explicitly_label_compartment(self):
        app.select_instances(options(all_instances=True), self.targets(),
                             {"parent": "Root", "child": "Root / Apps"}, set())
        self.assertIn("one | Compartment: Root |", self.output.getvalue())
        self.assertIn("two | Compartment: Root / Apps |", self.output.getvalue())

    def test_registered_platform_wins_over_original_image(self):
        compute = Mock()
        records = [instance("upgraded"), instance("windows")]
        managed = {
            "upgraded": NS(os_family="ORACLE_LINUX_10", architecture="AARCH64"),
            "windows": NS(os_family="WINDOWS_SERVER_2025", architecture="X86_64"),
        }

        result = {record.id: platform for record, platform
                  in app.scan_candidates(compute, records, {}, managed)}

        self.assertEqual(result["upgraded"], app.Platform("ORACLE_LINUX_10", "AARCH64", "ORACLE"))
        self.assertEqual(result["windows"], app.Platform("WINDOWS_SERVER_2025", "X86_64", "MICROSOFT"))
        compute.get_image.assert_not_called()

    def test_unreadable_image_is_not_eligible(self):
        compute = Mock()
        compute.get_image.side_effect = oci.exceptions.ServiceError(
            404, "NotAuthorizedOrNotFound", {}, "image unavailable")

        self.assertEqual(app.scan_candidates(compute, [instance("unknown")], {}, {}), [])
        self.assertIn("unsupported/unverified: 1", self.output.getvalue())

    def test_unexpected_image_service_error_is_not_swallowed(self):
        compute = Mock()
        compute.get_image.side_effect = oci.exceptions.ServiceError(
            500, "InternalError", {}, "unexpected")
        with self.assertRaises(oci.exceptions.ServiceError):
            app.scan_candidates(compute, [instance("unknown")], {}, {})

    def test_selection_parser_all_none_and_deduplicated_ranges(self):
        self.assertEqual(app.parse_instance_selection(" ALL ", 5), [0, 1, 2, 3, 4])
        for value in ("none", "q", "quit"):
            with self.subTest(value=value):
                self.assertEqual(app.parse_instance_selection(value, 5), [])
        self.assertEqual(app.parse_instance_selection("5, 1,2-4,3", 5), [0, 1, 2, 3, 4])

    def test_selection_parser_rejects_invalid_and_out_of_bounds_input(self):
        for value in ("", "0", "6", "4-2", "1-6", "-1", "1,,2", "1;2", "one"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.parse_instance_selection(value, 5)

    @patch.object(app.sys.stdin, "isatty", return_value=False)
    @patch("builtins.input")
    def test_noninteractive_all_does_not_prompt(self, prompt, _):
        targets = self.targets()
        self.assertEqual(app.select_instances(options(all_instances=True), targets,
                         {"parent": "Parent", "child": "Parent/Child"}, {"one"}), targets)
        prompt.assert_not_called()
        self.assertIn("Selected 3 of 3", self.output.getvalue())
        self.assertIn("2 new registrations", self.output.getvalue())

    @patch("builtins.input")
    def test_explicit_ocids_select_only_eligible_subset(self, prompt):
        targets = self.targets()
        chosen = app.select_instances(options(instance_ids="three, one,three"), targets,
                                      {"parent": "Parent", "child": "Parent/Child"}, set())
        self.assertEqual([record.id for record, _ in chosen], ["one", "three"])
        prompt.assert_not_called()

    def test_explicit_unknown_or_empty_ocids_are_rejected(self):
        for value in ("one,foreign-instance", " , "):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "out-of-scope/OKE"):
                app.select_instances(options(instance_ids=value), self.targets(),
                                     {"parent": "Parent", "child": "Parent/Child"}, set())

    @patch.object(app.sys.stdin, "isatty", return_value=False)
    @patch("builtins.input")
    def test_non_tty_without_selection_fails_without_prompt(self, prompt, _):
        with self.assertRaisesRegex(SystemExit, "no OCI changes made"):
            app.select_instances(options(), self.targets(),
                                 {"parent": "Parent", "child": "Parent/Child"}, set())
        prompt.assert_not_called()

    @patch("builtins.input", side_effect=["4", "2-3"])
    def test_interactive_invalid_choice_can_be_corrected(self, prompt):
        chosen = app.select_instances(options(interactive=True), self.targets(),
                                      {"parent": "Parent", "child": "Parent/Child"}, set())
        self.assertEqual([record.id for record, _ in chosen], ["two", "three"])
        self.assertEqual(prompt.call_count, 2)

    @patch("builtins.input", return_value="none")
    def test_interactive_none_cancels_entire_run(self, _):
        with self.assertRaisesRegex(SystemExit, "Cancelled; no OCI changes made"):
            app.select_instances(options(interactive=True), self.targets(),
                                 {"parent": "Parent", "child": "Parent/Child"}, set())

    @patch("builtins.input", return_value="n")
    def test_empty_interactive_candidates_can_cancel_cleanup(self, _):
        with self.assertRaisesRegex(SystemExit, "Cancelled; no OCI changes made"):
            app.select_instances(options(interactive=True), [], {"parent": "Parent"}, set())

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    @patch.object(app.time, "sleep")
    def test_registration_wait_checks_children_on_each_poll(self, sleep, _):
        managed = Mock()
        parent_record = NS(id="parent-instance")
        child_record = NS(id="child-instance")
        managed.list_managed_instances.side_effect = [
            response([parent_record]), response([]),
            response([parent_record]), response([child_record]),
        ]
        with patch.object(app.time, "monotonic", return_value=100):
            result = app.wait_for_managed_instances(
                options(), managed, {"parent-instance", "child-instance"}, ["parent", "child"])

        self.assertEqual(result, {"parent-instance": parent_record, "child-instance": child_record})
        self.assertEqual(managed.list_managed_instances.call_args_list,
                         [call(compartment_id="parent"), call(compartment_id="child")] * 2)
        sleep.assert_called_once_with(20)

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_registration_timeout_returns_only_requested_members(self, _):
        managed = Mock()
        requested = NS(id="requested")
        managed.list_managed_instances.return_value = response([requested, NS(id="unselected")])

        result = app.wait_for_managed_instances(options(registration_timeout=0), managed,
                                               {"requested", "missing"})

        self.assertEqual(result, {"requested": requested})
        managed.list_managed_instances.assert_called_once_with(compartment_id="parent")


class MainTreeIntegrationTests(unittest.TestCase):
    """Exercise orchestration using only mocked OCI responses and credentials."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.args = options(config_file="unused", profile="DEFAULT", region=None,
                            admin_group=None, operator_group=None, identity_domain=None,
                            instance_dynamic_group=None, skip_iam=False, unregistration_timeout=0,
                            software_source_map=None, profile_map=None, group_prefix="osmh")
        self.stack.enter_context(patch.object(app, "arguments", return_value=self.args))
        self.stack.enter_context(patch.object(app.oci.config, "from_file", return_value={
            "tenancy": "tenancy", "region": "us-ashburn-1", "user": "test-user"}))
        self.stack.enter_context(patch.object(app.oci.config, "validate_config"))
        self.stack.enter_context(patch.object(app, "list_call_get_all_results", side_effect=pages))
        self.compute, self.identity, self.profiles = Mock(), Mock(), Mock()
        self.stack.enter_context(patch.object(app, "load_auth", return_value=(
            {"tenancy": "tenancy", "region": "us-ashburn-1", "user": "test-user"}, {})))
        self.stack.enter_context(patch.object(app, "prepare_identity", return_value=self.identity))
        self.managed, self.groups, self.software, self.work, self.oke = [Mock() for _ in range(5)]
        constructors = [
            (app.oci.core, "ComputeClient", self.compute),
            (app.oci.identity, "IdentityClient", self.identity),
            (app.oci.os_management_hub, "OnboardingClient", self.profiles),
            (app.oci.os_management_hub, "ManagedInstanceClient", self.managed),
            (app.oci.os_management_hub, "ManagedInstanceGroupClient", self.groups),
            (app.oci.os_management_hub, "SoftwareSourceClient", self.software),
            (app.oci.os_management_hub, "WorkRequestClient", self.work),
            (app.oci.container_engine, "ContainerEngineClient", self.oke),
        ]
        for namespace, name, client in constructors:
            self.stack.enter_context(patch.object(namespace, name, return_value=client))
        self.stack.enter_context(patch.object(app, "discover_compartments", return_value=[
            NS(id="parent", name="Parent"), NS(id="child", name="Child")]))
        self.parent = instance("ocid1.instance.parent")
        self.child = instance("ocid1.instance.child", compartment="child")
        self.windows = instance("ocid1.instance.windows", compartment="child", image="windows-image")
        self.node = instance("ocid1.instance.oke", compartment="child", state="TERMINATED")
        self.terminated = instance("ocid1.instance.terminated", compartment="child", state="TERMINATED")
        self.by_compartment = {"parent": [self.parent],
                               "child": [self.child, self.windows, self.node, self.terminated]}
        self.compute.list_instances.side_effect = lambda compartment: response(self.by_compartment[compartment])
        self.compute.get_image.return_value = response(NS(
            operating_system="Windows", operating_system_version="Server 2022"))
        self.stack.enter_context(patch.object(app, "discover_oke_instance_ids",
                                            return_value={self.node.id: "managed node pool"}))

        def managed_record(compute_record, family="ORACLE_LINUX_9"):
            return NS(id=compute_record.id, display_name=compute_record.display_name,
                      compartment_id=compute_record.compartment_id, location="OCI_COMPUTE",
                      os_family=family, architecture="X86_64")

        self.parent_mi, self.child_mi, self.node_mi, self.dead_mi = [
            managed_record(record) for record in (self.parent, self.child, self.node, self.terminated)]
        self.windows_mi = managed_record(self.windows, "WINDOWS_SERVER_2022")

        def managed_inventory(*, compartment_id):
            if compartment_id == "parent":
                return response([self.parent_mi])
            records = [self.child_mi, self.node_mi, self.dead_mi]
            if self.compute.update_instance.called:
                records.append(self.windows_mi)
            return response(records)

        self.managed.list_managed_instances.side_effect = managed_inventory
        self.iam = self.stack.enter_context(patch.object(app, "ensure_tree_iam", return_value=False))
        self.cleanup = self.stack.enter_context(patch.object(
            app, "cleanup_terminated_instances", side_effect=lambda *args: app.CleanupResult()))
        self.stack.enter_context(patch.object(app, "select_terminated_instances", return_value={self.terminated.id}))
        self.sources = self.stack.enter_context(patch.object(app, "ensure_oracle_linux_sources",
            return_value={"ORACLE_LINUX_9:X86_64": ["root-baseos", "root-appstream"]}))
        self.profiles.list_profiles.return_value = response([])
        self.profiles.create_profile.return_value = response(NS(id="windows-profile"))
        self.groups.list_managed_instance_groups.return_value = response([])
        self.group_calls = self.stack.enter_context(patch.object(app, "ensure_groups", wraps=app.ensure_groups))

    def test_interactive_cancel_precedes_all_mutation_helpers(self):
        self.args.interactive = True
        with patch("builtins.input", return_value="none"), self.assertRaisesRegex(SystemExit, "Cancelled"):
            app.main()

        self.iam.assert_not_called()
        self.cleanup.assert_not_called()
        self.sources.assert_not_called()
        self.group_calls.assert_not_called()
        self.compute.update_instance.assert_not_called()
        self.profiles.create_profile.assert_not_called()
        self.groups.create_managed_instance_group.assert_not_called()
        self.managed.delete_managed_instance.assert_not_called()

    def test_cleanup_only_does_not_configure_iam_or_register(self):
        self.args.cleanup_only = True
        with patch("builtins.input") as prompt:
            app.main()
        prompt.assert_not_called()
        self.iam.assert_not_called()
        self.sources.assert_not_called()
        self.compute.update_instance.assert_not_called()
        self.group_calls.assert_not_called()
        self.assertEqual([m.id for c in self.cleanup.call_args_list for m in c.args[3]], [self.terminated.id])

    def test_failed_tenancy_preflight_precedes_scan_and_mutations(self):
        with patch.object(app, "prepare_identity", side_effect=SystemExit("wrong tenancy")):
            with self.assertRaisesRegex(SystemExit, "wrong tenancy"):
                app.main()
        self.compute.list_instances.assert_not_called()
        self.iam.assert_not_called()
        self.cleanup.assert_not_called()
        self.sources.assert_not_called()

    def test_tenancy_root_compute_scan_and_centralized_group_plan(self):
        root = "ocid1.tenancy.oc1..example"
        self.args.compartment_id = root
        self.args.all_instances = True
        self.args.dry_run = True
        self.parent.compartment_id = root
        self.parent_mi.compartment_id = root
        self.by_compartment[root] = self.by_compartment.pop("parent")
        self.managed.list_managed_instances.side_effect = lambda **kw: response(
            [self.parent_mi] if kw["compartment_id"] == root else [self.child_mi])
        with patch.object(app, "load_auth", return_value=(
                {"tenancy": root, "region": "workload", "user": "test-user"}, {})), \
                patch.object(app, "discover_compartments", return_value=[
                    NS(id=root, name="Example (root)"), NS(id="child", name="Child")]):
            app.main()
        self.assertEqual(self.compute.list_instances.call_args_list, [call(root), call("child")])
        self.assertIn(self.parent.id, self.group_calls.call_args.args[2])
        self.groups.list_managed_instance_groups.assert_called_once_with(compartment_id=root)
        self.assertIn("TENANCY-ROOT SCOPE", self.output.getvalue())
        self.compute.update_instance.assert_not_called()

    def test_signer_forwarded_to_every_workload_client(self):
        self.args.all_instances = True
        self.args.dry_run = True
        signer = Mock()
        config = {"tenancy": "tenancy", "region": "workload", "user": "test-user"}
        with patch.object(app, "load_auth", return_value=(config, {"signer": signer})):
            app.main()
        constructors = [app.oci.core.ComputeClient, app.oci.container_engine.ContainerEngineClient,
                        app.oci.os_management_hub.OnboardingClient,
                        app.oci.os_management_hub.ManagedInstanceClient,
                        app.oci.os_management_hub.ManagedInstanceGroupClient,
                        app.oci.os_management_hub.SoftwareSourceClient,
                        app.oci.os_management_hub.WorkRequestClient]
        for constructor in constructors:
            constructor.assert_called_once_with(config, signer=signer)

    def test_dry_run_reads_subtree_but_plans_groups_only_in_parent(self):
        self.args.all_instances = True
        self.args.dry_run = True
        app.main()

        self.assertEqual(self.compute.list_instances.call_args_list, [call("parent"), call("child")])
        self.assertEqual(self.managed.list_managed_instances.call_args_list,
                         [call(compartment_id="parent"), call(compartment_id="child")])
        self.groups.list_managed_instance_groups.assert_called_once_with(compartment_id="parent")
        self.assertEqual(self.group_calls.call_args.args[0].compartment_id, "parent")
        self.assertEqual(set(self.group_calls.call_args.args[2]),
                         {self.parent.id, self.child.id, self.windows.id})
        self.assertIn("create group osmh-windows_server_2022-x86_64", self.output.getvalue())
        self.assertIn("create group osmh-oracle_linux_9-x86_64", self.output.getvalue())
        self.compute.update_instance.assert_not_called()
        self.profiles.create_profile.assert_not_called()
        self.groups.create_managed_instance_group.assert_not_called()
        self.managed.delete_managed_instance.assert_not_called()

    def test_child_cleanup_and_registration_keep_parent_group_destination(self):
        self.args.all_instances = True
        created = {}

        def create_group(details):
            group = NS(id=details.display_name, display_name=details.display_name,
                       lifecycle_state="ACTIVE", managed_instance_ids=[],
                       software_source_ids=details.software_source_ids,
                       software_sources=[], os_family=details.os_family,
                       arch_type=details.arch_type, vendor_name=details.vendor_name)
            created[group.id] = group
            return response(group)

        self.groups.create_managed_instance_group.side_effect = create_group
        self.groups.get_managed_instance_group.side_effect = lambda group_id: response(created[group_id])
        with patch.object(app.oci, "wait_until", side_effect=lambda client, result, **kwargs: result):
            app.main()

        cleanup_args = {entry.args[0].compartment_id: entry.args for entry in self.cleanup.call_args_list}
        self.assertEqual(set(cleanup_args), {"parent", "child"})
        self.assertEqual([record.id for record in cleanup_args["child"][3]],
                         [self.terminated.id])
        self.assertEqual([record.id for record in cleanup_args["parent"][3]], [])
        self.assertEqual(self.managed.list_managed_instances.call_args_list,
                         [call(compartment_id="parent"), call(compartment_id="child")] * 2)
        self.compute.update_instance.assert_called_once()
        self.assertEqual(self.compute.update_instance.call_args.args[0], self.windows.id)
        self.assertEqual(self.profiles.create_profile.call_args.args[0].compartment_id, "parent")
        self.assertEqual(self.groups.create_managed_instance_group.call_count, 2)
        for entry in self.groups.create_managed_instance_group.call_args_list:
            self.assertEqual(entry.args[0].compartment_id, "parent")
        attached = {identifier for entry in self.groups.attach_managed_instances_to_managed_instance_group.call_args_list
                    for identifier in entry.args[1].managed_instances}
        self.assertEqual(attached, {self.parent.id, self.child.id, self.windows.id})
        self.iam.assert_called_once_with(self.args, self.identity, "tenancy", ["parent", "child"], "test-user")


if __name__ == "__main__":
    unittest.main()
