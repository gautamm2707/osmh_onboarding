#!/usr/bin/env python3
"""Build/push the worker and deploy OCI Functions + Resource Scheduler using Terraform.

Default is a Terraform plan; --apply builds/pushes and asks Terraform to apply.
No credentials are copied into the image or written into Terraform variables.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace as NS

import oci
from osmh_runtime import load_auth, prepare_identity
from osmh_tags import namespace_name
from osmh_function_setup import home_region as find_home, resolve_image, docker_session


def validate_image(image):
    match = re.fullmatch(r"([a-z0-9.-]+)/([a-z0-9_-]+)/([a-z0-9._/-]+):([A-Za-z0-9_.-]+)", image or "")
    if not match or match[4] == "latest":
        raise SystemExit("Use a full versioned OCIR image, e.g. iad.ocir.io/namespace/repository:v1 (not latest).")
    return match[1], match[2], match[3]


def ensure_repository(config, kwargs, compartment_id, image, apply=False):
    """Create a private OCIR repository if absent, without changing existing repositories."""
    _, namespace, repository = validate_image(image)
    actual = oci.object_storage.ObjectStorageClient(config, **kwargs).get_namespace(
        compartment_id=config["tenancy"]).data
    if namespace != actual:
        raise SystemExit("The image's registry namespace does not match the authenticated tenancy. "
                         "No repository or image changes made.")
    client = oci.artifacts.ArtifactsClient(config, **kwargs)
    repositories = oci.pagination.list_call_get_all_results(
        client.list_container_repositories, config["tenancy"],
        compartment_id_in_subtree=True, display_name=repository).data
    # SDK paginated collection responses may expose items; pagination typically flattens them.
    repositories = repositories.items if hasattr(repositories, "items") and not isinstance(repositories, dict) else repositories
    matches = [r for r in repositories if r.display_name == repository]
    if matches:
        if len(matches) != 1 or matches[0].lifecycle_state != "AVAILABLE":
            raise SystemExit(f"OCIR repository {repository} is ambiguous or not AVAILABLE.")
        print(f"Reuse OCIR repository {repository}")
        return
    print(f"{'Create' if apply else '[plan] Would create'} private OCIR repository {repository}")
    if apply:
        client.create_container_repository(oci.artifacts.models.CreateContainerRepositoryDetails(
            compartment_id=compartment_id, display_name=repository, is_public=False))


def terraform_output(terraform, env, state, name):
    result = subprocess.run([*terraform, "output", "-raw", state, name],
                            env=env, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def invoke_initial_reconciliation(config, kwargs, function_id, timeout_seconds=300):
    print("Starting initial OSMH reconciliation now in synchronous Function mode...")
    function = oci.functions.FunctionsManagementClient(config, **kwargs).get_function(function_id).data
    endpoint = getattr(function, "invoke_endpoint", None)
    if not endpoint:
        raise SystemExit(f"OCI did not return an invoke endpoint for Function {function_id}. "
                         "Wait a minute and invoke it manually, or rerun deployment.")
    client = oci.functions.FunctionsInvokeClient(
        config, **{**kwargs, "service_endpoint": endpoint, "timeout": (10, max(60, timeout_seconds + 300))})
    deadline = time.monotonic() + max(0, timeout_seconds)
    delay = 10
    while True:
        try:
            response = client.invoke_function(
                function_id,
                invoke_function_body=io.BytesIO(b"{}"),
                fn_invoke_type="sync")
            request_id = response.headers.get("opc-request-id") if getattr(response, "headers", None) else None
            suffix = f" opc-request-id={request_id}" if request_id else ""
            print(f"Initial reconciliation completed for {function_id}.{suffix}")
            body = getattr(response, "data", None)
            if hasattr(body, "read"):
                body = body.read()
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            if body:
                try:
                    payload = json.loads(body)
                    tail = payload.get("output_tail") or []
                    if tail:
                        print("Initial reconciliation output:")
                        print("\n".join(tail[-80:]))
                    print(f"Initial reconciliation status: {payload.get('status')}; "
                          f"duration: {payload.get('duration_seconds')}s")
                except (TypeError, ValueError):
                    print(str(body)[-4000:])
            return True
        except oci.exceptions.TransientServiceError as exc:
            if time.monotonic() >= deadline:
                print(f"Initial reconciliation was not accepted before timeout: "
                      f"{exc.status}/{exc.code}: {exc.message}. "
                      "Deployment and schedule are ready; rerun the same command or invoke the Function manually.")
                return False
            print(f"Function invoke endpoint is temporarily unavailable "
                  f"({exc.status}/{exc.code}); retrying in {delay}s...")
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(delay * 2, 60)


def deployment_mode(args):
    if args.application_id:
        return "application_id"
    if args.create_network:
        return "create_network"
    return "subnet_ids"


def reconcile_deployment_mode(args, state_dir):
    mode_file = state_dir / "deployment-mode.json"
    current = {
        "mode": deployment_mode(args),
        "application_id": args.application_id or "",
        "subnet_ids": [s.strip() for s in (args.subnet_ids or "").split(",") if s.strip()],
        "create_network": bool(args.create_network),
    }
    state_file = state_dir / "terraform.tfstate"
    if not state_file.exists():
        mode_file.write_text(json.dumps(current, sort_keys=True, indent=2))
        return current
    if not mode_file.exists():
        try:
            state = json.loads(state_file.read_text())
            resources = state.get("resources", [])
        except (OSError, json.JSONDecodeError):
            resources = []
        addresses = {resource.get("type") for resource in resources}
        if "oci_functions_application" in addresses:
            previous = {"mode": "create_network" if "oci_core_vcn" in addresses else "subnet_ids",
                        "application_id": "", "subnet_ids": current["subnet_ids"],
                        "create_network": "oci_core_vcn" in addresses}
            mode_file.write_text(json.dumps(previous, sort_keys=True, indent=2))
        else:
            mode_file.write_text(json.dumps(current, sort_keys=True, indent=2))
            return current
    previous = json.loads(mode_file.read_text())
    if previous.get("mode") == current["mode"]:
        mode_file.write_text(json.dumps(current, sort_keys=True, indent=2))
        return current
    print("Existing deployment state was created with "
          f"{previous.get('mode')}; preserving that mode to avoid deleting managed resources.")
    args.application_id = previous.get("application_id", "")
    args.create_network = previous.get("mode") == "create_network"
    args.subnet_ids = ",".join(previous.get("subnet_ids") or [])
    return previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compartment_id")
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--region", help="Optional assertion of home region; Function/OCIR always deploy there")
    parser.add_argument("--application-id", help="Existing active OCI Functions application OCID in the home region")
    networking = parser.add_mutually_exclusive_group()
    networking.add_argument("--subnet-ids", help="Comma-separated existing subnet OCIDs")
    networking.add_argument("--create-network", action="store_true", help="Create a private VCN, subnet, NAT gateway and HTTPS egress rules")
    parser.add_argument("--image", help="Optional home-region image override; auto-generated by default")
    parser.add_argument("--workload-regions", default="", help="CSV or all; default deployment region")
    parser.add_argument("--tag-namespace")
    parser.add_argument("--auth", choices=("auto", "api_key", "security_token"), default="auto")
    parser.add_argument("--repository-families", default="uek,ksplice,mysql,oci")
    parser.add_argument("--group-prefix", default="osmh")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Approve Terraform apply without its confirmation prompt")
    parser.add_argument("--skip-build", action="store_true", help="Image is already pushed to OCIR")
    parser.add_argument("--disable-iam-bootstrap", action="store_true")
    parser.add_argument("--disable-schedule", action="store_true")
    parser.add_argument("--skip-initial-invoke", action="store_true",
                        help="Deploy and schedule only; do not immediately invoke the Function after apply")
    parser.add_argument("--initial-invoke-timeout", type=int, default=300,
                        help="Seconds to retry transient failures while starting the first detached invocation")
    parser.add_argument("--enable-logging", action="store_true",
                        help="Create a Functions invocation service log. Disabled by default to avoid conflicts on reused apps.")
    args = parser.parse_args(argv)
    subnets = [s.strip() for s in (args.subnet_ids or "").split(",") if s.strip()]
    if args.application_id:
        if not args.application_id.startswith("ocid1.fnapp."):
            parser.error("--application-id must be an OCI Functions application OCID.")
        if subnets or args.create_network:
            parser.error("--application-id cannot be combined with --subnet-ids or --create-network.")
    elif (not subnets and not args.create_network) or any(not s.startswith("ocid1.subnet.") for s in subnets):
        parser.error("Supply --application-id, --create-network, or subnet OCIDs separated by commas.")
    args.config_file = "~/.oci/config"
    args.skip_iam, args.admin_group = True, None
    config, kwargs = load_auth(args)
    identity = prepare_identity(config, kwargs, args.compartment_id, require_workload_region=False)
    namespace = namespace_name(args.compartment_id, args.tag_namespace)
    home = find_home(identity, config["tenancy"])
    if args.region and args.region != home.region_name:
        raise SystemExit(f"Function and OCIR must use home region {home.region_name}.")
    args.region = home.region_name
    config = {**config, "region": home.region_name}
    home_region = home.region_name
    args.image, registry_host, registry_user = resolve_image(
        identity, config, kwargs, home, args.compartment_id, args.image)
    network = oci.core.VirtualNetworkClient(config, **kwargs)
    for subnet in subnets:
        if network.get_subnet(subnet).data.lifecycle_state != "AVAILABLE":
            raise SystemExit(f"Subnet is not AVAILABLE: {subnet}")
    if args.application_id:
        from osmh_function_setup import validate_application
        validate_application(NS(function_application_id=args.application_id, deployment_region=args.region),
                             config, kwargs)
    ensure_repository(config, kwargs, args.compartment_id, args.image)
    root = Path(__file__).resolve().parent
    values = dict(tenancy_id=config["tenancy"], compartment_id=args.compartment_id,
                  region=args.region, home_region=home_region, profile=args.profile,
                  auth="SecurityToken" if args.auth == "security_token" or (
                      args.auth == "auto" and config.get("security_token_file")) else "APIKey",
                  subnet_ids=subnets, image=args.image, tag_namespace=namespace,
                  application_id=args.application_id or "",
                  create_network=args.create_network, repository_families=args.repository_families,
                  group_prefix=args.group_prefix,
                  workload_regions=args.workload_regions,
                  bootstrap_iam=not args.disable_iam_bootstrap, schedule_enabled=not args.disable_schedule,
                  logging_enabled=args.enable_logging)
    # Separate state for every tenancy/scope/deployment region. Reuse on subsequent runs.
    state_dir = root / ".deployment" / config["tenancy"] / args.compartment_id / args.region
    state_dir.mkdir(parents=True, exist_ok=True)
    reconcile_deployment_mode(args, state_dir)
    subnets = [s.strip() for s in (args.subnet_ids or "").split(",") if s.strip()]
    values["subnet_ids"] = subnets
    values["application_id"] = args.application_id or ""
    values["create_network"] = args.create_network
    env = dict(os.environ)
    env.update({f"TF_VAR_{key}": json.dumps(value) if not isinstance(value, str) else value
                for key, value in values.items()})
    env["TF_DATA_DIR"] = str(state_dir / "providers")
    terraform = ["terraform", f"-chdir={root / 'deployment'}"]
    subprocess.run([*terraform, "init", "-input=false"], env=env, check=True)
    state = f"-state={state_dir / 'terraform.tfstate'}"
    plan = str(state_dir / "deployment.tfplan")
    subprocess.run([*terraform, "plan", "-input=false", state, f"-out={plan}"], env=env, check=True)
    if not args.apply:
        print("Plan only. Re-run with --apply to build/push the image and deploy the function and 22:00 IST schedule.")
        return
    if args.yes:
        rendered = subprocess.run([*terraform, "show", "-json", plan], env=env, check=True,
                                  capture_output=True, text=True)
        changes = json.loads(rendered.stdout).get("resource_changes", [])
        destructive = [c["address"] for c in changes if "delete" in c.get("change", {}).get("actions", [])]
        if destructive:
            raise SystemExit("Automatic deployment refuses deletion/replacement of existing resources: "
                             + ", ".join(destructive) + ". Review and apply a deliberate Terraform change separately.")
    if not args.skip_build:
        with docker_session(registry_host, registry_user) as docker_env:
            ensure_repository(config, kwargs, args.compartment_id, args.image, apply=True)
            subprocess.run(["docker", "build", "--platform", "linux/amd64", "-f", "function/Dockerfile",
                            "-t", args.image, "."], cwd=root, check=True, env=docker_env)
            subprocess.run(["docker", "push", args.image], check=True, env=docker_env)
    command = [*terraform, "apply", state]
    if args.yes:
        command.append("-auto-approve")
    else:
        if input("Apply this saved deployment plan? [y/N]: ").strip().lower() not in {"y", "yes"}:
            raise SystemExit("Deployment cancelled. Any newly pushed image/repository remains available.")
    command.append(plan)
    subprocess.run(command, env=env, check=True)
    function_id = terraform_output(terraform, env, state, "function_id")
    if args.skip_initial_invoke:
        print("Function prerequisites, deployment and daily 22:00 IST schedule applied. "
              "Initial invocation skipped by request.")
    else:
        started = invoke_initial_reconciliation(config, kwargs, function_id, args.initial_invoke_timeout)
        if started:
            print("Function prerequisites, deployment, immediate reconciliation start and daily 22:00 IST schedule applied. "
                  "Check the Function invocation status/logs and OSMH instance status/group membership.")
        else:
            print("Function prerequisites, deployment and daily 22:00 IST schedule applied. "
                  "Initial reconciliation did not start because OCI Functions invoke was temporarily unavailable.")


if __name__ == "__main__":
    main()
