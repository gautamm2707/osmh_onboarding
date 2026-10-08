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
        self.assertIn("default: ${session.region}\n    visible: false", schema)
        self.assertNotIn("title: Function image", schema)
        self.assertNotIn("ocir_auth_token", schema.casefold())


if __name__ == "__main__":
    unittest.main()
