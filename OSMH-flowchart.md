> This diagram documents the earlier direct-onboarding workflow. The default
> command now selects and tags instances; a scheduled OCI Function runs the
> onboarding phase. See [README.md](README.md) for the current workflow.

```mermaid
flowchart TD
    A(["Start: provide a compartment or tenancy ID"])
    A --> B["Connect to OCI using your credentials<br/>Check account and selected regions"]
    B --> C["Find servers in the selected compartment<br/>and all its sub-compartments"]
    C --> D["Show server names, compartments,<br/>operating systems and processor types"]
    D --> E["Exclude Kubernetes worker nodes<br/>Skip stopped or unsupported servers"]
    E --> F["You choose which eligible servers to manage"]
    F --> G["Separately, choose any confirmed terminated<br/>servers to remove from OSMH records"]
    G --> H{"Dry run?"}

    H -->|Yes| I["Preview planned actions<br/>Make no changes"]
    I --> Z(["Finish"])

    H -->|No| J["Prepare access permissions<br/>Create or update IAM groups and policies<br/>unless using --skip-iam"]
    J --> K["Remove selected terminated-server records<br/>and check cleanup progress"]
    K --> L["Prepare software repositories<br/>and registration settings as needed"]
    L --> M{"Already registered<br/>in OSMH?"}

    M -->|Yes| N["Keep existing registration<br/>Do not reconfigure its agent"]
    M -->|No| O["Assign registration settings<br/>Request OSMH agent enablement"]
    O --> P["Wait for the server to register"]
    P --> Q{"Registration confirmed?"}
    Q -->|Yes| R["Organize registered servers into OSMH groups<br/>by operating system and processor type"]
    N --> R
    Q -->|No| S["Report the server as incomplete<br/>Agent or access troubleshooting is needed"]
    R --> T["Keep OSMH groups in the supplied parent compartment<br/>within each region"]
    T --> U["Continue with remaining selected regions<br/>Report results and skipped regions"]
    S --> U
    U --> Z
```
