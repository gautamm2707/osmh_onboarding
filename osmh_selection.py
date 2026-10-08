"""Opt in the exact Compute selection authorized by a Resource Manager stack."""
import argparse
import json
import re

import oci

from onboard_osmh import list_managed_in_compartments, scan_candidates
from osmh_discovery import discover_oke_instance_ids
from osmh_runtime import load_auth, prepare_identity
from osmh_tags import TAG_KEY, TAG_VALUE, apply_instance_tag


def instance_ids(value):
    if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not re.fullmatch(r"ocid1\.instance\.[A-Za-z0-9._-]+", item)
            for item in value):
        raise ValueError("onboard_instance_ids must be a nonempty list of Compute OCIDs.")
    return sorted(set(value))


def tag_selected(args, compute, container_engine, managed):
    ids = instance_ids(args.instance_ids)
    instances = [compute.get_instance(item).data for item in ids]
    for expected, instance in zip(ids, instances):
        if (instance.id != expected or instance.compartment_id != args.compartment_id
                or instance.lifecycle_state != "RUNNING"):
            raise SystemExit("A selected instance changed compartment/state; no instances tagged. Refresh the stack selection.")
        previous = ((instance.defined_tags or {}).get(args.tag_namespace) or {}).get(TAG_KEY)
        if previous not in (None, TAG_VALUE):
            raise SystemExit(f"Conflicting opt-in tag on {instance.id}; no instances tagged.")
    # Complete eligibility checks before the first write. Reuse the worker's OS
    # and OKE checks instead of trusting labels or the Console's resource list.
    oke = discover_oke_instance_ids(container_engine, [args.compartment_id], instances)
    inventory = list_managed_in_compartments(managed, [args.compartment_id])
    eligible = scan_candidates(compute, instances, oke,
                               {item.id: item for item in inventory if item.location == "OCI_COMPUTE"})
    if {item.id for item, _ in eligible} != set(ids):
        raise SystemExit("Selection contains OKE, unsupported or unverifiable instances; no instances tagged.")
    for instance, _ in eligible:
        # Re-reads state and tags and uses ETag to preserve concurrent changes.
        apply_instance_tag(args, compute, instance, args.tag_namespace, {args.compartment_id})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compartment_id")
    parser.add_argument("--region", required=True)
    parser.add_argument("--tag-namespace", required=True)
    parser.add_argument("--instance-ids", required=True, type=json.loads)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.auth = "resource_principal"
    args.skip_iam = True
    config, kwargs = load_auth(args)
    prepare_identity(config, kwargs, args.compartment_id)
    tag_selected(args, oci.core.ComputeClient(config, **kwargs),
                 oci.container_engine.ContainerEngineClient(config, **kwargs),
                 oci.os_management_hub.ManagedInstanceClient(config, **kwargs))


if __name__ == "__main__":
    main()
