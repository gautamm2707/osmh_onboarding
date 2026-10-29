"""Portable OCI authentication and read-only tenancy/region preflight.

No tenancy OCIDs, region names, realm domains or service endpoints are embedded.
The SDK resolves endpoints, including non-commercial realms it supports.
"""

from pathlib import Path
import re

import oci
from oci.pagination import list_call_get_all_results


def load_auth(args):
    """Return (configuration, client keyword arguments) without changing OCI."""
    mode = args.auth
    tagging = getattr(args, "workflow", None) == "tag"
    if mode == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        config = {"tenancy": signer.get_claim("res_tenant"), "region": args.region or signer.region}
    elif mode == "instance_principal":
        if not tagging and not args.skip_iam and not getattr(args, "cleanup_only", False) and not args.admin_group:
            raise SystemExit("Instance-principal authentication has no user to add to an IAM group. "
                             "Use --skip-iam with preconfigured access, or --admin-group <existing OCID>.")
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {"tenancy": signer.tenancy_id, "region": args.region or signer.region}
    else:
        config = oci.config.from_file(str(Path(args.config_file).expanduser()), args.profile)
        if args.region:
            config["region"] = args.region
        if mode == "auto":
            mode = "security_token" if config.get("security_token_file") else "api_key"
        signer = None
        if mode == "security_token":
            if not config.get("security_token_file") or not config.get("key_file"):
                raise SystemExit("Session authentication requires security_token_file and key_file in the OCI profile.")
            token = Path(config["security_token_file"]).expanduser().read_text().strip()
            private_key = oci.signer.load_private_key_from_file(
                str(Path(config["key_file"]).expanduser()), config.get("pass_phrase"))
            signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
        oci.config.validate_config(config, **({"signer": signer} if signer else {}))
    if not config.get("tenancy") or not config.get("region"):
        raise SystemExit("Authentication must provide a tenancy OCID and workload region; use --region if needed.")
    if (not tagging and mode != "resource_principal" and not args.skip_iam
            and not getattr(args, "cleanup_only", False) and not args.admin_group and not config.get("user")):
        raise SystemExit("Automatic administrator-group membership requires 'user' in the OCI profile. "
                         "Use --skip-iam or an explicit existing --admin-group OCID.")
    print(f"Authentication: {mode}; tenancy: {config['tenancy']}; workload region: {config['region']}")
    return config, {"signer": signer} if signer else {}


def validate_compartment_tenancy(identity, compartment_id, tenancy_id):
    """Verify the whole ancestry; refuse cross-tenancy or unverifiable targets."""
    if compartment_id.startswith("ocid1.tenancy."):
        if compartment_id != tenancy_id:
            raise SystemExit("Target tenancy OCID does not match the authenticated tenancy; no OCI changes made.")
        tenancy = identity.get_tenancy(tenancy_id).data
        if tenancy.id != tenancy_id:
            raise SystemExit("Tenancy lookup returned an unexpected OCID; no OCI changes made.")
        return
    if not re.fullmatch(r"ocid1\.compartment\.[A-Za-z0-9._-]+", compartment_id):
        raise SystemExit("Provide a compartment OCID or the authenticated tenancy OCID, not a name.")
    current = compartment_id
    visited = set()
    while current != tenancy_id:
        if current in visited or not current.startswith("ocid1.compartment."):
            raise SystemExit("Target compartment does not belong to the authenticated tenancy, or its ancestry is invalid.")
        visited.add(current)
        compartment = identity.get_compartment(current).data
        if compartment.id != current or compartment.lifecycle_state != "ACTIVE":
            raise SystemExit(f"Cannot validate ACTIVE compartment {current}; no OCI changes made.")
        current = compartment.compartment_id
        if not current:
            raise SystemExit("Cannot determine compartment ancestry; no OCI changes made.")


def prepare_identity(config, client_kwargs, compartment_id, require_workload_region=True):
    """Discover subscriptions, route IAM to home, and reject wrong-tenancy input."""
    identity = oci.identity.IdentityClient(config, **client_kwargs)
    try:
        subscriptions = list_call_get_all_results(
            identity.list_region_subscriptions, config["tenancy"]).data
        ready = [r for r in subscriptions if r.status == "READY"]
        if require_workload_region and config["region"] not in {r.region_name for r in ready}:
            raise SystemExit(f"Region {config['region']} is not a READY subscription in this tenancy. "
                             "Select a subscribed region using --region.")
        homes = [r.region_name for r in ready if r.is_home_region]
        if len(homes) != 1:
            raise SystemExit("Cannot determine a unique READY tenancy home region; no OCI changes made.")
        # Do not mutate the workload config: Compute, OKE and OSMH stay regional.
        identity = oci.identity.IdentityClient({**config, "region": homes[0]}, **client_kwargs)
        validate_compartment_tenancy(identity, compartment_id, config["tenancy"])
    except oci.exceptions.ServiceError as exc:
        raise SystemExit(
            "Tenancy preflight failed; no OCI changes made. The caller needs permission "
            "to read region subscriptions and the target's ancestor compartments. Check "
            f"the OCI profile/tenancy. OCI {exc.status}/{exc.code}: {exc.message}") from exc
    print(f"IAM home region: {homes[0]}; compartment ownership verified.")
    return identity
