# Ops platform: plan

One server, managed entirely as code, that runs side services, monitors everything
I host, backs up off the box with proven restores, and publishes a public status page.

## Decisions (2026-09-15)

| Question | Choice | Why |
|---|---|---|
| Hosting | Second Oracle Cloud free-tier Ampere A1 instance | Free. The language school app (production) already uses a 12 GB A1 instance; the free tier allows 24 GB A1 in total. Experiments and failure drills stay off the production box. |
| Domain | Personal domain (to buy) | Keeps portfolio infrastructure separate from the school's brand. |
| Alerts | Telegram bot | Instant on the phone, doesn't get lost in spam. |

## Existing estate (what gets monitored)

| Site | Where it runs |
|---|---|
| deutscheslernzentrum.de | Oracle A1 instance: Caddy + app + MySQL 8.4 (docker compose), pull-based auto-deploy, nightly `backup.sh` (local disk only, off-site push still commented out) |
| caretrack-25m.pages.dev | Cloudflare Pages |
| nabil-sehli.github.io/portfolio | GitHub Pages |
| n8n solar workflows | Local Docker only, to be hosted in phase 2 |

## Phases

### 1. Server from code (week 1)
- Terraform (OCI provider): VCN, subnet, security list, A1 instance.
- Ansible: SSH keys only, firewall (OCI iptables **and** security list), fail2ban,
  unattended-upgrades, Docker, swap.
- Proof: destroy and rebuild from zero with one command; record the time.

### 2. Services behind one proxy (week 1-2)
- Caddy with a subdomain per service and automatic HTTPS.
- n8n hosting the solar workflows as a live demo, behind auth.
- Only Caddy publishes ports; everything else on internal networks.

### 3. Monitoring (week 2)
- Uptime Kuma: public status page for all four sites.
- Prometheus + node_exporter + cAdvisor + blackbox_exporter, Grafana dashboards.
- Alerts to Telegram: site down, disk > 80%, TLS certificate expiring within 14 days.

### 4. Backups proven to restore (week 2-3)
- restic, encrypted, to Cloudflare R2 or Backblaze B2.
- Close the gap in the school app: its dumps and uploads get pushed off-site too.
- Monthly automated restore drill: latest backup into a throwaway database, row counts checked, alert on failure.

### 5. CI and a failure drill (week 3)
- GitHub Actions: `terraform fmt/validate`, `ansible-lint`.
- Break something on purpose (delete a data volume), restore, write an incident report with a timeline.

### 6. Portfolio section
- Architecture diagram, link to the live status page, the incident report, rebuild and restore times.

### 7. LLM observability (2026-09-19) - done, live
- A failure forensics tool for the n8n workflows: every run becomes a trace that names the
  exact node that failed, with the model, tokens, cost and latency of each step.
- `llmobs`, a standard-library Python collector on the existing stack: SQLite for the traces,
  `/metrics` for the Prometheus that is already here, a plain HTML page for reading a broken run.
- Grafana dashboard (failure rate, p95 latency, spend, failures by node) and Telegram alerts on
  failure rate, provider 429/5xx, slow runs, daily spend over budget, and an unpriced model.
- An n8n error-trigger workflow reports every failed run without touching the workflows.
- Found on the way: the monitoring stack had been dead since the previous night's reboot
  (`restart: unless-stopped`); fixed to `always` and written up in `docs/incidents/`.
- Next: an Uptime Kuma watchdog on Prometheus itself, and the same event on successful runs,
  from inside the two solar workflows, for token
  and cost coverage on the happy path.

## Needs me at the computer

- [ ] OCI console: confirm remaining free A1 allowance (Governance → Limits, Quotas and Usage → Compute, "Ampere A1")
- [ ] OCI API signing key for Terraform (Profile → API keys → Add API key), note tenancy/user OCIDs and region
- [ ] Buy the domain and point its nameservers at Cloudflare (free DNS)
- [ ] Create the Telegram bot with @BotFather and note the token and my chat ID
- [ ] Create the off-site bucket (R2 or B2) and an access key scoped to it

## Side fix
- The language school repo's README still says it's hosted on Railway and links the old Railway URL.
