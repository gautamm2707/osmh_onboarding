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
    @patch.object(logs, "run_oci")
    @patch.object(logs, "all_logs")
    def test_existing_service_combination_is_reused_without_create(self, all_logs, run_oci):
        all_logs.return_value = [service_log()]
        result = logs.ensure_log(args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired")
        self.assertEqual(result, "ocid1.log.oc1.iad.existing")
        run_oci.assert_not_called()

    @patch.object(logs, "run_oci")
    @patch.object(logs, "list_logs")
    @patch.object(logs, "all_logs")
    def test_display_name_collision_gets_a_unique_suffix(self, all_logs, list_logs, run_oci):
        created = service_log("ocid1.log.oc1.iad.created", "desired-2")
        all_logs.side_effect = [[], [created]]
        list_logs.return_value = [{"display-name": "desired"}]
        run_oci.return_value = subprocess.CompletedProcess([], 0, json.dumps({"data": {}}), "")
        result = logs.ensure_log(args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired")
        self.assertEqual(result, "ocid1.log.oc1.iad.created")
        command = run_oci.call_args.args[0]
        self.assertEqual(command[command.index("--display-name") + 1], "desired-2")

    @patch.object(logs, "run_oci")
    @patch.object(logs, "list_logs", return_value=[])
    @patch.object(logs, "all_logs")
    def test_concurrent_conflict_is_relisted_and_reused(self, all_logs, _list_logs, run_oci):
        all_logs.side_effect = [[], [service_log()]]
        run_oci.return_value = subprocess.CompletedProcess([], 1, "", "409-Conflict")
        result = logs.ensure_log(args(), "functions", "ocid1.fnapp.oc1.iad.app", "invoke", "desired")
        self.assertEqual(result, "ocid1.log.oc1.iad.existing")

    def test_source_accepts_cli_and_provider_shapes(self):
        direct = service_log()
        provider = {"configuration": [{"source": [{"service": "functions", "resource": "app", "category": "invoke"}]}]}
        self.assertTrue(logs.matches_service(direct, "functions", "ocid1.fnapp.oc1.iad.app", "invoke"))
        self.assertTrue(logs.matches_service(provider, "functions", "app", "invoke"))


if __name__ == "__main__":
    unittest.main()
