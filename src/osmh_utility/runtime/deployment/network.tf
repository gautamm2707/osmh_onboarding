# Optional isolated network for the Function only. No public IPs or ingress rules.
resource "oci_core_vcn" "function" {
  count          = var.create_network ? 1 : 0
  compartment_id = var.compartment_id
  display_name   = "${local.name}-vcn"
  cidr_blocks    = ["10.254.0.0/24"]
  dns_label      = "osmhfn"
}

resource "oci_core_nat_gateway" "function" {
  count          = var.create_network ? 1 : 0
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.function[0].id
  display_name   = "${local.name}-nat"
}

resource "oci_core_route_table" "function" {
  count          = var.create_network ? 1 : 0
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.function[0].id
  display_name   = "${local.name}-routes"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.function[0].id
  }
}

resource "oci_core_security_list" "function" {
  count          = var.create_network ? 1 : 0
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.function[0].id
  display_name   = "${local.name}-egress"
  egress_security_rules {
    protocol    = "6"
    destination = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }
  egress_security_rules {
    protocol    = "17"
    destination = "169.254.169.254/32"
    udp_options {
      min = 53
      max = 53
    }
  }
  egress_security_rules {
    protocol    = "6"
    destination = "169.254.169.254/32"
    tcp_options {
      min = 53
      max = 53
    }
  }
}

resource "oci_core_subnet" "function" {
  count                      = var.create_network ? 1 : 0
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.function[0].id
  cidr_block                 = "10.254.0.0/24"
  display_name               = "${local.name}-subnet"
  dns_label                  = "function"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.function[0].id
  security_list_ids          = [oci_core_security_list.function[0].id]
}

output "function_subnet_ids" {
  value = var.create_network ? [oci_core_subnet.function[0].id] : var.subnet_ids
}
