import argparse
import json
import subprocess
import unittest
from unittest.mock import patch

import ensure_service_logs as logs


def args():
    return argparse.Namespace(
        compartment_id="ocid1.compartment.oc1..scope",
        log_group_id="ocid1.loggroup.oc1.iad.stack",
        region="us-ashburn-1",
        deployment_id="abc123",
    )


def service_log(identifier="ocid1.log.oc1.iad.existing", name="existing"):
    return {
        "id": identifier,
        "display-name": name,
        "log-group-id": "ocid1.loggroup.oc1.iad.existing",
        "configuration": {
            "source": {
                "service": "functions",
                "resource": "ocid1.fnapp.oc1.iad.app",
                "category": "invoke",
            }
        },
    }


class EnsureServiceLogsTests(unittest.TestCase):
    def test_response_items_accepts_prefixed_json(self):
        result = subprocess.CompletedProcess([], 0, 'NOTICE: initializing\n{"data": [{"id": "one"}]}\nDone', "")
        self.assertEqual(logs.response_items(result, "list logs"), [{"id": "one"}])

    def test_response_items_accepts_items_envelope(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"data": {"items": [{"id": "one"}]}}), "")
        self.assertEqual(logs.response_items(result, "list logs"), [{"id": "one"}])

    @patch.object(logs.time, "sleep")
    @patch.object(logs, "run_oci")
    def test_list_items_retries_invalid_success_response(self, run_oci, sleep):
        run_oci.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps({"data": [{"id": "one"}]}), ""),
        ]
        self.assertEqual(logs.list_items(["logging", "log", "list"], "region", "list logs"), [{"id": "one"}])
        sleep.assert_called_once_with(2)

    @patch.object(logs, "run_oci")
    def test_existing_service_combination_is_reused_without_create(self, run_oci):
        result = logs.ensure_log(
            args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired", [service_log()]
        )
        self.assertEqual(result, "ocid1.log.oc1.iad.existing")
        run_oci.assert_not_called()

    @patch.object(logs, "run_oci")
    def test_display_name_collision_gets_a_unique_suffix(self, run_oci):
        run_oci.return_value = subprocess.CompletedProcess([], 0, json.dumps({"data": {}}), "")
        result = logs.ensure_log(
            args(),
            "functions",
            "ocid1.fnapp.oc1.iad.app",
            "invoke",
            "desired",
            [{"display-name": "desired", "log-group-id": args().log_group_id}],
        )
        self.assertEqual(result, "created")
        command = run_oci.call_args.args[0]
        self.assertEqual(command[command.index("--display-name") + 1], "desired-2")

    @patch.object(logs, "run_oci")
    def test_conflict_is_treated_as_already_configured(self, run_oci):
        run_oci.return_value = subprocess.CompletedProcess([], 1, "", "409-Conflict")
        result = logs.ensure_log(
            args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired", None
        )
        self.assertEqual(result, "existing")

    @patch.object(logs, "run_oci")
    def test_unavailable_discovery_does_not_block_creation(self, run_oci):
        run_oci.return_value = subprocess.CompletedProcess([], 0, "", "")
        result = logs.ensure_log(
            args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired", None
        )
        self.assertEqual(result, "created")

    @patch.object(logs, "run_oci")
    def test_non_conflict_create_failure_is_nonblocking(self, run_oci):
        run_oci.return_value = subprocess.CompletedProcess([], 1, "", "NotAuthorized")
        result = logs.ensure_log_nonblocking(
            args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired", None
        )
        self.assertEqual(result, "skipped")

    def test_source_accepts_cli_and_provider_shapes(self):
        direct = service_log()
        provider = {"configuration": [{"source": [{"service": "functions", "resource": "app", "category": "invoke"}]}]}
        self.assertTrue(logs.matches_service(direct, "functions", "ocid1.fnapp.oc1.iad.app", "invoke"))
        self.assertTrue(logs.matches_service(provider, "functions", "app", "invoke"))


if __name__ == "__main__":
    unittest.main()
