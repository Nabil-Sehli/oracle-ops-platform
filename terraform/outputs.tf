output "public_ip" {
  value       = oci_core_instance.server.public_ip
  description = "Point the DNS A records at this, and put it in ansible/inventory.ini."
}

output "ssh" {
  value = "ssh ubuntu@${oci_core_instance.server.public_ip}"
}

output "image_used" {
  value = data.oci_core_images.ubuntu_arm.images[0].display_name
}
