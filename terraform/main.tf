// Network, firewall and one Always Free ARM instance.
//
// Note for later: OCI has TWO firewalls. This security list is one of them; the
// Ubuntu image also ships its own iptables rules that drop everything but SSH.
// Ansible opens the second one. A port open here and closed there times out
// silently, which is the single most common "my Oracle instance is unreachable".

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

// Canonical's published ARM image, newest first, so a rebuild is never pinned
// to an image that Oracle has since removed.
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_vcn" "main" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.name_prefix}-vcn"
  cidr_blocks    = ["10.10.0.0/16"]
  dns_label      = "${var.name_prefix}vcn"
}

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-igw"
  enabled        = true
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_security_list" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.name_prefix}-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  // SSH goes over Tailscale, so port 22 is closed to the internet by default.
  // ssh_allowed_cidrs opens it for bootstrapping a new server or as a break-glass
  // if the tailnet is down. Everything else is reached through Caddy on 80/443.
  dynamic "ingress_security_rules" {
    for_each = var.ssh_allowed_cidrs
    content {
      source   = ingress_security_rules.value
      protocol = "6" // TCP
      tcp_options {
        min = 22
        max = 22
      }
    }
  }

  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6"
    tcp_options {
      min = 80
      max = 80
    }
  }

  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6"
    tcp_options {
      min = 443
      max = 443
    }
  }

  // HTTP/3. Caddy serves it, and without this the browser silently falls back.
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "17" // UDP
    udp_options {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_subnet" "public" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.main.id
  display_name               = "${var.name_prefix}-subnet"
  cidr_block                 = "10.10.1.0/24"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.public.id]
  dns_label                  = "${var.name_prefix}sub"
  prohibit_public_ip_on_vnic = false
}

resource "oci_core_instance" "server" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name        = "${var.name_prefix}-server"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = var.instance_ocpus
    memory_in_gbs = var.instance_memory_gb
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_arm.images[0].id
    boot_volume_size_in_gbs = var.boot_volume_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = true
    hostname_label   = var.name_prefix
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
  }

  // A newer image published by Canonical must not silently replace a running
  // server; rebuilds are deliberate (terraform taint), never a side effect.
  lifecycle {
    ignore_changes = [source_details[0].source_id]
  }
}
