"""Packaging must never copy local credentials, state, or unreviewed files."""
import tempfile
from pathlib import Path
import unittest
import zipfile
from urllib.parse import parse_qs, urlsplit

from package_resource_manager import PACKAGE_FILES, ROOT, deploy_url, package
from run_devops_build import retryable
from validate_build_connection import ValidationError, oci_error_summary, repository_coordinates


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

    def test_build_repository_url_is_strict_and_parsed(self):
        self.assertEqual(
            repository_coordinates("https://github.com/gautamm2707/osmh_onboarding.git"),
            ("gautamm2707", "osmh_onboarding"),
        )
        for value in (
            "http://github.com/owner/repo",
            "https://token@github.com/owner/repo",
            "https://github.com/owner/repo/extra",
            "https://example.com/owner/repo",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                repository_coordinates(value)

    def test_oci_error_summary_excludes_cli_noise(self):
        output = 'TransientServiceError:\n{"code":"InternalError","message":"Unable to validate","opc-request-id":"request-id"}\n'
        self.assertEqual(
            oci_error_summary(output),
            "InternalError: Unable to validate: request request-id",
        )

    def test_build_retry_only_handles_source_iam_failures(self):
        self.assertTrue(retryable("Error fetching secret variable from vault"))
        self.assertTrue(retryable("Unable to fetch build_spec due to RelatedResourceNotAuthorizedOrNotFound"))
        self.assertFalse(retryable("Container build command failed"))

    def test_console_schema_uses_dynamic_compute_list_and_managed_auth(self):
        schema = (ROOT / "schema.yaml").read_text()
        variables = (ROOT / "orm_variables.tf").read_text()
        self.assertIn("type: list\n    valueType: selected_instance_id", schema)
        self.assertIn("target_compartment_ocid:\n    type: oci:identity:compartment:id", schema)
        self.assertIn("compartment_ocid:\n    type: string\n    visible: false", schema)
        self.assertIn("type: oci:core:instance:id", schema)
        self.assertIn("type: oci:kms:secret:id", schema)
        self.assertIn("github_token_secret_compartment_ocid:\n    type: oci:identity:compartment:id", schema)
        self.assertIn("compartmentId: ${github_token_secret_compartment_ocid}", schema)
        self.assertIn("region:\n    type: oci:identity:region:name", schema)
        self.assertIn("title: Region", schema)
        self.assertNotIn("session.region", schema)
        self.assertNotIn("console_region", schema)
        self.assertIn("source_commit:\n    type: string\n    default: a7af62aa22cf15ec9b108fc91f68d26402a1d244\n    visible: false", schema)
        self.assertIn('default     = "a7af62aa22cf15ec9b108fc91f68d26402a1d244"', variables)
        self.assertNotIn("title: Function image", schema)
        self.assertNotIn("ocir_auth_token", schema.casefold())
        self.assertIn("title: Onboard all eligible instances", schema)
        self.assertIn("not: ['${onboard_all_instances}']", schema)
        for title in ("Workload regions", "Build connection readiness timeout in seconds",
                      "Total build retry timeout in seconds", "Initial Function IAM wait in seconds"):
            self.assertNotIn("title: " + title, schema)

    def test_compartment_tag_default_is_managed(self):
        tags = (ROOT / "orm_tags_iam.tf").read_text()
        self.assertIn('resource "oci_identity_tag_default" "new_opt_in"', tags)
        self.assertIn('resource "oci_identity_tag_default" "existing_opt_in"', tags)
        self.assertGreaterEqual(tags.count('value             = "osmanagementhub"'), 2)
        self.assertIn("local.existing_tag_defaults", tags)
        self.assertNotIn("data.oci_identity_tag_defaults.existing_opt_in[0].tag_defaults) ==", tags)
        query = tags.split('data "oci_identity_tag_defaults" "existing_opt_in"', 1)[1].split("}\n", 1)[0]
        self.assertIn("compartment_id", query)
        self.assertNotIn("tag_definition_id", query)
        self.assertIn("item.tag_definition_id == local.tag_definition_id", (ROOT / "orm_versions.tf").read_text())

    def test_namespace_is_discovered_by_exact_name(self):
        versions = (ROOT / "orm_versions.tf").read_text()
        tags = (ROOT / "orm_tags_iam.tf").read_text()
        self.assertIn("ns.name == var.tag_namespace", versions)
        self.assertIn("reuse_tag_namespace", versions)
        self.assertIn('resource "oci_identity_tag" "existing_missing"', tags)

    def test_secret_is_validated_against_independent_compartment(self):
        build = (ROOT / "orm_build.tf").read_text()
        self.assertIn('data "oci_vault_secret" "github_token"', build)
        self.assertIn("self.compartment_id == local.secret_compartment_id", build)

    def test_cloud_build_uses_supported_runner_and_repository_settings(self):
        build = (ROOT / "orm_build.tf").read_text()
        dockerfile = (ROOT / "function/Dockerfile").read_text()
        dockerignore = (ROOT / ".dockerignore").read_text()
        requirements = (ROOT / "function/requirements.txt").read_text()
        self.assertIn('image                              = "OL8_X86_64_STANDARD_10"', build)
        self.assertNotIn("is_immutable", build)
        self.assertIn('resource "oci_identity_dynamic_group" "build_pipeline"', build)
        self.assertIn('resource "oci_identity_dynamic_group" "connection"', build)
        self.assertIn("resource.type = 'devopsbuildpipeline'", build)
        self.assertIn("resource.type = 'devopsconnection'", build)
        self.assertIn('to read secret-family in tenancy', build)
        self.assertGreaterEqual(build.count('to read secret-family in tenancy'), 2)
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
        self.assertIn("--index-url https://pypi.org/simple", dockerfile)
        self.assertNotIn("artifactory-builds.oci.oraclecorp.com", dockerfile)
        self.assertIn("fdk==0.1.125", requirements)
        self.assertIn("!osmh_selection.py", dockerignore)

    def test_first_apply_allows_iam_to_propagate(self):
        variables = (ROOT / "orm_variables.tf").read_text()
        schema = (ROOT / "schema.yaml").read_text()
        build = (ROOT / "orm_build.tf").read_text()
        self.assertIn('variable "iam_wait_seconds"', variables)
        self.assertIn("default     = 3600", variables)
        self.assertIn("iam_wait_seconds:\n    type: integer", schema)
        self.assertIn("default: 3600", schema)
        self.assertIn("validate_build_connection.py", build)
        self.assertIn("validation_run        = plantimestamp()", build)
        self.assertNotIn('command = "sleep ${var.iam_wait_seconds}"', build)
        self.assertIn("validate_build_connection.py", PACKAGE_FILES)
        self.assertIn("run_devops_build.py", PACKAGE_FILES)
        self.assertIn('resource "terraform_data" "build_run"', build)
        self.assertNotIn('resource "oci_devops_build_run" "image"', build)
        self.assertIn('variable "runtime_iam_wait_seconds"', variables)
        self.assertIn("default     = 120", variables)
        self.assertIn('variable "build_timeout_seconds"', variables)
        self.assertIn("default     = 7200", variables)
        runtime = (ROOT / "orm_runtime.tf").read_text()
        self.assertIn('command = "sleep ${var.runtime_iam_wait_seconds}"', runtime)


if __name__ == "__main__":
    unittest.main()
