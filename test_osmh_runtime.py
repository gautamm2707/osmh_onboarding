"""Offline portability checks; never read real credentials or call OCI."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import oci
import osmh_runtime as runtime


TENANCY = "ocid1.tenancy.oc2..example"
ROOT = "ocid1.compartment.oc2..parent"
CHILD = "ocid1.compartment.oc2..child"


def args(**kwargs):
    return NS(**{**dict(auth="auto", config_file="unused", profile="OTHER_TENANCY",
                       region=None, skip_iam=False, admin_group=None), **kwargs})


def response(data):
    return NS(data=data)


class RuntimeTests(unittest.TestCase):
    @patch.object(runtime.oci.config, "validate_config")
    @patch.object(runtime.oci.config, "from_file")
    def test_named_profile_api_key_and_region_override(self, read, validate):
        read.return_value = {"tenancy": TENANCY, "user": "user", "region": "home"}
        config, kwargs = runtime.load_auth(args(region="workload"))
        read.assert_called_once_with("unused", "OTHER_TENANCY")
        validate.assert_called_once_with(config)
        self.assertEqual(config["tenancy"], TENANCY)
        self.assertEqual(config["region"], "workload")
        self.assertEqual(kwargs, {})

    @patch.object(runtime.oci.config, "validate_config")
    @patch.object(runtime.oci.config, "from_file")
    @patch.object(runtime.Path, "read_text", return_value=" token\n")
    @patch.object(runtime.oci.signer, "load_private_key_from_file", return_value="key")
    @patch.object(runtime.oci.auth.signers, "SecurityTokenSigner")
    def test_session_token_auto_detection(self, signer, key, token, read, validate):
        read.return_value = {"tenancy": TENANCY, "region": "workload", "user": "user",
                             "key_file": "key-path", "security_token_file": "token-path"}
        config, kwargs = runtime.load_auth(args())
        signer.assert_called_once_with("token", "key")
        self.assertEqual(kwargs, {"signer": signer.return_value})
        validate.assert_called_once_with(config, signer=signer.return_value)

    @patch.object(runtime.oci.auth.signers, "InstancePrincipalsSecurityTokenSigner")
    @patch.object(runtime.oci.config, "from_file")
    def test_instance_principal_needs_no_config_file(self, read, signer):
        signer.return_value = NS(tenancy_id=TENANCY, region="metadata-region")
        config, kwargs = runtime.load_auth(args(auth="instance_principal", skip_iam=True))
        read.assert_not_called()
        self.assertEqual(config, {"tenancy": TENANCY, "region": "metadata-region"})
        self.assertEqual(kwargs["signer"], signer.return_value)

    @patch.object(runtime.oci.auth.signers, "InstancePrincipalsSecurityTokenSigner")
    def test_instance_principal_cannot_create_user_membership(self, signer):
        with self.assertRaisesRegex(SystemExit, "no user"):
            runtime.load_auth(args(auth="instance_principal"))
        signer.assert_not_called()

    @patch.object(runtime.oci.config, "validate_config")
    @patch.object(runtime.oci.config, "from_file")
    def test_session_without_user_requires_existing_iam(self, read, validate):
        read.return_value = {"tenancy": TENANCY, "region": "workload"}
        with self.assertRaisesRegex(SystemExit, "membership requires"):
            runtime.load_auth(args())

    def test_compartment_ancestry_reaches_configured_tenancy(self):
        identity = Mock()
        identity.get_compartment.side_effect = [
            response(NS(id=CHILD, compartment_id=ROOT, lifecycle_state="ACTIVE")),
            response(NS(id=ROOT, compartment_id=TENANCY, lifecycle_state="ACTIVE"))]
        runtime.validate_compartment_tenancy(identity, CHILD, TENANCY)
        self.assertEqual(identity.get_compartment.call_count, 2)

    def test_foreign_tenancy_and_cycles_rejected(self):
        for parent in ("ocid1.tenancy.oc2..foreign", ROOT, None):
            identity = Mock()
            identity.get_compartment.return_value = response(
                NS(id=ROOT, compartment_id=parent, lifecycle_state="ACTIVE"))
            with self.subTest(parent=parent), self.assertRaises(SystemExit):
                runtime.validate_compartment_tenancy(identity, ROOT, TENANCY)

    def test_invalid_target_rejected_before_api(self):
        identity = Mock()
        for target in ("some-name", ""):
            with self.subTest(target=target), self.assertRaises(SystemExit):
                runtime.validate_compartment_tenancy(identity, target, TENANCY)
        identity.get_compartment.assert_not_called()

    def test_authenticated_tenancy_root_is_accepted(self):
        identity = Mock()
        identity.get_tenancy.return_value = response(NS(id=TENANCY))
        runtime.validate_compartment_tenancy(identity, TENANCY, TENANCY)
        identity.get_tenancy.assert_called_once_with(TENANCY)
        identity.get_compartment.assert_not_called()

    def test_foreign_tenancy_root_rejected_before_lookup(self):
        identity = Mock()
        with self.assertRaisesRegex(SystemExit, "does not match"):
            runtime.validate_compartment_tenancy(identity, "ocid1.tenancy.oc2..foreign", TENANCY)
        identity.get_tenancy.assert_not_called()

    def test_unexpected_tenancy_lookup_is_rejected(self):
        identity = Mock()
        identity.get_tenancy.return_value = response(NS(id="foreign"))
        with self.assertRaisesRegex(SystemExit, "unexpected OCID"):
            runtime.validate_compartment_tenancy(identity, TENANCY, TENANCY)

    def test_root_iam_policies_use_tenancy_scope_and_no_duplicates(self):
        from osmh_iam import policy_statements, compartment_rule, simple_rule_compartments
        statements = policy_statements(TENANCY, TENANCY, "group admins", "dynamic-group workers", "group operators")
        self.assertEqual(len(statements), len(set(statements)))
        self.assertTrue(all("in tenancy" in s for s in statements))
        self.assertFalse(any("in compartment id" in s for s in statements))
        self.assertEqual(simple_rule_compartments(compartment_rule([TENANCY, ROOT, CHILD])),
                         {TENANCY, ROOT, CHILD})

    def test_inactive_compartment_rejected(self):
        identity = Mock()
        identity.get_compartment.return_value = response(
            NS(id=ROOT, compartment_id=TENANCY, lifecycle_state="DELETED"))
        with self.assertRaisesRegex(SystemExit, "ACTIVE"):
            runtime.validate_compartment_tenancy(identity, ROOT, TENANCY)

    @patch.object(runtime, "validate_compartment_tenancy")
    @patch.object(runtime, "list_call_get_all_results")
    @patch.object(runtime.oci.identity, "IdentityClient")
    def test_iam_uses_home_workload_config_and_signer_unchanged(self, client, pages, ancestry):
        pages.return_value = response([
            NS(region_name="home", status="READY", is_home_region=True),
            NS(region_name="workload", status="READY", is_home_region=False)])
        config = {"tenancy": TENANCY, "region": "workload"}
        signer = Mock()
        result = runtime.prepare_identity(config, {"signer": signer}, ROOT)
        self.assertEqual(config["region"], "workload")
        self.assertEqual(client.call_args_list[1].args[0]["region"], "home")
        self.assertIs(client.call_args_list[1].kwargs["signer"], signer)
        ancestry.assert_called_once_with(result, ROOT, TENANCY)

    @patch.object(runtime, "validate_compartment_tenancy")
    @patch.object(runtime, "list_call_get_all_results")
    @patch.object(runtime.oci.identity, "IdentityClient")
    def test_unsubscribed_or_unready_region_rejected(self, client, pages, ancestry):
        for state in ("IN_PROGRESS", "OTHER"):
            pages.return_value = response([NS(region_name="workload", status=state, is_home_region=True)])
            with self.subTest(state=state), self.assertRaisesRegex(SystemExit, "READY subscription"):
                runtime.prepare_identity({"tenancy": TENANCY, "region": "workload"}, {}, ROOT)
        ancestry.assert_not_called()

    @patch.object(runtime, "list_call_get_all_results")
    @patch.object(runtime.oci.identity, "IdentityClient")
    def test_missing_home_region_rejected(self, client, pages):
        pages.return_value = response([NS(region_name="workload", status="READY", is_home_region=False)])
        with self.assertRaisesRegex(SystemExit, "home region"):
            runtime.prepare_identity({"tenancy": TENANCY, "region": "workload"}, {}, ROOT)

    @patch.object(runtime, "validate_compartment_tenancy")
    @patch.object(runtime, "list_call_get_all_results")
    @patch.object(runtime.oci.identity, "IdentityClient")
    def test_multiregion_preflight_allows_bootstrap_region_not_subscribed(self, client, pages, ancestry):
        pages.return_value = response([NS(region_name="home", status="READY", is_home_region=True)])
        identity = runtime.prepare_identity({"tenancy": TENANCY, "region": "bootstrap"}, {}, ROOT,
                                            require_workload_region=False)
        ancestry.assert_called_once_with(identity, ROOT, TENANCY)
        self.assertEqual(client.call_args.args[0]["region"], "home")

    @patch.object(runtime, "list_call_get_all_results")
    @patch.object(runtime.oci.identity, "IdentityClient")
    def test_read_denied_is_actionable_no_write(self, client, pages):
        pages.side_effect = oci.exceptions.ServiceError(403, "NotAuthorized", {}, "denied")
        with self.assertRaisesRegex(SystemExit, "no OCI changes made"):
            runtime.prepare_identity({"tenancy": TENANCY, "region": "workload"}, {}, ROOT)
        client.return_value.create_policy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
