# Ops platform

One Oracle Cloud free-tier server, created by Terraform, configured by Ansible, running the
services I use to watch my own projects: a public status page, uptime checks, automation
workflows, metrics dashboards, and off-site backups with a monthly restore drill.

Nothing here is clicked together by hand. The repository is the server: a fresh box reaches the
same state with `terraform apply` and one playbook run, and a full re-run reports `changed=0`.

**Live:** [status page](https://status.nabil-ops.duckdns.org) · the other hosts (n8n editor,
Uptime Kuma, Grafana) sit behind a login.

## What runs

| Service | Why |
|---|---|
| **Caddy** | The only container publishing ports. Terminates TLS with automatic Let's Encrypt certificates, proxies everything else on an internal Docker network. |
| **Uptime Kuma** | Checks four public endpoints every minute and serves the public status page. Alerts to Telegram. |
| **n8n** | Runs my [call center automation workflows](https://github.com/Nabil-Sehli/n8n-solar-callcenter-automations). |
| **Prometheus + Alertmanager** | Server and container metrics, 30-day retention, alerts to Telegram. |
| **node_exporter + cAdvisor** | Host metrics (CPU, memory, disk, network) and per-container metrics. |
| **Grafana** | Dashboards, provisioned from this repo rather than saved in the UI. |
| **llmobs** | A ~600-line collector that turns every n8n run into a trace, Prometheus metrics and a spend figure. Answers *which node broke*, not just *a run failed*. |
| **restic** | Nightly encrypted backups to Backblaze B2, plus a monthly automated restore drill. |

## Layout

```
terraform/                  VCN, subnet, security list, Ampere A1 instance
ansible/
  site.yml                  the ops server: base, ssh, firewall, fail2ban, docker, monitoring, llmobs, stack, backup
  school.yml                off-site backups for a second, pre-existing production server
  run.ps1                   runs ansible-playbook in a container (no Ansible on Windows)
  roles/
    base ssh firewall fail2ban   hardening: unattended upgrades, key-only SSH, iptables, ban repeat offenders
    docker                       engine, log rotation
    monitoring                   Prometheus, Alertmanager, Grafana configs and dashboards
    llmobs                       LLM run collector: forensics UI, metrics, cost, Grafana dashboard
    stack                        compose file, Caddyfile, service environment
    backup                       restic, systemd timers, restore drill
    school_offsite               uploads another server's nightly dumps off-site
scripts/kuma-setup.js       configures Uptime Kuma over its socket.io API, idempotent
scripts/n8n/                workflow that reports every failed run to the collector
tests/                      unit and HTTP tests for the collector (python -m unittest discover -s tests)
```

## Decisions worth explaining

- **Ansible in a container.** There's no Ansible control node for Windows. `run.ps1` mounts the
  repo and the SSH keys into a small image, so the same playbook runs from this laptop or from CI.
- **Two backup keys, two passwords.** Each server has its own Backblaze application key, limited
  to one bucket and one prefix, and its own restic password. A compromised server can't read or
  delete the other one's backups.
- **`sqlite3 .backup` instead of stopping containers.** n8n and Uptime Kuma keep serving while a
  consistent copy of their databases is taken; copying the live file could catch it mid-write.
- **Backups prove themselves.** A monthly drill restores the newest snapshot, re-verifies a
  tenth of the stored data, runs an integrity check on the restored databases and reports to
  Telegram. An untested backup is a guess. I also deleted n8n's data volume on purpose and
  rebuilt it from B2: [the write-up](docs/incidents/2026-09-17-n8n-data-loss-drill.md) has the
  timings and what it exposed.
- **Observability the pipeline reports itself.** Prometheus can see that a container is up and
  n8n can say an execution failed; neither can say *which node* failed, on which model, at what
  cost. The workflows post one event per run to a small collector, which keeps the trace in
  SQLite for 14 days and folds the same event into counters. The counters live in their own
  table and are never pruned, so the nightly retention pass can't make a `rate()` run backwards.
- **Prices in configuration, not in code.** Spend is tokens x a price table in `site.yml`. A
  model that isn't in the table is counted but deliberately *not* priced, and raises
  `LlmModelNotPriced` - a cost panel quietly reading zero is worse than one admitting it can't
  tell.
- **`restart: always`, not `unless-stopped`.** After the nightly upgrade reboot, Prometheus and
  Alertmanager shut down cleanly in ~100 ms, which is exactly what `unless-stopped` treats as "do
  not start this again" - and nothing noticed for 35 hours, because Prometheus is the thing that
  would have alerted. The containers too slow to finish stopping were restarted and looked fine.
  [The write-up](docs/incidents/2026-09-18-monitoring-silent-after-reboot.md) has the timeline.
- **Two independent alert paths.** Alertmanager reports what's wrong; an Uptime Kuma push monitor
  reports *silence* — if a nightly backup never pings, that's an alert too.
- **The webhooks pay for themselves.** n8n webhooks spend LLM quota and send mail, so Caddy
  rejects requests without a shared-secret header before n8n starts an execution.
- **node_exporter binds to the Docker bridge only.** It needs the host network namespace for real
  NIC statistics, so its port is opened to Docker networks and to nothing else.
- **Adding to a server I didn't want to change.** The second server already had its own nightly
  backup unit from another repository. A systemd drop-in adds `OnSuccess=` to it, so the upload
  chains onto the existing job without editing a file that repository owns.

## Running it

```powershell
# once
cd terraform
Copy-Item terraform.tfvars.example terraform.tfvars   # tenancy OCIDs, region, your IP, SSH key
terraform init; terraform apply

cd ..\ansible
Copy-Item inventory.ini.example inventory.ini          # the instance's public IP
Copy-Item secrets.example.yml secrets.yml              # passwords, tokens, keys

# every time (Docker Desktop must be running)
.\run.ps1 site.yml --check --diff    # dry run
.\run.ps1 site.yml
```

`secrets.yml`, `inventory.ini`, `terraform.tfvars` and the Terraform state stay out of git.

## Checks

CI runs on every push: `terraform fmt -check` and `terraform validate`, `ansible-lint` at its
`production` profile, `shellcheck` over the backup scripts, and the collector's own test suite
(30 tests: ingest validation, deduplication, cost arithmetic, Prometheus exposition format,
retention, and the HTTP surface including the ingest token).
