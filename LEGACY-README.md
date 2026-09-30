# Historical direct-onboarding workflow (superseded)

This document describes the earlier compartment-rule workflow and is retained
for reference only. Commands without `--workflow` now select/tag instances.
Use [README.md](README.md) for the current tag-based setup and scheduled Function.

## Earlier OCI OSMH onboarding reference

Run from this directory using the virtual environment with the OCI Python SDK
installed and a working OCI SDK configuration (`~/.oci/config` by default).
Keep `onboard_osmh.py`, `osmh_discovery.py`, `osmh_iam.py`, and `osmh_runtime.py` together. The
configured region determines which Compute and OSMH instances are processed.
The supplied compartment and **all active descendant compartments** are scanned.
By default this is a regional scan. Add `--all-regions` to process every READY
subscribed region in the authenticated tenancy (not every public region in OCI).

## Using this solution in another tenancy

The executable code contains no customer-specific tenancy, user, compartment,
repository or profile OCIDs, and no fixed region or service endpoints. The tenancy
comes from your selected authentication configuration, not from this README or
an existing map file. **Do not copy credentials or OCID map files from another
tenancy.** Map files are optional and are read only when explicitly passed.

This is a reusable solution for OCI tenancies **where OSMH is available** and the
caller has the required permissions. It cannot activate an unavailable service,
subscribe a tenancy to a region, increase quotas, override organization security
policies, or repair guest agent/network prerequisites. It does not manage
cross-tenancy resources. Supply a compartment OCID from the same tenancy as the
credentials, or that tenancy's own OCID to include root and all active descendants.
A tenancy OCID different from the authenticated tenancy is rejected before changes.

### First run in a new tenancy

1. Copy the four Python modules above and `requirements.txt` to a directory on
   your machine. Use a supported Python 3.10+ installation (tested here on 3.14).
   Create an isolated environment and install the OCI SDK:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

2. Configure credentials for **that tenancy**, following Oracle's
   [SDK configuration guide](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm).
   For API-key authentication, a named profile in `~/.oci/config` looks like:

   ```ini
   [TARGET_TENANCY]
   user=<user-ocid-in-target-tenancy>
   fingerprint=<uploaded-api-key-fingerprint>
   tenancy=<target-tenancy-ocid>
   region=<workload-region-name>
   key_file=<absolute-path-to-private-api-key>
   ```

   Upload the matching public key to that OCI user. Keep the private key local;
   never include it in this project or source control. A `[DEFAULT]` profile
   allows the compartment-only invocation. Named profiles let the same script
   switch between tenancies without source changes.

3. Have the tenancy administrator verify the permissions described below.
   Preflight additionally requires `inspect tenancies` (region subscriptions)
   and read access to the target's ancestor compartments. OSMH, Compute and OKE
   inventory must be readable **before** automatic IAM setup. Check that the
   region is subscribed and OSMH is available. Supported Compute images must have
   an operational Oracle Cloud Agent/OSMH plugin and the outbound connectivity
   described in [OSMH prerequisites](https://docs.oracle.com/en-us/iaas/osmh/doc/getstarted.htm).
   This script does not change VCNs, NSGs, routes or guest firewalls.

4. Preview, using a compartment OCID from the new tenancy:

   ```bash
   python onboard_osmh.py <compartment-ocid> --profile TARGET_TENANCY --dry-run
   ```

   Confirm the printed tenancy, workload region and automatically discovered IAM
   home region. Review the numbered instances, OKE exclusions and IAM policy
   statements. Dry run is read-only but still needs discovery permissions.

5. Apply with the same configuration and choose the desired instances:

   ```bash
   python onboard_osmh.py <compartment-ocid> --profile TARGET_TENANCY
   ```

   The script verifies compartment ancestry against the configured tenancy before
   any mutations. IAM calls go to the discovered **home region**; Compute, OKE and
   OSMH calls stay in the **workload region**. Override only the latter using
   `--region <subscribed-region>`, or use `--all-regions` for a regional sweep.
   No cross-tenancy action is implicit.

### Authentication and centrally managed IAM

- API keys: default `--auth auto`, or explicitly `--auth api_key`.
- OCI CLI sessions: create a session with `oci session authenticate` (requires
  the OCI CLI separately). Select its config profile with `--profile`; `auto`
  detects `security_token_file`, or pass `--auth security_token`. The session
  must remain valid for the run; this script does not renew session tokens.
  Automatic user-group membership also requires the correct `user` OCID in the
  config. If absent, use an existing admin group or `--skip-iam`.
- Instance principals: run **on an OCI Compute instance** already authorized by
  its own IAM dynamic group. No local API-key config is needed:

  ```bash
  python onboard_osmh.py <compartment-ocid> --auth instance_principal --skip-iam --all --dry-run
  ```

  Remove `--dry-run` after reviewing. Instance principals have no user to add to
  the generated admin group. Use `--skip-iam` with preconfigured OSMH policies, or
  provide `--admin-group <existing-group-ocid>` if this principal already has IAM
  administration rights. Permissions granted to that user group do **not** grant
  permissions to the executing instance principal.

For non-Default identity domains or centrally managed access, the portable path
is to have an administrator provision the policies and dynamic group there and
run with `--skip-iam`. Existing group/dynamic-group OCIDs can instead be supplied
where OCI's IAM API resolves them. The script does not automatically provision
SCIM groups in arbitrary identity domains. `--identity-domain` qualifies names
in policy subjects as well as name lookup; it does not provision identity domains.
When supplying OCIDs for non-Default groups, also supply the actual identity
domain name. Without a domain, the script verifies OCID overrides against the
Default-domain inventory before converting them to policy names.

Endpoints and realm domains are resolved by the OCI SDK, not hardcoded to the
commercial realm. Other realms still require SDK region support and actual OSMH
availability; they have not been live-tested here. Offline tests cover multiple
tenancy identifiers, authentication methods and home/workload region separation.

References: [IAM home-region requirements](https://docs.oracle.com/en-us/iaas/Content/Identity/regions/managingregions.htm),
[Session authentication](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clitoken.htm).

## Run with just the parent compartment

From a terminal:

```bash
source .venv/bin/activate
python onboard_osmh.py <compartment-ocid>
```

### Scan root and all subcompartments

Use the authenticated tenancy OCID as the positional argument:

```bash
python onboard_osmh.py <tenancy-ocid> --profile <config-profile> --dry-run
python onboard_osmh.py <tenancy-ocid> --profile <config-profile>
```

Root Compute instances are included explicitly, along with instances in every
active descendant compartment. Scanning uses the configured workload region
unless `--all-regions` is supplied. The selected profile must have
read access across the entire tree, including OKE inventory. Any failed discovery
stops the run instead of silently skipping inaccessible compartments.

Interactive selection and OKE exclusions still apply. Choose `all` to select all
eligible instances, or pass `--all` for unattended selection. Stopped/terminated
instances and unsupported OS/architecture combinations are not registration
candidates. OSMH groups and new profiles are centralized in root, not recreated
in each child compartment. Existing foreign-group memberships are preserved.

**Scope warning:** automatic IAM setup generates tenancy-wide policy statements
using `in tenancy`, and the dynamic-group rule includes root and all discovered
compartment IDs. It is not restricted to the interactively selected instances.
Confirmed terminated non-OKE cleanup candidates also cover the whole tree, but
unregistration requires separate selection/approval. Review the dry-run output before applying.

## All subscribed regions

Choose a single region, an explicit comma-separated subset, or all subscribed regions:

```bash
python onboard_osmh.py <tenancy-or-compartment-ocid> --profile pridhu --region us-ashburn-1 --dry-run
python onboard_osmh.py <tenancy-or-compartment-ocid> --profile pridhu --regions us-ashburn-1,us-phoenix-1,eu-frankfurt-1 --dry-run
```

`--region` and `--regions` are aliases; both accept one name or a comma-separated
list. Use region names (not display labels). Quote the list if it contains spaces:
`--regions "us-ashburn-1, us-phoenix-1"`. Whitespace is trimmed, duplicates are
removed, and explicit regions run in the supplied order. Empty entries are rejected.
In multi-region runs, unsubscribed regions are skipped (with a reminder to check
the region name); subscribed but not-ready regions are skipped with their actual
subscription status. READY regions continue in the requested order. Skips are
reported both before processing and in the final summary. Skips alone do not fail
an otherwise successful run, but no eligible regions or an actual processing
failure produces a nonzero exit status. An unavailable first entry does not block
later regions: subscription discovery uses the configured/instance region.
Single-region runs still require a READY subscription. With neither flag, the configured region
is used. Region-list flags cannot be combined with `--all-regions`.

```bash
python onboard_osmh.py <tenancy-or-compartment-ocid> --profile pridhu --all-regions --dry-run
python onboard_osmh.py <tenancy-or-compartment-ocid> --profile pridhu --all-regions
```

Regions are discovered dynamically from tenancy subscriptions; only READY
subscriptions are processed. The script does not subscribe to new regions. IAM
stays in the home region; Compute, OKE, profiles, repositories, groups and cleanup
use a separate regional client context. Groups/profiles are centralized in the
supplied compartment **in each region**, not shared across regions.

Each region is scanned, selected and applied before advancing to the next region.
Service errors, unavailable endpoints and incomplete operations appear in the
final regional summary and produce a nonzero exit status; other regions continue.
Cancellation stops the run, but does not roll back earlier regions. A READY
subscription alone does not guarantee OSMH availability or adequate permissions.
For explicit `--instance-ids` or `--unregister-instance-ids`, use a single `--region`.

The same selection prompts, failure handling and per-region summary apply to
explicit multi-region lists. Remove `--dry-run` from any example to apply changes.

Profile/software-source OCID maps for multi-region runs (explicit lists or
`--all-regions`) must use this structure,
with an entry for **every** processed READY region (`{}` means auto-discover;
skipped regions need no map entry):

```json
{"regions": {"us-ashburn-1": {}, "us-phoenix-1": {}}}
```

Each inner object keeps the usual `OS_FAMILY:ARCH` keys and region-local OCIDs.
Flat OCID maps are rejected in multi-region mode. The separate repository map
uses vendor repo IDs and can be reused across regions.

The script first scans the tree and prints the counts of total Compute instances,
identified OKE instances excluded, eligible running instances, non-running
instances, and unsupported/unverified instances. It then displays numbered
eligible instances with an explicit `Compartment:` path, OS/architecture, and
registration status. Paths within the scanned tree distinguish repeated leaf
names, for example `Example (root) / Production / Apps`. OKE-excluded and other
skipped instance rows also show their compartment paths.
Enter `all`, indexes/ranges such as `1,3-5`, or `none` to cancel before any changes.
Only selected instances are considered for registration and parent-group
reconciliation. Existing members of a different group/lifecycle stage are
reported and retained there; this script does not automatically migrate them.

Append `--dry-run` to preview this complete workflow. For automation with no
terminal, explicitly use `--all` or `--instance-ids ocid1.instance...,ocid1.instance...`:

```bash
python onboard_osmh.py <compartment-ocid> --all --dry-run
python onboard_osmh.py <compartment-ocid> --all
```

Dry run makes no OCI changes. It previews unregistration, profile/plugin changes,
and group reconciliation. Group actions for new instances depend on successful
registration during an actual run. Dry run cannot prove that write permissions,
guest agent health, or network connectivity will permit enrollment.

## Automatic IAM setup and prerequisites

No group arguments are required. Using the parent compartment's OCID suffix,
the script creates/reuses these resources in the **Default identity domain**:

- `osmh-admins-<suffix>`: generic IAM group, with the configured API user added.
- `osmh-instances-<suffix>`: dynamic group using an `ANY` rule containing the
  exact OCID of the parent and every active descendant. Reruns update an owned
  generated rule when the subtree changes. Membership is based on compartments,
  not the interactive selection; OKE exclusion applies to the script's actions.
- `osmh-automation-<suffix>`: root IAM policy granting the groups the OSMH/Advisor
  permissions. Statements use group and dynamic-group **names**; compartment policy scope inherits to
  descendants. The template also grants the admin group tenancy-wide OSMH access
  for root vendor sources, as in the previous version.

The invoking SDK user must **already** be authorized to read the whole compartment
tree, Compute instances/images, OSMH inventory, and OKE node pools (`read
cluster-family`), and to create/update IAM groups, memberships, dynamic groups and
root policies. Creating a group cannot bootstrap those administrative privileges.
Automatic user enrollment requires a user resolvable by the Default-domain IAM
API. For other identity domains, configure IAM there separately and use
`--skip-iam`; legacy name/OCID override flags remain available where the IAM API
can resolve them. Dynamic-group quotas still apply and must be resolved by a
tenancy administrator if exhausted. Rule changes may take about an hour to
propagate; the short initial wait cannot guarantee immediate registration.

To use an existing IAM setup without modifying it:

```bash
python onboard_osmh.py <compartment-ocid> --skip-iam
```

The script checks its own `osmh-automation-<suffix>` policy, not all
effective permissions across the tenancy. A template addition does not establish
that a previously onboarded compartment lacks access. On rerun, exact old
OCID-subject template statements in the script-owned policy are converted to
name-subject statements with the same permissions and conditions. Conversion
uses one ETag-protected policy update and avoids duplicate template grants.
Dry run prints each proposed replacement. Custom statements (including custom
OCID-based grants) and other policies are preserved. Customized/unowned
dynamic-group rules are not overwritten.

For example, the generated subjects now look like:

```text
Allow group osmh-admins-<suffix> to manage osmh-family in tenancy
Allow dynamic-group osmh-instances-<suffix> to {OSMH_MANAGED_INSTANCE_ACCESS} in tenancy where request.principal.id = target.managed-instance.id
```

Default-domain names need no domain prefix. Non-Default names use
`DomainName/GroupName`, quoting domain/group components that contain spaces.
Compartment scopes and dynamic-group matching rules still use compartment OCIDs
to avoid ambiguity; this change concerns the group subjects only.

## OKE exclusions and centralized OSMH groups

Worker nodes are identified through OKE node-pool membership across the selected
tree, recognized OKE tags (`OKEclusterName` + `OKEnodePoolName`, or
`Oracle-Tags.CreatedBy=oke`), and `oke_init_script` metadata. They are excluded from
registration, instance selection, and terminated-record cleanup. Failure to read
OKE inventory stops the run before mutations. Virtual nodes aren't Compute
instances. Untagged self-managed workers, or workers belonging to a pool outside
the tree without these signals, cannot be reliably classified: deselect them in
interactive mode. Instance names alone are not used to infer OKE membership.

Profiles and new OSMH groups are created only in the supplied parent compartment,
one group per OS/version/architecture (for example `osmh-oracle_linux_9-aarch64`
or `osmh-windows_server_2016-x86_64`). Groups can contain instances from child
compartments. Compute instances and managed-instance records remain in their
original compartments. Registration polling and cleanup cover the whole tree.

## Windows and Oracle Linux

- Oracle Linux 7: x86_64 and aarch64; requires `ol7_latest` plus `ol7_latest_ELS`
  from the matching regional/architecture catalog. Missing/restricted ELS sources
  stop onboarding rather than silently using unsupported sources.
- Oracle Linux 8, 9, and 10: discover BaseOS/AppStream sources and Linux profiles.
  Catalog entries in `AVAILABLE` state are explicitly selected for OCI using
  their software-source OCIDs (not their textual repository IDs). Profile creation
  waits until both repositories are `ACTIVE` and `availability_at_oci=SELECTED`.
  Dry run reports this required selection without changing availability. The
  wait is controlled by `--source-selection-timeout` (default 300 seconds).
- Ubuntu 20.04: x86_64 only. Ubuntu 22.04 and 24.04: x86_64 and aarch64.
  Reuse/create `UBUNTU_STANDALONE` profiles (`CANONICAL`, `OCI_LINUX`), and create
  corresponding groups without Oracle RPM repositories. This does not rewrite
  Ubuntu APT sources or configure Ubuntu Pro credentials.
- Windows Server 2016, 2019, 2022, 2025 and Windows 11 on x86-64: reuse a compatible
  Windows standalone profile or create one using `OCI_WINDOWS`. Windows groups do
  not have Linux software sources. Existing Windows Update/WSUS settings are not
  changed.
- Only running Compute instances are onboarded. Supported images need Oracle
  Cloud Agent with an operational OS Management Hub Agent plugin, applicable
  instance-principal permissions, and network access to required services.
- Already registered instances skip plugin/tag updates. Compatible script groups
  are reconciled without reattaching the same vendor repository under another OCID.
- A conflicting existing `OsmhProfile` tag is left untouched and reported as skipped.

### Additional Oracle repositories

The CLI defaults to `--repository-families uek,ksplice,mysql,oci`. It discovers
UEK release repositories, Ksplice (including ELS), MySQL community repositories,
and OCI-included repositories in the **matching OS/architecture regional catalog**.
Debug/source/test/preview variants are excluded. A family absent from the catalog
is reported; an explicitly requested missing repository is an error. Required
base/ELS repositories must always be present. Restricted sources require the
appropriate availability/entitlement; the script does not purchase entitlements.

When several UEK or MySQL repositories exist, interactive runs ask for repository
numbers/ranges or `none`. Match UEK to the instance kernel and MySQL to the intended
stream; do not select competing versions. Because groups are per OS/architecture,
the chosen repositories apply to that platform group, not individually per guest.
The script cannot infer installed MySQL or the running guest kernel from image metadata.

Unattended runs must resolve ambiguous choices using `--repository-map`:

```json
{
  "ORACLE_LINUX_7:X86_64": ["ol7_UEKR6", "ol7_UEKR6_ELS", "ol7_ksplice", "ol7_ksplice_ELS", "ol7_oci_included"],
  "ORACLE_LINUX_9:X86_64": ["ol9_UEKR7", "ol9_ksplice", "ol9_oci_included"]
}
```

These are examples, not guaranteed regional availability. Add exact MySQL repo IDs
from your catalog after choosing a compatible stream. Map entries override **extra**
selection for that platform; base/ELS sources remain required. An empty list opts
out of extras for that platform. `--repository-families none` selects minimal
sources globally. An explicit `--software-source-map` instead overrides the entire
source set for that platform and takes precedence over family/repository selection.

New Linux profiles include the resolved source set. Existing compatible profiles
are reused; additional repositories are reconciled on the destination group.
Existing foreign-group/lifecycle membership remains untouched. Selecting sources
does not install MySQL, change the running kernel, install the Ksplice client, or
apply package updates. Existing group members also receive group source changes.

References: [OS/architecture support](https://docs.oracle.com/en-us/iaas/osmh/doc/getstarted.htm),
[OL7 ELS repositories](https://docs.oracle.com/en-us/iaas/oracle-linux/oci/general-notices.htm),
[Ksplice requirements](https://docs.oracle.com/en-us/iaas/osmh/doc/linux-package-management.htm).

## Terminated-instance cleanup

Cleanup now has its **own selection**, separate from registration. In a terminal,
the script lists verified terminated non-OKE OSMH records with compartment paths
and prompts for `1,3-5`, `all`, or `none` (Enter defaults to `none`). In unattended
runs, no cleanup is approved unless `--unregister-all` or
`--unregister-instance-ids <comma-separated-compute-ocids>` is supplied. Registration
`--all` does NOT approve cleanup. Use `--skip-terminated-cleanup` to disable discovery.
To perform cleanup without registration or IAM configuration:

```bash
python onboard_osmh.py <tenancy-or-compartment-ocid> --profile pridhu --cleanup-only --dry-run
```

Remove `--dry-run` after reviewing; permissions must already exist. Add
`--all-regions` for region-by-region cleanup selection. Only selected IDs reach
the unregistration function, which rechecks termination/compartment/OKE tags
immediately before any deletion. Registration `none` still cancels the run;
use `--cleanup-only` if you do not want to select any running instances.
For each OCI Compute registration in a scanned compartment, the script reads
the corresponding Compute instance. It unregisters the OSMH record only if that
fresh response reports `TERMINATED` and the same compartment. It never terminates
Compute instances. Successful unregistration removes access to that record's OSMH
job history/reports; there is no undo operation for that history.

HTTP `202 Accepted` is only submission acknowledgement. The script prints the
work-request OCID and verifies that the instance disappears from a successful
OSMH compartment list query, waiting up to 300 seconds per terminated instance.
Change this with `--unregistration-timeout 600`. It resumes a pending unregister
work request instead of sending another deletion, and reports failed work-request
errors or a timeout. Pending cleanup does not prevent onboarding the other
instances, but the final exit status is 1 until all attempted cleanup is confirmed.
An Inactive/Offline status alone is not proof of removal. The instance can disappear
before its work-request status catches up; removal and job completion are distinct.

Stopped, stopping, terminating, moved, and non-OCI instances are not unregistered.
An inaccessible or expired Compute record returning `404 NotAuthorizedOrNotFound`
is retained and reported, because that error cannot prove termination. Such old
records need separate verification before manual removal. To disable cleanup,
add `--skip-terminated-cleanup`.

## Tests

```bash
.venv/bin/python -B -m unittest -v
```

Tests mock OCI calls; they do not change tenancy resources.

References: [OSMH prerequisites](https://docs.oracle.com/en-us/iaas/osmh/doc/getstarted.htm),
[Multi-compartment groups](https://docs.oracle.com/en-us/iaas/osmh/doc/understand-groups.htm),
[OKE node tags](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengtaggingclusterresources_tagging-oke-resources_node-tags.htm),
[Windows profiles](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/os-management-hub/profile/create-windows-stand-alone-profile.html),
[Unregistering instances](https://docs.oracle.com/en-us/iaas/osmh/doc/unregister-instance.htm).
