#!/usr/bin/env python3
"""Onboard Oracle Linux 7/8/9/10, Ubuntu and Windows OCI instances to OS Management Hub.

By default, scans the supplied compartment tree, excludes OKE workers, prompts
for selection, applies a defined opt-in tag, and automatically deploys a scheduled
Function through reconcile_osmh.py. The Function runs the onboard-tagged workflow, creating/reconciling IAM and OSMH
resources. OSMH profiles/groups stay in the supplied parent. Registered instances
are not reconfigured. Use --all or --instance-ids for unattended tag selection.

Unregisters OSMH records only for confirmed TERMINATED Compute instances.
Requires: ``pip install -r requirements.txt`` and OCI credentials (SDK config
or an authorized OCI instance principal).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import oci
    from oci.pagination import list_call_get_all_results
except ImportError:
    sys.exit("Missing dependency: pip install oci")

from osmh_discovery import discover_compartments, discover_oke_instance_ids, oke_tag_reason
from osmh_iam import ensure_tree_iam
from osmh_runtime import load_auth, prepare_identity
from osmh_tags import namespace_name, is_opted_in, ensure_tag_namespace, apply_instance_tag


PLUGIN_NAME = "OS Management Hub Agent"
PROFILE_TAG = "OsmhProfile"
IAM_PROPAGATION_SECONDS = 90


@dataclass(frozen=True)
class Platform:
    os_family: str
    arch_type: str
    vendor_name: str


@dataclass
class CleanupResult:
    removed: set[str] = field(default_factory=set)
    incomplete: set[str] = field(default_factory=set)


def arguments(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("compartment_id", help="Parent compartment or tenancy OCID: scan root and descendants; create OSMH groups here")
    p.add_argument("--workflow", choices=("tag", "onboard-tagged"), default="tag",
                   help="Default: select and tag instances. onboard-tagged reconciles only defined-tag opted-in instances.")
    p.add_argument("--tag-namespace", help="Defined tag namespace (default OSMH_<scope OCID suffix>); key managedby, value osmanagementhub")
    p.add_argument("--tag-only", action="store_true", help="Select/tag only; skip automatic Function deployment")
    p.add_argument("--deployment-region", help="Optional home-region assertion; Function and OCIR always use the tenancy home region")
    p.add_argument("--function-application-id",
                   help="Existing OCI Functions application OCID in the home region; skips Function app/network creation")
    p.add_argument("--network-compartment-id", help="Browse existing VCNs/subnets in this compartment tree instead of the onboarding tree")
    networking = p.add_mutually_exclusive_group()
    networking.add_argument("--function-subnet-ids", help="Existing Function subnet OCIDs, comma-separated, in deployment region")
    networking.add_argument("--create-function-network", action="store_true", help="Create a dedicated private VCN, subnet and NAT gateway")
    p.add_argument("--function-image", help="Optional home-region OCIR image override; default derived from namespace, scope and code hash")
    p.add_argument("--skip-function-build", action="store_true", help="Deploy an already-pushed function image")
    p.add_argument("--skip-initial-function-invoke", action="store_true",
                   help="Deploy and schedule only; do not immediately invoke the Function after deployment")
    p.add_argument("--enable-function-logging", action="store_true",
                   help="Create a Functions invocation service log. Default is off to avoid conflicts on reused apps.")
    p.add_argument("--defer-registration", action="store_true",
                   help="Check registration once and leave pending instances for the next scheduled reconciliation")
    p.add_argument("--config-file", default="~/.oci/config")
    p.add_argument("--profile", default="DEFAULT", help="OCI SDK config profile")
    regions = p.add_mutually_exclusive_group()
    regions.add_argument("--region", "--regions", dest="region",
                         help="One region or comma-separated regions, e.g. us-ashburn-1,us-phoenix-1")
    regions.add_argument("--all-regions", action="store_true", help="Process all READY subscribed regions, with separate selections per region")
    p.add_argument("--repository-map", type=Path, help="JSON OS_FAMILY:ARCH -> list of extra Oracle vendor repo IDs; portable across regions")
    p.add_argument("--repository-families", default="uek,ksplice,mysql,oci",
                   help="Extra Oracle repo families to discover: uek,ksplice,mysql,oci (default all); use none for minimal sources")
    p.add_argument("--auth", choices=("auto", "api_key", "security_token", "instance_principal", "resource_principal"),
                   default="auto", help="Authentication method; auto detects API key/session token from config")
    p.add_argument("--profile-map", type=Path,
                   help="JSON: {\"OS_FAMILY:ARCH\": \"profile OCID\"}. Overrides auto-discovery.")
    p.add_argument("--software-source-map", type=Path,
                   help="Optional complete Oracle source-set override: OS_FAMILY:ARCH -> source OCIDs. Default: OL7 Latest+ELS, OL8/9/10 BaseOS+AppStream, plus selected extra repositories. Multi-region maps use a regions object.")
    p.add_argument("--group-prefix", default="osmh", help="Prefix for groups created by this script")
    p.add_argument("--registration-timeout", type=int, default=900,
                   help="Seconds to wait for OSMH registration (default: 900)")
    p.add_argument("--admin-group", help="Optional existing IAM group override; otherwise create osmh-admins-<suffix>")
    p.add_argument("--operator-group",
                   help="Optional existing IAM group granted read-only OSMH operator access in the target compartment")
    p.add_argument("--identity-domain", help="Identity domain prefix for --admin-group")
    p.add_argument("--instance-dynamic-group",
                   help="Existing IAM dynamic group matching the exact defined-tag rule")
    p.add_argument("--skip-iam", action="store_true",
                   help="Use existing IAM permissions; do not inspect or change policies/dynamic groups")
    p.add_argument("--skip-terminated-cleanup", action="store_true",
                   help="Do not unregister confirmed terminated Compute instances from OSMH")
    cleanup = p.add_mutually_exclusive_group()
    cleanup.add_argument("--unregister-all", action="store_true", help="Explicitly approve cleanup of all verified terminated non-OKE records")
    cleanup.add_argument("--unregister-instance-ids", help="Comma-separated terminated Compute OCIDs approved for OSMH cleanup")
    p.add_argument("--unregistration-timeout", type=int, default=300,
                   help="Seconds per terminated instance to verify OSMH removal (default: 300)")
    p.add_argument("--source-selection-timeout", type=int, default=300,
                   help="Seconds to verify OCI vendor sources become ACTIVE/SELECTED (default: 300)")
    p.add_argument("--tag-propagation-timeout", type=int, default=180,
                   help="Seconds to retry instance tagging while a new defined tag propagates (default: 180)")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--interactive", action="store_true", help="Prompt for instance selection (default in a terminal)")
    selection.add_argument("--all", dest="all_instances", action="store_true",
                           help="Select all eligible non-OKE instances without prompting")
    selection.add_argument("--instance-ids", help="Comma-separated eligible Compute OCIDs to select without prompting")
    selection.add_argument("--cleanup-only", action="store_true", help="Only select/unregister terminated records; do not configure IAM or onboard instances")
    p.add_argument("--dry-run", action="store_true", help="Print actions without changing OCI")
    args = p.parse_args(argv)
    if args.function_application_id and (args.function_subnet_ids or args.create_function_network):
        p.error("--function-application-id cannot be combined with --function-subnet-ids or --create-function-network")
    args.tag_namespace = namespace_name(args.compartment_id, args.tag_namespace)
    args.allow_empty_admin_group = args.auth == "resource_principal"
    if args.workflow == "tag" and not args.cleanup_only:
        if args.unregister_all or args.unregister_instance_ids:
            p.error("Use --cleanup-only for explicit terminated-instance cleanup.")
        args.skip_terminated_cleanup = True
    if args.defer_registration:
        args.registration_timeout = 0
    return args


def load_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(path.expanduser().read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


def platform_from_image(instance: Any, image: Any) -> Platform | None:
    """Map supported Compute image metadata to OSMH values."""
    os_name = (getattr(image, "operating_system", "") or "").lower()
    version = str(getattr(image, "operating_system_version", "") or "")
    shape = getattr(instance, "shape", "") or ""
    arch = "AARCH64" if ".A1." in shape.upper() or ".A2." in shape.upper() else "X86_64"

    if "oracle linux" in os_name:
        major = version.split(".")[0]
        if major in {"7", "8", "9", "10"}:
            return Platform(f"ORACLE_LINUX_{major}", arch, "ORACLE")
    if "ubuntu" in os_name:
        match = re.search(r"\b(20\.04|22\.04|24\.04)\b", version)
        if match and not (match[1] == "20.04" and arch == "AARCH64"):
            return Platform("UBUNTU_" + match[1].replace(".", "_"), arch, "CANONICAL")
    if "windows" in os_name and arch == "X86_64":
        description = f"{os_name} {version}".lower()
        if "server" in description:
            match = re.search(r"\b(2016|2019|2022|2025)\b", description)
            if match:
                return Platform(f"WINDOWS_SERVER_{match[1]}", arch, "MICROSOFT")
        elif re.search(r"\b11\b", description):
            return Platform("WINDOWS_11", arch, "MICROSOFT")
    return None


def key(platform: Platform) -> str:
    return f"{platform.os_family}:{platform.arch_type}"


def required_vendor_source_tokens(platform: Platform) -> tuple[str, ...]:
    """Minimum Oracle Linux repos required by OSMH for supported OL releases."""
    if platform.vendor_name != "ORACLE" or platform.os_family not in {
        "ORACLE_LINUX_7", "ORACLE_LINUX_8", "ORACLE_LINUX_9", "ORACLE_LINUX_10"
    }:
        return ()
    return ("latest", "latest_ELS") if platform.os_family == "ORACLE_LINUX_7" else ("baseos", "appstream")


def action(args: argparse.Namespace, message: str) -> None:
    print(("[dry-run] " if args.dry_run else "") + message)


def list_managed_in_compartments(managed: Any, compartment_ids: list[str]) -> list[Any]:
    instances = {}
    for compartment_id in compartment_ids:
        for mi in list_call_get_all_results(
                managed.list_managed_instances, compartment_id=compartment_id).data:
            instances[mi.id] = mi
    return list(instances.values())


def compartment_labels(compartments: list[Any]) -> dict[str, str]:
    """Build paths within the scanned tree so repeated leaf names are distinguishable."""
    by_id = {c.id: c for c in compartments}
    labels = {}
    for compartment in compartments:
        parts, seen, current = [], set(), compartment
        while current is not None and current.id not in seen:
            seen.add(current.id)
            parts.append(current.name or current.id)
            current = by_id.get(getattr(current, "compartment_id", None))
        labels[compartment.id] = " / ".join(reversed(parts))
    return labels


def scan_candidates(compute: Any, instances: list[Any], oke_instances: dict[str, str],
                    existing_managed: dict[str, Any],
                    compartments: dict[str, str] | None = None) -> list[tuple[Any, Platform]]:
    """Classify every Compute instance before proposing any changes."""
    targets = []
    images: dict[str, Any] = {}
    stopped = unsupported = oke_count = 0
    for instance in sorted(instances, key=lambda i: (i.compartment_id, i.display_name, i.id)):
        label = (compartments or {}).get(instance.compartment_id, instance.compartment_id)
        display = f"{instance.display_name} | Compartment: {label}"
        if instance.id in oke_instances:
            oke_count += 1
            print(f"EXCLUDE OKE: {display} ({instance.id}) — {oke_instances[instance.id]}")
            continue
        if instance.lifecycle_state != "RUNNING":
            stopped += 1
            print(f"SKIP {display}: Compute state {instance.lifecycle_state}")
            continue
        mi = existing_managed.get(instance.id)
        # Registered OS/architecture reflects in-place upgrades better than the
        # original image. Keep image-based discovery for unregistered instances.
        family = getattr(mi, "os_family", "") or ""
        arch = getattr(mi, "architecture", "") or ""
        platform = None
        if (family in {"ORACLE_LINUX_7", "ORACLE_LINUX_8", "ORACLE_LINUX_9", "ORACLE_LINUX_10"}
                and arch in {"X86_64", "AARCH64"}):
            platform = Platform(family, arch, "ORACLE")
        elif family in {"UBUNTU_20_04", "UBUNTU_22_04", "UBUNTU_24_04"}:
            arch = {"AMD64": "X86_64", "ARM64": "AARCH64"}.get(arch, arch)
            if arch in {"X86_64", "AARCH64"} and not (family == "UBUNTU_20_04" and arch == "AARCH64"):
                platform = Platform(family, arch, "CANONICAL")
        elif family in {"WINDOWS_SERVER_2016", "WINDOWS_SERVER_2019", "WINDOWS_SERVER_2022",
                        "WINDOWS_SERVER_2025", "WINDOWS_11"} and arch == "X86_64":
            platform = Platform(family, arch, "MICROSOFT")
        if platform is None:
            try:
                if instance.image_id not in images:
                    images[instance.image_id] = compute.get_image(instance.image_id).data
                platform = platform_from_image(instance, images[instance.image_id])
            except oci.exceptions.ServiceError as exc:
                if exc.status not in {401, 403, 404}:
                    raise
                print(f"Cannot inspect image for {display}: {exc.status}/{exc.code}")
        if platform is None:
            unsupported += 1
            print(f"SKIP {display}: unsupported or unverified OS/architecture")
        else:
            targets.append((instance, platform))
    print(f"Scan totals: {len(instances)} Compute instances; {oke_count} OKE excluded; "
          f"{len(instances) - oke_count} non-OKE.")
    print(f"Eligible for selection: {len(targets)}; non-running: {stopped}; "
          f"unsupported/unverified: {unsupported}.")
    return targets


def parse_instance_selection(value: str, size: int) -> list[int]:
    """Parse one-based comma-separated indexes/ranges, returning sorted zero-based indexes."""
    value = value.strip().lower()
    if value == "all":
        return list(range(size))
    if value in {"none", "q", "quit"}:
        return []
    selected = set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("Use all, none, or numbers/ranges such as 1,3-5.")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= size:
            raise ValueError(f"Selection must be between 1 and {size}.")
        selected.update(range(start - 1, end))
    return sorted(selected)


def select_instances(args: argparse.Namespace, targets: list[tuple[Any, Platform]],
                     compartments: dict[str, str], registered_ids: set[str]) -> list[tuple[Any, Platform]]:
    for index, (instance, platform) in enumerate(targets, 1):
        state = "already registered; reconcile parent group" if instance.id in registered_ids else "register"
        print(f"{index:>3}. {instance.display_name} | Compartment: {compartments.get(instance.compartment_id, instance.compartment_id)} | "
              f"{key(platform)} | {state}\n     {instance.id}")
    if args.all_instances:
        chosen = targets
    elif args.instance_ids:
        requested = {ocid.strip() for ocid in args.instance_ids.split(",") if ocid.strip()}
        unknown = requested - {i.id for i, _ in targets}
        if not requested or unknown:
            raise SystemExit(f"--instance-ids includes no selection or ineligible/out-of-scope/OKE IDs: {sorted(unknown)}")
        chosen = [(i, p) for i, p in targets if i.id in requested]
    else:
        if not args.interactive and not sys.stdin.isatty():
            raise SystemExit("No terminal for selection. Use --all or --instance-ids; no OCI changes made.")
        print("Choose instances for registration/parent-group reconciliation. 'none' cancels this run.")
        if not args.skip_terminated_cleanup:
            print("Terminated non-OKE records have a separate cleanup selection; registration selection does not approve deletion.")
        if not targets:
            try:
                proceed = input("No eligible instances. Continue IAM setup and terminated cleanup? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                proceed = "n"
            if proceed not in {"y", "yes"}:
                raise SystemExit("Cancelled; no OCI changes made.")
            return []
        while True:
            try:
                value = input("Select instances (all / none / e.g. 1,3-5): ")
                chosen = [targets[i] for i in parse_instance_selection(value, len(targets))]
                if not chosen:
                    raise SystemExit("Cancelled; no OCI changes made.")
                break
            except ValueError as exc:
                print(exc)
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("Cancelled; no OCI changes made.") from None
    print(f"Selected {len(chosen)} of {len(targets)} eligible non-OKE instance(s); "
          f"{sum(i.id not in registered_ids for i, _ in chosen)} new registrations.")
    return chosen


def source_preference(token: str, display_name: str) -> tuple[int, str]:
    """Prefer current BaseOS/AppStream sources over pinned patch repositories."""
    name = display_name.casefold()
    if token == "baseos":
        if "baseos_latest" in name:
            return (0, name)
        if "baseos_base" in name:
            return (1, name)
        if "patch" not in name:
            return (2, name)
        return (9, name)
    if re.search(r"_appstream(?:-(?:x86_64|aarch64))?$", name) or "appstream_latest" in name:
        return (0, name)
    if "patch" not in name:
        return (1, name)
    return (9, name)


def source_ready(source: Any) -> bool:
    return (getattr(source, "availability_at_oci", None) == "SELECTED"
            and getattr(source, "lifecycle_state", None) == "ACTIVE")


def extra_repository_ids(args, platform, catalog):
    """Attach every requested optional repository family advertised for this platform."""
    requested = load_json(getattr(args, "repository_map", None)).get(key(platform))
    if requested is not None:
        if not isinstance(requested, list) or any(not isinstance(x, str) for x in requested):
            raise SystemExit("Repository map values must be lists of exact vendor repo IDs.")
        return list(dict.fromkeys(requested))
    families = set(getattr(args, "repository_families", "none").lower().split(",")) - {"none", ""}
    if families - {"uek", "ksplice", "mysql", "oci"}:
        raise SystemExit("Unknown --repository-families; choose uek,ksplice,mysql,oci or none.")
    major = platform.os_family.rsplit("_", 1)[1]
    patterns = {"uek": rf"ol{major}_UEKR\d+(?:_ELS)?",
                "ksplice": rf"ol{major}_ksplice(?:_ELS)?",
                "mysql": rf"ol{major}_MySQL[A-Za-z0-9_]*",
                "oci": rf"ol{major}_oci_included"}
    selected = []
    for family in sorted(families):
        repos = sorted({s.repo_id for s in catalog
                        if getattr(s, "software_source_type", None) == "VENDOR"
                        and s.os_family == platform.os_family and s.arch_type == platform.arch_type
                        and getattr(s, "lifecycle_state", None) not in {"DELETED", "DELETING", "FAILED"}
                        and re.fullmatch(patterns[family], getattr(s, "repo_id", "") or "", re.I)
                        and not re.search(r"debug|source|preview|test", s.repo_id, re.I)})
        if not repos:
            print(f"No {family} repository advertised for {key(platform)} in this region; not attached.")
            continue
        if family in {"uek", "mysql"} and len(repos) > 1:
            print(f"Multiple {family} repositories advertised for {key(platform)}; "
                  f"auto-attaching all matches: {', '.join(repos)}")
        selected.extend(repos)
    return list(dict.fromkeys(selected))


def wait_for_selected_source(args: argparse.Namespace, software: Any, tenancy_id: str,
                              source: Any) -> str:
    """Confirm usability and resolve the selected OCID, including a tenancy copy."""
    deadline = time.monotonic() + max(0, args.source_selection_timeout)
    while True:
        candidates = list_call_get_all_results(
            software.list_software_sources, compartment_id=tenancy_id,
            os_family=[source.os_family], arch_type=[source.arch_type],
            availability_at_oci=["SELECTED"]).data
        for candidate in candidates:
            same_source = candidate.id == source.id
            same_vendor_repo = (
                getattr(candidate, "software_source_type", None) == "VENDOR"
                and getattr(source, "software_source_type", None) == "VENDOR"
                and getattr(source, "repo_id", None)
                and candidate.repo_id == source.repo_id)
            if (source_ready(candidate) and candidate.os_family == source.os_family
                    and candidate.arch_type == source.arch_type and (same_source or same_vendor_repo)):
                print(f"Confirmed OCI source SELECTED: {candidate.display_name} ({candidate.id})")
                return candidate.id
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"Software source {source.display_name} did not become ACTIVE/SELECTED "
                f"within {args.source_selection_timeout}s. No profile will be created with it. "
                "Check OSMH software sources/work requests and retry.")
        time.sleep(min(10, max(0, deadline - time.monotonic())))


def prepare_oci_source(args: argparse.Namespace, software: Any, tenancy_id: str,
                       source: Any) -> str:
    """AVAILABLE is catalog availability, not enrollment readiness."""
    if source_ready(source):
        return source.id
    state = getattr(source, "availability_at_oci", None)
    if getattr(source, "software_source_type", None) != "VENDOR":
        raise SystemExit(f"Source {source.id} is not ACTIVE/SELECTED for OCI. "
                         "Prepare this custom/third-party source before using --software-source-map.")
    if state not in {"AVAILABLE", "SELECTED"}:
        raise SystemExit(f"Source {source.display_name} has OCI availability {state!r}; "
                         "check its entitlement/availability before onboarding.")
    if state == "AVAILABLE":
        action(args, f"select Oracle Linux vendor source {source.display_name} for OCI ({source.id})")
        if not args.dry_run:
            try:
                software.change_availability_of_software_sources(
                    oci.os_management_hub.models.ChangeAvailabilityOfSoftwareSourcesDetails(
                        software_source_availabilities=[
                            oci.os_management_hub.models.SoftwareSourceAvailability(
                                software_source_id=source.id, availability_at_oci="SELECTED")]))
            except oci.exceptions.ServiceError as exc:
                raise SystemExit(
                    f"Cannot select source {source.display_name}: {exc.status}/{exc.code}: {exc.message}. "
                    "Source selection requires OSMH software-source permissions at tenancy root. "
                    f"OCI request ID: {getattr(exc, 'request_id', None)}") from exc
    if args.dry_run:
        action(args, f"verify {source.display_name} is ACTIVE/SELECTED before creating profiles")
        return f"dry-run-source:{source.id}"
    return wait_for_selected_source(args, software, tenancy_id, source)


def ensure_oracle_linux_sources(args: argparse.Namespace, software: Any, tenancy_id: str,
                                platforms: set[Platform], source_map: dict[str, Any]) -> dict[str, Any]:
    """Resolve and SELECT BaseOS/AppStream, never confuse catalog entries with enabled sources."""
    for platform in sorted(platforms, key=key):
        if platform.vendor_name != "ORACLE":
            continue
        map_key = key(platform)
        if source_map.get(map_key):
            resolved = []
            for source_id_value in source_map[map_key]:
                source = software.get_software_source(source_id_value).data
                if (source.os_family, source.arch_type) != (platform.os_family, platform.arch_type):
                    raise SystemExit(f"Mapped software source {source_id_value} is incompatible with {map_key}.")
                resolved.append(prepare_oci_source(args, software, tenancy_id, source))
            source_map[map_key] = resolved
            continue
        if not required_vendor_source_tokens(platform):
            continue
        catalog = list_call_get_all_results(
            software.list_software_sources, compartment_id=tenancy_id,
            os_family=[platform.os_family], arch_type=[platform.arch_type]).data
        major = platform.os_family.rsplit("_", 1)[1]
        required_repos = (["ol7_latest", "ol7_latest_ELS"] if major == "7" else
                          [f"ol{major}_baseos_latest", f"ol{major}_appstream"])
        required_repos += extra_repository_ids(args, platform, catalog)
        required_repos = list(dict.fromkeys(required_repos))
        chosen = []
        for repo_id in required_repos:
            candidates = [s for s in catalog
                          if getattr(s, "software_source_type", None) == "VENDOR"
                          and s.os_family == platform.os_family and s.arch_type == platform.arch_type
                          and getattr(s, "repo_id", None) == repo_id
                          and getattr(s, "lifecycle_state", None) not in {"DELETING", "DELETED", "FAILED"}]
            if not candidates:
                raise SystemExit(f"Cannot find vendor repository {repo_id} for {map_key} in the root catalog. "
                                 "Check regional availability and permissions, or provide --software-source-map.")
            chosen.append(min(candidates, key=lambda s: (
                not source_ready(s),
                getattr(s, "availability_at_oci", None) not in {"AVAILABLE", "SELECTED"}, s.id)))
        # Resolve both standard repos before any request; do not fall back to a
        # version-pinned patch or similarly named custom repository.
        source_map[map_key] = [prepare_oci_source(args, software, tenancy_id, source) for source in chosen]
    return source_map


def ensure_iam(args: argparse.Namespace, identity: Any, tenancy_id: str, compartment_name: str) -> bool:
    """Compatibility wrapper; tree-aware callers use ensure_tree_iam directly."""
    return ensure_tree_iam(args, identity, tenancy_id, [args.compartment_id], None)


def find_or_create_profiles(args: argparse.Namespace, profile_client: Any, tenancy_id: str,
                            requested: dict[str, Any], source_map: dict[str, Any],
                            platforms: set[Platform]) -> dict[Platform, str]:
    """Reuse compatible profiles, or create Linux/Windows registration profiles."""
    profiles = list_call_get_all_results(profile_client.list_profiles, compartment_id=args.compartment_id).data
    if args.compartment_id != tenancy_id:
        profiles += list_call_get_all_results(profile_client.list_profiles, compartment_id=tenancy_id).data
    selected: dict[Platform, str] = {}
    for platform in platforms:
        registration_type = "OCI_WINDOWS" if platform.vendor_name == "MICROSOFT" else "OCI_LINUX"
        override = requested.get(key(platform))
        if override:
            profile = profile_client.get_profile(override).data
            if (profile.os_family, profile.arch_type, profile.vendor_name, profile.registration_type) != (
                    platform.os_family, platform.arch_type, platform.vendor_name, registration_type):
                raise SystemExit(f"Mapped profile {override} is incompatible with {key(platform)} / {registration_type}.")
            selected[platform] = override
            continue
        managed_name = f"{args.group_prefix}-profile-{platform.os_family.lower()}-{platform.arch_type.lower()}"
        owned = [p for p in profiles if getattr(p, "display_name", None) == managed_name
                 and getattr(p, "os_family", None) == platform.os_family
                 and getattr(p, "arch_type", None) == platform.arch_type
                 and getattr(p, "vendor_name", None) == platform.vendor_name
                 and getattr(p, "registration_type", None) == registration_type
                 and getattr(p, "lifecycle_state", None) == "ACTIVE"]
        if len(owned) == 1:
            selected[platform] = owned[0].id
            continue
        if platform.vendor_name in {"MICROSOFT", "CANONICAL"}:
            ubuntu = platform.vendor_name == "CANONICAL"
            standalone_type = "UBUNTU_STANDALONE" if ubuntu else "WINDOWS_STANDALONE"
            label = "Ubuntu" if ubuntu else "Windows"
            compatible = [p for p in profiles
                          if p.os_family == platform.os_family and p.arch_type == platform.arch_type
                          and p.vendor_name == platform.vendor_name and p.registration_type == registration_type
                          and p.profile_type == standalone_type and p.lifecycle_state == "ACTIVE"]
            defaults = [p for p in compatible if getattr(p, "is_default_profile", False)]
            if len(defaults) == 1 or len(compatible) == 1:
                chosen = defaults[0] if len(defaults) == 1 else compatible[0]
                selected[platform] = chosen.id
                print(f"Use {label} profile {chosen.display_name} for {key(platform)}")
            else:
                action(args, f"create {label} standalone profile {managed_name}")
                if args.dry_run:
                    selected[platform] = f"dry-run-profile:{key(platform)}"
                else:
                    model = (oci.os_management_hub.models.CreateUbuntuStandAloneProfileDetails if ubuntu else
                             oci.os_management_hub.models.CreateWindowsStandAloneProfileDetails)
                    selected[platform] = profile_client.create_profile(
                        model(
                            compartment_id=args.compartment_id, display_name=managed_name,
                            description="Created by onboard_osmh.py", vendor_name=platform.vendor_name,
                            os_family=platform.os_family, arch_type=platform.arch_type,
                            registration_type=registration_type)).data.id
            continue
        sources = source_map.get(key(platform), [])
        if platform.vendor_name == "ORACLE" and sources:
            action(args, f"create OSMH software-source profile {managed_name}")
            if args.dry_run:
                selected[platform] = f"dry-run-profile:{key(platform)}"
            else:
                profile = profile_client.create_profile(
                    oci.os_management_hub.models.CreateSoftwareSourceProfileDetails(
                        compartment_id=args.compartment_id, display_name=managed_name,
                        description="Created by onboard_osmh.py", vendor_name=platform.vendor_name,
                        os_family=platform.os_family, arch_type=platform.arch_type,
                        registration_type="OCI_LINUX", software_source_ids=sources)).data
                selected[platform] = profile.id
            continue
        candidates = [p for p in profiles if getattr(p, "os_family", None) == platform.os_family
                      and getattr(p, "arch_type", None) == platform.arch_type
                      and getattr(p, "vendor_name", None) == platform.vendor_name
                      and getattr(p, "lifecycle_state", None) == "ACTIVE"]
        # Prefer a service-provided/default OCI profile.  A mapped profile is the
        # deterministic alternative when a tenancy has several compatible profiles.
        defaults = [p for p in candidates if getattr(p, "is_default_profile", False)]
        if len(defaults) == 1:
            selected[platform] = defaults[0].id
        elif len(candidates) == 1:
            selected[platform] = candidates[0].id
        else:
            names = ", ".join(getattr(p, "display_name", p.id) for p in candidates)
            raise SystemExit(f"No unambiguous profile for {key(platform)}. For Oracle Linux, provide its software sources in --software-source-map and this script will create a profile; otherwise supply --profile-map. Candidates: {names or 'none'}")
    return selected


def enable_plugin(args: argparse.Namespace, compute: Any, instance: Any, profile_id: str) -> bool:
    tags = dict(getattr(instance, "freeform_tags", None) or {})
    current = tags.get(PROFILE_TAG)
    if current and current != profile_id:
        print(f"SKIP {instance.display_name}: existing {PROFILE_TAG} points to a different profile")
        return False
    agent_config = getattr(instance, "agent_config", None)
    plugin_state = {p.name: p.desired_state for p in (getattr(agent_config, "plugins_config", None) or [])}
    management_disabled = getattr(agent_config, "is_management_disabled", False) is True
    all_plugins_disabled = getattr(agent_config, "are_all_plugins_disabled", False) is True
    if (current == profile_id and plugin_state.get(PLUGIN_NAME) == "ENABLED"
            and not management_disabled and not all_plugins_disabled):
        # Reapply the desired state on retries. This causes Oracle Cloud Agent to
        # reevaluate a profile that was assigned shortly before IAM propagation.
        print(f"RETRY {instance.display_name}: reapply OSMH plugin configuration")
    tags[PROFILE_TAG] = profile_id
    action(args, f"set {PROFILE_TAG} and enable {PLUGIN_NAME} on {instance.display_name}")
    if not args.dry_run:
        details = oci.core.models.UpdateInstanceDetails(
            freeform_tags=tags,
            agent_config=oci.core.models.UpdateInstanceAgentConfigDetails(
                # OSMH is a management plugin. These flags can independently
                # suppress a plugin even when its desired state is ENABLED.
                is_management_disabled=False,
                are_all_plugins_disabled=False,
                plugins_config=[oci.core.models.InstanceAgentPluginConfigDetails(
                    name=name, desired_state=state)
                    for name, state in {**plugin_state, PLUGIN_NAME: "ENABLED"}.items()]))
        compute.update_instance(instance.id, details)
    return True


def wait_for_managed_instances(args: argparse.Namespace, managed: Any, compute_ids: set[str],
                               compartment_ids: list[str] | None = None) -> dict[str, Any]:
    """Return managed instances keyed by OCI compute OCID after OCA registration."""
    deadline = time.monotonic() + args.registration_timeout
    while True:
        all_instances = list_managed_in_compartments(managed, compartment_ids or [args.compartment_id])
        # OCI-managed instances use the Compute instance OCID as their managed
        # instance ID. ``instance_id`` is kept as a compatibility fallback for
        # SDK/API versions that expose it separately.
        found = {next((candidate for candidate in (
                     getattr(mi, "id", None), getattr(mi, "instance_id", None),
                     getattr(mi, "compute_instance_id", None)) if candidate in compute_ids), None): mi
                 for mi in all_instances}
        found = {k: v for k, v in found.items() if k in compute_ids}
        if found.keys() >= compute_ids or time.monotonic() >= deadline:
            return found
        print(f"Waiting for OSMH registration ({len(found)}/{len(compute_ids)})...")
        time.sleep(20)


def wait_for_unregistration(args: argparse.Namespace, managed: Any, work_requests: Any,
                            mi: Any, work_request_id: str | None) -> bool:
    """Confirm disappearance, not merely HTTP acceptance or agent inactivity."""
    deadline = time.monotonic() + max(0, args.unregistration_timeout)
    last_status = None
    while True:
        # A successful compartment list avoids treating an ambiguous GET 404
        # (NotAuthorizedOrNotFound) as proof that deletion completed.
        remaining = list_call_get_all_results(
            managed.list_managed_instances, compartment_id=args.compartment_id).data
        if not any(record.id == mi.id for record in remaining):
            print(f"Confirmed OSMH removal: {mi.display_name} is absent from the compartment instance list.")
            return True
        status = "NO_WORK_REQUEST_ID"
        if work_request_id:
            work = work_requests.get_work_request(work_request_id).data
            status = work.status
            if status in {"FAILED", "CANCELED", "SKIPPED"}:
                errors = list_call_get_all_results(
                    work_requests.list_work_request_errors, work_request_id).data
                details = "; ".join(f"{error.code}: {error.message}" for error in errors)
                print(f"Unregistration {status} for {mi.display_name}; work request {work_request_id}. "
                      f"{details or getattr(work, 'message', None) or 'No service error details returned.'}")
                return False
        if status != last_status:
            print(f"Waiting for OSMH removal: {mi.display_name}; work request status={status}")
            last_status = status
        if time.monotonic() >= deadline:
            print(f"Unregistration not confirmed before timeout: {mi.display_name} remains in OSMH. "
                  f"Work request: {work_request_id or 'not returned'} ({status}). "
                  "Inactive/Offline does not mean unregistered.")
            return False
        time.sleep(min(10, max(0, deadline - time.monotonic())))


def cleanup_terminated_instances(args: argparse.Namespace, compute: Any, managed: Any,
                                 managed_instances: list[Any], work_requests: Any) -> CleanupResult:
    """Unregister only OCI instances positively verified as terminated now.

    Missing list entries and NotAuthorizedOrNotFound are not proof of deletion:
    the instance might have moved compartments or become inaccessible.
    """
    result = CleanupResult()
    if args.skip_terminated_cleanup:
        return result
    for mi in managed_instances:
        if (getattr(mi, "location", None) != "OCI_COMPUTE"
                or getattr(mi, "compartment_id", None) != args.compartment_id
                or not mi.id.startswith("ocid1.instance.")):
            continue
        try:
            instance = compute.get_instance(mi.id).data
        except oci.exceptions.ServiceError as exc:
            if exc.status in (401, 403, 404):
                print(f"RETAIN {mi.display_name}: cannot confirm Compute state "
                      f"({exc.status}/{exc.code}); no unregistration performed.")
                continue
            raise
        if instance.compartment_id != args.compartment_id or instance.lifecycle_state != "TERMINATED":
            continue
        if oke_tag_reason(instance):
            print(f"RETAIN OKE instance {mi.display_name}: excluded from OSMH cleanup.")
            continue
        action(args, f"unregister terminated instance {mi.display_name} ({mi.id}) from OSMH")
        if args.dry_run:
            print(f"[dry-run] verify removal for up to {args.unregistration_timeout} seconds after request")
            continue
        try:
            pending = list_call_get_all_results(
                work_requests.list_work_requests, compartment_id=args.compartment_id,
                resource_id=mi.id, operation_type=["UNREGISTER_MANAGED_INSTANCE"],
                status=["WAITING", "ACCEPTED", "IN_PROGRESS", "CANCELING"],
                sort_by="timeCreated", sort_order="DESC").data
            if pending:
                request_id = pending[0].id
                print(f"Resume existing unregistration for {mi.display_name}; work request {request_id}")
            else:
                response = managed.delete_managed_instance(mi.id)
                request_id = response.headers.get("opc-work-request-id")
                print(f"Unregistration request accepted for {mi.display_name} (HTTP {response.status}); "
                      f"work request {request_id or 'not returned'}. Verifying removal...")
            if wait_for_unregistration(args, managed, work_requests, mi, request_id):
                result.removed.add(mi.id)
            else:
                result.incomplete.add(mi.id)
        except oci.exceptions.ServiceError as exc:
            result.incomplete.add(mi.id)
            print(f"Cleanup not confirmed for {mi.display_name}: {exc.status}/{exc.code}: {exc.message}. "
                  "Check the OSMH instance list and work request before retrying.")
    return result


def select_terminated_instances(args, compute, managed_inventory, oke_instances, compartments):
    """Read-only cleanup discovery/approval; deletion rechecks Compute state later."""
    if args.skip_terminated_cleanup:
        return set()
    candidates = []
    for mi in managed_inventory:
        if (mi.id in oke_instances or getattr(mi, "location", None) != "OCI_COMPUTE"
                or not mi.id.startswith("ocid1.instance.")):
            continue
        try:
            instance = compute.get_instance(mi.id).data
        except oci.exceptions.ServiceError as exc:
            if exc.status not in {401, 403, 404}:
                raise
            print(f"RETAIN {mi.display_name}: termination cannot be verified ({exc.status}/{exc.code}).")
            continue
        if (instance.lifecycle_state == "TERMINATED" and instance.compartment_id == mi.compartment_id
                and not oke_tag_reason(instance)):
            candidates.append(mi)
    candidates.sort(key=lambda mi: (mi.compartment_id, mi.display_name, mi.id))
    print(f"Verified terminated OSMH records eligible for cleanup: {len(candidates)}")
    for index, mi in enumerate(candidates, 1):
        print(f"  {index}. {mi.display_name} | Compartment: {compartments.get(mi.compartment_id, mi.compartment_id)} | {mi.id}")
    explicit = getattr(args, "unregister_instance_ids", None)
    if explicit:
        requested = set(filter(None, (x.strip() for x in explicit.split(","))))
        if not requested or requested - {mi.id for mi in candidates}:
            raise SystemExit("Cleanup selection contains unverified, OKE or out-of-scope IDs; no cleanup performed.")
        return requested
    if getattr(args, "unregister_all", False):
        return {mi.id for mi in candidates}
    if not candidates:
        return set()
    if not sys.stdin.isatty() and not getattr(args, "interactive", False):
        print("Cleanup not approved: use --unregister-all or --unregister-instance-ids. Retaining all records.")
        return set()
    print("Unregistering removes OSMH history/reports. It does not terminate Compute instances.")
    while True:
        try:
            answer = input("Unregister numbers/ranges, all, or none [none]: ").strip() or "none"
            indexes = parse_instance_selection(answer, len(candidates))
            return {candidates[i].id for i in indexes}
        except ValueError as exc:
            print(exc)
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Cleanup selection cancelled; no OCI changes made in this region.") from None


def source_id(source: Any) -> str:
    return source if isinstance(source, str) else source.id


def missing_group_sources(group: Any, wanted: list[str], software: Any,
                          cache: dict[str, Any]) -> list[str]:
    """Compare OCIDs and vendor repo identities (root sources can be replicated)."""
    attached = {
        source_id(source)
        for field in ("software_sources", "software_source_ids")
        for source in (getattr(group, field, None) or [])
    }

    def repository_identity(ocid: str) -> tuple[str, ...]:
        if ocid.startswith("dry-run-source:"):
            return ("ocid", ocid)
        if ocid not in cache:
            cache[ocid] = software.get_software_source(ocid).data
        source = cache[ocid]
        repo = getattr(source, "repo_id", None)
        if getattr(source, "software_source_type", None) == "VENDOR" and repo:
            return ("vendor", repo, source.os_family, source.arch_type)
        # Never equate custom/versioned sources merely by display name or repo ID.
        return ("ocid", ocid)

    unmatched = [ocid for ocid in wanted if ocid not in attached]
    if not unmatched:
        return []
    known = {repository_identity(ocid) for ocid in attached}
    missing = []
    for ocid in unmatched:
        identity = repository_identity(ocid)
        if identity not in known:
            missing.append(ocid)
            known.add(identity)
    return missing


def ensure_groups(args: argparse.Namespace, group_client: Any, managed_instances: dict[str, Any],
                  expected_platforms: dict[str, Platform], source_map: dict[str, Any], software: Any) -> None:
    by_platform: dict[Platform, list[Any]] = defaultdict(list)
    for compute_id, mi in managed_instances.items():
        # Use image-derived platform values here. They are known before
        # registration, whereas list responses differ slightly between SDK versions.
        by_platform[expected_platforms[compute_id]].append(mi)
    existing = list_call_get_all_results(group_client.list_managed_instance_groups,
                                         compartment_id=args.compartment_id).data
    source_cache: dict[str, Any] = {}
    for platform, members in by_platform.items():
        name = f"{args.group_prefix}-{platform.os_family.lower()}-{platform.arch_type.lower()}"
        group = next((g for g in existing if g.display_name == name), None)
        eligible_members = []
        for mi in members:
            current_group = getattr(mi, "managed_instance_group", None)
            current_id = getattr(current_group, "id", None)
            if current_id and (not group or current_id != group.id):
                print(f"SKIP group change for {mi.display_name}: already belongs to group {current_id}; "
                      "existing memberships are preserved.")
            elif getattr(mi, "lifecycle_stage", None):
                print(f"SKIP group change for {mi.display_name}: belongs to a lifecycle stage.")
            else:
                eligible_members.append(mi)
        members = eligible_members
        if not members:
            continue
        sources = source_map.get(key(platform), []) if platform.vendor_name == "ORACLE" else []
        if not sources and platform.vendor_name == "ORACLE":
            raise SystemExit(f"Cannot create group {name}: no software sources were resolved for {key(platform)}.")
        if not group:
            action(args, f"create group {name}")
            if args.dry_run:
                continue
            details = dict(compartment_id=args.compartment_id, display_name=name,
                           os_family=platform.os_family, arch_type=platform.arch_type,
                           vendor_name=platform.vendor_name, location="OCI_COMPUTE")
            if sources:
                details["software_source_ids"] = sources
            group = group_client.create_managed_instance_group(
                oci.os_management_hub.models.CreateManagedInstanceGroupDetails(**details)).data
            group = oci.wait_until(
                group_client, group_client.get_managed_instance_group(group.id),
                evaluate_response=lambda response: response.data.lifecycle_state == "ACTIVE",
                max_wait_seconds=600, max_interval_seconds=10).data
        else:
            group = group_client.get_managed_instance_group(group.id).data
            group_platform = Platform(group.os_family, group.arch_type, group.vendor_name)
            if group_platform != platform:
                raise SystemExit(f"Existing group {name} has incompatible platform {group_platform}")
        missing_sources = missing_group_sources(group, sources, software, source_cache)
        if missing_sources:
            action(args, f"attach {len(missing_sources)} software source(s) to {name}")
            if not args.dry_run:
                try:
                    group_client.attach_software_sources_to_managed_instance_group(
                        group.id, oci.os_management_hub.models.AttachSoftwareSourcesToManagedInstanceGroupDetails(
                            software_sources=missing_sources))
                except oci.exceptions.ServiceError as exc:
                    if exc.status != 409:
                        raise
                    # A concurrent run may already have attached the repositories.
                    # Suppress the conflict only after verifying the full desired set.
                    group = group_client.get_managed_instance_group(group.id).data
                    if missing_group_sources(group, sources, software, source_cache):
                        raise
                    print(f"Software sources already attached to {name}")
        elif sources:
            print(f"Software sources already attached to {name} (checked OCIDs/repo IDs)")
        attached = set(getattr(group, "managed_instance_ids", None) or [])
        ids = [m.id for m in members if m.id not in attached]
        if ids:
            action(args, f"attach {len(ids)} managed instance(s) to {name}")
            if not args.dry_run:
                group_client.attach_managed_instances_to_managed_instance_group(
                    group.id, oci.os_management_hub.models.AttachManagedInstancesToManagedInstanceGroupDetails(
                        managed_instances=ids))
        else:
            print(f"Managed instances already attached to {name}")


def regional_map(args, path):
    mapping = load_json(path)
    if "regions" in mapping:
        regions = mapping["regions"]
        if not isinstance(regions, dict) or not isinstance(regions.get(args.region), dict):
            raise SystemExit(f"Map {path} needs a regions.{args.region} object (use an empty object for auto-discovery).")
        return regions[args.region]
    if mapping and (getattr(args, "all_regions", False) or getattr(args, "multi_region", False)):
        raise SystemExit("Profile/software-source OCID maps must be keyed by regions for multi-region runs. "
                         "OCIDs cannot be reused across regions.")
    return mapping


def run_region(args, config, client_kwargs, identity):
    compute = oci.core.ComputeClient(config, **client_kwargs)
    # Registration-profile operations are exposed by OnboardingClient in the
    # current OCI Python SDK (there is no ProfileClient class).
    profile_client = oci.os_management_hub.OnboardingClient(config, **client_kwargs)
    managed = oci.os_management_hub.ManagedInstanceClient(config, **client_kwargs)
    groups = oci.os_management_hub.ManagedInstanceGroupClient(config, **client_kwargs)
    software = oci.os_management_hub.SoftwareSourceClient(config, **client_kwargs)
    work_requests = oci.os_management_hub.WorkRequestClient(config, **client_kwargs)
    container_engine = oci.container_engine.ContainerEngineClient(config, **client_kwargs)
    tenancy_id = config["tenancy"]

    if args.compartment_id == tenancy_id:
        print("TENANCY-ROOT SCOPE: scan root and all active compartments in this workload region. "
              "OSMH groups/profiles are centralized in root; generated IAM policies are tenancy-wide.")
        if not args.skip_terminated_cleanup:
            print("Cleanup candidates cover the entire tree, with separate selection before unregistration.")

    # Scan and select before ALL mutations, including IAM and cleanup.
    compartments = discover_compartments(identity, args.compartment_id)
    compartment_ids = [compartment.id for compartment in compartments]
    compartment_names = compartment_labels(compartments)
    print(f"Scanning {len(compartment_ids)} compartment(s) in {config['region']}; "
          f"OSMH groups/profiles will be created only in {args.compartment_id}.")
    instances = []
    for compartment in compartments:
        rows = list_call_get_all_results(compute.list_instances, compartment.id).data
        instances.extend(rows)
        print(f"  {compartment.name}: {len(rows)} Compute instance(s)")
    oke_instances = discover_oke_instance_ids(container_engine, compartment_ids, instances)
    workflow = getattr(args, "workflow", None)
    if workflow == "tag" and not args.cleanup_only:
        managed_inventory = list_managed_in_compartments(managed, compartment_ids)
        existing_managed = {mi.id: mi for mi in managed_inventory
                            if getattr(mi, "location", None) == "OCI_COMPUTE"}
        candidates = scan_candidates(compute, instances, oke_instances, existing_managed, compartment_names)
        print("Select instances to opt in with a defined tag; the scheduled Function performs onboarding.")
        if not candidates:
            print("No eligible instances to tag; no changes made.")
            return
        targets = select_instances(args, candidates, compartment_names, set(existing_managed))
        if not targets:
            return
        namespace = ensure_tag_namespace(args, identity)
        for instance, _ in targets:
            apply_instance_tag(args, compute, instance, namespace, set(compartment_ids))
        print(f"Selected {len(targets)} instance(s) for {namespace}.managedby=osmanagementhub. "
              "Tagged instances are ready for scheduled reconciliation.")
        return len(targets)
    if workflow == "onboard-tagged" and not args.cleanup_only:
        tagged = [i for i in instances if is_opted_in(i, args.tag_namespace)]
        print(f"Tag filter: {len(tagged)}/{len(instances)} Compute instances opted in; "
              f"{sum(i.id in oke_instances for i in instances)} OKE instances excluded from onboarding.")
        instances = tagged
    managed_inventory = list_managed_in_compartments(managed, compartment_ids)
    existing_managed = {mi.id: mi for mi in managed_inventory if getattr(mi, "location", None) == "OCI_COMPUTE"}
    candidates = scan_candidates(compute, instances, oke_instances, existing_managed, compartment_names)
    targets = ([] if getattr(args, "cleanup_only", False) else
               select_instances(args, candidates, compartment_names, set(existing_managed)))
    cleanup_ids = select_terminated_instances(args, compute, managed_inventory, oke_instances, compartment_names)

    if workflow == "onboard-tagged" and not targets and not cleanup_ids:
        print("No eligible tagged instances; no IAM or OSMH changes needed.")
        return

    iam_changed = (False if getattr(args, "cleanup_only", False) else
                   ensure_tree_iam(args, identity, tenancy_id, compartment_ids, config.get("user")))
    if iam_changed:
        print(f"Waiting {IAM_PROPAGATION_SECONDS} seconds before OSMH operations. "
              "IAM dynamic-group membership changes can take about an hour to propagate.")
        time.sleep(IAM_PROPAGATION_SECONDS)
    cleanup = CleanupResult()
    for compartment_id in compartment_ids:
        scoped_args = argparse.Namespace(**{**vars(args), "compartment_id": compartment_id})
        scoped_inventory = [mi for mi in managed_inventory
                            if mi.compartment_id == compartment_id and mi.id in cleanup_ids]
        result = cleanup_terminated_instances(scoped_args, compute, managed, scoped_inventory, work_requests)
        cleanup.removed.update(result.removed)
        cleanup.incomplete.update(result.incomplete)
    existing_managed = {mi.id: mi for mi in managed_inventory if mi.id not in cleanup.removed
                        and getattr(mi, "location", None) == "OCI_COMPUTE"}
    if cleanup.incomplete:
        print(f"Cleanup incomplete for {len(cleanup.incomplete)} instance(s); continuing onboarding. "
              "The script will exit with status 1.", file=sys.stderr)
    if not targets:
        print("No registration actions selected.")
        if cleanup.incomplete:
            raise SystemExit(1)
        return
    source_map = ensure_oracle_linux_sources(
        args, software, tenancy_id, {p for _, p in targets}, regional_map(args, args.software_source_map))
    pending = [(instance, platform) for instance, platform in targets if instance.id not in existing_managed]
    profile_ids = find_or_create_profiles(args, profile_client, tenancy_id, regional_map(args, args.profile_map),
                                          source_map, {p for _, p in pending}) if pending else {}
    eligible = []
    for instance, platform in targets:
        if instance.id in existing_managed:
            print(f"Already registered in OSMH: {instance.display_name}; no plugin reconfiguration")
            eligible.append((instance, platform))
        elif enable_plugin(args, compute, instance, profile_ids[platform]):
            eligible.append((instance, platform))
    expected_platforms = {instance.id: platform for instance, platform in eligible}
    if args.dry_run:
        print("[dry-run] Group actions for new instances below depend on successful registration.")
        planned_members = {instance.id: existing_managed.get(instance.id, instance)
                           for instance, _ in eligible}
        ensure_groups(args, groups, planned_members, expected_platforms, source_map, software)
        return
    if not eligible:
        if cleanup.incomplete:
            raise SystemExit(1)
        return
    registered = wait_for_managed_instances(args, managed, {i.id for i, _ in eligible}, compartment_ids)
    missing = {i.display_name for i, _ in eligible if i.id not in registered}
    if missing:
        print(("PENDING: registration will be reconciled on the next run: "
               if getattr(args, "defer_registration", False) else "Not registered before timeout: ")
              + ", ".join(sorted(missing)), file=sys.stderr)
    ensure_groups(args, groups, registered, expected_platforms, source_map, software)
    if (missing and not getattr(args, "defer_registration", False)) or cleanup.incomplete:
        raise SystemExit(1)


def parse_regions(value):
    """Preserve requested order, ignore duplicates and reject empty list entries."""
    if value is None:
        return []
    regions = [part.strip() for part in value.split(",")]
    if any(not re.fullmatch(r"[a-z][a-z0-9-]*", region) for region in regions):
        raise SystemExit("Provide region names separated by commas, without empty entries "
                         "(example: us-ashburn-1,us-phoenix-1).")
    return list(dict.fromkeys(regions))


def main(argv=None) -> None:
    args = arguments(argv)
    requested_regions = parse_regions(args.region)
    args.multi_region = getattr(args, "all_regions", False) or len(requested_regions) > 1
    # Bootstrap multi-region discovery through the configured/instance region,
    # not the first requested region, which could be unavailable or misspelled.
    args.region = requested_regions[0] if requested_regions and not args.multi_region else None
    config, client_kwargs = load_auth(args)
    identity = (prepare_identity(config, client_kwargs, args.compartment_id, require_workload_region=False)
                if args.multi_region else prepare_identity(config, client_kwargs, args.compartment_id))
    if not args.multi_region:
        args.region = config["region"]
        from osmh_deployment import prepare, deploy
        prepare(args, config, [args.region], identity, client_kwargs)
        selected = run_region(args, config, client_kwargs, identity)
        deploy(args, [args.region], selected)
        return
    if args.instance_ids or getattr(args, "unregister_instance_ids", None):
        raise SystemExit("Use a single --region for explicit instance-ID selections; multi-region runs support per-region prompts or --all.")
    subscriptions = list_call_get_all_results(identity.list_region_subscriptions, config["tenancy"]).data
    statuses = {r.region_name: r.status for r in subscriptions}
    wanted = sorted(statuses) if getattr(args, "all_regions", False) else requested_regions
    regions, results = [], {}
    for region in wanted:
        if region not in statuses:
            results[region] = "SKIPPED: tenancy is not subscribed to this region (check the region name)."
        elif statuses[region] != "READY":
            results[region] = f"SKIPPED: region subscription is not READY (status={statuses[region]})."
        else:
            regions.append(region)
        if region in results:
            print(f"{region}: {results[region]}")
    if not regions:
        print("\nRegional summary:")
        for region, result in results.items():
            print(f"  {region}: {result}")
        raise SystemExit("No requested regions are READY subscriptions; no changes performed.")
    print(f"Multi-region run: {', '.join(regions)}. Each region is scanned/selected/applied separately.")
    # Validate every regional map before the first mutation in any region.
    for region in regions:
        scoped = argparse.Namespace(**{**vars(args), "region": region})
        regional_map(scoped, args.profile_map)
        regional_map(scoped, args.software_source_map)
    from osmh_deployment import prepare, deploy
    prepare(args, config, regions, identity, client_kwargs)
    selected_count = 0
    for region in regions:
        print(f"\n=== Region: {region} ===")
        scoped = argparse.Namespace(**{**vars(args), "region": region})
        try:
            selected = run_region(scoped, {**config, "region": region}, client_kwargs, identity)
            if isinstance(selected, int):
                selected_count += selected
            results[region] = "completed" if not args.dry_run else "preview completed"
        except oci.exceptions.ServiceError as exc:
            results[region] = f"FAILED: {exc.status}/{exc.code}: {exc.message}"
        except oci.exceptions.RequestException as exc:
            results[region] = f"FAILED: region endpoint/network request: {exc}"
        except SystemExit as exc:
            if "cancel" in str(exc).lower():
                raise SystemExit("Multi-region run cancelled. Earlier regions may already have completed changes; "
                                 f"current region reported: {exc}") from None
            results[region] = f"FAILED/incomplete: {exc}"
        print(f"{region}: {results[region]}")
    print("\nRegional summary:")
    for region in wanted:
        print(f"  {region}: {results[region]}")
    if any(result.startswith("FAILED") for result in results.values()):
        print("Automatic deployment skipped because at least one regional scan failed. "
              "Previously applied tags remain; fix the failure and rerun.")
        raise SystemExit(1)
    deploy(args, regions, selected_count)


if __name__ == "__main__":
    main()
