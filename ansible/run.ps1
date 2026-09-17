# Runs ansible-playbook in a container against the ops server.
#   .\run.ps1 site.yml            apply
#   .\run.ps1 site.yml --check    dry run
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

# Built only when missing: a rebuild checks Docker Hub, which fails on a flaky
# connection. After editing the Dockerfile run: docker image rm ops-ansible
# The first call after Docker Desktop's Resource Saver pause can fail even
# though the image exists, so check a few times before deciding to build.
for ($i = 0; $i -lt 3; $i++) {
  docker image inspect ops-ansible *> $null
  if ($LASTEXITCODE -eq 0) { break }
  Start-Sleep -Seconds 5
}
if ($LASTEXITCODE -ne 0) {
  docker build -q -t ops-ansible $PSScriptRoot | Out-Null
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Windows file permissions don't survive the bind mount and ssh refuses a
# world-readable key, so the keys are copied in with the right mode first.
# ops_platform: the ops server. id_ed25519: the language school box (school.yml).
$inner = 'mkdir -p /root/.ssh && install -m 600 /keys/ops_platform /root/.ssh/ops_platform && install -m 600 /keys/id_ed25519 /root/.ssh/id_ed25519 && cp /keys/known_hosts /root/.ssh/known_hosts && exec ansible-playbook $@'
docker run --rm `
  -v "${PSScriptRoot}:/work" `
  -v "$env:USERPROFILE\.ssh\ops_platform:/keys/ops_platform:ro" `
  -v "$env:USERPROFILE\.ssh\id_ed25519:/keys/id_ed25519:ro" `
  -v "$env:USERPROFILE\.ssh\known_hosts:/keys/known_hosts:ro" `
  -e ANSIBLE_CONFIG=/work/ansible.cfg `
  -e ANSIBLE_FORCE_COLOR=1 `
  ops-ansible sh -c $inner sh @Rest
exit $LASTEXITCODE
