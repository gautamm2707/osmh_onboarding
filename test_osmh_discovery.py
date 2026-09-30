"""Offline tests of subtree traversal and OKE exclusion; no OCI requests."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

import oci
import osmh_discovery as discovery


def response(data, next_page=None):
    headers = {"opc-next-page": next_page} if next_page else {}
    return oci.response.Response(200, headers, data, None)


def compartment(name, parent, state="ACTIVE"):
    return NS(id=name, name=name, compartment_id=parent, lifecycle_state=state)


def instance(instance_id, **fields):
    return NS(id=instance_id, **fields)


def client_mock():
    client = Mock()
    # Keep the real OCI pagination/retry wrapper active in these tests.
    client.list_compartments.__name__ = "list_compartments"
    client.list_node_pools.__name__ = "list_node_pools"
    return client


class DiscoveryTests(unittest.TestCase):
    def test_tenancy_root_and_paginated_nested_compartments_are_included(self):
        root = "ocid1.tenancy.oc1..example"
        client = client_mock()
        client.get_tenancy.return_value = response(NS(id=root, name="Example"))

        def listing(parent, **kwargs):
            if parent == root:
                if kwargs.get("page") == "next":
                    return response([compartment("b", root)])
                return response([compartment("a", root)], "next")
            return response([compartment("nested", "a")] if parent == "a" else [])

        client.list_compartments.side_effect = listing
        found = discovery.discover_compartments(client, root)
        self.assertEqual([c.id for c in found], [root, "a", "b", "nested"])
        self.assertEqual(found[0].name, "Example (root)")
        client.get_compartment.assert_not_called()
        self.assertEqual({c.args[0] for c in client.list_compartments.call_args_list},
                         {root, "a", "b", "nested"})

    def test_empty_tenancy_still_includes_root_for_compute_scan(self):
        root = "ocid1.tenancy.oc1..empty"
        client = client_mock()
        client.get_tenancy.return_value = response(NS(id=root, name="Empty"))
        client.list_compartments.return_value = response([])
        self.assertEqual([c.id for c in discovery.discover_compartments(client, root)], [root])

    def test_denied_tenancy_lookup_stops_discovery(self):
        client = client_mock()
        client.get_tenancy.side_effect = oci.exceptions.ServiceError(403, "NotAuthorized", {}, "denied")
        with self.assertRaisesRegex(SystemExit, "Cannot read compartment"):
            discovery.discover_compartments(client, "ocid1.tenancy.oc1..example")
        client.list_compartments.assert_not_called()

    def test_nested_compartments_paginate_without_scanning_siblings(self):
        root = compartment("root", "outside")
        children = [compartment("a", "root"), compartment("b", "root")]
        grandchild = compartment("grandchild", "a")
        client = client_mock()
        client.get_compartment.return_value = response(root)

        def listing(parent, **kwargs):
            if parent == "root":
                if kwargs.get("page") == "page-two":
                    return response([children[1]])
                return response([children[0]], "page-two")
            return response([grandchild] if parent == "a" else [])

        client.list_compartments.side_effect = listing
        result = discovery.discover_compartments(client, "root")
        self.assertEqual([value.id for value in result], ["root", "a", "b", "grandchild"])
        calls = client.list_compartments.call_args_list
        self.assertEqual({value.args[0] for value in calls}, {"root", "a", "b", "grandchild"})
        self.assertTrue(all(value.kwargs["access_level"] == "ANY" for value in calls))
        self.assertTrue(all(value.kwargs["compartment_id_in_subtree"] is False for value in calls))

    def test_nonactive_compartments_are_not_traversed(self):
        client = client_mock()
        client.get_compartment.return_value = response(compartment("root", "tenancy"))
        client.list_compartments.return_value = response([compartment("deleted", "root", "DELETED")])
        self.assertEqual([v.id for v in discovery.discover_compartments(client, "root")], ["root"])
        client.list_compartments.assert_called_once()

    def test_inaccessible_child_stops_scan_instead_of_partial_tree(self):
        client = client_mock()
        client.get_compartment.return_value = response(compartment("root", "tenancy"))
        client.list_compartments.side_effect = [response([compartment("child", "root")]),
            oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "denied")]
        with self.assertRaisesRegex(SystemExit, "Cannot list child compartments of child"):
            discovery.discover_compartments(client, "root")

    def test_wrong_parent_is_not_added_to_scope(self):
        client = client_mock()
        client.get_compartment.return_value = response(compartment("root", "tenancy"))
        client.list_compartments.return_value = response([compartment("sibling", "tenancy")])
        with self.assertRaisesRegex(SystemExit, "unexpected parent"):
            discovery.discover_compartments(client, "root")

    def test_oke_pool_pages_and_child_pools_exclude_only_scanned_instances(self):
        client = client_mock()

        def listing(compartment_id, **kwargs):
            if compartment_id == "root":
                if kwargs.get("page") == "page-two":
                    return response([NS(id="pool2")])
                return response([NS(id="pool1")], "page-two")
            return response([NS(id="pool3")])

        client.list_node_pools.side_effect = listing
        members = {"pool1": ["worker", "not-in-scan"], "pool2": [], "pool3": ["child-worker"]}
        client.get_node_pool.side_effect = lambda pool_id: response(
            NS(nodes=[NS(id=value) for value in members[pool_id]]))
        instances = [instance("worker"), instance("child-worker"), instance("regular", display_name="oke-app")]
        result = discovery.discover_oke_instance_ids(client, ["root", "child"], instances)
        self.assertEqual(set(result), {"worker", "child-worker"})
        self.assertIn("pool3", result["child-worker"])
        self.assertEqual(client.get_node_pool.call_count, 3)

    def test_exact_oke_markers_cover_nodes_without_visible_pool_membership(self):
        client = client_mock()
        client.list_node_pools.return_value = response([])
        instances = [
            instance("quick", freeform_tags={"OKEclusterName": "c", "OKEnodePoolName": "p"}),
            instance("creator", defined_tags={"Oracle-Tags": {"CreatedBy": "oke"}}),
            instance("bootstrap", metadata={"oke_init_script": "script"}),
            instance("looks-similar", display_name="oke-test", freeform_tags={"team": "oke"}),
            instance("human", defined_tags={"Oracle-Tags": {"CreatedBy": "okeuser"}}),
            instance("partial-tags", freeform_tags={"OKEclusterName": "c"}),
        ]
        result = discovery.discover_oke_instance_ids(client, ["root"], instances)
        self.assertEqual(set(result), {"quick", "creator", "bootstrap"})
        client.list_node_pools.assert_called_once()

    def test_failed_oke_inventory_is_fatal_even_if_tagged_nodes_found(self):
        for failure in ("list_node_pools", "get_node_pool"):
            with self.subTest(operation=failure):
                client = client_mock()
                client.list_node_pools.return_value = response([NS(id="pool")])
                getattr(client, failure).side_effect = oci.exceptions.ServiceError(
                    404, "NotAuthorizedOrNotFound", {}, "denied")
                with self.assertRaisesRegex(SystemExit, "partial discovery"):
                    discovery.discover_oke_instance_ids(client, ["root"], [
                        instance("tagged", metadata={"oke_init_script": "script"}), instance("unknown")])


if __name__ == "__main__":
    unittest.main()
