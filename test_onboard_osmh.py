"""Offline regression tests. Run: .venv/bin/python -B -m unittest -v"""
import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import oci
import onboard_osmh as app


def args(**overrides):
    return NS(**dict(dict(compartment_id="comp", group_prefix="osmh", dry_run=False,
                         skip_iam=False, skip_terminated_cleanup=False,
                         unregistration_timeout=0), **overrides))


def response(data, status=200):
    return NS(data=data, status=status, headers={})


def pages(method, *a, **kw):
    return method(*a, **kw)


def service_error(status):
    return oci.exceptions.ServiceError(status, "NotAuthorizedOrNotFound", {}, "test")


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_windows_image_names_and_versions(self):
        for year in (2016, 2019, 2022, 2025):
            for name, version in [("Windows", f"Server {year} Standard"),
                                  (f"Windows Server {year} Datacenter", "")]:
                with self.subTest(name=name, version=version):
                    platform = app.platform_from_image(NS(shape="VM.Standard.E4.Flex"),
                        NS(operating_system=name, operating_system_version=version))
                    self.assertEqual(platform, app.Platform(f"WINDOWS_SERVER_{year}", "X86_64", "MICROSOFT"))
        self.assertIsNone(app.platform_from_image(NS(shape="VM.Standard.E4.Flex"),
            NS(operating_system="Windows", operating_system_version="Server 2012")))

    def test_windows_11(self):
        self.assertEqual(app.platform_from_image(NS(shape="VM.Standard.E4.Flex"),
            NS(operating_system="Windows", operating_system_version="11")),
            app.Platform("WINDOWS_11", "X86_64", "MICROSOFT"))

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_windows_profile_creation_and_dry_run(self, _):
        platform = app.Platform("WINDOWS_SERVER_2016", "X86_64", "MICROSOFT")
        client = Mock()
        client.list_profiles.return_value = response([])
        client.create_profile.return_value = response(NS(id="win-profile"))
        app.find_or_create_profiles(args(dry_run=True), client, "tenancy", {}, {}, {platform})
        client.create_profile.assert_not_called()
        result = app.find_or_create_profiles(args(), client, "tenancy", {}, {}, {platform})
        self.assertEqual(result[platform], "win-profile")
        details = client.create_profile.call_args.args[0]
        self.assertIsInstance(details, oci.os_management_hub.models.CreateWindowsStandAloneProfileDetails)
        self.assertEqual(details.registration_type, "OCI_WINDOWS")
        self.assertNotIn("software_source_ids", details.swagger_types)

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_reuses_windows_profile(self, _):
        platform = app.Platform("WINDOWS_SERVER_2016", "X86_64", "MICROSOFT")
        client = Mock()
        profile = NS(id="profile", display_name="service-profile", os_family=platform.os_family,
                     arch_type=platform.arch_type, vendor_name="MICROSOFT", registration_type="OCI_WINDOWS",
                     profile_type="WINDOWS_STANDALONE", lifecycle_state="ACTIVE", is_default_profile=False)
        client.list_profiles.side_effect = [response([]), response([profile])]
        self.assertEqual(app.find_or_create_profiles(args(), client, "root", {}, {}, {platform})[platform], "profile")
        client.create_profile.assert_not_called()

    def test_duplicate_vendor_repositories_across_ocids(self):
        software = Mock()
        software.get_software_source.side_effect = lambda _: response(NS(
            software_source_type="VENDOR", repo_id="ol8_baseos_latest",
            os_family="ORACLE_LINUX_8", arch_type="X86_64"))
        group = NS(software_sources=[NS(id="child-source")], software_source_ids=None)
        self.assertEqual(app.missing_group_sources(group, ["root-source"], software, {}), [])

    def test_custom_sources_are_not_equated_by_repository_name(self):
        software = Mock()
        software.get_software_source.return_value = response(NS(software_source_type="CUSTOM", repo_id="same"))
        group = NS(software_sources=[NS(id="custom-old")], software_source_ids=[])
        self.assertEqual(app.missing_group_sources(group, ["custom-new"], software, {}), ["custom-new"])

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_repeat_group_run_does_not_attach_empty_or_duplicate_lists(self, _):
        platform = app.Platform("ORACLE_LINUX_8", "X86_64", "ORACLE")
        group = NS(id="group", display_name="osmh-oracle_linux_8-x86_64", os_family=platform.os_family,
                   arch_type=platform.arch_type, vendor_name=platform.vendor_name,
                   software_sources=[NS(id="source")], software_source_ids=None, managed_instance_ids=["instance"])
        client = Mock()
        client.list_managed_instance_groups.return_value = response([group])
        client.get_managed_instance_group.return_value = response(group)
        app.ensure_groups(args(), client, {"instance": NS(id="instance")},
                          {"instance": platform}, {app.key(platform): ["source"]}, Mock())
        client.attach_software_sources_to_managed_instance_group.assert_not_called()
        client.attach_managed_instances_to_managed_instance_group.assert_not_called()

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    @patch.object(app.oci, "wait_until", side_effect=lambda client, result, **kw: result)
    def test_windows_group_omits_sources(self, *_):
        platform = app.Platform("WINDOWS_SERVER_2016", "X86_64", "MICROSOFT")
        group = NS(id="group", software_sources=[], software_source_ids=[], managed_instance_ids=[])
        client = Mock()
        client.list_managed_instance_groups.return_value = response([])
        client.create_managed_instance_group.return_value = response(group)
        client.get_managed_instance_group.return_value = response(group)
        app.ensure_groups(args(), client, {"instance": NS(id="instance")}, {"instance": platform}, {}, Mock())
        details = client.create_managed_instance_group.call_args.args[0]
        self.assertEqual(details.vendor_name, "MICROSOFT")
        self.assertIsNone(details.software_source_ids)
        client.attach_software_sources_to_managed_instance_group.assert_not_called()
        self.assertEqual(client.attach_managed_instances_to_managed_instance_group.call_args.args[1].managed_instances, ["instance"])

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_cleanup_requires_positive_terminated_state_and_scope(self, _):
        ids = ["terminated", "running", "stopped", "terminating", "missing", "moved"]
        records = [NS(id=f"ocid1.instance.{i}", display_name=i, compartment_id="comp", location="OCI_COMPUTE") for i in ids]
        compute, managed = Mock(), Mock()
        compute.get_instance.side_effect = [response(NS(lifecycle_state=s, compartment_id="comp"))
            for s in ("TERMINATED", "RUNNING", "STOPPED", "TERMINATING")] + [
            service_error(404), response(NS(lifecycle_state="TERMINATED", compartment_id="elsewhere"))]
        managed.delete_managed_instance.return_value = response(None, 204)
        managed.list_managed_instances.return_value = response([])
        work = Mock()
        work.list_work_requests.return_value = response([])
        result = app.cleanup_terminated_instances(args(), compute, managed, records, work)
        self.assertEqual(result.removed, {"ocid1.instance.terminated"})
        self.assertFalse(result.incomplete)
        managed.delete_managed_instance.assert_called_once_with("ocid1.instance.terminated")

    def test_cleanup_dry_run_and_non_oci_are_non_mutating(self):
        compute, managed = Mock(), Mock()
        compute.get_instance.return_value = response(NS(lifecycle_state="TERMINATED", compartment_id="comp"))
        record = NS(id="ocid1.instance.dead", display_name="dead", compartment_id="comp", location="OCI_COMPUTE")
        result = app.cleanup_terminated_instances(args(dry_run=True), compute, managed, [record], Mock())
        self.assertFalse(result.removed)
        managed.delete_managed_instance.assert_not_called()
        compute.reset_mock()
        record.location = "ON_PREMISE"
        app.cleanup_terminated_instances(args(), compute, managed, [record], Mock())
        compute.get_instance.assert_not_called()

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_accepted_request_is_not_completed_cleanup(self, _):
        record = NS(id="ocid1.instance.dead", display_name="dead", compartment_id="comp", location="OCI_COMPUTE")
        compute, managed, work = Mock(), Mock(), Mock()
        compute.get_instance.return_value = response(NS(lifecycle_state="TERMINATED", compartment_id="comp"))
        work.list_work_requests.return_value = response([])
        work.get_work_request.return_value = response(NS(status="ACCEPTED"))
        accepted = response(None, 202)
        accepted.headers["opc-work-request-id"] = "work-id"
        managed.delete_managed_instance.return_value = accepted
        managed.list_managed_instances.return_value = response([record])
        result = app.cleanup_terminated_instances(args(), compute, managed, [record], work)
        self.assertEqual(result.incomplete, {record.id})
        self.assertFalse(result.removed)
        self.assertIn("not confirmed before timeout", self.output.getvalue())

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_pending_unregister_is_resumed_without_another_delete(self, _):
        record = NS(id="ocid1.instance.dead", display_name="dead", compartment_id="comp", location="OCI_COMPUTE")
        compute, managed, work = Mock(), Mock(), Mock()
        compute.get_instance.return_value = response(NS(lifecycle_state="TERMINATED", compartment_id="comp"))
        work.list_work_requests.return_value = response([NS(id="pending-work")])
        managed.list_managed_instances.return_value = response([])
        result = app.cleanup_terminated_instances(args(), compute, managed, [record], work)
        self.assertEqual(result.removed, {record.id})
        managed.delete_managed_instance.assert_not_called()

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_failed_unregistration_reports_service_errors(self, _):
        record = NS(id="ocid1.instance.dead", display_name="dead")
        managed, work = Mock(), Mock()
        managed.list_managed_instances.return_value = response([record])
        work.get_work_request.return_value = response(NS(status="FAILED", message=None))
        work.list_work_request_errors.return_value = response([NS(code="AgentError", message="example failure")])
        self.assertFalse(app.wait_for_unregistration(args(), managed, work, record, "work-id"))
        self.assertIn("AgentError: example failure", self.output.getvalue())

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    @patch.object(app.time, "sleep")
    def test_record_disappears_even_while_job_is_accepted(self, sleep, _):
        record = NS(id="ocid1.instance.dead", display_name="dead")
        managed, work = Mock(), Mock()
        managed.list_managed_instances.side_effect = [response([record]), response([])]
        work.get_work_request.return_value = response(NS(status="ACCEPTED"))
        self.assertTrue(app.wait_for_unregistration(args(unregistration_timeout=30), managed, work, record, "work-id"))
        sleep.assert_called_once()

    @patch.object(app, "list_call_get_all_results", side_effect=pages)
    def test_work_succeeded_but_record_present_is_not_confirmed(self, _):
        record = NS(id="ocid1.instance.dead", display_name="dead")
        managed, work = Mock(), Mock()
        managed.list_managed_instances.return_value = response([record])
        work.get_work_request.return_value = response(NS(status="SUCCEEDED"))
        self.assertFalse(app.wait_for_unregistration(args(), managed, work, record, "work-id"))

    def test_skip_iam_does_not_call_identity(self):
        identity = Mock()
        self.assertFalse(app.ensure_iam(args(skip_iam=True), identity, "root", "comp"))
        self.assertEqual(identity.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
