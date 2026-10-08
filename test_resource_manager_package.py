"""Packaging must never copy local credentials, state, or unreviewed files."""
import tempfile
from pathlib import Path
import unittest
import zipfile
from urllib.parse import parse_qs, urlsplit

from package_resource_manager import PACKAGE_FILES, ROOT, deploy_url, package


class ResourceManagerPackageTests(unittest.TestCase):
    def fixture(self, root):
        for name in PACKAGE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture: " + name)

    def test_allowlist_excludes_credentials_state_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            for name in (".env", "terraform.tfstate", "profile-map.json", "private.key", "extra.tf"):
                (root / name).write_text("MUST NOT SHIP")
            output = root / "stack.zip"
            first = package(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(PACKAGE_FILES))
                self.assertTrue(all(b"MUST NOT SHIP" not in archive.read(n) for n in archive.namelist()))
                self.assertIsNone(archive.testzip())
            self.assertEqual(first, package(root, output))

    def test_symlink_and_missing_source_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / PACKAGE_FILES[0]
            path.unlink()
            with self.assertRaises(ValueError):
                package(root, root / "stack.zip")
            path.symlink_to(root / PACKAGE_FILES[1])
            with self.assertRaises(ValueError):
                package(root, root / "stack.zip")
            self.assertFalse((root / "stack.zip").exists())

    def test_nested_zip_url_encoded_as_one_parameter(self):
        source = "https://objectstorage.example/p/opaque/n/ns/b/bucket/o/stack.zip?versionId=a&x=1"
        url = urlsplit(deploy_url(source))
        self.assertEqual(url.netloc, "cloud.oracle.com")
        self.assertEqual(parse_qs(url.query), {"zipUrl": [source]})

    def test_credential_bearing_and_non_https_urls_rejected(self):
        for value in ("http://example.org/a.zip", "https://user:secret@example.org/a.zip", "file:///a.zip", "https://example.org/a.zip#fragment"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy_url(value)

    def test_console_schema_uses_dynamic_compute_list_and_managed_auth(self):
        schema = (ROOT / "schema.yaml").read_text()
        self.assertIn("type: list\n    valueType: selected_instance_id", schema)
        self.assertIn("target_compartment_ocid:\n    type: oci:identity:compartment:id", schema)
        self.assertIn("compartment_ocid:\n    type: string\n    visible: false", schema)
        self.assertIn("type: oci:core:instance:id", schema)
        self.assertIn("type: oci:kms:secret:id", schema)
        self.assertIn("region:\n    type: oci:identity:region:name", schema)
        self.assertIn("title: Region", schema)
        self.assertNotIn("session.region", schema)
        self.assertNotIn("console_region", schema)
        self.assertIn("source_commit:\n    type: string\n    default: 0ae272811ea33d3c78d903d5bbe1f146310a5428\n    visible: false", schema)
        self.assertNotIn("title: Function image", schema)
        self.assertNotIn("ocir_auth_token", schema.casefold())

    def test_cloud_build_uses_supported_runner_and_repository_settings(self):
        build = (ROOT / "orm_build.tf").read_text()
        self.assertIn('image                              = "OL8_X86_64_STANDARD_10"', build)
        self.assertNotIn("is_immutable", build)
        self.assertIn('resource "oci_identity_dynamic_group" "build_pipeline"', build)
        self.assertIn('resource "oci_identity_dynamic_group" "connection"', build)
        self.assertIn("resource.type = 'devopsbuildpipeline'", build)
        self.assertIn("resource.type = 'devopsconnection'", build)
        self.assertIn('to read secret-family in tenancy', build)
        self.assertIn('to manage devops-family in tenancy', build)
        self.assertIn('to manage repos ${local.scope}', build)
        self.assertIn("from = oci_identity_dynamic_group.build[0]", build)
        self.assertIn("to   = oci_identity_dynamic_group.build_pipeline[0]", build)
        self.assertIn("from = oci_identity_policy.build[0]", build)
        self.assertIn("to   = oci_identity_policy.build_pipeline[0]", build)
        self.assertNotIn("target.secret.id", build)
        self.assertNotIn("target.project.id", build)
        self.assertNotIn("target.repo.name", build)
        self.assertNotIn("condition     = self.state", build)

    def test_first_apply_allows_iam_to_propagate(self):
        variables = (ROOT / "orm_variables.tf").read_text()
        schema = (ROOT / "schema.yaml").read_text()
        self.assertIn('variable "iam_wait_seconds"', variables)
        self.assertIn("default     = 3600", variables)
        self.assertIn("iam_wait_seconds:\n    type: integer", schema)
        self.assertIn("default: 3600", schema)


if __name__ == "__main__":
    unittest.main()
