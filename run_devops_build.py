#!/usr/bin/env python3
"""Run an OCI DevOps build and retry transient source/IAM failures."""
from __future__ import annotations

import argparse
import json
import sys
import time

from validate_build_connection import ValidationError, oci_error_summary, run_oci


RETRYABLE_FAILURES = (
    "error fetching secret variable from vault",
    "unable to fetch build_spec",
    "relatedresourcenotauthorizedornotfound",
    "notauthorizedornotfound",
)
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CANCELING"}


def response_data(result, action: str) -> dict:
    if result.returncode:
        raise ValidationError(f"OCI DevOps could not {action}: {oci_error_summary(result.stderr or result.stdout)}")
    try:
        return json.loads(result.stdout)["data"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValidationError(f"OCI DevOps returned an invalid response while attempting to {action}.") from error


def create_build(args) -> str:
    commit_info = json.dumps(
        {
            "commitHash": args.commit,
            "repositoryBranch": args.branch,
            "repositoryUrl": args.repository_url,
        },
        separators=(",", ":"),
    )
    result = run_oci(
        [
            "devops",
            "build-run",
            "create",
            "--build-pipeline-id",
            args.pipeline_id,
            "--display-name",
            args.display_name,
            "--commit-info",
            commit_info,
        ],
        args.region,
    )
    data = response_data(result, "create the build run")
    build_run_id = data.get("id")
    if not build_run_id:
        raise ValidationError("OCI DevOps did not return a build run OCID.")
    print(f"Started OCI DevOps build run {build_run_id}.", flush=True)
    return build_run_id


def wait_for_build(build_run_id: str, region: str, deadline: float) -> tuple[str, str]:
    last_report = 0.0
    while True:
        if time.monotonic() >= deadline:
            raise ValidationError("Timed out while waiting for the OCI DevOps build run.")
        result = run_oci(["devops", "build-run", "get", "--build-run-id", build_run_id], region)
        data = response_data(result, "read the build run")
        state = str(data.get("lifecycle-state", "UNKNOWN")).upper()
        details = str(data.get("lifecycle-details") or "")
        now = time.monotonic()
        if state in TERMINAL_STATES:
            return state, details
        if now - last_report >= 60:
            print(f"Build run {build_run_id} is {state}.", flush=True)
            last_report = now
        time.sleep(min(15, max(1, round(deadline - now))))


def retryable(details: str) -> bool:
    lowered = details.casefold()
    return any(fragment in lowered for fragment in RETRYABLE_FAILURES)


def execute(args) -> None:
    deadline = time.monotonic() + args.timeout_seconds
    delay = 30
    attempt = 0
    while True:
        attempt += 1
        build_run_id = create_build(args)
        state, details = wait_for_build(build_run_id, args.region, deadline)
        if state == "SUCCEEDED":
            print(f"OCI DevOps build succeeded on attempt {attempt}: {build_run_id}", flush=True)
            return
        if state != "FAILED" or not retryable(details):
            raise ValidationError(f"OCI DevOps build {build_run_id} ended in {state}: {details or 'no details'}")
        remaining = round(deadline - time.monotonic())
        if remaining <= 0:
            raise ValidationError(f"OCI DevOps build retries timed out after: {details or 'source authorization failure'}")
        sleep_seconds = min(delay, remaining)
        print(
            f"Transient OCI source/IAM failure on build attempt {attempt}: {details}. "
            f"Retrying in {sleep_seconds}s.",
            flush=True,
        )
        time.sleep(sleep_seconds)
        delay = min(delay * 2, 120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        execute(args)
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
