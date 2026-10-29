import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from osmh_utility import cli


class UtilityTests(unittest.TestCase):
    def test_forwarding_preserves_args_cwd_and_exit_status(self):
        args = ["ocid1.compartment.test", "--regions", "a,b", "--profile", "profile with spaces",
                "--repository-map", "relative map.json", "--dry-run"]
        with patch.object(cli.subprocess, "call", return_value=7) as call:
            result = cli.main([*args, "--state-dir", "state with spaces"])
        self.assertEqual(result, 7)
        positional, keywords = call.call_args
        self.assertEqual(positional[0][3:], args)
        self.assertTrue(Path(positional[0][2]).is_file())
        self.assertNotIn("cwd", keywords)
        self.assertEqual(keywords["env"]["OSMH_STATE_DIR"], str(Path("state with spaces").resolve()))
        self.assertEqual(keywords["env"]["PYTHONUTF8"], "1")

    def test_environment_state_override(self):
        with patch.dict(os.environ, {"OSMH_STATE_DIR": "from-env"}):
            self.assertEqual(cli.state_root(), Path("from-env").resolve())
            self.assertEqual(cli.state_root("explicit"), Path("explicit").resolve())

    def test_import_is_repeatable_and_excludes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "old", Path(tmp) / "new"
            scope = source / "tenancy" / "scope" / "region"
            scope.mkdir(parents=True)
            data = '{"version": 4, "resources": []}'
            (scope / "terraform.tfstate").write_text(data)
            (scope / "deployment-mode.json").write_text('{"mode": "existing"}')
            (scope / "secret.key").write_text("must not copy")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.import_state(source, dest)
                cli.import_state(source, dest)
            self.assertEqual(len(list(dest.rglob("*.tfstate"))), 1)
            self.assertFalse(list(dest.rglob("*.key")))
            self.assertEqual((scope / "terraform.tfstate").read_text(), data)

    def test_conflicting_state_fails_before_any_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "old", Path(tmp) / "new"
            for root, serial in [(source, 2), (dest, 3)]:
                scope = root / "tenancy" / "scope" / "region"
                scope.mkdir(parents=True)
                (scope / "terraform.tfstate").write_text(json.dumps(
                    {"version": 4, "serial": serial, "resources": []}))
            (source / "tenancy/scope/region/deployment-mode.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "Existing state differs"):
                cli.import_state(source, dest)
            self.assertFalse((dest / "tenancy/scope/region/deployment-mode.json").exists())

    def test_bundled_assets(self):
        root = Path(cli.__file__).parent / "runtime"
        for name in ["onboard_osmh.py", "reconcile_osmh.py", "deploy_osmh_function.py",
                     ".dockerignore", "function/Dockerfile", "function/func.py",
                     "function/requirements.txt", "deployment/main.tf", "deployment/network.tf",
                     "deployment/variables.tf", "deployment/.terraform.lock.hcl"]:
            self.assertTrue((root / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
