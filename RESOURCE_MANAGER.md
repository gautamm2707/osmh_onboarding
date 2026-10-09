# Deploy OSMH with OCI Resource Manager

The repository root is the Resource Manager Terraform entry point. The `deployment/` directory remains the separate Terraform entry point used by the local Python deployer. Do not use both deployments to manage the same scope. Creating an ORM stack for an existing local deployment is not a state migration.

[Onboard in OSMH](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https%3A%2F%2Fgithub.com%2Fgautamm2707%2Fosmh_onboarding%2Fraw%2Frefs%2Fheads%2Fmain%2Fresource-manager%2Fosmh-resource-manager.zip)

The button loads the published `resource-manager/osmh-resource-manager.zip` from this repository’s `main` branch. That ZIP contains `schema.yaml` and the Resource Manager Terraform at its root, with the separate `deployment/` configuration excluded. The link opens Create Stack; review the variables, Plan and Apply in your OCI session. See the [blog](BLOG.md) for the reader-facing workflow.

## Files

| File | Responsibility |
| --- | --- |
| `orm_versions.tf` | Providers, tenancy context, regional providers and configuration checks |
| `orm_variables.tf` | Stack inputs |
| `orm_network.tf` | Optional private network, NAT, HTTPS/DNS egress |
| `orm_build.tf` | DevOps build and delivery, private OCIR, build IAM |
| `orm_tags_iam.tf` | Opt-in tags and Compute OSMH access |
| `orm_runtime.tf` | Function, operational IAM, logging, schedule, initial invocation |
| `orm_outputs.tf` | Resource identifiers and onboarding instructions |
| `schema.yaml` | Resource Manager Console form |
| `build_spec.yaml` | Linux AMD64 build executed by OCI DevOps |
| `validate_build_connection.py` | Vault PAT/source preflight and bounded OCI DevOps connection readiness polling |
| `package_resource_manager.py` | Allowlisted ZIP packaging and deployment URL generation |
| `resource-manager/osmh-resource-manager.zip` | Published package used by the blog button |

Terraform credentials are supplied by Resource Manager. The root configuration intentionally does not set a local profile or an API-key authentication mode. Local validation needs no OCI credentials; local plan/apply would require separately configured credentials.

## Package and publish

From the repository root, run:

```bash
python3 package_resource_manager.py
```

The output is `.deployment/osmh-resource-manager.zip`. It contains the root Terraform, schema, build specification, worker source, and deployment documentation. The packager does not include local OCI credentials, `.env`, `profile-map.json`, state, plans, virtual environments, provider binaries, or local deployment settings.

To use a local package, upload the ZIP through **Resource Manager → Stacks → Create stack → My configuration → ZIP file**. Use the ZIP root as the working directory. Select Terraform **1.5.x** and the intended deployment region in the Console navigation.

To update the package used directly by the blog button, regenerate the tracked ZIP after changing the sources or schema, then commit and push both the sources and ZIP to `main`:

```bash
python3 package_resource_manager.py --output resource-manager/osmh-resource-manager.zip
```

The ZIP includes only the packager’s explicit allowlist. The full repository archive is not used by this button, so the CLI Terraform cannot be selected accidentally. This ZIP contains documentation but does not include itself.

For an immutable release, publish the ZIP as a public GitHub release asset and generate an encoded button link:

```bash
python3 package_resource_manager.py --source-url \
  https://github.com/gautamm2707/osmh_onboarding/releases/download/v1.0.0/osmh-resource-manager.zip
```

The release URL above is an example destination; creating a package does not create a release. The command prints Markdown and HTML links labelled **Onboard in OSMH**. Replace the blog/README links with the published versioned URL. An OCI Object Storage pre-authenticated request URL is also supported by Oracle's deploy button; its expiry controls how long the link works.

The button's ZIP must be reachable without authentication. A private GitHub repository can still be used as a DevOps build source through the Vault-backed GitHub connection, but its private ZIP cannot serve as the public button target. Supply an accessible release package or upload the ZIP manually.

## Cloud build prerequisites

1. Publish a commit containing the Function sources, `function/Dockerfile`, and root `build_spec.yaml`.
2. Create a GitHub PAT supported by OCI DevOps with repository read access, including any required organization authorization. Store it as a secret in OCI Vault in the deployment compartment and region. The form provides a Vault-secret dropdown; the raw token is never entered into Resource Manager.
3. The published form supplies a hidden, reviewed commit SHA. The repository and branch are also fixed by the form. The stack creates separate exact-resource dynamic groups for the build pipeline and GitHub connection. OCI requires the pipeline to manage `devops-family` in the tenancy and the connection to read `secret-family` in the tenancy while resolving external source files. Regional repository and notification access remains scoped to the deployment compartment.
4. Apply builds on the managed Oracle Linux 8 x86 runner with Podman. A Deliver Artifacts stage pushes the image without an OCIR user auth token. Terraform waits for `SUCCEEDED` before creating/updating the Function.

The Console form intentionally does not expose Function-image choices. It builds the image and delivers it to a private OCIR repository. Terraform callers can still use the hidden compatibility variables to supply an existing image, but that is not part of the guided Console workflow.

Do not enter an OCIR `AUTH_TOKEN`. Auth tokens are used by local Docker/Fn clients. In this stack, OCI DevOps delivers the image with its resource principal and OCI Functions pulls it through the stack's repository IAM policy. The Function invocation itself uses OCI IAM, not an OCIR credential.

## If Configure variables still shows the old form

Fields named `auth` (default `APIKey`), `bootstrap_iam`, `compartment_id`, and `home_region` identify the separate CLI configuration in `deployment/variables.tf`. In the Resource Manager configuration, hidden `compartment_ocid` is the compartment chosen on Stack information, while `target_compartment_ocid` is the independently selectable deployment and onboarding scope. The form starts with the **Compartment and Compute selection** group.

To load the updated form in the Create stack wizard:

1. For the published package, cancel the uncreated wizard and open **Onboard in OSMH** again. To upload a local copy instead, select **Previous** to return to **Stack information**.
2. Under configuration source, select **My configuration → .Zip file** and replace the old source with **`osmh-resource-manager-console-v1.10.zip`** from the project folder. This is a visible copy of the generated `.deployment/osmh-resource-manager.zip`.
3. If a working directory is requested, use the ZIP root (empty/default). The ZIP has `schema.yaml` and `orm_*.tf` directly at its root and excludes `deployment/`.
4. Select **Next**. The first group should be **Compartment and Compute selection**, with **Deployment and onboarding compartment** and a dropdown-list control for **Compute instances to onboard**.
5. If the wizard retains the previous source, cancel the uncreated stack and start a new Create stack wizard with this ZIP.

A browser refresh does not replace a configuration already loaded in the wizard. The blog button loads the dedicated published ZIP; future local edits require regenerating and pushing that ZIP. For a full repository archive obtained separately, choose the directory containing `schema.yaml` and `orm_variables.tf` as the working directory, rather than its `deployment/` child.

[Oracle ZIP upload instructions](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/create-stack-local.htm)

## Configure variables

The form presents these controls in order:

| Order | Control | Behavior |
| --- | --- | --- |
| 1 | Region | Required OCI region dropdown; Plan verifies that the selected region is a READY tenancy subscription |
| 2 | Deployment and onboarding compartment | Independently selectable from the compartment that stores the Resource Manager stack |
| 3 | Compute instances | Dynamic dropdown list populated from the selected compartment |
| 4 | Function application | Create a new application, or enter an existing application's OCID |
| 5 | Networking for a new application | Create VCN/subnet/NAT/routes/security rules, or choose a VCN and then its regional subnet |
| 6 | Daily schedule | Enable/disable and choose a UTC time in 15-minute intervals |
| 7 | Function build authentication | Vault dropdown for the GitHub token secret; the reviewed source SHA is populated and hidden |

Tagging and advanced settings follow these controls. The default time remains **16:30 UTC** (22:00 IST). Terraform callers can supply any valid `HH:MM` time through `schedule_time_utc`.

**Native Console limits:** Oracle automatically fills the specially named `compartment_ocid` from Stack information, so it is retained as hidden stack context and the form uses a differently named target variable. The region is an explicit OCI region dropdown and Plan verifies that it is a READY tenancy subscription; switch the OCI Console to that region before selecting regional resources. The Compute picker accepts only `compartmentId`. It has no recursive-subtree, lifecycle-state, OKE-membership, image/OS, or OSMH-registration filter. Consequently, one generic deploy-button package cannot combine descendant compartments or hide those rows in the native dropdown. Apply validates the selected Compute state and scope; the Function checks OKE and OS eligibility before writing any tags and skips instances already present in OSMH. The scheduled worker scans the full target subtree for tagged instances. Oracle's schema also has no Function application picker, so existing applications use an OCID field validated during Plan. See [Oracle's supported schema and instance-picker definition](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm).

After changing compartment, reselect instances, application and networking. Dropdowns show resources the caller can view; they do not grant access. Plan validates selected instance state/scope, existing application state/shape/scope, and subnet scope/VCN. Guest OS and OKE checks run inside the initial Function invocation.

## Scope and existing resources

- `compartment_ocid` is supplied automatically from Stack information and identifies where the Resource Manager stack is stored. `target_compartment_ocid` is the independent deployment compartment and root of recursive instance discovery. Each new stack generates a random 128-bit deployment ID once and saves it in Terraform state. Resource names include that ID and the deployment region, so independent stacks can create resources in the same compartment. IAM resources are created through a separate provider in the tenancy home region. The Resource Manager job identity must have permission in the target compartment. Assign each instance to one onboarding automation; separate names do not coordinate overlapping worker selections or OSMH groups.
- `workload_regions` is empty for the deployment region, CSV for selected regions, or `all` for all READY subscriptions. This package targets the commercial OCI realm (`oc1`).
- New networks use `10.254.0.0/24`, no ingress rules, HTTPS egress, and DNS to the OCI resolver. For established networking, clear **Create a new VCN and subnet**, then select the existing VCN and regional subnet in that compartment. Existing routes/security rules must provide outbound HTTPS and DNS. The hidden `subnet_ids` input is retained for earlier non-Console callers.
- Reused applications must be `ACTIVE`, `GENERIC_X86`, and in the selected region and compartment. Clear **Create a new Function application** and supply `application_id`. Network controls are hidden and ignored in this mode. If the application already has its invocation log, set `logging_enabled=false`.
- Namespace names are tenancy-wide. By default, a new stack creates `OSMH_<deployment_id>` and its `managedby` key, even if `OSMH` already exists. `tag_namespace` is the prefix for new namespaces (at most the first 67 characters are used). Use the actual tag shown in `instance_opt_in_tag` for future instances. To intentionally share an existing tag instead, set `use_existing_tag_namespace=true`, enter its exact name in `tag_namespace`, and provide its OCID. The stack reads the existing namespace and `managedby` key, checking their name/retirement/allowed value; it does not import them or change ownership. A missing existing key must be created by the administrator before using reuse mode.
- Set `create_instance_iam=false` only when equivalent Compute agent grants exist. The stack does not change any existing dynamic groups or IAM policies. Its created instance group matches the opt-in tag; policy grants constrain access to the selected scope.
- The Function uses the existing worker with IAM setup disabled. Operational OSMH access is still tenancy-wide. Review root policies before Apply. No human administrator group or automatic user membership is created by this path.

## Select instances and verify onboarding

Select instances in the form, then Plan/Apply. Terraform reads existing Compute instances without importing or taking ownership of them. Apply sends the exact selected IDs to a detached Function invocation; the deployed Function configuration stores a digest of that selection and rejects a payload with different IDs. The worker checks the whole selection for scope, running state, OS support, OKE membership and conflicting opt-in tags before tagging, then starts reconciliation. Concurrent changes or later API failures can still interrupt a batch; completed tags are retained and retrying the same selection is safe.

A nonempty selection always requests this initial invocation, even when `invoke_after_deploy=false`. Leave the selection empty when deploying infrastructure for instances already tagged. The image must include the updated handler and selection helper; older images cannot process the new payload.

The scheduled invocation uses `{}` and reconciles all opted-in instances in the configured compartment tree/workload regions. It does not reapply the stack selection each day. Add future instances by updating the selection and applying, or by applying the tag shown in `instance_opt_in_tag`. Removing an instance from the selection does not remove its tag or unregister it.

For interactive bulk tagging outside the Console:

```bash
python onboard_osmh.py <SCOPE_OCID> --profile <OCI_PROFILE> \
  --regions us-ashburn-1,us-phoenix-1 --tag-only
```

Use the stack's scope, regions and namespace; pass `--tag-namespace` for a custom name. Do not omit `--tag-only` against an ORM-managed scope.

Apply success confirms that the detached invocation was accepted, not that tagging or guest registration finished. Inspect the Function log and OSMH registrations/groups. If the job fails before tagging completes, correct the reported issue and retry the initial invocation with the selected IDs, or replace `oci_functions_invoke_function.initial[0]` in the next Apply. A normal `{}` invocation only reconciles instances already tagged. Pending registrations are checked again at the chosen daily UTC time. Removing a tag does not unregister the guest.

## Recovery and updates

### Name conflicts and upgrades from v1.7 or earlier

Earlier releases derived names from only the compartment and region. Two independent stacks in that scope therefore requested the same DevOps project, IAM and repository names. Selecting **Create a new Function application** and **Create a new VCN** did not change those names. OCI also retains a DevOps project for 72 hours after scheduling cascade deletion, so a pending deletion can still cause `The project name already exists`. See [Oracle's project deletion documentation](https://docs.oracle.com/en-us/iaas/Content/devops/using/delete_project.htm).

Version **1.8** gives each new stack its own deployment ID and tag namespace. The ID is persisted before dependent OCI resources are created. A failed Apply can therefore resume in the **same stack** without generating another set of names. A genuinely new stack gets a different ID automatically. This prevents the known cross-stack name collisions; IAM, quota, network and build errors still need to be corrected when reported.

Version **1.9** separates the exact build-pipeline and external-connection principals. It applies the tenancy-scoped `devops-family` and `secret-family` grants required by OCI's `RelatedResourceNotAuthorizedOrNotFound` build guidance. These grants apply only to the two generated dynamic groups, but their resource scope is tenancy-wide. Review this access before Apply. The existing v1.8 build dynamic group and policy are moved in state to the pipeline resources, so the upgrade does not replace them; one connection dynamic group and policy are added.

Version **1.10** removes the fixed one-hour build delay. Apply first checks that the selected Vault secret contains an active raw GitHub PAT and that the pinned commit contains `build_spec.yaml`. It then polls OCI's connection validation with exponential backoff for up to the configured timeout and starts the build immediately when validation succeeds. The PAT is held only in the preflight process memory and is never stored in Terraform state or printed in job logs. The separate pre-invocation Function IAM wait now defaults to 120 seconds.

For an existing or partially created stack, **Edit stack → replace the configuration ZIP with v1.10 → retain the same variables → Plan**. Generated `name`/`display_name` fields are assigned only at resource creation: narrowly scoped `ignore_changes` rules preserve names already in state, including the old `OSMH` namespace. Image URIs, IAM and tagging use the actual stored repository/namespace names. The v1.9 state moves preserve the existing build dynamic group and policy while splitting connection access. Review the plan before Apply; the upgrade should not replace existing OCI resources. No namespace migration, import or manual state edit is required for resources already recorded in that stack.

Keep the original stack and state for retries. Changing to a new stack does not transfer ownership of an old stack's resources or clean up orphans. The package does not silently adopt or delete objects belonging to another state. Generated names remain fixed on later updates; changing the namespace prefix does not rename an existing managed namespace. Intentional replacements or state loss require a separate recovery plan, particularly for retained DevOps projects and protected tag namespaces.

The build readiness timeout is a maximum, not a fixed delay. A malformed or inactive PAT fails before a build run is created. After the PAT and pinned source pass, OCI connection validation is retried while dynamic-group membership propagates. If the timeout expires, correct the reported connection or IAM issue and retry the same stack. Increment `build_revision` to request a new run if needed. If the image was already delivered before a later failure, keep the existing successful build state while retrying Function deployment. Increment the revision before rebuilding, because the OCIR repository prevents overwriting existing tags.

Published forms pin `source_commit`; Terraform callers can override it. Changing `source_commit` or `build_revision` replaces the build run and selects a new image tag. Reapplying identical inputs does not request a new build. Function updates request a new initial invocation when enabled. If the initial invoke is rejected, infrastructure can still exist; retry Apply after correcting the reported issue. Disabling `invoke_after_deploy` suppresses the initial job only when the Compute selection is empty. Pending registration is not a failed Terraform deployment.

The first Apply starts connection validation immediately and can retry for up to one hour because OCI documents that dynamic-group membership can require about one hour to take effect. A ready connection proceeds without waiting for the full timeout. If validation times out, open **Developer Services → DevOps → Projects → External connections** and inspect the connection; if a later build fails, inspect **Build runs** and its lifecycle details.

Do not switch an established stack between creating and reusing its application/network/tag namespace, or between cloud builds and an existing image, without reviewing the plan. Such changes can destroy resources currently owned by the stack. Preserve the stack state and use the same stack for updates.

## Teardown

Disable the schedule before retiring the automation. OSMH profiles, software sources, groups and registrations created by the worker are outside this Terraform state and remain after stack destruction. Existing instances, reused networking/applications, Vault secrets, and reused tag namespaces also remain.

New tag namespaces/keys have `prevent_destroy=true`: a normal Destroy plan deliberately stops so active opt-in tags are not removed accidentally. For intentional teardown, first decide how tagged instances will be managed, remove/transfer the relevant tags, and remove these lifecycle guards from a reviewed configuration. Destroy may also require deleting the stack's retained images from its private OCIR repository. Removing the stack's instance policy can interrupt agent access unless equivalent access remains.

## Validation

```bash
terraform init -backend=false
terraform fmt -check
terraform validate
terraform test
python3 -B -m unittest test_resource_manager_package test_osmh_selection test_osmh_tags -v
```

`terraform test` uses mocked OCI providers and needs Terraform 1.14 or later locally. Naming tests use the real random provider and separate test states to verify two stacks in the same compartment and a retry of the first. Tests are not part of the uploaded ZIP; the deployment configuration remains compatible with Resource Manager Terraform 1.5.x. The tests make no OCI changes. A real non-production stack Apply is still required to verify service availability, IAM propagation, source access, the container build, image delivery, invocation, and guest registration in your tenancy.

References: [Deploy button](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/deploybutton.htm), [configuration requirements](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager.htm), [Console schema](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm), [DevOps IAM](https://docs.oracle.com/en-us/iaas/Content/devops/using/devops_iampolicies.htm).
