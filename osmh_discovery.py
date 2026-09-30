"""Read-only compartment and OKE discovery for the OSMH onboarding script.

Compartment discovery follows direct children so a non-tenancy root is supported.
OKE detection uses node-pool membership and OKE-specific instance metadata/tags;
an instance name or Oracle Linux image alone never establishes OKE membership.
The clients must use the same region as Compute. Virtual nodes are not Compute
instances and therefore need no separate enumeration.

Untagged self-managed nodes (not in managed pools) cannot be identified reliably
by these APIs. Review such instances during interactive selection. Pools outside
the selected subtree are not enumerated; documented node tags/metadata provide
additional exclusion signals for moved nodes or pools outside the subtree.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any, Iterable

import oci
from oci.pagination import list_call_get_all_results


def _discovery_error(operation: str, resource_id: str, exc: Exception) -> SystemExit:
    if isinstance(exc, oci.exceptions.ServiceError):
        detail = f"OCI {exc.status} {exc.code}: {exc.message}"
    else:
        detail = str(exc)
    return SystemExit(
        f"Cannot {operation} {resource_id}: {detail}. Scan stopped because "
        "partial discovery could include OKE workers or miss child compartments. "
        "The invoking principal needs read access to compartments and "
        "cluster-family throughout the selected subtree."
    )


def discover_compartments(identity: Any, root_id: str) -> list[Any]:
    """Return the root followed by all ACTIVE descendants, without sibling scans.

    ListCompartments' subtree flag works only for the tenancy root. Explicit
    breadth-first traversal works for every compartment, follows every page, and
    requests access_level=ANY to avoid the ACCESSIBLE filter hiding descendants.
    Any access or topology error stops discovery rather than silently truncating
    the requested tree. Terminated/deleting descendants are not traversed.
    """
    try:
        if root_id.startswith("ocid1.tenancy."):
            tenancy = identity.get_tenancy(root_id).data
            # Tenancy is the root compartment, but the Tenancy model does not
            # expose Compartment.lifecycle_state. Normalize for traversal.
            root = SimpleNamespace(id=tenancy.id, name=f"{tenancy.name} (root)",
                                   compartment_id=None, lifecycle_state="ACTIVE")
        else:
            root = identity.get_compartment(root_id).data
    except oci.exceptions.ServiceError as exc:
        raise _discovery_error("read compartment", root_id, exc) from exc
    if getattr(root, "id", None) != root_id:
        raise SystemExit(f"Compartment lookup returned an unexpected OCID for {root_id}.")
    if getattr(root, "lifecycle_state", None) != "ACTIVE":
        raise SystemExit(f"Target compartment {root_id} is not ACTIVE.")

    found = [root]
    pending = deque([root])
    seen = {root_id}
    while pending:
        parent = pending.popleft()
        try:
            children = list_call_get_all_results(
                identity.list_compartments, parent.id,
                lifecycle_state="ACTIVE", access_level="ANY",
                compartment_id_in_subtree=False,
            ).data
        except oci.exceptions.ServiceError as exc:
            raise _discovery_error("list child compartments of", parent.id, exc) from exc
        for child in sorted(children, key=lambda value: (value.name or "", value.id)):
            if child.lifecycle_state != "ACTIVE":
                continue
            if child.compartment_id != parent.id:
                raise SystemExit(
                    f"Compartment tree changed or returned an unexpected parent for {child.id}; "
                    "rerun discovery before onboarding."
                )
            if child.id in seen:
                continue
            seen.add(child.id)
            found.append(child)
            pending.append(child)
    return found


def oke_tag_reason(instance: Any) -> str | None:
    """Return a specific OKE marker without interpreting generic names/tags.

    Quick Create's OKEclusterName/OKEnodePoolName tags are documented by Oracle:
    https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengtaggingclusterresources_tagging-oke-resources_node-tags.htm
    The oke_init_script metadata key is documented in OKE custom cloud-init:
    https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengusingcustomcloudinitscripts.htm
    Oracle-Tags.CreatedBy=oke is also used in Oracle's OKE examples. These tags
    can be modified by a user, so the node-pool inventory is always checked too.
    """
    freeform = getattr(instance, "freeform_tags", None) or {}
    defined = getattr(instance, "defined_tags", None) or {}
    metadata = getattr(instance, "metadata", None) or {}
    if freeform.get("OKEclusterName") and freeform.get("OKEnodePoolName"):
        return "OKE Quick Create node tags (OKEclusterName and OKEnodePoolName)"
    if (defined.get("Oracle-Tags", {}) or {}).get("CreatedBy") == "oke":
        return "OKE creator tag (Oracle-Tags.CreatedBy=oke)"
    if metadata.get("oke_init_script"):
        return "OKE worker bootstrap metadata (oke_init_script)"
    return None


def discover_oke_instance_ids(container_engine: Any, compartment_ids: Iterable[str],
                              instances: Iterable[Any]) -> dict[str, str]:
    """Map discovered Compute instance IDs to evidence for their OKE exclusion.

    Enumerate every node pool (all lifecycle states) in the selected subtree,
    then GetNodePool to obtain nodes[].id, the backing Compute instance OCID.
    A failed List/Get is fatal even when some nodes have recognizable tags;
    allowing the remaining instances would risk enrolling unrecognized workers.
    """
    instance_map = {instance.id: instance for instance in instances}
    excluded = {}
    for instance_id, instance in instance_map.items():
        reason = oke_tag_reason(instance)
        if reason:
            excluded[instance_id] = reason

    seen_pools = set()
    for compartment_id in dict.fromkeys(compartment_ids):
        try:
            pools = list_call_get_all_results(
                container_engine.list_node_pools, compartment_id,
            ).data
        except oci.exceptions.ServiceError as exc:
            raise _discovery_error("enumerate OKE node pools in", compartment_id, exc) from exc
        for summary in pools:
            if summary.id in seen_pools:
                continue
            seen_pools.add(summary.id)
            try:
                pool = container_engine.get_node_pool(summary.id).data
            except oci.exceptions.ServiceError as exc:
                raise _discovery_error("read OKE node pool", summary.id, exc) from exc
            for node in pool.nodes or []:
                if node.id in instance_map:
                    excluded[node.id] = f"OKE node-pool membership ({summary.id})"
    return excluded
