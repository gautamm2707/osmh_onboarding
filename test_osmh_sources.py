"""Regression coverage: vendor catalog presence is not SELECTED availability."""
import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import onboard_osmh as app


def source(repo, state="AVAILABLE", ocid=None, kind="VENDOR", arch="X86_64"):
    return NS(id=ocid or f"ocid1.osmhsoftwaresource.{repo}", repo_id=repo,
              display_name=f"{repo}-{arch.lower()}", os_family="ORACLE_LINUX_10",
              arch_type=arch, lifecycle_state="ACTIVE", availability_at_oci=state,
              availability="SELECTED", software_source_type=kind)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.args = NS(dry_run=False, source_selection_timeout=0)
        self.platform = app.Platform("ORACLE_LINUX_10", "X86_64", "ORACLE")
        self.output = io.StringIO()
        self.context = contextlib.redirect_stdout(self.output)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.pagination = patch.object(app, "list_call_get_all_results", side_effect=lambda f, *a, **kw: f(*a, **kw))
        self.pagination.start()
        self.addCleanup(self.pagination.stop)

    def test_available_catalog_sources_are_selected_using_ocids(self):
        catalog = [source("ol10_baseos_latest"), source("ol10_appstream")]
        selected = [source(s.repo_id, "SELECTED", ocid="tenancy-" + s.repo_id) for s in catalog]
        software = Mock()
        software.list_software_sources.side_effect = [NS(data=catalog), NS(data=selected), NS(data=selected)]
        result = app.ensure_oracle_linux_sources(self.args, software, "root", {self.platform}, {})
        self.assertEqual(result[app.key(self.platform)], [s.id for s in selected])
        self.assertEqual(software.change_availability_of_software_sources.call_count, 2)
        for call, original in zip(software.change_availability_of_software_sources.call_args_list, catalog):
            details = call.args[0].software_source_availabilities[0]
            self.assertEqual(details.software_source_id, original.id)
            self.assertEqual(details.availability_at_oci, "SELECTED")
            self.assertIsNone(details.availability)

    def test_selected_repositories_reused_and_custom_patch_ignored(self):
        selected = [source("ol10_baseos_latest", "SELECTED"), source("ol10_appstream", "SELECTED")]
        software = Mock()
        software.list_software_sources.return_value = NS(data=[
            source("ol10_u1_baseos_patch", "SELECTED"),
            source("ol10_baseos_latest", "SELECTED", "custom", kind="CUSTOM"), *selected])
        result = app.ensure_oracle_linux_sources(self.args, software, "root", {self.platform}, {})
        self.assertEqual(result[app.key(self.platform)], [s.id for s in selected])
        software.change_availability_of_software_sources.assert_not_called()

    def test_dry_run_reports_selection_without_writes_or_waiting(self):
        self.args.dry_run = True
        software = Mock()
        software.list_software_sources.return_value = NS(data=[source("ol10_baseos_latest"), source("ol10_appstream")])
        result = app.ensure_oracle_linux_sources(self.args, software, "root", {self.platform}, {})
        self.assertTrue(all(s.startswith("dry-run-source:") for s in result[app.key(self.platform)]))
        self.assertIn("verify ol10_appstream", self.output.getvalue())
        software.change_availability_of_software_sources.assert_not_called()
        self.assertEqual(software.list_software_sources.call_count, 1)

    def test_timeout_never_returns_unselected_source(self):
        software = Mock()
        software.list_software_sources.return_value = NS(data=[])
        with self.assertRaisesRegex(SystemExit, "did not become ACTIVE/SELECTED"):
            app.prepare_oci_source(self.args, software, "root", source("ol10_baseos_latest"))

    def test_non_oci_selected_does_not_imply_oci_selected(self):
        record = source("ol10_appstream", "AVAILABLE")
        self.assertFalse(app.source_ready(record))
        record.availability_at_oci = "SELECTED"
        record.lifecycle_state = "CREATING"
        self.assertFalse(app.source_ready(record))

    def test_restricted_source_not_selected(self):
        software = Mock()
        with self.assertRaisesRegex(SystemExit, "entitlement"):
            app.prepare_oci_source(self.args, software, "root", source("ol10_appstream", "RESTRICTED"))
        software.change_availability_of_software_sources.assert_not_called()

    def test_explicit_map_validates_architecture(self):
        software = Mock()
        software.get_software_source.return_value = NS(data=source("ol10_baseos_latest", arch="AARCH64"))
        with self.assertRaisesRegex(SystemExit, "incompatible"):
            app.ensure_oracle_linux_sources(self.args, software, "root", {self.platform},
                                            {app.key(self.platform): ["mapped"]})
        software.change_availability_of_software_sources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
