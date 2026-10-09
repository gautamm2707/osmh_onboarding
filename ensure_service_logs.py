#!/usr/bin/env python3
"""Idempotently enable OCI service logs without duplicate-create conflicts."""
from __future__ import annotations

import argparse
import json
import sys
import time

from validate_build_connection import ValidationError, oci_error_summary, run_oci


def json_response(output: str) -> dict:
    """Decode OCI JSON even when the CLI emits a harmless prefix or suffix."""
    stripped = output.strip()
    if not stripped:
        raise ValueError("empty response")
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "data" in value:
            return value
    raise ValueError("no OCI JSON object")


def response_items(result, action: str) -> list[dict]:
    if result.returncode:
        raise ValidationError(f"OCI Logging could not {action}: {oci_error_summary(result.stderr or result.stdout)}")
    try:
        data = json_response(result.stdout).get("data", [])
    except ValueError as error:
        raise ValidationError(f"OCI Logging returned an invalid response while attempting to {action}.") from error
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        raise ValidationError(f"OCI Logging returned an invalid item list while attempting to {action}.")
    return data


def list_items(arguments: list[str], region: str, action: str) -> list[dict]:
    """Retry a successful-but-incomplete list response once."""
    delays = (2,)
    for attempt in range(len(delays) + 1):
        result = run_oci(arguments, region)
        try:
            return response_items(result, action)
        except ValidationError:
            if result.returncode or attempt == len(delays):
                raise
            delay = delays[attempt]
            print(
                f"OCI Logging list response is not ready (attempt {attempt + 1}); retrying in {delay}s.",
                flush=True,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def list_log_groups(compartment_id: str, region: str) -> list[dict]:
    return list_items(
        ["logging", "log-group", "list", "--compartment-id", compartment_id, "--all"],
        region,
        "list log groups",
    )


def list_logs(log_group_id: str, region: str) -> list[dict]:
    return list_items(
        ["logging", "log", "list", "--log-group-id", log_group_id, "--all"],
        region,
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


def ensure_log(
    args,
    service: str,
    resource: str,
    category: str,
    name: str,
    discovered_logs: list[dict] | None,
) -> str:
    if discovered_logs is not None:
        matches = [item for item in discovered_logs if matches_service(item, service, resource, category)]
        if matches:
            existing = matches[0]
            if len(matches) > 1:
                print(
                    f"WARNING: OCI Logging returned multiple logs for {service}/{category}; "
                    f"reusing {existing.get('id')} and continuing.",
                    flush=True,
                )
            else:
                print(
                    f"Reusing OCI service log {existing.get('id')} in log group "
                    f"{existing.get('log-group-id') or existing.get('log_group_id')} for {service}/{category}.",
                    flush=True,
                )
            return str(existing.get("id"))

        target_logs = [
            item
            for item in discovered_logs
            if (item.get("log-group-id") or item.get("log_group_id")) == args.log_group_id
        ]
        display_name = unique_name(
            name,
            {str(item.get("display-name") or item.get("display_name")) for item in target_logs},
        )
    else:
        # The deployment id makes this name unique to the stack. If a previous
        # attempt already created it, OCI returns 409 and we continue below.
        display_name = name

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
        output = result.stderr or result.stdout
        if "409" in output or "conflict" in output.casefold():
            print(
                f"OCI service logging is already configured for {service}/{category}; continuing.",
                flush=True,
            )
            return "existing"
        raise ValidationError(f"OCI Logging could not create {service}/{category}: {oci_error_summary(output)}")

    print(f"Requested OCI service log {display_name} for {service}/{category}.", flush=True)
    return "created"


def ensure_log_nonblocking(
    args,
    service: str,
    resource: str,
    category: str,
    name: str,
    discovered_logs: list[dict] | None,
) -> str:
    """Keep optional observability failures from blocking core deployment."""
    try:
        return ensure_log(args, service, resource, category, name, discovered_logs)
    except ValidationError as error:
        print(f"WARNING: {error} Continuing without this optional service log.", flush=True)
        return "skipped"


def cleanup(args) -> None:
    owned_names = {
        "function-invocations",
        "devops-builds",
        f"osmh-{args.deployment_id}-function-invocations",
        f"osmh-{args.deployment_id}-devops-builds",
    }
    try:
        logs = list_logs(args.log_group_id, args.region)
    except ValidationError as error:
        print(f"WARNING: {error} Skipping optional service-log cleanup.", flush=True)
        return
    for log in logs:
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
            print(
                f"WARNING: OCI Logging could not delete {log.get('id')}: "
                f"{oci_error_summary(result.stderr or result.stdout)}. Continuing cleanup.",
                flush=True,
            )


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
            try:
                discovered_logs = all_logs(args.compartment_id, args.region)
            except ValidationError as error:
                discovered_logs = None
                print(
                    f"WARNING: {error} Falling back to idempotent service-log creation.",
                    flush=True,
                )
            ensure_log_nonblocking(
                args,
                "functions",
                args.function_application_id,
                "invoke",
                f"osmh-{args.deployment_id}-function-invocations",
                discovered_logs,
            )
            if args.devops_project_id:
                ensure_log_nonblocking(
                    args,
                    "devops",
                    args.devops_project_id,
                    "all",
                    f"osmh-{args.deployment_id}-devops-builds",
                    discovered_logs,
                )
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
