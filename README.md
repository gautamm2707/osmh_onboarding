# OSMH Utility

Install once, then run `osmh-onboard` from any directory on macOS or Windows.
This repository contains the installable OSMH Utility and its required runtime,
Function and Terraform files. The previous standalone-script and Resource Manager
layouts remain in Git history; their setup instructions do not apply to this
layout. This repository change does not delete existing OCI resources.

```text
osmh-onboard <compartment-or-tenancy-OCID> --regions us-ashburn-1 --profile DEFAULT
```

The existing workflow scans the selected compartment and its descendants in the
requested regions, excludes detected OKE instances, and offers instance selection.
It applies the defined opt-in tag, prepares/deploys a home-region OCI Function,
requests an initial detached reconciliation, and configures daily reconciliation
at 22:00 IST (16:30 UTC). The Function performs IAM/OSMH reconciliation. A detached
invocation being accepted does not mean that instance registration has completed;
check Function logs and OSMH status. `--enable-function-logging` enables the optional
service log; review its implications when sharing a Function application.

The default opt-in tag is `OSMH.managedby = osmanagementhub`. New instances need
this tag to be included by the worker, which only scans its configured scope and
regions. It does not tag every new instance automatically. OSMH profiles/groups
are centralized in the selected parent scope in each workload region.

The runtime includes discovery paths for Oracle Linux 7–10, Ubuntu
20.04/22.04/24.04 and Windows Server. Eligibility still depends on the specific
image, architecture, plugin availability, region and OSMH support. Applicable
Oracle software-source discovery includes UEK, Ksplice, MySQL and OCI families;
use `--repository-families none` to opt out of these extra families.

## Prerequisites

- Python 3.10 or newer; Python 3.12 is a practical choice and matches the Function
  container. `pip` must be available. The installation installs the OCI Python SDK.
- Git for cloning, or download/extract the source ZIP from GitHub.
- Terraform 1.5 or newer on PATH; the bundled provider constraint is OCI >=7,<9
  and a provider lock file is included.
- Docker CLI and a running Docker engine using Linux containers. Docker Desktop
  works on Mac and Windows. On Mac, Colima is another option; start it before use.
  Builds target `linux/amd64`, including when the client is an Apple Silicon Mac.
- OCI API signing key and profile in `~/.oci/config` (Mac) or
  `%USERPROFILE%\.oci\config` (Windows). The profile must include `user`, `tenancy`,
  `fingerprint`, `key_file`, and `region`. The public API key must be uploaded to
  that OCI user. A session-token profile is also supported while its token is valid.
- An OCI **auth token** for OCIR login, supplied at the hidden interactive prompt.
  This is different from the API signing key and your Console password.
- Permissions to discover compartments/regions/instances and OKE resources; manage
  the relevant tags, IAM bootstrap resources, OSMH resources, OCIR repositories,
  Functions, schedules and logging; and create network resources if that option is
  selected. An appropriately authorized tenancy administrator can bootstrap this.
  The utility cannot grant its caller permissions that the caller lacks.
- Subscribed workload regions, service availability/quotas, supported instance
  images/agents, and outbound connectivity from both the local machine and Function
  subnet. An existing subnet must already provide the required connectivity.

Automatic Terraform deployment uses the standard OCI config location above.
For another tenancy, add a named profile and select it with `--profile`; the input
compartment/root OCID must belong to that profile's tenancy. Resource availability,
IAM and networking still need validation in each tenancy.

## Install and start a Docker engine

The utility needs both the **Docker command-line client** and a **running engine**
to build/push its Function image. Installing the Python utility or just the Docker
CLI does not start an engine. On Mac, choose Colima or Docker Desktop; on Windows,
use Docker Desktop with Linux containers. Docker Compose is not required.

### Mac option A: Colima and Docker CLI

1. If `brew --version` fails, install Homebrew using its
   [official installer](https://brew.sh/):

   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

   Follow the installer's **Next steps**, including adding Homebrew to PATH, and
   reopen Terminal. Then check `brew --version`.

2. Install Colima, the Docker client and Buildx:

   ```bash
   brew install colima docker docker-buildx
   colima version
   docker --version
   docker buildx version
   ```

   If Buildx is not found, register the installed plugin:

   ```bash
   mkdir -p "$HOME/.docker/cli-plugins"
   ln -s "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" "$HOME/.docker/cli-plugins/docker-buildx"
   docker buildx version
   ```

   If that link already exists, inspect it instead of overwriting it. An alternative
   is to merge Homebrew's plugin directory into `cliPluginsExtraDirs` in your
   existing Docker configuration, following the
   [Homebrew Buildx instructions](https://formulae.brew.sh/formula/docker-buildx).
   Preserve other Docker settings.

3. Start the default Colima profile with the Docker runtime:

   ```bash
   colima start --runtime docker
   ```

   For a **new** profile on Apple Silicon with macOS 13+, you can instead enable
   VZ/Rosetta for the utility's `linux/amd64` builds:

   ```bash
   colima start --runtime docker --vm-type vz --vz-rosetta
   ```

   Install Rosetta if macOS requests it. An existing profile's VM type/architecture
   cannot be changed in place; retain its configuration or create a separate
   named profile rather than deleting its data. See
   [Colima VM configuration](https://colima.run/docs/configuration/).

4. Select the engine and verify it:

   ```bash
   colima status
   docker context ls
   docker context use colima
   docker info
   docker run --rm --platform linux/amd64 hello-world
   ```

   `docker info` must include **Server** information without a connection error.
   The test container also checks image download and amd64 execution; it needs
   internet access. For a named Colima profile, select its context from the list.

After a reboot, run `colima start` before using the utility. Optional automatic
startup for a Homebrew installation is `brew services start colima`. To stop a
manually started engine after use, run `colima stop`; this stops its containers.

Sources: [Colima installation](https://colima.run/docs/installation/) and
[Colima startup/contexts FAQ](https://colima.run/docs/faq/).

### Mac option B: Docker Desktop

1. Download the Apple Silicon or Intel installer appropriate for your Mac from
   [Docker's Mac installation page](https://docs.docker.com/desktop/setup/install/mac-install/).
2. Open the downloaded DMG, move Docker to Applications, then launch Docker.
   Complete first-run setup and wait until the engine is running.
3. Open a new Terminal and check:

   ```bash
   docker --version
   docker context ls
   docker info
   docker buildx version
   docker run --rm --platform linux/amd64 hello-world
   ```

If a stopped Colima context is still selected, switch to the Docker Desktop
context shown by `docker context ls` (commonly `desktop-linux`). Docker Desktop
includes the Docker client and Buildx, so separate Homebrew installations are
not needed for this option. Check Docker's published system requirements and
subscription terms for your organization.

### Windows: Docker Desktop with WSL 2

1. If WSL is not installed, open **PowerShell as Administrator** and run:

   ```powershell
   wsl --install
   ```

   Restart Windows when requested. Then run `wsl --update` and `wsl --version`.
   Follow [Microsoft's WSL setup](https://learn.microsoft.com/en-us/windows/wsl/install)
   if virtualization or Windows features need enabling.
2. Download and run the installer from
   [Docker's Windows installation page](https://docs.docker.com/desktop/setup/install/windows-install/).
   Confirm your Windows/hardware meets its requirements and select the WSL 2
   backend. Launch Docker Desktop and finish setup; installation alone does not
   guarantee the engine has started.
3. Ensure Docker Desktop uses **Linux containers**, then open a new PowerShell:

   ```powershell
   docker --version
   docker info
   docker buildx version
   docker run --rm --platform linux/amd64 hello-world
   ```

Continue with utility installation only after the Server check and test container
succeed. Docker Desktop includes Buildx; Colima is not required for this Windows
setup. Use Docker's installation page for current requirements and subscription
terms.

### Fix a missing socket or stopped engine

For `Cannot connect to the Docker daemon` or
`~/.colima/default/docker.sock: no such file or directory` on a Colima setup:

```bash
colima start
colima status
docker context ls
docker context use colima
docker info
```

If it still points to a different socket, check whether `DOCKER_HOST` or
`DOCKER_CONTEXT` is overriding the selected context. If those are stale settings
you no longer need, remove them from the current Mac shell with
`unset DOCKER_HOST DOCKER_CONTEXT`, then select the intended context again. For
Docker Desktop, start the app and select its context. Do not create an empty
socket file or delete Colima profiles to fix a stopped engine.

## Install on macOS

If Docker is not installed or its engine is not running, complete
[Install and start a Docker engine](#install-and-start-a-docker-engine) first.

With Python installed, open Terminal:

```bash
git clone https://github.com/gautamm2707/osmh_onboarding.git
cd osmh_onboarding
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
osmh-onboard --version
osmh-onboard --help
docker info
terraform version
```

Use the Python 3.12 executable instead of `python3` if you have several versions
and want that version. In each new terminal, activate the environment once:

```bash
source /path/to/osmh_onboarding/.venv/bin/activate
```

After activation, `osmh-onboard` works from any folder. Without activation, invoke
`/path/to/osmh_onboarding/.venv/bin/osmh-onboard` directly. Replace `/path/to`
with the actual parent directory of your clone.

## Install on Windows (PowerShell)

Complete the [Windows Docker setup](#windows-docker-desktop-with-wsl-2) first if
Docker is not already available.

Open PowerShell in your preferred parent directory and clone the repository.
Do not copy a Mac `.venv`, Terraform provider cache or installed binaries.
Use Docker Desktop with its Linux/WSL2 engine running and Terraform on PATH.
Install Python 3.12 first, or replace `-3.12` with an installed version (3.10+):

```powershell
git clone https://github.com/gautamm2707/osmh_onboarding.git
cd osmh_onboarding
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\osmh-onboard.exe --version
.\.venv\Scripts\osmh-onboard.exe --help
docker info
terraform version
```

You may activate with `.\.venv\Scripts\Activate.ps1` and then use `osmh-onboard`
from any folder. If PowerShell blocks activation, use the full executable path;
no execution-policy change is necessary:

```powershell
& "C:\path\to\osmh_onboarding\.venv\Scripts\osmh-onboard.exe" --help
```

Create `%USERPROFILE%\.oci\config` and copy/configure your authorized API signing
key securely. Update `key_file` to its Windows location. A portable example is
`key_file=~/.oci/oci_api_key.pem`. Do not copy Mac absolute paths into that profile.

## Existing deployments: import state before the first real run

The utility's state location is independent of the old scripts. Import the
**authoritative** old `.deployment` directory if you already deployed resources
for the same tenancy/scope/region. Do not start from empty state for an existing
deployment. This command copies state and deployment-mode files, not credentials,
plans, or provider caches, and does not contact OCI:

```bash
osmh-onboard --import-state "/path/to/previous/project/.deployment"
```

Use the folder from the most recent deployment; an earlier copied snapshot may
be stale.
Do not import competing copies: the importer refuses differing destination files.
Run the import when no deployment is running and use the utility's state copy for
future runs. The old folder stays intact; avoid running both copies for the same
deployment.

For Windows, securely transfer the authoritative `.deployment` directory first:

```powershell
& "C:\path\to\osmh_onboarding\.venv\Scripts\osmh-onboard.exe" --import-state "C:\Migration\.deployment"
```

Terraform state may contain sensitive resource/configuration data; keep it private
and backed up, not in Git or a distributable package. This utility does not provide
shared remote state or coordinate independent machines.

The importer expects `<tenancy>/<scope>/<home-region>/terraform.tfstate`, as
created by the previous scripts. It does not migrate OCI Resource Manager stack
state or arbitrary Terraform layouts; those need an explicit migration first.

## Preview, then onboard

Replace `YOUR_COMPARTMENT_OCID` below with a real compartment OCID. Mac (with the
virtual environment activated):

```bash
osmh-onboard "YOUR_COMPARTMENT_OCID" --regions us-ashburn-1 --profile DEFAULT --dry-run
osmh-onboard "YOUR_COMPARTMENT_OCID" --regions us-ashburn-1 --profile DEFAULT
```

Windows (PowerShell, no activation needed):

```powershell
& "C:\path\to\osmh_onboarding\.venv\Scripts\osmh-onboard.exe" "YOUR_COMPARTMENT_OCID" --regions us-ashburn-1 --dry-run
& "C:\path\to\osmh_onboarding\.venv\Scripts\osmh-onboard.exe" "YOUR_COMPARTMENT_OCID" --regions us-ashburn-1
```

Dry-run still requires valid OCI credentials and read access. It previews actions
without applying OCI changes; it does not prove that later write permissions,
Docker builds, network access or registrations will succeed.

On a real run, follow the Function app/network prompts, select the desired
instances, and enter the OCIR auth token when prompted. Successful deployment and
accepted detached invocation are separate from successful OSMH registration.

Other examples (replace the OCID placeholders):

```text
osmh-onboard YOUR_COMPARTMENT_OCID --regions us-ashburn-1,us-phoenix-1 --profile MY_PROFILE
osmh-onboard YOUR_TENANCY_OCID --all-regions --profile MY_PROFILE
osmh-onboard YOUR_COMPARTMENT_OCID --regions us-ashburn-1 --tag-only
osmh-onboard YOUR_COMPARTMENT_OCID --regions us-ashburn-1 --function-application-id YOUR_FNAPP_OCID
osmh-onboard YOUR_COMPARTMENT_OCID --regions us-ashburn-1 --cleanup-only --dry-run
```

Workload regions and the Function home region are separate. Unavailable region
subscriptions are reported/skipped while available requested regions continue.
The target must belong to the selected profile's tenancy. The nightly worker
never unregisters instances: remove `--dry-run` from the cleanup example to
explicitly select and approve confirmed terminated instances for unregistration.
Inactive OSMH status alone is not proof of Compute termination.

## Use without environment activation (optional)

Install using `pipx` instead of the virtual-environment steps above. `pipx` exposes
the command on your user PATH and keeps dependencies isolated.

Mac, if Homebrew is installed:

```bash
brew install pipx
pipx ensurepath
pipx install git+https://github.com/gautamm2707/osmh_onboarding.git
```

Windows:

```powershell
py -3.12 -m pip install --user pipx
py -3.12 -m pipx ensurepath
py -3.12 -m pipx install git+https://github.com/gautamm2707/osmh_onboarding.git
```

Open a new terminal, then run `osmh-onboard --help` from any directory.
For a local checkout, use `pipx install .`. For a pinned Git installation, append
`@<commit-sha>` to the repository URL.

## Troubleshooting and important behavior

- Start Docker Desktop/Colima and confirm `docker info` reports a server before
  a deployment. Windows builds require Linux containers.
- OCI read access does not imply permission to create IAM or root-scoped
  software-source resources. Check the caller's profile, permissions and policy
  propagation for authorization errors.
- For plugin failures or pending registration, inspect Oracle Cloud Agent/plugin
  status and instance network access. Repeated local deployment does not repair
  an unsupported image or unreachable service endpoint.
- If Terraform proposes deleting/replacing resources, automatic deployment stops.
  Check the selected application/network and the authoritative state. Do not
  remove state to bypass that check. Some tags/resources may already be applied.
- An accepted detached invocation does not establish successful registration.
  Check Function results/logs and OSMH status/group membership. Configure invocation
  logging if diagnostic output is needed; it is optional by default.
- New networks, NAT gateways, image storage, Functions and logs may incur OCI
  charges and consume quotas. Uninstalling this package does not delete those
  resources or disable its schedule.

## State, updates and maintenance

- Default state: `~/.osmh-utility/state` on Mac;
  `%USERPROFILE%\.osmh-utility\state` on Windows.
- Layout: `<tenancy-OCID>/<scope-OCID>/<home-region>/terraform.tfstate`.
- Override with `--state-dir PATH` or environment variable `OSMH_STATE_DIR`.
  Use the same location for import and subsequent runs. An explicit `--state-dir`
  takes precedence over the environment variable.
- Terraform templates are copied to that scope's writable directory. Installed
  package files are not used to store state, providers or plans.
- `osmh-onboard --utility-help` lists wrapper-specific options; `--help` lists all
  existing onboarding options. Original CLI arguments are passed to the runtime.
- For updates, stop active deployments, run `git pull --ff-only`, then
  `python -m pip install --upgrade .` from the clone using its virtual environment.
  On Windows without activation, use `.\.venv\Scripts\python.exe`. Pulling source
  alone does not change a normally installed command. For pipx, use
  `pipx install --force git+https://github.com/gautamm2707/osmh_onboarding.git`.
  State survives reinstalls. Changes in older script folders do not update this
  independent utility.
- `src/osmh_utility/cli.py` is the launcher; `src/osmh_utility/runtime/` contains
  the scripts and Function/Terraform assets. `tests/` contains offline CLI tests.

Only source, package metadata, documentation, tests and required runtime assets
are tracked. Credentials, deployment state, virtual environments, generated ZIPs,
build outputs and obsolete standalone/Resource Manager files are excluded.

## Verification and limitations

Run the offline checks with the installed environment:

```text
python -m unittest discover -s tests -v
```

Packaging does not eliminate the existing script's service prerequisites or turn
it into a standalone binary. Native Windows launch/deployment and live OCI runs
need validation on those environments; local packaging tests cannot establish that.

Reference documentation:

- [Python command-line packaging](https://packaging.python.org/en/latest/guides/creating-command-line-tools/)
- [OCI SDK configuration](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm)
- [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/)
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Terraform installation](https://developer.hashicorp.com/terraform/install)
