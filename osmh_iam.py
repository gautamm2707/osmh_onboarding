"""IAM setup for a selected OSMH compartment tree.

The caller must already have permission to discover compartments and administer
IAM. Creating these resources cannot bootstrap an otherwise unauthorized user.
Automatic groups use the default identity domain and contain the API user from
the SDK configuration. Existing groups can be selected by OCID instead.
"""

from __future__ import annotations

import re
from typing import Any

import oci
from oci.pagination import list_call_get_all_results
from osmh_tags import tag_rule


GROUP_DESCRIPTION = "OSMH administrators (managed by onboard_osmh.py)"
DG_DESCRIPTION = "OSMH Compute instances (managed by onboard_osmh.py)"
POLICY_DESCRIPTION = "OSMH access (managed by onboard_osmh.py)"


def action(args: Any, message: str) -> None:
    print(("[dry-run] " if args.dry_run else "") + message)


def compartment_rule(compartment_ids: list[str]) -> str:
    """OCI compartment membership is exact, so enumerate the whole subtree."""
    ids = sorted(set(compartment_ids))
    if not ids:
        raise SystemExit("Cannot configure an empty compartment-tree matching rule.")
    return "ANY {" + ", ".join(f"instance.compartment.id = '{cid}'" for cid in ids) + "}"


def simple_rule_compartments(rule: str) -> set[str] | None:
    """Recognize only unqualified compartment OR rules (and a single ALL term).

    A literal OCID appearing somewhere in a rule does not establish membership:
    ALL can also have an instance or tag restriction. Refuse unknown syntax.
    """
    match = re.fullmatch(r"\s*(ANY|ALL)\s*\{\s*(.*?)\s*\}\s*", rule, re.I | re.S)
    body = match.group(2) if match else rule.strip()
    terms = body.split(",")
    if match and match.group(1).upper() == "ALL" and len(terms) != 1:
        return None
    if not match and len(terms) != 1:
        return None
    ids = set()
    for term in terms:
        item = re.fullmatch(r"\s*instance\.compartment\.id\s*=\s*'([^']+)'\s*", term, re.I)
        if not item:
            return None
        ids.add(item.group(1))
    return ids or None


def _normalize(statement: str) -> str:
    return " ".join(statement.split())


def policy_statements(compartment_id: str, tenancy_id: str, admin: str,
                      dynamic_group: str, operator: str | None = None) -> list[str]:
    """Subjects include their type and name, e.g. 'group osmh-admins'."""
    scope = "in tenancy" if compartment_id == tenancy_id else f"in compartment id {compartment_id}"
    statements = [
        # Kept from the previous template: vendor repositories live at root.
        f"Allow {admin} to manage osmh-family in tenancy",
        f"Allow {admin} to manage osmh-family {scope}",
        f"Allow {admin} to manage management-agents {scope}",
        f"Allow {admin} to manage management-agent-install-keys {scope}",
        f"Allow {admin} to use appmgmt-family {scope}",
        f"Allow {admin} to read metrics {scope}",
        f"Allow {admin} to read instances {scope}",
        f"Allow {admin} to {{INSTANCE_UPDATE}} {scope}",
        f"Allow {admin} to read osmh-profiles in tenancy where target.profile.compartment.id = '{tenancy_id}'",
        f"Allow {admin} to read osmh-software-sources in tenancy where target.softwareSource.compartment.id = '{tenancy_id}'",
        f"Allow {dynamic_group} to {{OSMH_MANAGED_INSTANCE_ACCESS}} {scope} where request.principal.id = target.managed-instance.id",
        f"Allow {dynamic_group} to use metrics {scope} where target.metrics.namespace = 'oracle_appmgmt'",
        f"Allow {dynamic_group} to {{MGMT_AGENT_DEPLOY_PLUGIN_CREATE, MGMT_AGENT_INSPECT, MGMT_AGENT_READ}} {scope}",
        f"Allow {dynamic_group} to {{APPMGMT_MONITORED_INSTANCE_READ, APPMGMT_MONITORED_INSTANCE_ACTIVATE}} {scope} where request.instance.id = target.monitored-instance.id",
        f"Allow {dynamic_group} to {{INSTANCE_READ, INSTANCE_UPDATE}} {scope} where request.instance.id = target.instance.id",
        f"Allow {dynamic_group} to {{APPMGMT_WORK_REQUEST_READ, INSTANCE_AGENT_PLUGIN_INSPECT}} {scope}",
        f"Allow any-user to read instances {scope} where request.principal.type = 'osmh-dynamic-sets'",
        f"Allow any-user to inspect management-agents {scope} where request.principal.type = 'osmh-dynamic-sets'",
    ]
    if operator:
        statements.extend([
            f"Allow {operator} to read osmh-family {scope}",
            f"Allow {operator} to use appmgmt-family {scope}",
            f"Allow {operator} to read metrics {scope}",
        ])
    return list(dict.fromkeys(statements))


def _resolve(identity: Any, tenancy_id: str, reference: str, kind: str,
             identity_domain: str | None = None) -> Any | None:
    """Resolve existing groups. Name lookup is limited to default-domain IAM."""
    if reference.startswith("ocid1."):
        resource = getattr(identity, f"get_{kind}")(reference).data
        if not identity_domain:
            defaults = list_call_get_all_results(
                getattr(identity, f"list_{kind}s"), tenancy_id).data
            if not any(item.id == resource.id and item.name == resource.name for item in defaults):
                raise SystemExit("Cannot verify the identity domain for the supplied group OCID. "
                                 "Provide --identity-domain for name-based policies, or use --skip-iam.")
        return resource
    if "/" in reference:
        domain, reference = reference.split("/", 1)
        if identity_domain and domain != identity_domain:
            raise SystemExit("The supplied identity-domain and group reference disagree.")
        identity_domain = domain
    if identity_domain and identity_domain.casefold() != "default":
        raise SystemExit(
            "Automatic IAM creation/name lookup supports the default identity domain. "
            "For a different domain, provide existing --admin-group and "
            "--instance-dynamic-group OCIDs (and --operator-group OCID if used), "
            "or use --skip-iam after an administrator configures access.")
    results = list_call_get_all_results(
        getattr(identity, f"list_{kind}s"), tenancy_id).data
    found = [resource for resource in results if resource.name == reference
             and getattr(resource, "lifecycle_state", "ACTIVE") not in {"DELETING", "DELETED"}]
    if len(found) > 1:
        raise SystemExit(f"More than one IAM {kind} matches {reference!r}; supply its OCID.")
    return found[0] if found else None


def _policy_name(value: str) -> str:
    if not value or any(c in value for c in "'\"/\n\r"):
        raise SystemExit("IAM group/domain name cannot be safely represented in a policy.")
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+", value) else f"'{value}'"


def _subject(kind: str, resource: Any | None, planned_name: str,
             identity_domain: str | None = None) -> str:
    reference = planned_name
    if "/" in reference:
        prefix, reference = reference.split("/", 1)
        if identity_domain and prefix.casefold() != identity_domain.casefold():
            raise SystemExit("The supplied identity-domain and group reference disagree.")
        identity_domain = prefix
    name = resource.name if resource else reference
    if name.startswith("ocid1."):
        raise SystemExit("Cannot render a name-based policy without resolving the group OCID.")
    # Omitting Default is supported in both legacy IAM and identity-domain tenancies.
    qualifier = (f"{_policy_name(identity_domain)}/" if identity_domain
                 and identity_domain.casefold() != "default" else "")
    return f"{kind} {qualifier}{_policy_name(name)}"


def reconcile_policy_statements(current, desired, legacy, subject_pairs):
    """Convert only exact old template grants, preserving all custom grants."""
    legacy_set = {_normalize(s) for s in legacy}
    desired_set = {_normalize(s) for s in desired}
    updated, conversions, seen = [], [], set()
    for statement in current:
        candidate = statement
        normalized = _normalize(statement)
        if normalized in legacy_set:
            for old, new in subject_pairs:
                prefix = f"Allow {old} to "
                if normalized.startswith(prefix):
                    replacement = f"Allow {new} to " + normalized[len(prefix):]
                    if _normalize(replacement) in desired_set:
                        candidate = replacement
                    break
        if _normalize(candidate) != normalized:
            conversions.append((statement, candidate))
        normalized = _normalize(candidate)
        # Deduplicate template grants only, never unrelated user statements.
        if normalized not in desired_set or normalized not in seen:
            updated.append(candidate)
        seen.add(normalized)
    missing = [s for s in desired if _normalize(s) not in seen]
    return updated + missing, missing, conversions


def ensure_tree_iam(args: Any, identity: Any, tenancy_id: str,
                    compartment_ids: list[str], caller_user_id: str) -> bool:
    """Create/reconcile owned IAM resources; return whether writes were made.

    Adds missing statements and converts exact old template subjects to names. Existing unrelated statements,
    groups, memberships and policies are never deleted. Dry runs are read-only.
    """
    if getattr(args, "skip_iam", False):
        print("Using existing IAM permissions (--skip-iam).")
        return False
    try:
        return _ensure_tree_iam(args, identity, tenancy_id, compartment_ids, caller_user_id)
    except oci.exceptions.ServiceError as exc:
        if exc.status in (401, 403, 404):
            raise SystemExit(
                "IAM setup was denied or an IAM resource could not be resolved. The "
                "configured API user must already have permission to administer groups, "
                "memberships, dynamic groups and root-compartment policies. Automatic "
                "creation cannot grant the caller its own bootstrap access. Use an "
                "authorized administrator configuration or --skip-iam with existing "
                f"permissions. OCI: {exc.code}: {exc.message}") from exc
        raise


def _ensure_tree_iam(args: Any, identity: Any, tenancy_id: str,
                     compartment_ids: list[str], caller_user_id: str) -> bool:
    root = args.compartment_id
    ids = sorted(set([root, *compartment_ids]))
    suffix = root[-12:].lower()
    admin_arg = getattr(args, "admin_group", None)
    dg_arg = getattr(args, "instance_dynamic_group", None)
    operator_arg = getattr(args, "operator_group", None)
    domain = getattr(args, "identity_domain", None)
    admin_name = admin_arg or f"osmh-admins-{suffix}"
    namespace = getattr(args, "tag_namespace", None)
    dg_name = dg_arg or (f"osmh-tagged-{suffix}" if namespace else f"osmh-instances-{suffix}")
    policy_name = f"osmh-automation-{suffix}"
    required_rule = tag_rule(namespace) if namespace else compartment_rule(ids)
    empty_admin = getattr(args, "allow_empty_admin_group", False)

    # Resolve and validate every pre-existing target before making any writes.
    admin = _resolve(identity, tenancy_id, admin_name, "group", domain)
    if admin_arg and admin is None:
        raise SystemExit(f"Existing administrator group {admin_name!r} was not found; supply its OCID or omit --admin-group.")
    if not admin_arg:
        if not caller_user_id and not empty_admin:
            raise SystemExit("Automatic group membership needs the API user OCID in the OCI SDK config ('user').")
        if admin and getattr(admin, "description", None) != GROUP_DESCRIPTION:
            raise SystemExit(
                f"IAM group {admin_name!r} already exists but is not marked as script-owned. "
                "No membership or permissions were changed. Explicitly select an existing "
                "administrator group with --admin-group if it is the intended group.")
        # Validate that this is a user the IAM API can resolve before creating groups.
        if caller_user_id:
            identity.get_user(caller_user_id)

    dg = _resolve(identity, tenancy_id, dg_name, "dynamic_group", domain)
    dg_response = identity.get_dynamic_group(dg.id) if dg else None
    dg = dg_response.data if dg_response else None
    update_rule = False
    if dg is None and dg_arg:
        raise SystemExit(f"Existing dynamic group {dg_name!r} was not found; supply its OCID or omit --instance-dynamic-group.")
    if dg:
        if namespace:
            if _normalize(getattr(dg, "matching_rule", "") or "") != _normalize(required_rule):
                raise SystemExit(f"Existing dynamic group {dg.name!r} does not match the tag rule. "
                                 f"Review with an IAM administrator: {required_rule}")
            if not dg_arg and getattr(dg, "description", None) != DG_DESCRIPTION:
                raise SystemExit(f"Dynamic group {dg.name!r} is not script-owned; use an explicit override after review.")
    if dg and not namespace:
        current_ids = simple_rule_compartments(getattr(dg, "matching_rule", "") or "")
        if dg_arg:
            if current_ids is None or not set(ids).issubset(current_ids):
                raise SystemExit(
                    f"Existing dynamic group {dg.name!r} does not have a verifiable rule covering "
                    f"all {len(ids)} scanned compartments. An IAM administrator must configure "
                    f"this rule, or omit --instance-dynamic-group for an automatic group:\n{required_rule}")
        else:
            if getattr(dg, "description", None) != DG_DESCRIPTION or current_ids is None:
                raise SystemExit(
                    f"Dynamic group {dg.name!r} is not script-owned or its matching rule was "
                    "customized. Refusing to overwrite it. Supply a compatible existing "
                    "--instance-dynamic-group or restore the generated compartment-only rule.")
            update_rule = current_ids != set(ids)

    operator = _resolve(identity, tenancy_id, operator_arg, "group", domain) if operator_arg else None
    if operator_arg and operator is None:
        raise SystemExit(f"Existing operator group {operator_arg!r} was not found.")

    # Validate name/domain rendering before making any IAM changes.
    subjects = [_subject("group", admin, admin_name, domain),
                _subject("dynamic-group", dg, dg_name, domain),
                _subject("group", operator, operator_arg, domain) if operator else None]

    policies = list_call_get_all_results(identity.list_policies, tenancy_id).data
    existing = next((p for p in policies if p.name == policy_name), None)
    policy_response = identity.get_policy(existing.id) if existing else None
    existing = policy_response.data if policy_response else None
    if existing and getattr(existing, "description", None) != POLICY_DESCRIPTION:
        raise SystemExit(
            f"IAM policy {policy_name!r} already exists but is not marked as script-owned. "
            "Use --skip-iam after an administrator configures the required permissions.")

    memberships = []
    if admin and not admin_arg and caller_user_id:
        memberships = list_call_get_all_results(
            identity.list_user_group_memberships, tenancy_id,
            user_id=caller_user_id, group_id=admin.id).data
        memberships = [m for m in memberships if getattr(m, "lifecycle_state", "ACTIVE")
                       not in {"DELETING", "DELETED", "INACTIVE"}]

    changed = False
    # Attempt DG creation before adding an administrator membership, so a tenancy
    # with a full DynamicResourceGroups quota fails before changing user access.
    if dg is None:
        action(args, f"create dynamic group {dg_name}: {required_rule}")
        if not args.dry_run:
            try:
                dg = identity.create_dynamic_group(oci.identity.models.CreateDynamicGroupDetails(
                    compartment_id=tenancy_id, name=dg_name,
                    description=DG_DESCRIPTION, matching_rule=required_rule)).data
            except oci.exceptions.ServiceError as exc:
                detail = str(exc).casefold()
                if ("dynamicresourcegroups" in detail and ("quota" in detail or "limit" in detail)) or exc.code == "LimitExceeded":
                    raise SystemExit(
                        "The tenancy cannot create another dynamic group (OCI quota/limit). "
                        "Ask an IAM administrator to free capacity/increase the limit, or "
                        "provide --instance-dynamic-group <existing-group-name-or-OCID> "
                        f"whose rule covers every scanned compartment:\n{required_rule}") from exc
                raise
            changed = True
    elif update_rule:
        action(args, f"update owned dynamic group {dg.name} for {len(ids)} compartment(s): {required_rule}")
        if not args.dry_run:
            identity.update_dynamic_group(dg.id,
                oci.identity.models.UpdateDynamicGroupDetails(matching_rule=required_rule),
                if_match=dg_response.headers.get("etag"))
            changed = True
            print("Dynamic-group rule changes can take about one hour to propagate in OCI; "
                  "a short policy wait does not guarantee instance-principal access is ready.")
    else:
        print(f"Dynamic group {dg.name} matches the defined tag." if namespace else
              f"Dynamic group {dg.name} covers all {len(ids)} scanned compartment(s).")

    if admin is None:
        action(args, f"create IAM administrator group {admin_name} in the default identity domain")
        if not args.dry_run:
            admin = identity.create_group(oci.identity.models.CreateGroupDetails(
                compartment_id=tenancy_id, name=admin_name, description=GROUP_DESCRIPTION)).data
            changed = True
    if admin and not admin_arg and not args.dry_run:
        if getattr(admin, "lifecycle_state", "ACTIVE") == "CREATING":
            print(f"Waiting for IAM group {admin_name} to become ACTIVE...")
            response = oci.wait_until(identity, identity.get_group(admin.id),
                evaluate_response=lambda result: result.data.lifecycle_state != "CREATING",
                fetch_func=lambda **_: identity.get_group(admin.id),
                max_wait_seconds=300, max_interval_seconds=10)
            admin = response.data
        if getattr(admin, "lifecycle_state", "ACTIVE") != "ACTIVE":
            raise SystemExit(f"IAM group {admin_name} is not ACTIVE; retry when OCI finishes creating it.")
    if not admin_arg and not memberships and caller_user_id:
        action(args, f"add configured API user {caller_user_id} to script-owned IAM group {admin_name}")
        if not args.dry_run:
            identity.add_user_to_group(oci.identity.models.AddUserToGroupDetails(
                user_id=caller_user_id, group_id=admin.id))
            changed = True

    if empty_admin and not caller_user_id:
        print(f"IAM administrator group {admin_name} has no automatic user assignment; "
              "the Function authenticates with its separately authorized resource principal.")

    statements = policy_statements(root, tenancy_id, *subjects)
    old_subjects = [f"group id {admin.id}" if admin else subjects[0],
                    f"dynamic-group id {dg.id}" if dg else subjects[1],
                    f"group id {operator.id}" if operator else None]
    legacy = policy_statements(root, tenancy_id, *old_subjects)
    current = list(existing.statements or []) if existing else []
    updated, missing, conversions = reconcile_policy_statements(
        current, statements, legacy, [(old, new) for old, new in zip(old_subjects, subjects) if old])
    if not existing or updated != current:
        action(args, (f"update IAM policy {policy_name}: add {len(missing)} missing statement(s), "
                      f"convert {len(conversions)} template statement(s) from group OCIDs to names"
                      if existing else f"create IAM policy {policy_name}"))
        print("Policy grants apply to the supplied compartment and its descendants; "
              "the administrator group also receives tenancy-wide OSMH access for root vendor sources.")
        if args.dry_run:
            for old, new in conversions:
                print(f"  replace: {old}\n     with: {new}")
            for statement in missing:
                print(f"  {statement}")
        else:
            if existing:
                identity.update_policy(existing.id,
                    oci.identity.models.UpdatePolicyDetails(statements=updated),
                    if_match=policy_response.headers.get("etag"))
            else:
                identity.create_policy(oci.identity.models.CreatePolicyDetails(
                    compartment_id=tenancy_id, name=policy_name,
                    description=POLICY_DESCRIPTION, statements=statements))
            changed = True
    else:
        print(f"IAM policy {policy_name} already matches the script template.")
    return changed
