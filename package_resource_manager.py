#!/usr/bin/env python3
"""Create a reproducible Resource Manager ZIP from an explicit source allowlist."""
from __future__ import annotations

import argparse
import hashlib
import html
from pathlib import Path
from urllib.parse import quote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parent
PACKAGE_FILES = (
    "orm_versions.tf", "orm_variables.tf", "orm_network.tf", "orm_build.tf",
    "orm_tags_iam.tf", "orm_runtime.tf", "orm_outputs.tf", "schema.yaml",
    "build_spec.yaml", "validate_build_connection.py", "run_devops_build.py", "onboard_osmh.py", "reconcile_osmh.py", "osmh_discovery.py",
    "osmh_iam.py", "osmh_runtime.py", "osmh_tags.py", "osmh_deployment.py",
    "osmh_function_setup.py", "osmh_selection.py", "function/Dockerfile", "function/requirements.txt",
    "function/func.py", "BLOG.md", "RESOURCE_MANAGER.md", ".terraform.lock.hcl",
)


def deploy_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Use a publicly accessible HTTPS ZIP URL without embedded user credentials or a fragment.")
    return "https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=" + quote(source_url, safe="")


def package(source: Path, output: Path) -> str:
    source = source.resolve()
    resolved = []
    for name in PACKAGE_FILES:
        path = source / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Missing or unsafe package source: {name}")
        resolved.append((name, path))
    if output.resolve() in [p.resolve() for _, p in resolved]:
        raise ValueError("The output path must not overwrite a package source file.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in resolved:
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".deployment/osmh-resource-manager.zip")
    parser.add_argument("--source-url", help="Public HTTPS URL where this ZIP will be published; prints the deployment button.")
    args = parser.parse_args()
    url = deploy_url(args.source_url) if args.source_url else None
    digest = package(ROOT, args.output)
    print(f"ZIP: {args.output.resolve()}")
    print(f"SHA256: {digest}")
    if url:
        print(f"Markdown: [Onboard in OSMH]({url})")
        print(f'HTML: <a href="{html.escape(url, quote=True)}">Onboard in OSMH</a>')
        print("Publish the ZIP at that URL before sharing the button.")


if __name__ == "__main__":
    main()
