# Published Resource Manager package

`osmh-resource-manager.zip` is the dedicated deployment package used by **Onboard in OSMH** in the blog and repository README. Its root contains `schema.yaml` and `orm_*.tf`; it excludes the separate CLI `deployment/` directory, local credentials, state, provider binaries and virtual environments.

After changing any packaged source, schema or documentation, run from the repository root:

```bash
python3 package_resource_manager.py --output resource-manager/osmh-resource-manager.zip
```

Commit the updated sources and ZIP together. The packager uses an explicit allowlist and deterministic ZIP metadata. The archive does not include this directory or itself.
