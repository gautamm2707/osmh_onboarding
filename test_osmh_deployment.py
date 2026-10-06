"""Offline checks for the automatic local deployment chain and its mutation boundaries."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import onboard_osmh as app
import osmh_deployment as chain
import reconcile_osmh as reconcile
import deploy_osmh_function as deployment

ROOT = "ocid1.compartment.oc1..test123456789012"
TENANCY = "ocid1.tenancy.oc1..test"
IMAGE = "iad.ocir.io/testnamespace/osmh-worker:v1"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        home = patch.object(chain.setup, "home_region", return_value=NS(region_name="us-ashburn-1", region_key="IAD"))
        home.start()
        self.addCleanup(home.stop)
        image = patch.object(chain.setup, "resolve_image", return_value=(IMAGE, "iad.ocir.io", "testnamespace/user"))
        image.start()
        self.addCleanup(image.stop)

    def args(self, *extras):
        return app.arguments([ROOT, "--all", "--function-image", IMAGE,
                              "--create-function-network", *extras])

    @patch.object(chain.subprocess, "run")
    @patch.object(chain.shutil, "which", return_value="tool")
    def test_dry_run_does_not_prompt_build_or_invoke_deployer(self, which, run):
        args = self.args("--dry-run")
        with patch("builtins.input") as prompt:
            chain.prepare(args, {"tenancy": TENANCY}, ["us-ashburn-1"])
            chain.deploy(args, ["us-ashburn-1"], 2)
        prompt.assert_not_called()
        run.assert_not_called()
        which.assert_not_called()

    @patch.object(chain.subprocess, "run")
    def test_chain_passes_profile_regions_namespace_and_one_apply(self, run):
        args = self.args("--profile", "pridhu", "--deployment-region", "us-ashburn-1")
        chain.deploy(args, ["us-ashburn-1", "us-phoenix-1"], 3)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("reconcile_osmh.py"))
        for flag, value in [("--profile", "pridhu"), ("--workload-regions", "us-ashburn-1,us-phoenix-1"),
                            ("--region", "us-ashburn-1"), ("--image", IMAGE)]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--deploy", command)
        self.assertIn("--create-network", command)
        self.assertIn("--apply", command)
        self.assertIn("--yes", command)

    @patch.object(chain.subprocess, "run")
    def test_chain_can_reuse_existing_function_application(self, run):
        args = app.arguments([ROOT, "--all", "--function-image", IMAGE,
                              "--function-application-id", "ocid1.fnapp.oc1..app"])
        args.deployment_region = "us-ashburn-1"
        chain.deploy(args, ["us-ashburn-1"], 1)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--application-id") + 1], "ocid1.fnapp.oc1..app")
        self.assertNotIn("--create-network", command)
        self.assertNotIn("--subnet-ids", command)

    @patch.object(chain.subprocess, "run")
    def test_chain_passes_optional_invocation_logging(self, run):
        args = self.args("--enable-function-logging")
        args.deployment_region = "us-ashburn-1"
        chain.deploy(args, ["us-ashburn-1"], 1)
        self.assertIn("--enable-logging", run.call_args.args[0])

    @patch.object(chain.subprocess, "run")
    def test_chain_can_skip_initial_function_invoke(self, run):
        args = self.args("--skip-initial-function-invoke")
        args.deployment_region = "us-ashburn-1"
        chain.deploy(args, ["us-ashburn-1"], 1)
        self.assertIn("--skip-initial-invoke", run.call_args.args[0])

    def test_terraform_avoids_bootstrap_dynamic_groups_and_uses_supported_scheduler_action(self):
        main = Path("deployment/main.tf").read_text()
        self.assertNotIn("resource \"oci_identity_dynamic_group\"", main)
        self.assertIn('action             = "START_RESOURCE"', main)
        self.assertIn("count              = var.logging_enabled ? 1 : 0", main)

    def test_deployment_mode_backfills_existing_managed_network_state(self):
        args = NS(application_id="ocid1.fnapp.oc1..external", subnet_ids="", create_network=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform.tfstate").write_text(json.dumps({"resources": [
                {"type": "oci_functions_application"},
                {"type": "oci_core_vcn"},
            ]}))
            mode = deployment.reconcile_deployment_mode(args, root)
            self.assertEqual(mode["mode"], "create_network")
            self.assertEqual(args.application_id, "")
            self.assertTrue(args.create_network)
            self.assertEqual(args.subnet_ids, "")

    @patch.object(chain.subprocess, "run")
    def test_all_regions_passes_future_subscription_discovery(self, run):
        args = self.args("--all-regions", "--deployment-region", "us-ashburn-1")
        chain.deploy(args, ["us-ashburn-1"], 1)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--workload-regions") + 1], "all")

    @patch.object(chain.subprocess, "run")
    def test_cleanup_worker_tag_only_and_empty_selection_never_deploy(self, run):
        for extras in [("--tag-only",), ("--workflow", "onboard-tagged")]:
            chain.deploy(self.args(*extras), ["us-ashburn-1"], 2)
        cleanup = app.arguments([ROOT, "--cleanup-only"])
        chain.deploy(cleanup, ["us-ashburn-1"], 2)
        chain.deploy(self.args(), ["us-ashburn-1"], 0)
        run.assert_not_called()

    @patch.object(chain.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["deploy"]))
    def test_failed_deployment_is_not_reported_as_success(self, run):
        with self.assertRaisesRegex(SystemExit, "Applied instance tags"):
            chain.deploy(self.args("--deployment-region", "us-ashburn-1"), ["us-ashburn-1"], 1)

    @patch.object(chain.shutil, "which", return_value="tool")
    @patch.object(chain.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["docker"]))
    def test_missing_docker_fails_before_tagging(self, run, which):
        with self.assertRaisesRegex(SystemExit, "no instances tagged"):
            chain.prepare(self.args(), {"tenancy": TENANCY}, ["us-ashburn-1"])

    def test_reconcile_deployment_mode_does_not_run_worker(self):
        with patch.object(deployment, "main") as deploy, patch.object(app, "main") as worker:
            reconcile.main(["--deploy", ROOT, "--apply"])
        deploy.assert_called_once_with([ROOT, "--apply"])
        worker.assert_not_called()

    def test_scheduled_mode_never_imports_or_calls_deployment(self):
        with patch.object(deployment, "main") as deploy, patch.object(app, "main") as worker:
            reconcile.main([ROOT, "--auth", "resource_principal"])
        deploy.assert_not_called()
        self.assertIn("--workflow", worker.call_args.args[0])
        self.assertIn("onboard-tagged", worker.call_args.args[0])

    def test_multi_region_tags_all_then_deploys_once_and_skips_unsubscribed(self):
        args = self.args("--regions", "us-ashburn-1,missing,us-phoenix-1", "--dry-run")
        identity = Mock()
        identity.list_region_subscriptions.return_value = NS(data=[
            NS(region_name="us-ashburn-1", status="READY"), NS(region_name="us-phoenix-1", status="READY")])
        events = []
        def tag(scoped, *rest):
            events.append(scoped.region)
            return 1
        with patch.object(app, "load_auth", return_value=({"tenancy": TENANCY, "region": "us-ashburn-1"}, {})), \
                patch.object(app, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
                patch.object(app, "prepare_identity", return_value=identity), \
                patch.object(app, "arguments", return_value=args), \
                patch.object(app, "run_region", side_effect=tag), \
                patch.object(chain, "prepare"), \
                patch.object(chain, "deploy", side_effect=lambda *a: events.append("deploy")) as deploy:
            app.main()
        self.assertEqual(events, ["us-ashburn-1", "us-phoenix-1", "deploy"])
        self.assertEqual(deploy.call_args.args[1:], (["us-ashburn-1", "us-phoenix-1"], 2))

    def test_regional_failure_stops_deployment_but_other_regions_are_scanned(self):
        args = self.args("--regions", "us-ashburn-1,us-phoenix-1", "--dry-run")
        identity = Mock()
        identity.list_region_subscriptions.return_value = NS(data=[
            NS(region_name="us-ashburn-1", status="READY"), NS(region_name="us-phoenix-1", status="READY")])
        with patch.object(app, "load_auth", return_value=({"tenancy": TENANCY, "region": "us-ashburn-1"}, {})), \
                patch.object(app, "list_call_get_all_results", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
                patch.object(app, "prepare_identity", return_value=identity), \
                patch.object(app, "arguments", return_value=args), \
                patch.object(app, "run_region", side_effect=[SystemExit("scan failed"), 1]) as scan, \
                patch.object(chain, "prepare"), patch.object(chain, "deploy") as deploy:
            with self.assertRaises(SystemExit):
                app.main()
        self.assertEqual(scan.call_count, 2)
        deploy.assert_not_called()

    def test_repository_only_created_in_apply_and_foreign_namespace_refused(self):
        client = Mock()
        with patch.object(deployment.oci.object_storage, "ObjectStorageClient") as storage, \
                patch.object(deployment.oci.artifacts, "ArtifactsClient", return_value=client), \
                patch.object(deployment.oci.pagination, "list_call_get_all_results", return_value=NS(data=[])):
            storage.return_value.get_namespace.return_value = NS(data="testnamespace")
            deployment.ensure_repository({"tenancy": TENANCY}, {}, ROOT, IMAGE)
            client.create_container_repository.assert_not_called()
            deployment.ensure_repository({"tenancy": TENANCY}, {}, ROOT, IMAGE, apply=True)
            details = client.create_container_repository.call_args.args[0]
            self.assertEqual(details.compartment_id, ROOT)
            self.assertFalse(details.is_public)
            storage.return_value.get_namespace.return_value = NS(data="othernamespace")
            with self.assertRaisesRegex(SystemExit, "does not match"):
                deployment.ensure_repository({"tenancy": TENANCY}, {}, ROOT, IMAGE, apply=True)
            self.assertEqual(client.create_container_repository.call_count, 1)

    def test_reused_application_existing_function_is_imported_before_plan(self):
        args = NS(application_id="ocid1.fnapp.oc1..app")
        function = NS(id="ocid1.fnfunc.oc1..fn", display_name="onboard-tagged-instances",
                      lifecycle_state="ACTIVE")
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[1:3] == ["state", "list"]:
                return NS(stdout="")
            return NS(stdout="")

        with patch.object(deployment.subprocess, "run", side_effect=run), \
                patch.object(deployment, "find_existing_function", return_value=function):
            deployment.import_existing_reused_function(args, {"region": "us-ashburn-1"}, {},
                                                       ["terraform", "-chdir=deployment"], {}, "-state=statefile")
        self.assertIn(["terraform", "-chdir=deployment", "import", "-input=false",
                       "-state=statefile", "oci_functions_function.worker", function.id], commands)

    def test_reused_application_skips_import_when_function_already_in_state(self):
        args = NS(application_id="ocid1.fnapp.oc1..app")
        with patch.object(deployment.subprocess, "run",
                          return_value=NS(stdout="oci_functions_function.worker\n")) as run, \
                patch.object(deployment, "find_existing_function") as find:
            deployment.import_existing_reused_function(args, {"region": "us-ashburn-1"}, {},
                                                       ["terraform"], {}, "-state=statefile")
        run.assert_called_once()
        find.assert_not_called()

    def test_existing_function_discovery_rejects_duplicates(self):
        rows = [NS(id="one", display_name="onboard-tagged-instances", lifecycle_state="ACTIVE"),
                NS(id="two", display_name="onboard-tagged-instances", lifecycle_state="ACTIVE")]
        with patch.object(deployment.oci.functions, "FunctionsManagementClient") as factory, \
                patch.object(deployment.oci.pagination, "list_call_get_all_results", return_value=NS(data=rows)):
            with self.assertRaisesRegex(SystemExit, "More than one active Function"):
                deployment.find_existing_function({"region": "us-ashburn-1"}, {}, "ocid1.fnapp.oc1..app")
        factory.return_value.list_functions.assert_not_called()

    def test_initial_reconciliation_invokes_function_detached_with_empty_payload(self):
        with patch.object(deployment.oci.functions, "FunctionsManagementClient") as management, \
                patch.object(deployment.oci.functions, "FunctionsInvokeClient") as factory:
            management.return_value.get_function.return_value = NS(data=NS(
                invoke_endpoint="https://invoke.example.com"))
            factory.return_value.invoke_function.return_value = NS(headers={"opc-request-id": "req"})
            self.assertTrue(deployment.invoke_initial_reconciliation(
                {"region": "us-ashburn-1"}, {}, "ocid1.fnfunc.oc1..fn"))
        self.assertEqual(factory.call_args.kwargs["service_endpoint"], "https://invoke.example.com")
        self.assertIn("timeout", factory.call_args.kwargs)
        call = factory.return_value.invoke_function.call_args
        self.assertEqual(call.args[0], "ocid1.fnfunc.oc1..fn")
        self.assertEqual(call.kwargs["fn_invoke_type"], "detached")
        self.assertEqual(call.kwargs["invoke_function_body"].read(), b"{}")

    def test_initial_reconciliation_retries_transient_invoke_errors(self):
        error = deployment.oci.exceptions.TransientServiceError(
            503, "FunctionInvokeServiceUnavailable", {}, "Timed out - server too busy")
        with patch.object(deployment.oci.functions, "FunctionsManagementClient") as management, \
                patch.object(deployment.oci.functions, "FunctionsInvokeClient") as factory, \
                patch.object(deployment.time, "sleep") as sleep, \
                patch.object(deployment.time, "monotonic", side_effect=[0, 1, 2]):
            management.return_value.get_function.return_value = NS(data=NS(
                invoke_endpoint="https://invoke.example.com"))
            factory.return_value.invoke_function.side_effect = [error, NS(headers={})]
            self.assertTrue(deployment.invoke_initial_reconciliation(
                {"region": "us-ashburn-1"}, {}, "ocid1.fnfunc.oc1..fn", timeout_seconds=60))
        self.assertEqual(factory.return_value.invoke_function.call_count, 2)
        sleep.assert_called_once()

    def test_initial_reconciliation_timeout_is_nonfatal_after_successful_deploy(self):
        error = deployment.oci.exceptions.TransientServiceError(
            503, "FunctionInvokeServiceUnavailable", {}, "Timed out - server too busy")
        with patch.object(deployment.oci.functions, "FunctionsManagementClient") as management, \
                patch.object(deployment.oci.functions, "FunctionsInvokeClient") as factory, \
                patch.object(deployment.time, "monotonic", side_effect=[0, 1]), \
                patch.object(deployment.time, "sleep"):
            management.return_value.get_function.return_value = NS(data=NS(
                invoke_endpoint="https://invoke.example.com"))
            factory.return_value.invoke_function.side_effect = error
            self.assertFalse(deployment.invoke_initial_reconciliation(
                {"region": "us-ashburn-1"}, {}, "ocid1.fnfunc.oc1..fn", timeout_seconds=0))


if __name__ == "__main__":
    unittest.main()
