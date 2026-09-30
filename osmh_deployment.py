"""Local one-command orchestration. Never used by the scheduled Function worker."""
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import osmh_function_setup as setup


def enabled(args):
    return (getattr(args, "workflow", None) == "tag" and not args.cleanup_only
            and not getattr(args, "tag_only", False))


def prepare(args, config, regions, identity=None, client_kwargs=None):
    """Resolve local deployment choices before tagging changes any instances."""
    if not enabled(args):
        return
    if args.auth not in {"auto", "api_key", "security_token"}:
        raise SystemExit("Automatic Function deployment needs an API-key/security-token profile. "
                         "Use --tag-only for principal-authenticated tag selection.")
    if Path(args.config_file).expanduser().resolve() != Path("~/.oci/config").expanduser().resolve():
        raise SystemExit("Automatic Terraform deployment uses profiles in ~/.oci/config. "
                         "Use that config location, or --tag-only for a custom SDK config file.")
    # Avoid silently ignoring options that would otherwise change scheduled behavior.
    if any(getattr(args, option, None) for option in (
            "profile_map", "software_source_map", "repository_map", "admin_group",
            "operator_group", "instance_dynamic_group", "identity_domain")):
        raise SystemExit("Automatic deployment currently uses automatic profile/source/IAM discovery. "
                         "Use --tag-only with these local overrides, or omit the override options.")
    home = setup.home_region(identity, config["tenancy"])
    if args.deployment_region and args.deployment_region != home.region_name:
        raise SystemExit(f"Function deployment must use the tenancy home region {home.region_name}. "
                         "Use --regions to select other workload regions.")
    args.deployment_region = home.region_name
    args.function_image, host, username = setup.resolve_image(
        identity, config, client_kwargs or {}, home, args.compartment_id, args.function_image)
    print(f"Home-region Function: {home.region_name}; image: {args.function_image}; Docker user: {username}")
    if args.dry_run:
        print(f"[dry-run] After tagging, would deploy one Function in {args.deployment_region}, "
              "with a daily 22:00 IST schedule.")
        return
    if not args.function_application_id and not args.function_subnet_ids and not args.create_function_network:
        if not sys.stdin.isatty():
            raise SystemExit("Supply --function-application-id, --function-subnet-ids or --create-function-network; no instances tagged.")
        app_mode = setup.choose("Function application option", ["existing", "new"],
                                lambda value: "Reuse an existing Function application" if value == "existing" else
                                "Create a new Function application")
        if app_mode == "existing":
            setup.select_application(args, identity, config, client_kwargs or {})
        else:
            mode = setup.choose("Function network option", ["existing", "new"],
                                lambda value: "Select an existing VCN/subnet" if value == "existing" else
                                "Create a new private VCN, subnet and NAT gateway")
            if mode == "existing":
                setup.select_network(args, identity, config, client_kwargs or {})
            else:
                args.create_function_network = True
    setup.validate_application(args, config, client_kwargs or {})
    from deploy_osmh_function import validate_image
    validate_image(args.function_image)
    if args.function_subnet_ids and any(not s.strip().startswith("ocid1.subnet.")
                                       for s in args.function_subnet_ids.split(",")):
        raise SystemExit("--function-subnet-ids must contain subnet OCIDs; no instances tagged.")
    if not shutil.which("terraform"):
        raise SystemExit("Install Terraform before automatic deployment; no instances tagged.")
    if not args.skip_function_build:
        if not shutil.which("docker"):
            raise SystemExit("Install/start Docker before automatic deployment; no instances tagged.")
        try:
            subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise SystemExit("Docker is not ready. Start Docker Desktop/Colima and rerun; no instances tagged.") from exc
    print(f"After selection: automatically deploy/reconcile Function infrastructure in {args.deployment_region}; "
          "schedule daily at 22:00 IST. Use --tag-only to disable deployment.")


def deploy(args, regions, selected_count):
    """Invoke reconcile --deploy once, after all regional selections finish."""
    if not enabled(args) or not selected_count:
        return
    argv = [sys.executable, str(Path(__file__).resolve().with_name("reconcile_osmh.py")),
            "--deploy", args.compartment_id, "--profile", args.profile,
            "--auth", args.auth, "--region", args.deployment_region,
            "--tag-namespace", args.tag_namespace, "--image", args.function_image or "<versioned-OCIR-image>",
            "--workload-regions", "all" if args.all_regions else ",".join(regions),
            "--repository-families", args.repository_families, "--group-prefix", args.group_prefix]
    if args.function_subnet_ids:
        argv.extend(["--subnet-ids", args.function_subnet_ids])
    elif args.function_application_id:
        argv.extend(["--application-id", args.function_application_id])
    else:
        argv.append("--create-network")
    if args.skip_function_build:
        argv.append("--skip-build")
    if args.skip_initial_function_invoke:
        argv.append("--skip-initial-invoke")
    if args.skip_iam:
        argv.append("--disable-iam-bootstrap")
    if args.enable_function_logging:
        argv.append("--enable-logging")
    if args.dry_run:
        print("[dry-run] Would run after successful tagging: " + shlex.join(argv + ["--apply", "--yes"]))
        print("[dry-run] No image build/push, repository, network, IAM, Function or schedule changes made.")
        return
    print("Tag selection complete. Running reconcile_osmh.py to deploy Function prerequisites and schedule...", flush=True)
    try:
        subprocess.run([*argv, "--apply", "--yes"], check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Function deployment failed. Applied instance tags and any completed deployment resources "
                         "remain; correct the reported issue and rerun the same command/state. "
                         "The function/schedule may not yet be ready.") from exc
