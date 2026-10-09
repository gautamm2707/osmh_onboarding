"""Opt in the exact Compute selection authorized by a Resource Manager stack."""
import argparse
import json
import re

import oci
from oci.pagination import list_call_get_all_results

from onboard_osmh import compartment_labels, list_managed_in_compartments, scan_candidates
from osmh_discovery import discover_compartments, discover_oke_instance_ids
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
    # OSMH inventory is a separate service and cannot filter the Resource
    # Manager Compute picker. Skip registered Compute instances before any tag
    # writes so selecting a stale row remains harmless.
    inventory = list_managed_in_compartments(managed, [args.compartment_id])
    existing_managed = {item.id: item for item in inventory if item.location == "OCI_COMPUTE"}
    unregistered = []
    for instance in instances:
        if instance.id in existing_managed:
            print(f"Already registered in OSMH; skip opt-in tag: {instance.display_name} ({instance.id})")
        else:
            unregistered.append(instance)
    if not unregistered:
        return

    # Complete eligibility checks before the first write. Reuse the worker's OS
    # and OKE checks instead of trusting labels or the Console's resource list.
    oke = discover_oke_instance_ids(container_engine, [args.compartment_id], unregistered)
    eligible = scan_candidates(compute, unregistered, oke, {})
    if {item.id for item, _ in eligible} != {item.id for item in unregistered}:
        raise SystemExit("Selection contains OKE, unsupported or unverifiable instances; no instances tagged.")
    for instance, _ in eligible:
        # Re-reads state and tags and uses ETag to preserve concurrent changes.
        apply_instance_tag(args, compute, instance, args.tag_namespace, {args.compartment_id})


def tag_all(args, identity, compute, container_engine, managed):
    """Tag every currently eligible, unregistered instance in the target tree."""
    compartments = discover_compartments(identity, args.compartment_id)
    compartment_ids = [item.id for item in compartments]
    labels = compartment_labels(compartments)
    instances = []
    for compartment in compartments:
        instances.extend(list_call_get_all_results(compute.list_instances, compartment.id).data)
    inventory = list_managed_in_compartments(managed, compartment_ids)
    registered = {item.id for item in inventory if getattr(item, "location", None) == "OCI_COMPUTE"}
    unregistered = [item for item in instances if item.id not in registered]
    for item in instances:
        if item.id in registered:
            print(f"Already registered in OSMH; skip opt-in tag: {item.display_name} ({item.id})")
    oke = discover_oke_instance_ids(container_engine, compartment_ids, unregistered)
    eligible = scan_candidates(compute, unregistered, oke, {}, labels)
    targets = []
    for instance, platform in eligible:
        previous = ((instance.defined_tags or {}).get(args.tag_namespace) or {}).get(TAG_KEY)
        if previous not in (None, TAG_VALUE):
            print(f"SKIP {instance.display_name}: conflicting {args.tag_namespace}.{TAG_KEY}={previous!r}")
            continue
        targets.append((instance, platform))
    for instance, _ in targets:
        apply_instance_tag(args, compute, instance, args.tag_namespace, set(compartment_ids))
    print(f"Tagged {len(targets)} eligible unregistered instance(s) across "
          f"{len(compartment_ids)} compartment(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compartment_id")
    parser.add_argument("--region", required=True)
    parser.add_argument("--tag-namespace", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--instance-ids", type=json.loads)
    choice.add_argument("--all", dest="all_instances", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.auth = "resource_principal"
    args.skip_iam = True
    config, kwargs = load_auth(args)
    identity = prepare_identity(config, kwargs, args.compartment_id)
    compute = oci.core.ComputeClient(config, **kwargs)
    container_engine = oci.container_engine.ContainerEngineClient(config, **kwargs)
    managed = oci.os_management_hub.ManagedInstanceClient(config, **kwargs)
    if args.all_instances:
        tag_all(args, identity, compute, container_engine, managed)
    else:
        tag_selected(args, compute, container_engine, managed)


if __name__ == "__main__":
    main()
