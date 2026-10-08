"""OCI FDK entrypoint. Deployment config fixes the authorized scope; payload cannot widen it."""
import hashlib
import hmac
import json
import re
from pathlib import Path
import subprocess
import sys
import time


def command(config, payload):
    if not isinstance(payload, dict) or set(payload) - {"dry_run", "onboard_instance_ids"}:
        raise ValueError("Only dry_run and stack-authorized onboard_instance_ids are accepted.")
    if "dry_run" in payload and not isinstance(payload["dry_run"], bool):
        raise ValueError("dry_run must be a JSON boolean.")
    authorized_selection(config, payload)
    target = config.get("OSMH_COMPARTMENT_ID", "")
    if not target.startswith(("ocid1.compartment.", "ocid1.tenancy.")):
        raise ValueError("OSMH_COMPARTMENT_ID must be a compartment or tenancy OCID.")
    args = [sys.executable, "-u", str(Path(__file__).resolve().with_name("reconcile_osmh.py")),
            target, "--auth", "resource_principal", "--tag-namespace", config["OSMH_TAG_NAMESPACE"],
            "--source-selection-timeout", "30"]
    args.extend(["--repository-families", config.get("OSMH_REPOSITORY_FAMILIES", "uek,ksplice,mysql,oci"),
                 "--group-prefix", config.get("OSMH_GROUP_PREFIX", "osmh")])
    regions = config.get("OSMH_REGIONS", "").strip()
    if regions == "all":
        args.append("--all-regions")
    elif regions:
        args.extend(["--regions", regions])
    if config.get("OSMH_SKIP_IAM", "false").lower() == "true":
        args.append("--skip-iam")
    if payload.get("dry_run", False):
        args.append("--dry-run")
    return args


def authorized_selection(config, payload):
    if "onboard_instance_ids" not in payload:
        return None
    # Only the selection recorded in the Function's deployment configuration
    # may opt in instances. Invocation permission alone cannot widen this set.
    ids = payload["onboard_instance_ids"]
    if not isinstance(ids, list) or not ids or any(
            not isinstance(item, str) or not re.fullmatch(r"ocid1\.instance\.[A-Za-z0-9._-]+", item)
            for item in ids):
        raise ValueError("onboard_instance_ids must be a nonempty list of Compute OCIDs.")
    canonical = json.dumps(sorted(set(ids)), separators=(",", ":"))
    expected = config.get("OSMH_SELECTION_SHA256", "")
    if not expected or not hmac.compare_digest(hashlib.sha256(canonical.encode()).hexdigest(), expected):
        raise ValueError("Instance selection does not match the deployed stack configuration.")
    if not config.get("OSMH_SELECTION_REGION"):
        raise ValueError("OSMH_SELECTION_REGION is required for selected-instance onboarding.")
    return canonical


def commands(config, payload):
    reconcile = command(config, payload)  # Validate the entire request before any action.
    selection = authorized_selection(config, payload)
    if selection is None:
        return [reconcile]
    tag = [sys.executable, "-u", str(Path(__file__).resolve().with_name("osmh_selection.py")),
           config["OSMH_COMPARTMENT_ID"], "--region", config["OSMH_SELECTION_REGION"],
           "--tag-namespace", config["OSMH_TAG_NAMESPACE"], "--instance-ids", selection]
    if payload.get("dry_run", False):
        tag.append("--dry-run")
    return [tag, reconcile]


def handler(ctx, data=None):
    from fdk import response

    payload = json.loads(data.getvalue() or b"{}") if data is not None else {}
    steps = commands(dict(ctx.Config()), payload)
    # Stream subprocess stdout/stderr to the application log. Kill on budget
    # expiry so OCI gets a failure, never a false "onboarding complete" response.
    started = time.time()
    output_tail = []
    try:
        for args in steps:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1)
            try:
                for line in process.stdout or []:
                    print(line, end="", flush=True)
                    output_tail.append(line.rstrip())
                    output_tail = output_tail[-200:]
                code = process.wait(timeout=3300)
            except subprocess.TimeoutExpired:
                process.kill()
                raise
            if code:
                raise subprocess.CalledProcessError(code, args)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), flush=True)
        raise RuntimeError("OSMH reconciliation failed/incomplete; inspect the function application log.") from exc
    return response.Response(ctx, response_data=json.dumps({
        "status": "reconciliation_pass_complete", "dry_run": payload.get("dry_run", False),
        "duration_seconds": round(time.time() - started, 1),
        "output_tail": output_tail,
        "note": "Guest registrations reported PENDING are rechecked on the next invocation."}),
        headers={"Content-Type": "application/json"})
