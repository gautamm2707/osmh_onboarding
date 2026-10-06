"""Read-only home-region/registry/network discovery and ephemeral Docker login."""
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlparse
import warnings

import oci
from oci.pagination import list_call_get_all_results
from osmh_discovery import discover_compartments


def home_region(identity, tenancy_id):
    subscriptions = list_call_get_all_results(identity.list_region_subscriptions, tenancy_id).data
    homes = [r for r in subscriptions if r.is_home_region and r.status == "READY"]
    if len(homes) != 1:
        raise SystemExit("Cannot determine a unique READY home region.")
    return homes[0]


def registry_details(identity, config, kwargs, home):
    config = {**config, "region": home.region_name}
    namespace = oci.object_storage.ObjectStorageClient(config, **kwargs).get_namespace(
        compartment_id=config["tenancy"]).data
    # Use the SDK's realm-aware Artifacts endpoint, not a hard-coded public-cloud suffix.
    endpoint = oci.artifacts.ArtifactsClient(config, **kwargs).base_client.endpoint
    endpoint_host = urlparse(endpoint).hostname
    if not endpoint_host or not endpoint_host.startswith("artifacts."):
        raise SystemExit("Cannot resolve this realm's home-region OCIR endpoint.")
    hosts = {endpoint_host, "ocir." + endpoint_host[len("artifacts."):]}
    if config["tenancy"].split(".")[2] == "oc1":
        hosts.update({f"{home.region_name}.ocir.io", f"{home.region_key.lower()}.ocir.io"})
        host = f"{home.region_key.lower()}.ocir.io"
    else:
        host = "ocir." + endpoint_host[len("artifacts."):]
    user_id = config.get("user")
    if not user_id:
        raise SystemExit("The selected OCI profile must include a user OCID for automatic Docker login.")
    user = identity.get_user(user_id).data
    if user.id != user_id or not user.name:
        raise SystemExit("Could not resolve the OCI profile's user for Docker login.")
    username = user.name
    if username.lower().startswith("default/"):
        username = username.split("/", 1)[1]
    elif "/" not in username and getattr(user, "identity_provider_id", None):
        provider = identity.get_identity_provider(user.identity_provider_id).data
        username = f"{provider.name}/{username}"
    if any(c.isspace() for c in username) or not namespace:
        raise SystemExit("OCI returned an invalid registry namespace or username.")
    return host, namespace, f"{namespace}/{username}", hosts


def automatic_image(host, namespace, compartment_id):
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("onboard_osmh.py", "osmh_discovery.py", "osmh_iam.py", "osmh_runtime.py",
                 "osmh_tags.py", "osmh_deployment.py", "osmh_function_setup.py", "reconcile_osmh.py",
                 "function/func.py", "function/Dockerfile", "function/requirements.txt"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return f"{host}/{namespace}/osmh-worker-{compartment_id[-12:]}:build-{digest.hexdigest()[:16]}"


def resolve_image(identity, config, kwargs, home, compartment_id, image=None):
    from deploy_osmh_function import validate_image
    host, namespace, username, allowed = registry_details(identity, config, kwargs, home)
    image = image or automatic_image(host, namespace, compartment_id)
    chosen_host, chosen_namespace, _ = validate_image(image)
    if chosen_host not in allowed or chosen_namespace != namespace:
        raise SystemExit("Function image must use this tenancy's home-region OCIR and namespace. "
                         f"Omit --function-image/--image for automatic selection ({host}/{namespace}/...).")
    return image, chosen_host, username


def choose(label, values, describe):
    if not values:
        raise SystemExit(f"No available {label}. Choose a new Function network or --network-compartment-id.")
    print(f"Select {label}:")
    for index, value in enumerate(values, 1):
        print(f"  {index}. {describe(value)}")
    while True:
        try:
            value = input(f"{label} number (q cancels): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Cancelled; no instances tagged.") from None
        if value.lower() in {"q", "quit"}:
            raise SystemExit("Cancelled; no instances tagged.")
        if value.isdigit() and 1 <= int(value) <= len(values):
            return values[int(value) - 1]
        print("Enter one of the listed numbers.")


def select_network(args, identity, config, kwargs):
    """List existing VCNs/subnets in the target tree (or explicit network compartment)."""
    network = oci.core.VirtualNetworkClient({**config, "region": args.deployment_region}, **kwargs)
    root = getattr(args, "network_compartment_id", None) or args.compartment_id
    from osmh_runtime import validate_compartment_tenancy
    validate_compartment_tenancy(identity, root, config["tenancy"])
    compartments = discover_compartments(identity, root)
    labels = {c.id: c.name for c in compartments}
    vcns = []
    for compartment in compartments:
        vcns.extend(v for v in list_call_get_all_results(network.list_vcns, compartment.id).data
                    if v.lifecycle_state == "AVAILABLE")
    vcn = choose("VCN", sorted(vcns, key=lambda v: (v.display_name or "", v.id)),
                 lambda v: f"{v.display_name} | {labels.get(v.compartment_id, v.compartment_id)} | {v.id}")
    subnets = []
    for compartment in compartments:
        subnets.extend(s for s in list_call_get_all_results(network.list_subnets, compartment.id, vcn_id=vcn.id).data
                       if s.lifecycle_state == "AVAILABLE" and s.vcn_id == vcn.id)
    subnet = choose("subnet", sorted(subnets, key=lambda s: (s.display_name or "", s.id)),
                    lambda s: f"{s.display_name} | {s.cidr_block} | "
                    f"{'private' if s.prohibit_public_ip_on_vnic else 'public'} | {s.id}")
    args.function_subnet_ids = subnet.id


def select_application(args, identity, config, kwargs):
    """List existing Function applications in the target tree (or explicit network compartment)."""
    client = oci.functions.FunctionsManagementClient({**config, "region": args.deployment_region}, **kwargs)
    root = getattr(args, "network_compartment_id", None) or args.compartment_id
    from osmh_runtime import validate_compartment_tenancy
    validate_compartment_tenancy(identity, root, config["tenancy"])
    compartments = discover_compartments(identity, root)
    by_id = {c.id: c for c in compartments}
    labels = {}
    for compartment in compartments:
        parts = []
        current = compartment
        while current:
            parts.append(current.name)
            current = by_id.get(getattr(current, "compartment_id", None))
        labels[compartment.id] = " / ".join(reversed(parts))
    apps = []
    for compartment in compartments:
        apps.extend(a for a in list_call_get_all_results(client.list_applications, compartment.id).data
                    if getattr(a, "lifecycle_state", "ACTIVE") == "ACTIVE")
    if not apps:
        print("No available Function application found in the selected compartment tree; "
              "continuing with new Function application setup.")
        return False
    app = choose("Function application", sorted(apps, key=lambda a: (a.display_name or "", a.id)),
                 lambda a: f"{a.display_name} | {labels.get(a.compartment_id, a.compartment_id)} | {a.id}")
    args.function_application_id = app.id
    return True


def validate_application(args, config, kwargs):
    if not getattr(args, "function_application_id", None):
        return
    client = oci.functions.FunctionsManagementClient({**config, "region": args.deployment_region}, **kwargs)
    app = client.get_application(args.function_application_id).data
    if getattr(app, "lifecycle_state", "ACTIVE") != "ACTIVE":
        raise SystemExit(f"Function application is not ACTIVE: {args.function_application_id}")
    print(f"Reuse Function application: {getattr(app, 'display_name', args.function_application_id)} "
          f"({args.function_application_id})")


def prepare_docker_config(directory):
    """Keep the selected Docker daemon/plugins without copying registry credentials."""
    original = Path(os.environ.get("DOCKER_CONFIG") or "~/.docker").expanduser()
    destination = Path(directory)
    settings = {}
    try:
        if (original / "config.json").exists():
            source = json.loads((original / "config.json").read_text())
            settings = {key: source[key] for key in ("currentContext", "cliPluginsExtraDirs") if key in source}
        if (original / "contexts").is_dir():
            (destination / "contexts").symlink_to((original / "contexts").resolve(), target_is_directory=True)
        if (original / "cli-plugins").is_dir():
            settings["cliPluginsExtraDirs"] = [str((original / "cli-plugins").resolve()),
                                              *settings.get("cliPluginsExtraDirs", [])]
        (destination / "config.json").write_text(json.dumps(settings))
    except (OSError, ValueError, TypeError):
        raise SystemExit("Cannot prepare temporary Docker settings. Check your Docker configuration.") from None


@contextmanager
def docker_session(host, username):
    """Never pass an auth token in argv/environment, logs, images or Terraform state."""
    print(f"Docker login: {username} at {host}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("OCI auth token for this user (hidden): ")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise SystemExit("A hidden auth-token prompt is required; run deployment in an interactive terminal.") from None
    if not token or "\n" in token or "\r" in token:
        raise SystemExit("Supply a nonempty OCI auth token.")
    with tempfile.TemporaryDirectory(prefix="osmh-docker-") as directory:
        prepare_docker_config(directory)
        env = {**os.environ, "DOCKER_CONFIG": directory}
        try:
            subprocess.run(["docker", "login", "--username", username, "--password-stdin", host],
                           input=token + "\n", text=True, capture_output=True, check=True, env=env, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise SystemExit("Docker login failed. Check the auth token for the displayed user and registry connectivity.") from None
        finally:
            token = None
        yield env
