"""OCI FDK entrypoint. Deployment config fixes the authorized scope; payload cannot widen it."""
import json
from pathlib import Path
import subprocess
import sys
import time


def command(config, payload):
    if not isinstance(payload, dict) or set(payload) - {"dry_run"}:
        raise ValueError("Only an optional boolean dry_run payload is accepted.")
    if "dry_run" in payload and not isinstance(payload["dry_run"], bool):
        raise ValueError("dry_run must be a JSON boolean.")
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


def handler(ctx, data=None):
    from fdk import response

    payload = json.loads(data.getvalue() or b"{}") if data is not None else {}
    args = command(dict(ctx.Config()), payload)
    # Stream subprocess stdout/stderr to the application log. Kill on budget
    # expiry so OCI gets a failure, never a false "onboarding complete" response.
    started = time.time()
    output_tail = []
    try:
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
