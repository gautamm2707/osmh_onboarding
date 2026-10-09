import hashlib
import io
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import sys

from function import func
import osmh_selection as selection

SCOPE = "ocid1.compartment.oc1..scope"
FIRST = "ocid1.instance.oc1.iad.first"
SECOND = "ocid1.instance.oc1.iad.second"


def instance(id=FIRST, **changes):
    value = dict(id=id, compartment_id=SCOPE, lifecycle_state="RUNNING", defined_tags={}, display_name=id)
    value.update(changes)
    return NS(**value)


def config(ids):
    return {"OSMH_COMPARTMENT_ID": SCOPE, "OSMH_TAG_NAMESPACE": "OSMH",
            "OSMH_SELECTION_REGION": "us-ashburn-1", "OSMH_REGIONS": "us-ashburn-1",
            "OSMH_SELECTION_SHA256": hashlib.sha256(json.dumps(sorted(set(ids)), separators=(",", ":")).encode()).hexdigest(),
            "OSMH_ONBOARD_ALL": "false"}


class SelectedInstanceTests(unittest.TestCase):
    def test_default_invocation_does_not_retag(self):
        steps = func.commands(config([FIRST]), {})
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0][2].endswith("reconcile_osmh.py"))

    def test_selection_is_canonical_and_dry_run_applies_to_both_steps(self):
        steps = func.commands(config([FIRST, SECOND]), {"onboard_instance_ids": [SECOND, FIRST, FIRST], "dry_run": True})
        self.assertEqual(len(steps), 2)
        self.assertEqual(json.loads(steps[0][steps[0].index("--instance-ids") + 1]), [FIRST, SECOND])
        self.assertTrue(all("--dry-run" in step for step in steps))

    def test_rejects_unapproved_or_malformed_selections(self):
        for payload in ([SECOND], [], [True], FIRST, ["--all"], [FIRST, SECOND]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                func.commands(config([FIRST]), {"onboard_instance_ids": payload})
        with self.assertRaises(ValueError):
            func.commands({"OSMH_COMPARTMENT_ID": SCOPE, "OSMH_TAG_NAMESPACE": "OSMH"}, {"onboard_instance_ids": [FIRST]})

    def test_all_instances_requires_stack_authorization(self):
        settings = config([])
        with self.assertRaises(ValueError):
            func.commands(settings, {"onboard_all_instances": True})
        settings["OSMH_ONBOARD_ALL"] = "true"
        steps = func.commands(settings, {"onboard_all_instances": True})
        self.assertEqual(len(steps), 2)
        self.assertIn("--all", steps[0])
        self.assertNotIn("--instance-ids", steps[0])

    def test_all_instances_rejects_false_or_mixed_selection(self):
        settings = config([FIRST])
        settings["OSMH_ONBOARD_ALL"] = "true"
        with self.assertRaises(ValueError):
            func.commands(settings, {"onboard_all_instances": False})
        with self.assertRaises(ValueError):
            func.commands(settings, {"onboard_all_instances": True, "onboard_instance_ids": [FIRST]})

    def fixture(self, instances):
        compute = Mock()
        compute.get_instance.side_effect = [NS(data=item) for item in instances]
        args = NS(instance_ids=[i.id for i in instances], compartment_id=SCOPE, tag_namespace="OSMH", dry_run=False)
        oke = patch.object(selection, "discover_oke_instance_ids", return_value={}).start()
        inventory = patch.object(selection, "list_managed_in_compartments", return_value=[]).start()
        scan = patch.object(selection, "scan_candidates", return_value=[(item, None) for item in instances]).start()
        apply = patch.object(selection, "apply_instance_tag").start()
        self.addCleanup(patch.stopall)
        return args, compute, oke, inventory, scan, apply

    def test_valid_selection_is_tagged_in_exact_scope(self):
        args, compute, oke, inventory, scan, apply = self.fixture([instance(), instance(SECOND)])
        selection.tag_selected(args, compute, Mock(), Mock())
        self.assertEqual(apply.call_count, 2)
        self.assertTrue(all(call.args[4] == {SCOPE} for call in apply.call_args_list))
        self.assertEqual(compute.get_instance.call_count, 2)

    def test_preflight_rejects_whole_batch_before_writes(self):
        for changes in ({"compartment_id": "ocid1.compartment.oc1..other"},
                        {"lifecycle_state": "STOPPED"},
                        {"defined_tags": {"OSMH": {"managedby": "another-service"}}}):
            with self.subTest(changes=changes):
                args, compute, oke, inventory, scan, apply = self.fixture([instance(), instance(SECOND, **changes)])
                with self.assertRaises(SystemExit):
                    selection.tag_selected(args, compute, Mock(), Mock())
                apply.assert_not_called()
                scan.assert_not_called()
                patch.stopall()

    def test_rejected_os_or_oke_node_prevents_all_tagging(self):
        args, compute, oke, inventory, scan, apply = self.fixture([instance(), instance(SECOND)])
        oke.return_value = {SECOND: "OKE node"}
        scan.return_value = [(instance(), None)]
        with self.assertRaises(SystemExit):
            selection.tag_selected(args, compute, Mock(), Mock())
        apply.assert_not_called()
        self.assertEqual(scan.call_args.args[2], {SECOND: "OKE node"})

    def test_registered_instances_are_skipped_before_eligibility_checks(self):
        args, compute, oke, inventory, scan, apply = self.fixture([instance(), instance(SECOND)])
        inventory.return_value = [NS(id=FIRST, location="OCI_COMPUTE")]
        scan.return_value = [(instance(SECOND), None)]
        selection.tag_selected(args, compute, Mock(), Mock())
        self.assertEqual([item.id for item in oke.call_args.args[2]], [SECOND])
        self.assertEqual([item.id for item in scan.call_args.args[1]], [SECOND])
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2].id, SECOND)

    def test_all_tags_only_eligible_unregistered_instances_in_tree(self):
        child = "ocid1.compartment.oc1..child"
        first = instance()
        second = instance(SECOND, compartment_id=child)
        args = NS(compartment_id=SCOPE, tag_namespace="OSMH", dry_run=False)
        with patch.object(selection, "discover_compartments", return_value=[
                NS(id=SCOPE, name="root", compartment_id=None),
                NS(id=child, name="child", compartment_id=SCOPE)]), \
                patch.object(selection, "list_call_get_all_results",
                             side_effect=[NS(data=[first]), NS(data=[second])]), \
                patch.object(selection, "list_managed_in_compartments",
                             return_value=[NS(id=FIRST, location="OCI_COMPUTE")]), \
                patch.object(selection, "discover_oke_instance_ids", return_value={}) as oke, \
                patch.object(selection, "scan_candidates", return_value=[(second, None)]), \
                patch.object(selection, "apply_instance_tag") as apply:
            selection.tag_all(args, Mock(), Mock(), Mock(), Mock())
        self.assertEqual([item.id for item in oke.call_args.args[2]], [SECOND])
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2].id, SECOND)
        self.assertEqual(apply.call_args.args[4], {SCOPE, child})

    def test_failed_tagging_does_not_start_reconciliation(self):
        process = Mock(stdout=iter(["tagging failed\n"]))
        process.wait.return_value = 1
        fake_fdk = NS(response=NS(Response=Mock()))
        with patch.object(func.subprocess, "Popen", return_value=process) as popen, patch.dict(sys.modules, {"fdk": fake_fdk}):
            with self.assertRaises(RuntimeError):
                func.handler(NS(Config=lambda: config([FIRST])), io.BytesIO(json.dumps({"onboard_instance_ids": [FIRST]}).encode()))
        self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
