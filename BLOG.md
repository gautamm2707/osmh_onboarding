# Onboard in OSMH with OCI Resource Manager

## Introduction

**Onboard in OSMH** automates OCI instance onboarding to OS Management Hub. Select instances in OCI Resource Manager, deploy the automation, and let an OCI Function check their registration daily at your chosen **UTC time**.

## Use case

A platform team manages instances across development, testing, and production compartments in multiple regions. This solution provides a consistent onboarding process: it scans the selected compartment tree, discovers tagged instances, excludes identified OKE workers, and creates or reuses OSMH registration profiles and management groups. Future instances enter the workflow when the same opt-in tag is applied.

## Prerequisites

- **OCI access:** Permission to manage Resource Manager stacks and jobs, Functions, schedules, networking, logging, tags, dynamic groups, and tenancy IAM policies. Cloud builds also require DevOps, Container Registry, and Notifications permissions.
- **Deployment region:** A commercial OCI tenancy with an active region subscription, required services, and available quotas. Choose the subscribed region from the OCI Console region menu before opening the deployment link.
- **Function build:** An OCI Vault secret containing a GitHub access token that can read the source repository. The published stack pins the reviewed source commit automatically, then builds and delivers the Function image.
- **Build resource principals:** The stack creates exact-resource dynamic groups for its DevOps pipeline and GitHub connection. OCI requires tenancy-scoped `devops-family` and `secret-family` grants for external source retrieval; review these IAM statements before Apply. Apply validates the PAT and pinned source, then polls the OCI connection and proceeds as soon as IAM is ready.
- **Networking:** Suitable regional subnets with outbound HTTPS and DNS access, or permission to create the supplied private network and NAT gateway.
- **Eligible instances:** Running instances with a supported OS/image, a working Oracle Cloud Agent with the OSMH plugin available, and guest connectivity to OSMH.
- **Optional shared tags:** To reuse a namespace, have its name and OCID ready, with an active `managedby` key permitting `osmanagementhub`. Otherwise, the stack creates its own uniquely named namespace.

## Deploy and onboard

**[Onboard in OSMH](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https%3A%2F%2Fgithub.com%2Fgautamm2707%2Fosmh_onboarding%2Fraw%2Frefs%2Fheads%2Fmain%2Fresource-manager%2Fosmh-resource-manager.zip)**

The link opens **Create Stack** in your OCI Console session, or prompts you to sign in, with the published Resource Manager package selected. The package contains the new Console form at its root.

1. Choose a subscribed region in the OCI Console, then select the **Deployment and onboarding compartment** independently from the compartment that stores the stack. Select one or more Compute instances from that target compartment.
2. Create a Function application, or enter an existing application’s OCID. For a new application, create networking or select an existing **VCN** and **regional subnet**.
3. Choose the daily **UTC start time** and select the GitHub token secret. The published source commit is populated automatically and cannot be changed in the form.
4. Create the stack, review **Plan**, and run **Apply**. A detached job applies the stack's opt-in tag to the selected instances and starts onboarding. Use the **`instance_opt_in_tag`** output when tagging future instances.

Each new stack receives a unique ID for its Function, network, build resources and tag namespace, allowing separate deployments in the same compartment. Retry a failed Apply in the same stack to retain resources already created. Assign each instance to one onboarding automation.

The Compute control is a compartment-dependent dropdown list. OCI’s native picker lists one compartment at a time and offers no state, OKE, or OSMH-registration filters. Apply rechecks every selection: stopped or OKE instances are rejected, while instances already registered in OSMH are skipped. Scheduled reconciliation scans the target compartment and its descendants for tagged instances. Resource Manager also has no native Function application picker, so existing applications use an OCID field. See [Oracle’s schema documentation](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm).

An OCIR `AUTH_TOKEN` is not requested. OCI DevOps delivers the image with a resource principal, and OCI Functions pulls and invokes it through IAM. This avoids placing a long-lived registry credential in Resource Manager variables or state.

Check Function logs and OSMH instance status to confirm registration. Pending registrations are checked again on later runs. Patch installation and patch schedules require separate configuration.

See the [deployment guide](RESOURCE_MANAGER.md) for packaging, configuration, and updates.
