# Tag-based OCI OS Management Hub onboarding

## Deploy from the OCI Console

The button loads the dedicated Resource Manager ZIP from this repository. Its root contains the new Console form and Terraform configuration. If an already-open wizard shows `auth` or `bootstrap_iam`, start again from the button or follow the [source correction steps](RESOURCE_MANAGER.md#if-configure-variables-still-shows-the-old-form).

[Onboard in OSMH](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https%3A%2F%2Fgithub.com%2Fgautamm2707%2Fosmh_onboarding%2Fraw%2Frefs%2Fheads%2Fmain%2Fresource-manager%2Fosmh-resource-manager.zip)

The repository root contains the Resource Manager Terraform and Console schema.
The published ZIP is `resource-manager/osmh-resource-manager.zip`. The link opens
Create Stack in your OCI session; review the configuration and run Plan/Apply.
The stack can build the Function image through OCI DevOps or use an existing OCIR
image, then deploy the Function, IAM, optional networking, logs and daily schedule.
The form uses the current subscribed Console region, then presents an onboarding compartment independent from the stack-storage compartment, Compute dropdown selection, application/network choices, a daily UTC time, and build-source authentication. Apply starts a detached job that validates and opts in selected instances through the defined tag. Existing Function applications use an OCID field because OCI has no native application dropdown.

Read the [updated blog](BLOG.md) and [Resource Manager deployment guide](RESOURCE_MANAGER.md).
For a direct-upload ZIP, run `python3 package_resource_manager.py`. Keep Resource
Manager and local deployments in separate, non-overlapping scopes. The instructions
below describe the existing local CLI deployment path.


`onboard_osmh.py <compartment-or-tenancy-OCID>` now scans the supplied root and
all active descendants, excludes OKE workers, prompts for instance selection,
creates a defined-tag namespace in the supplied compartment, and tags the selected
instances. It then **automatically calls `reconcile_osmh.py --deploy`**, which
builds/pushes the image and provisions the Function, bootstrap IAM, logging and
daily schedule. Existing resources in the deployment state are reused. It can
also create a dedicated private Function network and a private OCIR repository.

Inside the scheduled Function, `reconcile_osmh.py` discovers tagged instances and reuses
the existing IAM, repository, registration-profile, Cloud Agent and OS/architecture
group logic. The OCI Function packages it and runs daily through Resource Scheduler
at **22:00 IST / 16:30 UTC**. The laptop need not remain running after deployment.
The scheduled invocation only reconciles instances; it does not redeploy itself.
Deployment is local; before prompting, the selection phase checks current OSMH
inventory so already registered instances are marked correctly. After a successful
deployment, the deployer immediately starts one detached Function invocation for
initial onboarding, then the daily schedule handles newly tagged instances.

## One-command setup with region selection

Start Docker Desktop/Colima, install Terraform, and configure an authorized OCI
profile. Have an OCI auth token for the user in that profile ready:

```bash
cd /Users/gautammishra/Documents/OSMH
source .venv/bin/activate
python -m pip install -r requirements.txt
```

No separate `docker login`, registry username, namespace or image argument is
needed. The script resolves the selected profile's user OCID to its IAM username,
reads the tenancy's registry namespace, and uses the home-region OCIR. For example,
it resolves `id3kvohtwgjy/gautam.mishra@oracle.com` and `iad.ocir.io` when those are
the profile's actual user/namespace and Ashburn is the home region. Domain-qualified
usernames are handled as returned by IAM. The image name includes the scope OCID
suffix and a source-code hash to version code changes automatically.

Preview the complete workflow for two workload regions:

```bash
python onboard_osmh.py <COMPARTMENT_OR_TENANCY_OCID> \
  --profile pridhu \
  --regions us-ashburn-1,us-phoenix-1 \
  --dry-run
```

Repeat **the same command without `--dry-run`**. Choose the network option:

1. **Existing VCN/subnet:** select a numbered VCN, then an available subnet in it.
2. **New network:** create a dedicated private VCN, subnet and NAT gateway.

Select the desired instances in each workload region. After tagging completes,
deployment runs automatically; no separate
`reconcile_osmh.py` or Terraform command is required. Terraform's saved plan is
applied automatically; a plan that deletes/replaces existing resources is refused
for separate review. Before building/pushing the image, enter the OCI auth token
at the hidden prompt. This is an **OCI auth token**, not your console password or
API signing key. Login uses `--password-stdin`; temporary registry credentials are
removed after the build/push, and your normal Docker login configuration is not
modified. The selected Docker Desktop/Colima daemon context is preserved.

Region choices:

| Flag | Instances processed |
| --- | --- |
| `--regions us-ashburn-1` | One workload region |
| `--regions us-ashburn-1,us-phoenix-1` | Multiple workload regions |
| `--all-regions` | All READY subscribed regions, including future subscriptions on scheduled runs |
| no region flag | Region from the selected OCI profile |

The single Function, registry image, schedule and optional network always reside
in the **tenancy home region**, regardless of the workload regions. OCI requires
the Function image and Function to be in the same region.
`--deployment-region` is now an optional assertion of the home region; a different
value is rejected before tagging. Workload regions may be different.

The deployment picker first lets you reuse an existing Function application or
create a new one. Reusing an application avoids the Functions Application service
limit and does not ask for VCN/subnet input. The reuse picker searches the supplied
compartment/root and its descendants in the home region, then shows each Function
application with its compartment path. Use `--function-application-id <fnapp-OCID>`
for non-interactive reuse. To browse Function applications or shared networking
elsewhere, pass `--network-compartment-id <NETWORK_COMPARTMENT_OR_TENANCY_OCID>`
to search that tree. If creating a new app, the network picker uses the same scope.
You may bypass the network picker with `--create-function-network` or
`--function-subnet-ids <subnet-OCID>[,<another-subnet-OCID>]`. Existing subnets must
support Functions and have the required outbound connectivity; they are not
reconfigured. `--function-image` is an optional override, restricted to this
tenancy's namespace and home-region registry. `--skip-function-build` requires an
already-pushed image and skips Docker login. Otherwise, the hidden token prompt
requires an interactive terminal even when networking is provided through flags.
Once a Terraform state has created and owns a Function application/network, later
reruns preserve that ownership even if `--function-application-id` is supplied;
this avoids planning destructive deletes of the managed app/network.
Invocation log creation is off by default because reused Function applications
may already have an invoke log; pass `--enable-function-logging` only when you
want the script to create one. The first real Function run starts automatically
after deployment and transient Functions invoke errors are retried; pass
`--skip-initial-function-invoke` only when you want to deploy/schedule without
kicking off initial onboarding. Use `--tag-only` to suppress deployment entirely.

If any regional scan fails, deployment is skipped and applied tags remain. Fix
the reported issue and rerun. Unsubscribed/non-READY workload regions are skipped
without blocking valid regions. If deployment fails after tagging, its resources
and tags are retained; rerun with the same scope, deployment region and state.

## Tag and scope

The namespace defaults to the reusable tenancy-wide name `OSMH`. The key is
`managedby`, with allowed value `osmanagementhub`. The Compute dynamic group uses
a constant-size rule, for example:

```text
ALL {resource.type = 'instance', tag.OSMH.managedby.value = 'osmanagementhub'}
```

No compartment IDs are enumerated. Scope is enforced by recursive discovery and
the parent-scoped instance policies. New child compartments need no membership
rule update. Use the same `OSMH` tag namespace for every run and compartment
unless an administrator intentionally standardizes on a different namespace with
`--tag-namespace`.

Freeform tags, other namespaces and different values do not qualify. Existing
defined tags are preserved, with ETag protection against concurrent edits. A
conflicting `managedby` value stops tagging instead of being overwritten.

## Prerequisites

- Python 3.10+ locally (Function container uses Python 3.12), and a working OCI
  API-key or security-token profile in `~/.oci/config` for the target tenancy.
- The selection caller must read tenancy/subscriptions, compartments, Compute
  instances/images and OKE node pools; create/use tag namespaces in the parent;
  and update selected instances across its subtree. Tenancy-admin credentials
  cover setup; a restricted user needs these permissions granted beforehand.
- Deployment needs Terraform 1.5+, Docker with its daemon running, and an OCI
  auth token for the selected profile's user. Create the token in that user's OCI
  **Auth tokens** settings. Registry login and image naming are automatic. Never
  include credentials in the project or Function image.
- Either let the script create a private VCN/subnet/NAT gateway, or supply a subnet
  in the deployment region that supports Functions and provides HTTPS
  connectivity to OCIR and all used OCI APIs, including IAM in the home region
  and selected workload regions. Multi-region calls may need NAT/proxy routing
  beyond a regional service gateway. Existing subnets are not reconfigured. Newly
  created networking has no public instance IPs or ingress rules and permits
  outbound HTTPS and DNS. NAT, Function, registry and logging usage may incur costs.
- The deployer must be authorized to create Functions applications/functions,
  use/create networking and the registry, read VCN/subnet inventories, the profile's
  IAM user (and identity provider for legacy federated users), the registry namespace
  and repository inventory, create logs and schedules, and create root IAM
  policies/dynamic groups. Required services must be available and within quota.
- Guest instances need a supported OS/image, working Oracle Cloud Agent with
  the OSMH plugin available, and OSMH network connectivity. Tags do not install a
  missing agent or fix guest networking.

Existing supported Oracle Linux, Ubuntu and Windows combinations and repository
selection are retained. OKE exclusions use node pools and known OKE tags/metadata;
unmarked self-managed Kubernetes workers still need manual review. Only RUNNING,
supported, non-OKE instances are currently offered for selection.

## 1. Preview, select and tag

Run from this project folder:

```bash
cd /Users/gautammishra/Documents/OSMH
source .venv/bin/activate
python -m pip install -r requirements.txt
python onboard_osmh.py <compartment-or-tenancy-OCID> --dry-run
```

Choose `all`, indexes such as `1,3-5`, or `none` to cancel. Repeat without
`--dry-run` to apply the selected tags. Group arguments are no longer needed:

```bash
python onboard_osmh.py <compartment-or-tenancy-OCID>
```

Use `--profile pridhu` for another OCI profile. Region options remain
`--regions us-ashburn-1,us-phoenix-1` or `--all-regions`. Multi-region runs skip
unsubscribed/non-READY regions and select separately in each available region.
`--all` or `--instance-ids <CSV-OCIDs>` supports unattended tagging. Automatic
deployment follows tagging unless `--tag-only` is supplied. Use network flags to
bypass network selection, but image builds still ask for the auth token. Dry runs do not require
Docker, registry login, or deployment prompts and make no deployment changes.

Opt in future instances by applying the same defined tag at creation or rerunning
selection. The worker does not tag all newly created instances automatically.
Removing the tag prevents further reconciliation but does not unregister an
already managed instance.

## 2. Preview the worker locally

```bash
python reconcile_osmh.py <compartment-or-tenancy-OCID> --profile pridhu --dry-run
```

This reads tags already present in OCI. A tagging dry run does not persist them.
The worker makes no IAM changes if there are no eligible tagged instances. Remove
`--dry-run` to exercise the actual worker locally before deployment.

Its first run creates/reuses the generic `osmh-admins` IAM group,
`osmh-tagged-instances` dynamic group and `osmh-automation-policy` IAM policy.
The policy contains all OSMH statements managed by the worker. In a Function the
human admin group receives no automatic membership; the Function authenticates
through its own resource principal. A local API-user worker retains the earlier
automatic user-membership behavior.

Registered instances skip plugin reconfiguration. Profiles and OSMH groups stay
in the supplied parent/root within each region. Existing memberships in other
groups are preserved. Guest registration still in progress is reported as PENDING
and checked next time; a successful invocation does not mean all guests registered.

## 3. Separate deployment command (optional)

Normal `onboard_osmh.py` runs now perform this step automatically. This command
remains useful for deploying/updating infrastructure without repeating selection.

Home region, image version and Docker username are resolved automatically. Replace
the scope and subnet placeholders:

```bash
python deploy_osmh_function.py <compartment-or-tenancy-OCID> \
  --profile pridhu \
  --subnet-ids <subnet-OCID> \
  --workload-regions us-ashburn-1,us-phoenix-1
```

This validates tenancy/scope and the subnet, initializes Terraform, and prints a
plan. It changes no cloud resources, though it downloads providers and creates
local Terraform directories. Repeat with `--apply` to build/push Linux AMD64 and
apply Terraform after its confirmation. The build prompts for the profile user's
OCI auth token. `--skip-build` uses an already-pushed image without Docker login.
Optional `--image` overrides must use the home-region registry and correct namespace;
use a new version tag for each code release when overriding automatic naming.
Use `--create-network` instead of `--subnet-ids` for automatic networking. The
deployer creates a private OCIR repository if absent before the image push;
existing repositories are reused without changing their settings. Repository
creation is idempotent SDK setup, outside Terraform state; retain it for later
image versions. The namespace is verified against the authenticated tenancy.
Omit `--workload-regions` for the deployment region; set it to `all` for all READY
subscriptions. Avoid overlapping deployments managing the same instances.

Deployment creates or reuses an application, then creates the Function and
schedule. Optional invocation logging can be enabled with `--enable-function-logging`.
The Function and schedule permissions use exact resource-principal policy
conditions, so the deployment does not consume extra DynamicResourceGroups. The
worker creates/reuses the single tag-based Compute group on its first eligible
run. IAM groups, dynamic groups and policies are tenancy-root resources rather
than compartment-local resources. It also grants the Functions service read
access to the specific image repository.

The bootstrap policy grants the Function group/dynamic-group creation and policy
creation/update, so it can perform the requested first-run IAM setup. **Permission
to write policies is privileged tenancy access**, even for a single Function.
After setup succeeds, redeploy with `--disable-iam-bootstrap --skip-build` to remove
these privileges and configure the worker with `--skip-iam`. Operational access
remains, including the existing tenancy-wide OSMH grant for root vendor sources.

Terraform state is isolated by tenancy, scope and deployment region in
`.deployment/`. Preserve/protect it for updates; losing it can cause duplicate
resources or name collisions. Do not use separate Terraform state for the same
deployment. `--disable-schedule` leaves the schedule inactive. Direct Terraform
configuration is also available in `deployment/`.

The combined command uses profiles from `~/.oci/config` and API-key/security-token
authentication for local deployment. Custom SDK config paths, principal auth and
local profile/source/IAM overrides can still be used with `--tag-only` or the
local worker, but are rejected before tagging in automatic-deployment mode rather
than silently ignored. Repository-family and OSMH group-prefix options are passed
to the Function. `--skip-iam` maps to disabled IAM bootstrap and requires prior setup.

## 4. Verify in OCI

After deployment, the first detached Function invocation is started automatically.
Detached mode avoids client-side timeout while OSMH reconciliation continues in
OCI Functions. Check the Function invocation logs and OSMH instance/group status
for completion. If OCI Functions reports a temporary invoke-capacity error, the
deployer retries and leaves the deployment/schedule intact even if the initial
start is not accepted before timeout; rerun the same command or invoke manually.
Newly created instance-group membership can take about an hour to propagate, so
initial guest registrations may remain pending and be completed by the next
reconciliation. Terraform outputs the Function and schedule OCIDs. To start
another manual reconciliation, invoke in **Detached** mode:

```bash
oci fn function invoke --profile pridhu --region <TENANCY_HOME_REGION> \
  --function-id <function-OCID> --fn-invoke-type detached \
  --body '{"dry_run":true}' --file -
```

Use `--body '{}'` for a real reconciliation. Inspect the Functions application's
invocation status/log, the schedule's last/next run, and OSMH instance status/group
membership. The next scheduled run should be 16:30 UTC (22:00 IST). The schedule
invokes in Detached mode automatically. Payloads cannot override scope, regions
or credentials.

The worker has a 55-minute subprocess limit inside a 60-minute Detached Function.
A timeout or API failure is reported as a failed invocation. Completed idempotent
work is retained for the next run. Partition very large deployments into
non-overlapping scopes/regions if scans cannot finish within that budget. Avoid
concurrent manual/scheduled runs on the same scope; creation conflicts are surfaced
and can be retried after the other run.

## Cleanup and migration

An older deployment outside the home region is not migrated or deleted by this
update. Preserve its state and deliberately disable its old schedule before
creating an overlapping home-region deployment. This update never silently
deletes the old network, Function, schedule or repository.

The scheduled worker never unregisters instances. Retain explicit user selection
for confirmed terminated records:

```bash
python onboard_osmh.py <compartment-or-tenancy-OCID> --cleanup-only --dry-run
python onboard_osmh.py <compartment-or-tenancy-OCID> --cleanup-only
```

A single reusable tag-based Compute group is created instead of overwriting an
old compartment-based group's rules. Old groups, policy grants, registrations and
custom statements are preserved. Legacy grants remain effective until an
administrator retires them; this change does not revoke old compartment-wide
membership. Account for deployment bootstrap policies in tenancies near quota.

## Offline checks

```bash
python -B -m unittest discover -q
terraform -chdir=deployment init -backend=false
terraform -chdir=deployment validate
terraform -chdir=deployment fmt -check
```

Tests use mocks and make no OCI changes. A container build, real Function
invocation, and guest registration in a test compartment are still needed for
end-to-end validation.

References: [tag-based dynamic groups](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/managingdynamicgroups.htm),
[scheduled Functions](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionsschedulingfunctions-about.htm),
[schedule creation](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionsscheduling.htm),
[IAM permissions](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/iampolicyreference.htm).
