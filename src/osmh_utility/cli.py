"""Launch bundled scripts without depending on the caller's working directory."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__


def state_root(value=None):
    return Path(value or os.environ.get("OSMH_STATE_DIR") or
                Path.home() / ".osmh-utility" / "state").expanduser().resolve()


def import_state(source, destination):
    """Copy only persisted Terraform state/mode files; never overwrite conflicts."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"State source directory does not exist: {source}")
    allowed = {"terraform.tfstate", "terraform.tfstate.backup", "deployment-mode.json"}
    files = [p for p in source.rglob("*") if p.is_file() and p.name in allowed]
    if not any(p.name == "terraform.tfstate" for p in files):
        raise ValueError("No terraform.tfstate found. Supply the old .deployment directory.")
    # Validate every source and destination before copying anything.
    for path in files:
        relative = path.relative_to(source)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise ValueError(f"Symbolic link in state source: {relative}")
        if len(relative.parts) != 4:
            raise ValueError(f"Expected tenancy/scope/region/file state layout: {relative}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or (path.name.startswith("terraform.tfstate") and
                                        not {"version", "resources"} <= data.keys()):
            raise ValueError(f"Invalid state file: {relative}")
        target = destination / relative
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise ValueError(f"Existing state differs; nothing imported: {target}")
    count = 0
    for path in files:
        target = destination / path.relative_to(source)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also protects against a destination appearing after validation.
        with target.open("xb") as output, path.open("rb") as input_file:
            if os.name != "nt":
                os.chmod(target, 0o600)
            shutil.copyfileobj(input_file, output)
        count += 1
    print(f"Imported {count} state file(s) into {destination}. Source files are unchanged.")
    print("Use this state location for future runs; do not operate both copies concurrently.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="osmh-onboard", add_help=False, allow_abbrev=False,
        description="OSMH utility options. Use --help for the underlying onboarding options.")
    parser.add_argument("--version", action="store_true", help="Print utility version")
    parser.add_argument("--utility-help", action="store_true", help="Print these utility options")
    parser.add_argument("--state-dir", help="State root; default OSMH_STATE_DIR or ~/.osmh-utility/state")
    parser.add_argument("--import-state", metavar="OLD_DEPLOYMENT_DIR",
                        help="Copy prior deployment state into the state root, then exit; no OCI calls")
    args, forwarded = parser.parse_known_args(argv)
    if args.utility_help:
        parser.print_help()
        return 0
    if args.version:
        print(f"osmh-utility {__version__}")
        return 0
    state = state_root(args.state_dir)
    if args.import_state:
        if forwarded:
            parser.error("Run --import-state separately from onboarding arguments.")
        try:
            import_state(args.import_state, state)
        except (OSError, ValueError) as exc:
            print(f"State import failed: {exc}", file=sys.stderr)
            return 1
        return 0
    script = Path(__file__).resolve().parent / "runtime" / "onboard_osmh.py"
    env = dict(os.environ, OSMH_STATE_DIR=str(state), PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        # Inherit terminal streams for instance selection and hidden token prompts.
        return subprocess.call([sys.executable, "-u", str(script), *forwarded], env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
