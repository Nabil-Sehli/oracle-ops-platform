// Credentials come from the OCI console: Profile -> API keys -> Add API key.
// The config snippet Oracle shows after upload contains every value below.

variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }

variable "private_key_path" {
  type        = string
  description = "Path to the API signing private key (.pem) downloaded when the key was created."
}

variable "region" {
  type        = string
  description = "Home region of the tenancy, e.g. eu-frankfurt-1."
}

variable "compartment_ocid" {
  type        = string
  description = "Compartment to build in. The tenancy OCID is the root compartment and is fine here."
}

variable "ssh_public_key" {
  type        = string
  description = "Contents of the public key allowed to log in as the ubuntu user."
}

// Always Free allows 4 OCPUs and 24 GB of A1 in total across all instances.
// The language school instance already uses 2 OCPUs and 12 GB, so these
// defaults consume exactly what is left. Halve them if A1 capacity is refused.
variable "instance_ocpus" {
  type    = number
  default = 2
}

variable "instance_memory_gb" {
  type    = number
  default = 12
}

variable "boot_volume_gb" {
  type        = number
  default     = 100
  description = "Always Free includes 200 GB of block storage in total, boot volumes included."
}

variable "name_prefix" {
  type    = string
  default = "ops"
}

variable "ssh_allowed_cidrs" {
  type        = list(string)
  default     = []
  description = "Addresses allowed to reach port 22 from the internet. Empty: SSH only over Tailscale. Set to [\"<your ip>/32\"] to bootstrap a new server or if the tailnet is down."
}
