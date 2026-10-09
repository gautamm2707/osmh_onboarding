#!/usr/bin/env python3
"""Idempotently enable OCI service logs without duplicate-create conflicts."""
from __future__ import annotations

import argparse
import json
import sys
import time

from validate_build_connection import ValidationError, oci_error_summary, run_oci


def response_items(result, action: str) -> list[dict]:
    if result.returncode:
        raise ValidationError(f"OCI Logging could not {action}: {oci_error_summary(result.stderr or result.stdout)}")
    try:
        data = json.loads(result.stdout).get("data", [])
    except json.JSONDecodeError as error:
        raise ValidationError(f"OCI Logging returned an invalid response while attempting to {action}.") from error
    if not isinstance(data, list):
        raise ValidationError(f"OCI Logging returned an invalid item list while attempting to {action}.")
    return data


def list_log_groups(compartment_id: str, region: str) -> list[dict]:
    return response_items(
        run_oci(["logging", "log-group", "list", "--compartment-id", compartment_id, "--all"], region),
        "list log groups",
    )


def list_logs(log_group_id: str, region: str) -> list[dict]:
    return response_items(
        run_oci(["logging", "log", "list", "--log-group-id", log_group_id, "--all"], region),
        f"list logs in {log_group_id}",
    )


def source(log: dict) -> dict:
    configuration = log.get("configuration") or {}
    if isinstance(configuration, list):
        configuration = configuration[0] if configuration else {}
    value = configuration.get("source") or {}
    if isinstance(value, list):
        value = value[0] if value else {}
    return value


def matches_service(log: dict, service: str, resource: str, category: str) -> bool:
    value = source(log)
    return (
        value.get("service") == service
        and value.get("resource") == resource
        and value.get("category") == category
    )


def all_logs(compartment_id: str, region: str) -> list[dict]:
    logs: list[dict] = []
    for group in list_log_groups(compartment_id, region):
        group_id = group.get("id")
        if not group_id:
            continue
        for log in list_logs(group_id, region):
            item = dict(log)
            item.setdefault("log-group-id", group_id)
            logs.append(item)
    return logs


def unique_name(base: str, occupied: set[str]) -> str:
    if base not in occupied:
        return base
    for number in range(2, 100):
        candidate = f"{base}-{number}"
        if candidate not in occupied:
            return candidate
    raise ValidationError(f"OCI Logging has too many logs using the {base} name family.")


def ensure_log(args, service: str, resource: str, category: str, name: str) -> str:
    logs = all_logs(args.compartment_id, args.region)
    matches = [item for item in logs if matches_service(item, service, resource, category)]
    if len(matches) > 1:
        raise ValidationError(
            f"OCI Logging returned multiple logs for ({service}, {resource}, {category}); remove the duplicate service log."
        )
    if matches:
        existing = matches[0]
        print(
            f"Reusing OCI service log {existing.get('id')} in log group "
            f"{existing.get('log-group-id') or existing.get('log_group_id')} for {service}/{category}.",
            flush=True,
        )
        return str(existing.get("id"))

    target_logs = list_logs(args.log_group_id, args.region)
    display_name = unique_name(name, {str(item.get("display-name") or item.get("display_name")) for item in target_logs})
    configuration = json.dumps(
        {
            "compartmentId": args.compartment_id,
            "source": {
                "category": category,
                "resource": resource,
                "service": service,
                "sourceType": "OCISERVICE",
            },
        },
        separators=(",", ":"),
    )
    tags = json.dumps(
        {"osmhDeployment": args.deployment_id, "osmhManaged": "true"},
        separators=(",", ":"),
    )
    result = run_oci(
        [
            "logging", "log", "create",
            "--log-group-id", args.log_group_id,
            "--display-name", display_name,
            "--log-type", "SERVICE",
            "--is-enabled", "true",
            "--retention-duration", "30",
            "--configuration", configuration,
            "--freeform-tags", tags,
            "--wait-for-state", "SUCCEEDED",
            "--max-wait-seconds", "600",
        ],
        args.region,
    )
    if result.returncode:
        # A concurrent retry may have won the create race. Re-list before
        # treating OCI's duplicate response as a deployment failure.
        matches = [item for item in all_logs(args.compartment_id, args.region) if matches_service(item, service, resource, category)]
        if len(matches) == 1:
            print(f"Reusing concurrently created OCI service log {matches[0].get('id')}.", flush=True)
            return str(matches[0].get("id"))
        raise ValidationError(f"OCI Logging could not create {service}/{category}: {oci_error_summary(result.stderr or result.stdout)}")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        matches = [item for item in all_logs(args.compartment_id, args.region) if matches_service(item, service, resource, category)]
        if len(matches) == 1:
            print(f"Created OCI service log {matches[0].get('id')} as {display_name}.", flush=True)
            return str(matches[0].get("id"))
        time.sleep(5)
    raise ValidationError(f"OCI Logging accepted {service}/{category}, but the new log did not become visible.")


def cleanup(args) -> None:
    owned_names = {
        "function-invocations",
        "devops-builds",
        f"osmh-{args.deployment_id}-function-invocations",
        f"osmh-{args.deployment_id}-devops-builds",
    }
    for log in list_logs(args.log_group_id, args.region):
        tags = log.get("freeform-tags") or log.get("freeform_tags") or {}
        name = str(log.get("display-name") or log.get("display_name") or "")
        if tags.get("osmhDeployment") != args.deployment_id and name not in owned_names:
            continue
        result = run_oci(
            [
                "logging", "log", "delete",
                "--log-group-id", args.log_group_id,
                "--log-id", str(log.get("id")),
                "--force",
                "--wait-for-state", "SUCCEEDED",
                "--max-wait-seconds", "600",
            ],
            args.region,
        )
        if result.returncode and "NotAuthorizedOrNotFound" not in (result.stderr or result.stdout):
            raise ValidationError(f"OCI Logging could not delete {log.get('id')}: {oci_error_summary(result.stderr or result.stdout)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ensure", "cleanup"))
    parser.add_argument("--compartment-id", required=True)
    parser.add_argument("--log-group-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--function-application-id")
    parser.add_argument("--devops-project-id", default="")
    parser.add_argument("--enabled", choices=("true", "false"), default="true")
    args = parser.parse_args()
    try:
        if args.mode == "cleanup":
            cleanup(args)
        elif args.enabled == "false":
            print("OCI service log creation is disabled.", flush=True)
        else:
            if not args.function_application_id:
                raise ValidationError("The Function application OCID is required to configure service logging.")
            ensure_log(
                args,
                "functions",
                args.function_application_id,
                "invoke",
                f"osmh-{args.deployment_id}-function-invocations",
            )
            if args.devops_project_id:
                ensure_log(
                    args,
                    "devops",
                    args.devops_project_id,
                    "all",
                    f"osmh-{args.deployment_id}-devops-builds",
                )
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
