#!/usr/bin/env python3
"""Fail fast on a bad GitHub secret, then wait until OCI validates the connection."""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ValidationError(RuntimeError):
    """A safe, user-facing validation failure."""


def repository_coordinates(repository_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(repository_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("The build repository must be an HTTPS github.com URL.")
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    if len(parts) != 2:
        raise ValidationError("The build repository URL must identify one GitHub owner and repository.")
    return parts[0], parts[1]


def oci_error_summary(output: str) -> str:
    start = output.find("{")
    if start >= 0:
        try:
            details = json.loads(output[start:])
            fields = [details.get("code"), details.get("message")]
            request_id = details.get("opc-request-id")
            if request_id:
                fields.append(f"request {request_id}")
            return ": ".join(str(value) for value in fields if value)
        except json.JSONDecodeError:
            pass
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "unknown error")
    return first_line[:500]


def run_oci(arguments: list[str], region: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("oci") is None:
        raise ValidationError("OCI CLI is unavailable in the Resource Manager worker.")
    return subprocess.run(
        ["oci", *arguments, "--auth", "instance_obo_user", "--region", region, "--no-retry"],
        capture_output=True,
        text=True,
        check=False,
    )


def read_vault_token(secret_id: str, region: str) -> str:
    result = run_oci(
        ["secrets", "secret-bundle", "get", "--secret-id", secret_id, "--stage", "CURRENT"],
        region,
    )
    if result.returncode:
        raise ValidationError(
            "Cannot read the CURRENT Vault secret version: " + oci_error_summary(result.stderr or result.stdout)
        )
    try:
        payload = json.loads(result.stdout)
        encoded = payload["data"]["secret-bundle-content"]["content"]
        token = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("The Vault secret does not contain a valid UTF-8 token value.") from error
    if not token or token != token.strip():
        raise ValidationError("The Vault secret must contain only the raw GitHub PAT, with no whitespace or newline.")
    return token


def github_status(url: str, token: str) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "osmh-resource-manager-preflight",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except urllib.error.URLError as error:
        raise ValidationError(f"GitHub could not be reached from Resource Manager: {error.reason}") from error


def validate_github_source(token: str, repository_url: str, commit: str, build_spec: str) -> None:
    if github_status("https://api.github.com/user", token) != 200:
        raise ValidationError(
            "GitHub rejected the Vault secret. Replace its CURRENT value with the raw active PAT, not its name, URL, or description."
        )
    owner, repository = repository_coordinates(repository_url)
    path = urllib.parse.quote(build_spec.strip("/"), safe="/")
    query = urllib.parse.urlencode({"ref": commit})
    source_url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repository)}/contents/{path}?{query}"
    status = github_status(source_url, token)
    if status != 200:
        raise ValidationError(
            f"The PAT is active, but GitHub returned HTTP {status} for {build_spec} at commit {commit}. "
            "Grant the token repository read access and any required organization authorization."
        )


def wait_for_connection(connection_id: str, region: str, timeout_seconds: int) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    delay = 15
    attempt = 0
    last_error = "validation has not run"
    while True:
        attempt += 1
        result = run_oci(["devops", "connection", "validate", "--connection-id", connection_id], region)
        if result.returncode == 0:
            elapsed = round(time.monotonic() - started)
            print(f"OCI DevOps connection validated after {elapsed}s ({attempt} attempt(s)).", flush=True)
            return
        last_error = oci_error_summary(result.stderr or result.stdout)
        remaining = round(deadline - time.monotonic())
        if remaining <= 0:
            raise ValidationError(
                f"OCI DevOps did not validate the GitHub connection within {timeout_seconds}s. Last error: {last_error}"
            )
        sleep_seconds = min(delay, remaining)
        print(
            f"OCI DevOps connection is not ready (attempt {attempt}); retrying in {sleep_seconds}s.",
            flush=True,
        )
        time.sleep(sleep_seconds)
        delay = min(delay * 2, 120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-spec", default="build_spec.yaml")
    parser.add_argument("--region", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        token = read_vault_token(args.secret_id, args.region)
        validate_github_source(token, args.repository_url, args.commit, args.build_spec)
        del token
        print("GitHub PAT and pinned build source validated.", flush=True)
        wait_for_connection(args.connection_id, args.region, args.timeout_seconds)
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
