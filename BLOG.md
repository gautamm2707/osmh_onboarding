# Onboard in OSMH with OCI Resource Manager

## Introduction

**Onboard in OSMH** automates OCI instance onboarding to OS Management Hub. Select instances in OCI Resource Manager, deploy the automation, and let an OCI Function check their registration daily at your chosen **UTC time**.

## Use case

A platform team manages instances across development, testing, and production compartments in multiple regions. This solution provides a consistent onboarding process: it scans the selected compartment tree, discovers tagged instances, excludes identified OKE workers, and creates or reuses OSMH registration profiles and management groups. Future instances enter the workflow when the same opt-in tag is applied.

## Prerequisites

- **OCI access:** Permission to manage Resource Manager stacks and jobs, Functions, schedules, networking, logging, tags, dynamic groups, and tenancy IAM policies. Cloud builds also require DevOps, Container Registry, and Notifications permissions.
- **Deployment region:** A commercial OCI tenancy with an active region subscription, required services, and available quotas. Select the same region in the Console navigation and stack form.
- **Function image:** Either an existing versioned Linux AMD64 image in your tenancy’s selected-region OCIR, or a DevOps build using a published GitHub commit and a repository access token stored in OCI Vault. Supply the Vault secret OCID, not the token itself.
- **Networking:** Suitable regional subnets with outbound HTTPS and DNS access, or permission to create the supplied private network and NAT gateway.
- **Eligible instances:** Running instances with a supported OS/image, a working Oracle Cloud Agent with the OSMH plugin available, and guest connectivity to OSMH.
- **Existing tags:** If the `OSMH` namespace already exists, have its OCID ready and ensure its active `managedby` key permits `osmanagementhub`.

## Deploy and onboard

**[Onboard in OSMH](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https%3A%2F%2Fgithub.com%2Fgautamm2707%2Fosmh_onboarding%2Fraw%2Frefs%2Fheads%2Fmain%2Fresource-manager%2Fosmh-resource-manager.zip)**

The link opens **Create Stack** in your OCI Console session, or prompts you to sign in, with the published Resource Manager package selected. The package contains the new Console form at its root.

1. Choose **Region**, **Compartment**, and the **Compute instances** to onboard.
2. Create a Function application, or enter an existing application’s OCID. For a new application, create networking or select an existing **VCN** and **regional subnet**.
3. Choose the daily **UTC start time**, then configure the image and tag settings.
4. Create the stack, review **Plan**, and run **Apply**. A detached job applies `OSMH.managedby=osmanagementhub` to the selected instances and starts onboarding.

Compartment-dependent instance and VCN dropdowns are included. OCI Resource Manager has no documented native Function application picker, so existing applications use an OCID field. Regional dropdowns use the Console region. See [Oracle’s schema documentation](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm).

Check Function logs and OSMH instance status to confirm registration. Pending registrations are checked again on later runs. Patch installation and patch schedules require separate configuration.

See the [deployment guide](RESOURCE_MANAGER.md) for packaging, configuration, and updates.
