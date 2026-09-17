# Drill: total loss of the n8n data volume

**Date:** 2026-09-17 · **Type:** planned drill, production-like, on the ops server
**Result:** service restored in 31 seconds, no data lost
**Scope:** n8n only. Caddy and Uptime Kuma kept running; the language school server was not involved.

A backup that has never been restored is a guess. This drill deletes the volume holding every
n8n workflow, credential and execution, and rebuilds it from the off-site copy in Backblaze B2.

## What I did

| Time (UTC) | Step |
|---|---|
| 18:57:56 | Took a fresh backup (`ops-backup`), snapshot `6e5bebeb`. |
| 18:58 | **Pre-check:** restored that snapshot into a scratch directory and confirmed it held 2 workflows, 3 credentials and the n8n encryption key. Destroying data before knowing the backup is readable is not a drill, it's an outage. |
| 18:59 | Recorded the live state to compare against: 2 workflows, 3 credentials, 12 executions, webhook answering. |
| 19:01:38 | **Failure injected:** stopped n8n, removed the container, `docker volume rm ops_n8n_data`. |
| 19:01:55 | Uptime Kuma's minute check got `502` from Caddy and went into retry. |
| 19:02:04 | Ran `scripts/restore-volume.sh ops_n8n_data`. |
| 19:02:09 | Volume back, container started. **31 s of hard downtime.** |
| 19:02:55 | Uptime Kuma green again. |

## Verification after the restore

| Check | Result |
|---|---|
| Workflows | 2, both listed by `n8n list:workflow` with their original ids |
| Credentials | 3, and **all three decrypt** — the encryption key survived because `/opt/ops/n8n.env` is in the backup |
| Executions | 12, same as before |
| Public webhook | `POST /webhook/lead-qualification` answers again (400 on the invalid sample, as designed) |
| n8n log | no errors, no encryption-key mismatch |
| Uptime Kuma | back to UP on its own |

Nothing was lost: the snapshot was 3 minutes old, and the only writes in between were the
checks I ran myself.

## What the drill exposed

**1. The outage was shorter than the alerting thresholds, so no alert was sent.**
Uptime Kuma retries twice at 60 s before calling a monitor DOWN (~3 min), and the Prometheus
`ContainerMissing` rule needs the container absent for 2 min and then holds for 3 min. A 31 s
outage is invisible to both. That is the intended trade-off — alerting on every restart would
train me to ignore Telegram — but it's now a known number rather than an assumption: **an
outage has to last about 3 minutes before I hear about it.** Left as is.

**2. The restore left the volume without compose's labels.**
The first version of the script recreated the volume with `docker volume create`, and compose
then warned that the volume "was not created by Docker Compose". Harmless today, but it's the
kind of drift that bites later. Fixed: the script now lets `docker compose up -d --no-start`
create the volume so it carries compose's own labels. I verified in a throwaway project that
compose labels a volume it creates this way; the fixed path has not yet been exercised on the
live stack, so the next drill should start from a deleted volume again.

**3. The ad-hoc restore became a script.**
The first run was a sequence of commands I typed. That is exactly what you don't want to be
doing during a real incident, so the procedure is now `scripts/restore-volume.sh`, which handles
all three volumes, puts the consistent SQLite copy back in place of the excluded live file, and
fixes ownership.

## What a real recovery needs

Only three things, none of them on the server:

1. This repository (`terraform apply`, then `run.ps1 site.yml` rebuilds the server itself).
2. `ansible/secrets.yml` — it holds the restic password, without which the backups are
   unreadable, and the B2 keys.
3. The Backblaze bucket.

The n8n encryption key is inside the backup (`/opt/ops/n8n.env`) *and* in `secrets.yml`. Without
it the credentials would restore as unreadable blobs, which is the failure mode this drill was
really testing.

## Next drill

Same exercise on `ops_uptime_kuma_data`, and once a year a full rebuild: destroy the instance,
`terraform apply`, run the playbook, restore all three volumes, and time the whole thing.
