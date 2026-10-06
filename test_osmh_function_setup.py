"""Home-region, network selection and secret-handling checks. No live OCI calls."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import osmh_function_setup as setup
import osmh_deployment as chain
import onboard_osmh as app

ROOT = "ocid1.compartment.oc1..test123456789012"
CONFIG = {"tenancy": "ocid1.tenancy.oc1..test", "user": "ocid1.user.oc1..test", "region": "us-phoenix-1"}
HOME = NS(region_name="us-ashburn-1", region_key="IAD", is_home_region=True, status="READY")


class FunctionSetupTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def registry(self, name="gautam.mishra@oracle.com", config=CONFIG):
        identity = Mock()
        identity.get_user.return_value = NS(data=NS(id=CONFIG["user"], name=name, identity_provider_id=None))
        with patch.object(setup.oci.object_storage, "ObjectStorageClient") as storage, \
                patch.object(setup.oci.artifacts, "ArtifactsClient") as artifacts:
            storage.return_value.get_namespace.return_value = NS(data="id3kvohtwgjy")
            artifacts.return_value.base_client.endpoint = "https://artifacts.us-ashburn-1.oci.oraclecloud.com"
            result = setup.registry_details(identity, config, {}, HOME)
            self.assertEqual(storage.call_args.args[0]["region"], "us-ashburn-1")
        identity.get_user.assert_called_once_with(CONFIG["user"])
        return result

    def test_profile_user_ocid_resolves_login_name_and_home_registry(self):
        host, namespace, username, allowed = self.registry()
        self.assertEqual(host, "iad.ocir.io")
        self.assertEqual(username, "id3kvohtwgjy/gautam.mishra@oracle.com")
        self.assertIn("ocir.us-ashburn-1.oci.oraclecloud.com", allowed)
        self.assertNotIn("phx.ocir.io", allowed)

    def test_default_and_nondefault_domain_names(self):
        self.assertEqual(self.registry("Default/user")[2], "id3kvohtwgjy/user")
        self.assertEqual(self.registry("ExampleDomain/user")[2], "id3kvohtwgjy/ExampleDomain/user")

    @patch.object(setup, "registry_details", return_value=("iad.ocir.io", "ns", "ns/user", {"iad.ocir.io"}))
    def test_image_is_automatic_and_foreign_hosts_are_rejected(self, _):
        image, host, username = setup.resolve_image(Mock(), CONFIG, {}, HOME, ROOT)
        self.assertTrue(image.startswith("iad.ocir.io/ns/osmh-worker-123456789012:build-"))
        self.assertEqual(image, setup.automatic_image("iad.ocir.io", "ns", ROOT))
        for invalid in ("phx.ocir.io/ns/image:v1", "iad.ocir.io/other/image:v1", "evil.example/ns/image:v1"):
            with self.assertRaises(SystemExit):
                setup.resolve_image(Mock(), CONFIG, {}, HOME, ROOT, invalid)

    @patch.object(setup, "home_region", return_value=HOME)
    def test_home_region_overrides_workload_and_rejects_conflicting_deployment(self, _):
        args = app.arguments([ROOT, "--dry-run", "--regions", "us-phoenix-1"])
        with patch.object(setup, "resolve_image", return_value=("iad.ocir.io/ns/image:v1", "iad.ocir.io", "ns/user")):
            chain.prepare(args, CONFIG, ["us-phoenix-1"], Mock())
        self.assertEqual(args.deployment_region, "us-ashburn-1")
        args.deployment_region = "us-phoenix-1"
        with self.assertRaisesRegex(SystemExit, "home region"):
            chain.prepare(args, CONFIG, ["us-phoenix-1"], Mock())

    @patch.object(setup, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    def test_picker_filters_unavailable_vcns_and_subnets_and_uses_home(self, _):
        vcn = NS(id="vcn", display_name="Shared", compartment_id=ROOT, lifecycle_state="AVAILABLE")
        subnet = NS(id="subnet", display_name="Private", vcn_id="vcn", cidr_block="10.0.0.0/24",
                    lifecycle_state="AVAILABLE", prohibit_public_ip_on_vnic=True)
        args = NS(compartment_id=ROOT, deployment_region="us-ashburn-1", network_compartment_id=None)
        with patch.object(setup, "discover_compartments", return_value=[NS(id=ROOT, name="Root")]), \
                patch("osmh_runtime.validate_compartment_tenancy"), \
                patch.object(setup.oci.core, "VirtualNetworkClient") as factory, \
                patch("builtins.input", side_effect=["1", "1"]):
            client = factory.return_value
            client.list_vcns.return_value = NS(data=[vcn, NS(lifecycle_state="TERMINATING")])
            client.list_subnets.return_value = NS(data=[subnet, NS(lifecycle_state="TERMINATING")])
            setup.select_network(args, Mock(), CONFIG, {})
        self.assertEqual(factory.call_args.args[0]["region"], "us-ashburn-1")
        self.assertEqual(args.function_subnet_ids, "subnet")
        client.list_subnets.assert_called_once_with(ROOT, vcn_id="vcn")

    @patch.object(setup, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    def test_picker_reuses_active_function_application_in_home_region(self, _):
        child = "ocid1.compartment.oc1..child"
        sibling = "ocid1.compartment.oc1..sibling"
        app_row = NS(id="ocid1.fnapp.oc1..app", display_name="ExistingOSMH", compartment_id=child,
                     lifecycle_state="ACTIVE")
        args = NS(compartment_id=ROOT, deployment_region="us-ashburn-1", network_compartment_id=None)
        compartments = [NS(id=ROOT, name="Onboarding", compartment_id=CONFIG["tenancy"]),
                        NS(id=child, name="SharedFunctions", compartment_id=ROOT)]
        identity = Mock()
        with patch.object(setup, "discover_compartments", return_value=compartments) as discover, \
                patch("osmh_runtime.validate_compartment_tenancy"), \
                patch.object(setup.oci.functions, "FunctionsManagementClient") as factory, \
                patch("builtins.input", side_effect=["1"]):
            client = factory.return_value
            client.list_applications.side_effect = lambda compartment_id: NS(
                data=([app_row, NS(lifecycle_state="DELETED")] if compartment_id == child else
                      [NS(id="ignored", display_name="SiblingApp", compartment_id=sibling,
                          lifecycle_state="ACTIVE")] if compartment_id == sibling else []))
            setup.select_application(args, identity, CONFIG, {})
        self.assertEqual(factory.call_args.args[0]["region"], "us-ashburn-1")
        discover.assert_called_once_with(identity, ROOT)
        self.assertEqual(args.function_application_id, "ocid1.fnapp.oc1..app")
        self.assertIn("Onboarding / SharedFunctions", self.output.getvalue())
        self.assertNotIn("SiblingApp", self.output.getvalue())

    @patch.object(setup, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    def test_picker_returns_false_when_no_function_application_exists(self, _):
        args = NS(compartment_id=ROOT, deployment_region="us-ashburn-1", network_compartment_id=None)
        identity = Mock()
        with patch.object(setup, "discover_compartments", return_value=[NS(id=ROOT, name="Root")]), \
                patch("osmh_runtime.validate_compartment_tenancy"), \
                patch.object(setup.oci.functions, "FunctionsManagementClient") as factory, \
                patch("builtins.input") as prompt:
            factory.return_value.list_applications.return_value = NS(data=[])
            self.assertFalse(setup.select_application(args, identity, CONFIG, {}))
        prompt.assert_not_called()
        self.assertFalse(hasattr(args, "function_application_id"))
        self.assertIn("continuing with new Function application setup", self.output.getvalue())

    def test_validate_application_rejects_inactive_app(self):
        args = NS(function_application_id="ocid1.fnapp.oc1..app", deployment_region="us-ashburn-1")
        with patch.object(setup.oci.functions, "FunctionsManagementClient") as factory:
            factory.return_value.get_application.return_value = NS(data=NS(
                id=args.function_application_id, display_name="Old", lifecycle_state="DELETED"))
            with self.assertRaisesRegex(SystemExit, "not ACTIVE"):
                setup.validate_application(args, CONFIG, {})

    def test_login_uses_password_stdin_and_temporary_config(self):
        token = "secret-test-token"
        with patch.object(setup.getpass, "getpass", return_value=token), \
                patch.object(setup, "prepare_docker_config"), \
                patch.object(setup.subprocess, "run", return_value=NS(returncode=0)) as run:
            with setup.docker_session("iad.ocir.io", "namespace/user") as env:
                directory = Path(env["DOCKER_CONFIG"])
                self.assertTrue(directory.exists())
                command = run.call_args.args[0]
                self.assertNotIn(token, " ".join(command))
                self.assertIn("--password-stdin", command)
                self.assertEqual(run.call_args.kwargs["input"], token + "\n")
                self.assertNotIn(token, str(env))
            self.assertFalse(directory.exists())
        self.assertNotIn(token, self.output.getvalue())

    def test_login_failure_does_not_expose_captured_output(self):
        with patch.object(setup.getpass, "getpass", return_value="secret"), \
                patch.object(setup, "prepare_docker_config"), \
                patch.object(setup.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    1, ["docker"], output="secret", stderr="secret")):
            with self.assertRaises(SystemExit) as caught:
                with setup.docker_session("iad.ocir.io", "namespace/user"):
                    self.fail("must not build/push after login failure")
        self.assertNotIn("secret", str(caught.exception))

    def test_temporary_settings_preserve_context_without_copying_existing_auth(self):
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as destination:
            root = Path(original)
            (root / "contexts").mkdir()
            (root / "cli-plugins").mkdir()
            (root / "config.json").write_text(json.dumps({
                "currentContext": "colima", "credsStore": "desktop", "auths": {"registry": "secret"}}))
            with patch.dict(setup.os.environ, {"DOCKER_CONFIG": original}):
                setup.prepare_docker_config(destination)
            settings = json.loads((Path(destination) / "config.json").read_text())
            self.assertEqual(settings["currentContext"], "colima")
            self.assertNotIn("auths", settings)
            self.assertNotIn("credsStore", settings)
            self.assertEqual((Path(destination) / "contexts").resolve(), (root / "contexts").resolve())
            self.assertIn(str((root / "cli-plugins").resolve()), settings["cliPluginsExtraDirs"])
            self.assertIn("secret", (root / "config.json").read_text())


if __name__ == "__main__":
    unittest.main()
