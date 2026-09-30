"""Offline Ubuntu/OL7, repository, regional and cleanup-selection regressions."""
import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import oci
import onboard_osmh as app


def options(**kw):
    return NS(**{**dict(compartment_id="root", profile_map=None, software_source_map=None,
        repository_map=None, repository_families="none", region="r1", all_regions=False,
        dry_run=True, group_prefix="osmh", interactive=False, skip_terminated_cleanup=False,
        unregister_all=False, unregister_instance_ids=None, instance_ids=None,
        unregistration_timeout=0, source_selection_timeout=0), **kw})


def repo(name, family="ORACLE_LINUX_7", arch="X86_64", state="SELECTED"):
    return NS(id="id-" + name, repo_id=name, display_name=name, os_family=family,
              arch_type=arch, availability_at_oci=state, lifecycle_state="ACTIVE", software_source_type="VENDOR")


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(patch.object(app, "list_call_get_all_results", side_effect=lambda f,*a,**kw:f(*a,**kw)))

    def test_os_architecture_matrix(self):
        for os, version, vendor in [("Ubuntu", "20.04", "CANONICAL"), ("Ubuntu", "22.04", "CANONICAL"),
                                    ("Ubuntu", "24.04", "CANONICAL"), ("Oracle Linux", "7.9", "ORACLE")]:
            for arch, shape in [("X86_64", "VM.Standard.E4.Flex"), ("AARCH64", "VM.Standard.A1.Flex")]:
                with self.subTest(os=os, version=version, arch=arch):
                    p = app.platform_from_image(NS(shape=shape), NS(operating_system=os, operating_system_version=version))
                    if version == "20.04" and arch == "AARCH64":
                        self.assertIsNone(p)
                    else:
                        self.assertEqual((p.vendor_name, p.arch_type), (vendor, arch))

    def test_ubuntu_standalone_profile_not_oracle_sources(self):
        p = app.Platform("UBUNTU_24_04", "AARCH64", "CANONICAL")
        client = Mock()
        client.list_profiles.return_value = NS(data=[])
        client.create_profile.return_value = NS(data=NS(id="ubuntu-profile"))
        result = app.find_or_create_profiles(options(dry_run=False), client, "root", {}, {}, {p})
        details = client.create_profile.call_args.args[0]
        self.assertIsInstance(details, oci.os_management_hub.models.CreateUbuntuStandAloneProfileDetails)
        self.assertEqual(details.registration_type, "OCI_LINUX")
        self.assertEqual(details.vendor_name, "CANONICAL")
        self.assertEqual(result[p], "ubuntu-profile")
        software = Mock()
        self.assertEqual(app.ensure_oracle_linux_sources(options(), software, "root", {p}, {}), {})
        software.list_software_sources.assert_not_called()

    def test_ol7_requires_latest_and_els_on_each_arch(self):
        for arch in ("X86_64", "AARCH64"):
            software = Mock()
            catalog = [repo("ol7_latest", arch=arch), repo("ol7_latest_ELS", arch=arch)]
            software.list_software_sources.return_value = NS(data=catalog)
            p = app.Platform("ORACLE_LINUX_7", arch, "ORACLE")
            result = app.ensure_oracle_linux_sources(options(), software, "root", {p}, {})
            self.assertEqual(result[app.key(p)], [s.id for s in catalog])

    def test_ubuntu_group_does_not_require_rpm_software_sources(self):
        p = app.Platform("UBUNTU_22_04", "X86_64", "CANONICAL")
        groups = Mock()
        groups.list_managed_instance_groups.return_value = NS(data=[])
        mi = NS(id="ubuntu", display_name="Ubuntu", managed_instance_group=None, lifecycle_stage=None)
        app.ensure_groups(options(), groups, {mi.id:mi}, {mi.id:p}, {}, Mock())
        self.assertIn("create group osmh-ubuntu_22_04-x86_64", self.output.getvalue())
        groups.create_managed_instance_group.assert_not_called()

    def test_ubuntu_default_standalone_profile_is_reused(self):
        p = app.Platform("UBUNTU_20_04", "X86_64", "CANONICAL")
        profile = NS(id="default", display_name="Ubuntu default", os_family=p.os_family, arch_type=p.arch_type,
                     vendor_name=p.vendor_name, registration_type="OCI_LINUX", profile_type="UBUNTU_STANDALONE",
                     lifecycle_state="ACTIVE", is_default_profile=True)
        client = Mock()
        client.list_profiles.return_value = NS(data=[profile])
        result = app.find_or_create_profiles(options(dry_run=False), client, "root", {}, {}, {p})
        self.assertEqual(result[p], "default")
        client.create_profile.assert_not_called()

    def test_cleanup_rechecks_state_after_selection_before_deleting(self):
        compute, records, oke = self.cleanup_fixture()
        chosen = app.select_terminated_instances(options(unregister_all=True), compute, records, oke, {})
        self.assertIn(records[0].id, chosen)
        compute.get_instance.side_effect = None
        compute.get_instance.return_value = NS(data=NS(compartment_id="root", lifecycle_state="RUNNING"))
        managed = Mock()
        app.cleanup_terminated_instances(options(dry_run=False), compute, managed, [records[0]], Mock())
        managed.delete_managed_instance.assert_not_called()

    def test_missing_els_blocks_before_selection(self):
        software = Mock()
        software.list_software_sources.return_value = NS(data=[repo("ol7_latest", state="AVAILABLE")])
        with self.assertRaisesRegex(SystemExit, "ol7_latest_ELS"):
            app.ensure_oracle_linux_sources(options(), software, "root", {app.Platform("ORACLE_LINUX_7", "X86_64", "ORACLE")}, {})
        software.change_availability_of_software_sources.assert_not_called()

    def test_extended_families_filter_platform_and_debug_sources(self):
        p = app.Platform("ORACLE_LINUX_7", "X86_64", "ORACLE")
        names = ["ol7_UEKR6", "ol7_ksplice", "ol7_ksplice_ELS", "ol7_MySQL80", "ol7_oci_included"]
        catalog = [repo(n) for n in names] + [repo("ol7_MySQL80_debuginfo"), repo("ol7_UEKR7", arch="AARCH64")]
        selected = app.extra_repository_ids(options(repository_families="uek,ksplice,mysql,oci"), p, catalog)
        self.assertEqual(set(selected), set(names))

    @patch.object(app.sys.stdin, "isatty", return_value=False)
    def test_multiple_kernel_streams_are_auto_attached_for_function_runs(self, _):
        p = app.Platform("ORACLE_LINUX_7", "X86_64", "ORACLE")
        self.assertEqual(app.extra_repository_ids(options(repository_families="uek"), p,
                                                  [repo("ol7_UEKR5"), repo("ol7_UEKR6")]),
                         ["ol7_UEKR5", "ol7_UEKR6"])

    @patch("builtins.input", side_effect=AssertionError("repository selection should not prompt"))
    def test_multiple_mysql_streams_do_not_prompt(self, _):
        p = app.Platform("ORACLE_LINUX_7", "X86_64", "ORACLE")
        result = app.extra_repository_ids(options(repository_families="mysql", interactive=True), p,
                                          [repo("ol7_MySQL57"), repo("ol7_MySQL80")])
        self.assertEqual(result, ["ol7_MySQL57", "ol7_MySQL80"])

    @patch.object(app, "load_json", return_value={"ORACLE_LINUX_7:X86_64": ["ol7_UEKR6", "ol7_UEKR6_ELS"]})
    def test_portable_repository_map_overrides_ambiguous_families(self, _):
        self.assertEqual(app.extra_repository_ids(options(), app.Platform("ORACLE_LINUX_7", "X86_64", "ORACLE"), []),
                         ["ol7_UEKR6", "ol7_UEKR6_ELS"])

    def cleanup_fixture(self):
        records = [NS(id="ocid1.instance." + n, display_name=n, location="OCI_COMPUTE", compartment_id="root")
                   for n in ("dead1", "dead2", "live", "oke", "unknown", "moved")]
        compute = Mock()
        def get(identifier):
            name = identifier.rsplit(".", 1)[1]
            if name == "unknown":
                raise oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "missing")
            return NS(data=NS(compartment_id="elsewhere" if name == "moved" else "root",
                              lifecycle_state="RUNNING" if name == "live" else "TERMINATED",
                              freeform_tags={}, defined_tags={}, metadata={}))
        compute.get_instance.side_effect = get
        return compute, records, {"ocid1.instance.oke": "pool"}

    @patch("builtins.input", return_value="2")
    def test_cleanup_selection_only_chosen_verified_terminated(self, _):
        compute, records, oke = self.cleanup_fixture()
        chosen = app.select_terminated_instances(options(interactive=True), compute, records, oke, {"root":"Root"})
        self.assertEqual(chosen, {"ocid1.instance.dead2"})
        self.assertIn("Compartment: Root", self.output.getvalue())

    @patch.object(app.sys.stdin, "isatty", return_value=False)
    def test_noninteractive_registration_all_does_not_approve_cleanup(self, _):
        compute, records, oke = self.cleanup_fixture()
        self.assertEqual(app.select_terminated_instances(options(all_instances=True), compute, records, oke, {}), set())

    def test_explicit_cleanup_all_and_unsafe_ids(self):
        compute, records, oke = self.cleanup_fixture()
        self.assertEqual(app.select_terminated_instances(options(unregister_all=True), compute, records, oke, {}),
                         {"ocid1.instance.dead1", "ocid1.instance.dead2"})
        with self.assertRaisesRegex(SystemExit, "unverified"):
            app.select_terminated_instances(options(unregister_instance_ids="ocid1.instance.live"), compute, records, oke, {})

    def test_cleanup_skip_performs_no_compute_reads(self):
        compute, records, oke = self.cleanup_fixture()
        self.assertEqual(app.select_terminated_instances(options(skip_terminated_cleanup=True), compute, records, oke, {}), set())
        compute.get_instance.assert_not_called()

    @patch.object(app, "load_json", return_value={"regions":{"r1":{"p":"one"},"r2":{"p":"two"}}})
    def test_regional_ocid_map_isolated(self, _):
        self.assertEqual(app.regional_map(options(all_regions=True, region="r2"), "map"), {"p":"two"})
        with self.assertRaisesRegex(SystemExit, "needs a regions"):
            app.regional_map(options(all_regions=True, region="r3"), "map")

    @patch.object(app, "load_json", return_value={"p":"foreign-regional-ocid"})
    def test_flat_ocid_map_rejected_for_multiregion(self, _):
        with self.assertRaisesRegex(SystemExit, "keyed by regions"):
            app.regional_map(options(all_regions=True), "map")

    def test_all_regions_ready_only_and_failure_does_not_hide_other_results(self):
        identity = Mock()
        identity.list_region_subscriptions.return_value = NS(data=[
            NS(region_name="r1", status="READY"), NS(region_name="r2", status="READY"),
            NS(region_name="r3", status="IN_PROGRESS")])
        signer = Mock()
        with patch.object(app,"arguments",return_value=options(all_regions=True)), \
             patch.object(app,"load_auth",return_value=({"tenancy":"root", "region":"r1"},{"signer":signer})), \
             patch.object(app,"prepare_identity",return_value=identity), \
             patch.object(app,"run_region",side_effect=[SystemExit("region unavailable"), None]) as run:
            with self.assertRaises(SystemExit) as error:
                app.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual([c.args[1]["region"] for c in run.call_args_list], ["r1","r2"])
        self.assertTrue(all(c.args[2]["signer"] is signer for c in run.call_args_list))
        self.assertIn("r2: preview completed", self.output.getvalue())

    def test_cli_new_flags_and_region_exclusivity(self):
        with patch("sys.argv", ["script", "root", "--all-regions", "--cleanup-only", "--unregister-all"]):
            parsed = app.arguments()
        self.assertTrue(parsed.all_regions)
        self.assertTrue(parsed.cleanup_only)
        self.assertTrue(parsed.unregister_all)
        self.assertEqual(parsed.repository_families, "uek,ksplice,mysql,oci")
        with patch("sys.argv", ["script", "root", "--all-regions", "--region", "r1"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.arguments()

    def test_region_parser_preserves_order_trims_and_deduplicates(self):
        self.assertEqual(app.parse_regions(" r2, r1 ,r2"), ["r2", "r1"])
        self.assertEqual(app.parse_regions("r1"), ["r1"])
        self.assertEqual(app.parse_regions(None), [])
        for invalid in ("", "r1,", ",r1", "r1,,r2", "r1 r2", "R1"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                app.parse_regions(invalid)

    def test_regions_alias_and_all_regions_are_mutually_exclusive(self):
        for flag in ("--region", "--regions"):
            with patch("sys.argv", ["script", "root", flag, "r2,r1"]):
                self.assertEqual(app.arguments().region, "r2,r1")
            with patch("sys.argv", ["script", "root", flag, "r1", "--all-regions"]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.arguments()

    def test_explicit_region_subset_order_and_unavailable_regions_skipped(self):
        identity = Mock()
        identity.list_region_subscriptions.return_value = NS(data=[
            NS(region_name="r1", status="READY"), NS(region_name="r2", status="READY"),
            NS(region_name="r3", status="IN_PROGRESS"), NS(region_name="r4", status="READY")])
        for request, expected in [("r2,r1,r2", ["r2","r1"]), ("r1,r3", ["r1"]),
                                  ("r1,missing", ["r1"]), ("missing,r2,r1", ["r2","r1"]),
                                  ("r3,missing", None)]:
            with self.subTest(request=request), \
                    patch.object(app,"arguments",return_value=options(region=request)), \
                    patch.object(app,"load_auth",return_value=({"tenancy":"root", "region":"r2"},{})) as auth, \
                    patch.object(app,"prepare_identity",return_value=identity), \
                    patch.object(app,"run_region") as run:
                if expected is None:
                    with self.assertRaisesRegex(SystemExit, "No requested regions"):
                        app.main()
                    run.assert_not_called()
                else:
                    app.main()
                    self.assertIsNone(auth.call_args.args[0].region)
                    self.assertEqual([c.args[1]["region"] for c in run.call_args_list], expected)
                    self.assertTrue(all(c.args[0].multi_region for c in run.call_args_list))
                if "missing" in request:
                    self.assertIn("missing: SKIPPED: tenancy is not subscribed", self.output.getvalue())
                if "r3" in request:
                    self.assertIn("status=IN_PROGRESS", self.output.getvalue())

    def test_unavailable_regions_do_not_require_regional_map_entries(self):
        identity = Mock()
        identity.list_region_subscriptions.return_value = NS(data=[NS(region_name="r1", status="READY")])
        with patch.object(app,"arguments",return_value=options(region="missing,r1", profile_map="map")), \
             patch.object(app,"load_auth",return_value=({"tenancy":"root", "region":"bootstrap"},{})), \
             patch.object(app,"prepare_identity",return_value=identity) as preflight, \
             patch.object(app,"load_json",return_value={"regions":{"r1":{}}}), \
             patch.object(app,"run_region") as run:
            app.main()
        preflight.assert_called_once_with({"tenancy":"root", "region":"bootstrap"}, {}, "root",
                                         require_workload_region=False)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1]["region"], "r1")
        self.assertIn("Regional summary:", self.output.getvalue())

    @patch.object(app, "load_json", return_value={"p":"regional-ocid"})
    def test_explicit_multiple_regions_reject_flat_ocid_maps(self, _):
        with self.assertRaisesRegex(SystemExit, "keyed by regions"):
            app.regional_map(options(multi_region=True), "map")

    def test_deduplicated_single_region_uses_single_region_flow(self):
        with patch.object(app,"arguments",return_value=options(region="r1,r1")), \
             patch.object(app,"load_auth",return_value=({"tenancy":"root", "region":"r1"},{})), \
             patch.object(app,"prepare_identity",return_value=Mock()), \
             patch.object(app,"run_region") as run:
            app.main()
        run.assert_called_once()
        self.assertFalse(run.call_args.args[0].multi_region)


if __name__ == "__main__":
    unittest.main()
