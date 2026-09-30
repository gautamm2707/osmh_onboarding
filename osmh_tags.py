"""Defined-tag selection and constant-size instance dynamic-group rules."""

from copy import deepcopy
import re
import time

import oci
from oci.pagination import list_call_get_all_results

TAG_KEY = "managedby"
TAG_VALUE = "osmanagementhub"
TAG_DESCRIPTION = "OSMH opt-in (managed by onboard_osmh.py)"


def namespace_name(compartment_id, override=None):
    name = override or f"OSMH_{compartment_id[-12:]}"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", name):
        raise SystemExit("Tag namespace must start with a letter and contain only letters, digits or underscores (max 100).")
    return name


def tag_rule(namespace):
    namespace_name("", namespace)
    return ("ALL {resource.type = 'instance', "
            f"tag.{namespace}.{TAG_KEY}.value = '{TAG_VALUE}'" + "}")


def is_opted_in(instance, namespace):
    # Freeform tags intentionally do not qualify for dynamic-group membership.
    tags = getattr(instance, "defined_tags", None) or {}
    return (tags.get(namespace) or {}).get(TAG_KEY) == TAG_VALUE


def ensure_tag_namespace(args, identity):
    name = namespace_name(args.compartment_id, args.tag_namespace)
    existing = list_call_get_all_results(
        identity.list_tag_namespaces, args.compartment_id).data
    matches = [n for n in existing if n.name.casefold() == name.casefold()
               and getattr(n, "lifecycle_state", "ACTIVE") != "DELETED"]
    if len(matches) > 1:
        raise SystemExit(f"Ambiguous tag namespace {name}.")
    namespace = matches[0] if matches else None
    if namespace and (namespace.name != name or namespace.is_retired
                      or getattr(namespace, "lifecycle_state", "ACTIVE") != "ACTIVE"):
        raise SystemExit(f"Tag namespace {name} is retired, inactive or uses different capitalization.")
    if not namespace:
        print(f"{'[dry-run] ' if args.dry_run else ''}create tag namespace {name} in {args.compartment_id}")
        if args.dry_run:
            print(f"[dry-run] create tag key {TAG_KEY}; allowed value {TAG_VALUE}")
            return name
        namespace = identity.create_tag_namespace(oci.identity.models.CreateTagNamespaceDetails(
            compartment_id=args.compartment_id, name=name, description=TAG_DESCRIPTION)).data
        name = namespace.name
    tags = list_call_get_all_results(identity.list_tags, namespace.id).data
    tag = next((t for t in tags if t.name.casefold() == TAG_KEY), None)
    if tag:
        # ListTags returns TagSummary, which does not contain the validator.
        tag = identity.get_tag(namespace.id, tag.name).data
        if tag.name != TAG_KEY or tag.is_retired or getattr(tag, "lifecycle_state", "ACTIVE") != "ACTIVE":
            raise SystemExit(f"Tag key {name}.{TAG_KEY} is retired, inactive or has different capitalization.")
        validator = getattr(tag, "validator", None)
        allowed = getattr(validator, "values", None)
        if allowed is not None and TAG_VALUE not in allowed:
            raise SystemExit(f"Existing tag validator does not permit {TAG_VALUE}; no instances tagged.")
    else:
        print(f"{'[dry-run] ' if args.dry_run else ''}create tag key {name}.{TAG_KEY}")
        if not args.dry_run:
            identity.create_tag(namespace.id, oci.identity.models.CreateTagDetails(
                name=TAG_KEY, description=TAG_DESCRIPTION,
                validator=oci.identity.models.EnumTagDefinitionValidator(values=[TAG_VALUE])))
    args.tag_namespace = name
    return name


def _tag_validation_pending(error):
    return (getattr(error, "status", None) == 400
            and getattr(error, "code", None) == "InvalidParameter"
            and "Failed to validate tags" in (getattr(error, "message", "") or ""))


def apply_instance_tag(args, compute, instance, namespace, compartment_ids):
    from osmh_discovery import oke_tag_reason

    deadline = time.monotonic() + max(0, getattr(args, "tag_propagation_timeout", 180))
    announced_wait = False
    while True:
        # Re-read and use ETag: preserve concurrent/user tags, reject moved or terminated instances.
        response = compute.get_instance(instance.id)
        current = response.data
        if (current.compartment_id not in compartment_ids or current.lifecycle_state != "RUNNING"
                or oke_tag_reason(current)):
            raise SystemExit(f"Instance {instance.id} changed scope/state or is OKE; rerun selection.")
        if is_opted_in(current, namespace):
            print(f"Already tagged: {current.display_name}")
            return
        tags = deepcopy(current.defined_tags or {})
        previous = (tags.get(namespace) or {}).get(TAG_KEY)
        if previous not in (None, TAG_VALUE):
            raise SystemExit(f"{current.display_name} already has {namespace}.{TAG_KEY}={previous!r}; resolve this conflict before tagging.")
        tags.setdefault(namespace, {})[TAG_KEY] = TAG_VALUE
        print(f"{'[dry-run] ' if args.dry_run else ''}tag {current.display_name}: {namespace}.{TAG_KEY}={TAG_VALUE}")
        if args.dry_run:
            return
        try:
            compute.update_instance(current.id, oci.core.models.UpdateInstanceDetails(defined_tags=tags),
                                    if_match=response.headers.get("etag"))
            return
        except oci.exceptions.ServiceError as exc:
            if not _tag_validation_pending(exc) or time.monotonic() >= deadline:
                raise
            if not announced_wait:
                print(f"Waiting for defined tag {namespace}.{TAG_KEY} to propagate before tagging instances...")
                announced_wait = True
            time.sleep(10)
