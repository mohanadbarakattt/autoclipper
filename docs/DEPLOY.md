# Deploying the Clips service (free Oracle server) and switching it on in Virlo Studio

Goal: `https://clips-api.virlo.studio` runs the clipping service 24/7 for **$0/month** in hosting, and the
"كليبات اللايف" tool on virlo.studio talks to it. Only AI usage costs money (≈ $0.03-0.045 per finished clip,
paid from the 15 points each clip earns).

Time: about 1-2 hours, most of it waiting for Oracle and the first install.

```
virlo.studio (Vercel)  ──HTTPS + token──►  clips-api.virlo.studio  (Oracle VM, Ubuntu)
                                              Caddy (HTTPS) → saas web :8101 (API + signed media)
                                                              saas worker (scans + engine runs)
                                              /var/lib/virlo-clips (accounts DB, workspaces, backups)
```

## 0. Decisions before starting

| Decision | Recommended | Why |
|---|---|---|
| Where does the code come from? | push the `saas` branch to GitHub (`autoclipper`) | the server pulls it with one command; there are no secrets in it. If you'd rather not publish the SaaS code yet, keep `autoclipper` private until launch or use a deploy key |
| HTTPS | **Caddy** on the VM + one DNS record | works wherever virlo.studio's DNS lives (Vercel / registrar). Use **Cloudflare Tunnel** instead only if the domain's DNS is on Cloudflare |
| Oracle account type | **upgrade to Pay As You Go** (still $0 for Always Free shapes) + a $1 budget alert | free-tier-only accounts get idle VMs reclaimed and rarely get Ampere capacity; PAYG avoids both |
| Region | the closest to most users with A1 capacity (e.g. Frankfurt / Marseille / Jeddah) | lower latency to Egypt and Europe |

## 1. Oracle Cloud VM (you, in the browser)

1. Sign up at cloud.oracle.com (card needed for verification; not charged for Always Free).
2. Billing → **Upgrade to Pay As You Go**, then Budgets → create a **$1** monthly budget with an email alert.
3. Compute → Instances → **Create instance**:
   - Image: **Ubuntu 24.04** (Canonical). Shape: **VM.Standard.A1.Flex**, **2 OCPU / 12 GB** (the current
     Always Free limit).
   - Boot volume: 100-150 GB (Always Free allows 200 GB total).
   - Networking: create a VCN with a public subnet, **assign a public IPv4**.
   - SSH: upload your public key (or let it generate one and download it).
   - "Out of capacity"? Try another availability domain, or retry later; PAYG accounts get capacity faster.
4. Networking → your VCN → Security List → **Add ingress rules**: TCP **80** and **443** from `0.0.0.0/0`
   (SSH 22 is there already). Skip this if you use Cloudflare Tunnel.
5. Note the VM's **public IP**.

## 2. DNS

Add an **A record** `clips-api` → the VM's public IP wherever virlo.studio's DNS is managed (Vercel → Domains →
virlo.studio → DNS Records, or your registrar). Wait until `nslookup clips-api.virlo.studio` shows the IP.

## 3. Install the service (on the VM)

```bash
ssh ubuntu@<public-ip>
curl -fsSL https://raw.githubusercontent.com/mohanadbarakattt/autoclipper/saas/deploy/oracle/setup.sh -o setup.sh
less setup.sh            # read it before running it
sudo bash setup.sh       # Cloudflare Tunnel instead: sudo HTTPS=tunnel TUNNEL_TOKEN=... bash setup.sh
```

The script (safe to re-run) installs FFmpeg/Python/Caddy, adds 4 GB swap, creates a `virlo` system user,
clones the code to `/opt/virlo-clips`, creates `/etc/virlo-clips/saas.env` with a fresh random
`CLIPS_SERVICE_TOKEN` and media secret, opens ports 80/443 in the VM's firewall, and starts three systemd units:
`virlo-clips-web`, `virlo-clips-worker`, `virlo-clips-backup.timer`.

## 4. AI keys (paid, with spend limits)

| Key | Where | Limit to set |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio → API keys, on a Google Cloud project **with billing enabled** (paid tier: your users' videos aren't used for training) | Cloud Billing → Budgets & alerts, e.g. $20/month with alerts at 50/90% |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys | Settings → Limits: monthly spend limit, e.g. $10 |

```bash
sudo nano /etc/virlo-clips/saas.env      # paste both keys; keep GEMINI_MEDIA_RESOLUTION=low
sudo systemctl restart virlo-clips-web virlo-clips-worker
```

Optional (better signals, free): Twitch and Kick developer app keys in the same file.

## 5. Switch it on in Virlo Studio (Vercel)

1. On the VM: `sudo grep ^CLIPS_SERVICE_TOKEN /etc/virlo-clips/saas.env`
2. Vercel → virlo-studio → Settings → Environment Variables (Production + Preview):
   - `CLIPS_SERVICE_URL` = `https://clips-api.virlo.studio`
   - `CLIPS_SERVICE_TOKEN` = the token from step 1
3. On the VM, `/etc/virlo-clips/saas.env` already has `VIRLO_URL=https://virlo.studio`. For a Vercel preview
   test first, point it at the preview URL and restart.
4. Push the `clips` branch of virlo-studio → Vercel builds a **preview**. Test there (step 6), then merge to
   `master` for production.

## 6. Check it

From your PC (Git Bash):

```bash
CLIPS_SERVICE_URL=https://clips-api.virlo.studio CLIPS_SERVICE_TOKEN=<token> bash deploy/smoke_test.sh
```

Then the real test, on the Vercel preview with your own account:
1. Studio → **كليبات اللايف** → add a channel that streamed in the last few days (yours, or a campaign channel).
2. Watch the scan: `sudo journalctl -u virlo-clips-worker -f` on the VM. First run downloads the Whisper model
   (~1.6 GB) once.
3. Within ~20-40 min: clips appear, each charged 15 points (Studio → نقطي shows the debits), videos play.
4. Spend 0 points (new free account the next day) to see a clip lock and unlock after a top-up.

## 7. Running it

| Task | Command (on the VM) |
|---|---|
| Logs | `journalctl -u virlo-clips-worker -f` · `journalctl -u virlo-clips-web -f` · last engine run per user: `/var/lib/virlo-clips/workspaces/u<id>/last_run.log` |
| Status / queue | `curl -s localhost:8101/health` |
| Update to the latest code | `sudo bash /opt/virlo-clips/deploy/oracle/setup.sh` (pulls `saas`, reinstalls, restarts) |
| Creator opt-out / takedown | `sudo -u virlo /opt/virlo-clips/.venv/bin/python -m saas block-channel twitch <name> "reason"` (run from `/opt/virlo-clips`) |
| Backups | nightly at 03:30 UTC → `/var/lib/virlo-clips/backups` (14 days). Off-site: install rclone for the `virlo` user, add a Cloudflare R2 remote, set `BACKUP_RCLONE_REMOTE` |
| Disk | `df -h /` ; raw footage is deleted after processing, clips after the tier's history window |

## 8. Capacity and when to pay for more

2 OCPU / 12 GB with `SAAS_MAX_PARALLEL_JOBS=1` handles roughly **300-500 finished clips a month** (Whisper +
render ≈ 2-3 CPU-minutes per clip). Signs you've outgrown it: scans finish later than the next one is due, or
users wait over an hour for clips. Then, in order:
1. A second worker on another machine (your PC or a cheap VPS) — needs the shared-queue step (M7 in the plan).
2. Move to a Hetzner CCX23/CPX41 (~$30-45/month, ~3,000 clips/month) by re-running `setup.sh` there and
   restoring the latest backup into `/var/lib/virlo-clips`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Caddy can't get a certificate | DNS not pointing at the VM yet, or ports 80/443 closed in the Oracle security list |
| Virlo shows "خدمة الكليبات بتتجهز" | `CLIPS_SERVICE_URL` / `CLIPS_SERVICE_TOKEN` missing on Vercel, or the tokens differ |
| Scans run but 0 clips and Activity is empty | the service can't reach Virlo for the allowance (check `VIRLO_URL`), or the user has under 15 points |
| YouTube channels find no VODs | update yt-dlp: `sudo -u virlo /opt/virlo-clips/.venv/bin/pip install -U yt-dlp`, restart the worker |
| VM got stopped by Oracle | account isn't PAYG and the VM looked idle; upgrade (section 1) and start it again |
