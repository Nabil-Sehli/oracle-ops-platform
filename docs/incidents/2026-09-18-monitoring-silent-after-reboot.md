# Prometheus and Alertmanager stayed down after the nightly reboot

**When:** 2026-09-18 04:30 UTC → 2026-09-19 16:04 UTC (35.5 hours)
**Impact:** No server metrics and no Alertmanager alerts for a day and a half. Nothing reported it.
**Found:** by hand, while deploying an unrelated change.
**Severity:** no user-facing outage; the alerting path itself was blind, which is worse than it sounds.

## What happened

Unattended upgrades reboot the box at 04:30 UTC. On 2026-09-18 they did.

Docker asked every container to stop. Prometheus and Alertmanager handle `SIGTERM` quickly and
exit cleanly:

```
04:30:00.536  alertmanager  Shutting down gracefully
04:30:00.597  alertmanager  Received shutdown signal, exiting gracefully...
04:30:00.615  prometheus    TSDB stopped
04:30:00.615  prometheus    See you next time!
04:30:12      system boot
04:30:28      caddy, n8n, uptime-kuma, grafana, cadvisor, node-exporter started
```

Six containers came back. Two did not, and their `RestartCount` stayed at `0`.

## Why

Every service was `restart: unless-stopped`. The Docker docs describe it as "similar to `always`,
except that when the container is stopped, it is not restarted even after the Docker daemon
restarts" — and *stopped* means stopped, no matter who stopped it, including the daemon on its way
down.

So the outcome was decided by a race nobody designed:

- Prometheus and Alertmanager shut down in about 100 ms. They reached a clean stopped state before
  the daemon went away, so the daemon had no reason to bring them back.
- The other six were still shutting down when the daemon died. Their last recorded desired state
  was *running*, so the daemon restarted them at boot.

The well-behaved containers were punished for being well-behaved.

## Why nothing alerted

The two alert paths are supposed to be independent, and they were — but they don't cover this:

| Path | Covers | Saw this? |
|---|---|---|
| Alertmanager → Telegram | `ScrapeTargetDown`, `ContainerMissing`, disk, memory, CPU | No. Every one of those rules is *evaluated by Prometheus*. |
| Uptime Kuma → Telegram | Four public sites, plus the nightly-backup dead man's switch | No. Prometheus isn't a public site and doesn't ping Kuma. |

The monitoring stack was the one thing in the estate nothing was watching. It had also only been
running since 2026-09-17 17:18, so this was its **first** reboot — it failed the first time it was
asked to survive one, and would have failed again every night.

## Fix

`restart: always` for every service in `ansible/roles/stack/templates/compose.yml.j2`. `always`
restarts a container when the daemon starts regardless of how it stopped, which is the behaviour
this stack always wanted. Applied 2026-09-19 16:04 UTC; all nine containers are now `always`.

## What this leaves open

`always` fixes the restart, but nothing yet *notices* if it fails again. The gap is structural: an
alerting system can't be the thing that reports its own absence.

- **Next:** Uptime Kuma monitors against `prometheus:9090/-/healthy` and `alertmanager:9093/-/healthy`
  over the internal network. Kuma is a separate process with a separate notification path, and it
  survived this incident — it is the right watchdog.
- **Also worth doing:** a reboot drill. The backups are proven by a monthly restore drill; the
  stack's ability to come back from a reboot was assumed, and the assumption was wrong. `sudo reboot`
  once a quarter, then check all nine containers.

## Lesson

A restart policy is a behaviour under failure, and untested behaviour under failure is a guess —
the same reason the backups get a monthly drill. The detail that made this bite was ordinary and
documented; it only mattered because nothing ever rebooted the box and looked afterwards.
