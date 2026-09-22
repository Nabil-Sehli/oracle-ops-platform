output "public_ip" {
  value       = oci_core_instance.server.public_ip
  description = "Point the DNS A records at this. Ansible uses it only for the first run."
}

output "ssh" {
  value = (length(var.ssh_allowed_cidrs) > 0
    ? "ssh ubuntu@${oci_core_instance.server.public_ip}"
  : "port 22 is closed to the internet: ssh ubuntu@<tailnet address in ansible/inventory.ini>")
}

output "image_used" {
  value = data.oci_core_images.ubuntu_arm.images[0].display_name
}
