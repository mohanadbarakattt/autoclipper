# AutoClipper Cloud: SaaS plan

Status: **draft for approval** · branch `saas` · 2026-11-21

AutoClipper Cloud is the hosted, multi-user version of the open-source engine in `clipper/`. A user signs up,
pastes a Twitch / Kick / YouTube channel, and finished 1080x1920 clips (captions, hook, moving crop, payoff zoom,
posting schedule) show up in their dashboard. No PC, no API keys, no FFmpeg on their side.

Three products, kept separate:

| Product | Who | Where |
|---|---|---|
| AutoClipper (open source) | anyone self-hosting, $0 | `main` branch, public repo |
| AutoClipper Pro CRM | you, for your own clipping business | private `autoclipper-pro`, untouched |
| **AutoClipper Cloud** (this plan) | paying users | `saas/` package on this branch, same repo, engine reused |

## 1. Decisions taken (2026-11-21)

| Question | Decision |
|---|---|
| Customer | **Anyone, any channel** (own streams, clipping campaigns, clip pages). Safeguards in §6. |
| Pricing | **3 tiers, $19 / $49 / $99 per month**, free trial (§5) |
| Hosting budget at launch | **~$20-50/month**, one CPU server + object storage, GPU later |
| Domain | **virlo.studio** (you own it): app at `app.virlo.studio`, landing page at the root. It ties the hosted clipper into the Virlo brand. |
| Payments | **Polar** (merchant of record; lists Egypt as a payout country via Stripe Connect Express). Gumroad stays the fallback; billing sits behind one adapter so the swap is a single file. |

## 2. Architecture

```
            browser
               │  HTTPS (Cloudflare in front: TLS, caching, DDoS)
      ┌────────▼─────────┐
      │  saas web (FastAPI)│  signup/login, channels, clips, billing webhooks, admin
      └──┬─────────────┬──┘
         │ control DB  │ job rows
   ┌─────▼─────┐  ┌────▼─────────────────────────────┐
   │ saas.db   │  │ scheduler / worker pool (saas.worker)│
   │ users,    │  │  - picks tenants with work due      │
   │ channels, │  │  - runs the ENGINE in a subprocess  │
   │ usage...  │  │    with CLIPPER_ROOT=workspaces/<id>│
   └───────────┘  └────┬─────────────────────────────┘
                       │ unchanged engine: vod.poll_all → pipeline.run_once
                ┌──────▼───────────────────────────────┐
                │ workspaces/<tenant>/                   │
                │   config/streamers.json, profiles/     │  ← written by the web app
                │   data/state/clipper.db, ready_clips/  │  ← written by the engine
                └──────┬───────────────────────────────┘
                       │ finished clip
                 Cloudflare R2 (clips, thumbs; signed URLs, 30-day retention)
```

### Why "workspace per tenant" (and not a fork)

The engine is single-tenant by design: every path comes from `CLIPPER_ROOT` (`clipper/config.py`), the state lives
in one SQLite file per root, streamers come from `config/streamers.json`. Pointing `CLIPPER_ROOT` at a per-tenant
folder gives each user an isolated, complete engine with **zero changes to the pipeline**. The SaaS layer only:

1. writes the tenant's channels and profile into that folder (`config/streamers.json`, `config/profiles/*.json`),
2. runs the engine for that folder in a short-lived subprocess when work is due,
3. reads the tenant's `clipper.db` back (read-only) to list clips, and meters usage.

Every improvement to `clipper/` on `main` lands in the SaaS on the next merge.

### Components

| Part | MVP | Later |
|---|---|---|
| Web app | FastAPI + server-rendered pages / one static HTML app (same style as the current dashboard) | Next.js front end if needed |
| Control DB | SQLite (WAL) via stdlib `sqlite3` | Postgres when a second server is added |
| Auth | email + password (stdlib `scrypt`), random session token in an HttpOnly cookie, CSRF via SameSite=Lax + same-origin check | magic-link email, Google login, Twitch/YouTube OAuth for "verified owner" |
| Job queue | `jobs` table + a polling worker (`saas.worker`), N parallel slots = cores / 4 | Redis + RQ/Arq, separate GPU worker pool |
| Engine run | `python -m saas.run_tenant <id> --mode vod` → sets `CLIPPER_ROOT`, imports `clipper`, runs VOD scan + processes pending candidates, exits | per-tenant long-running live workers (paid tiers) |
| Whisper + FFmpeg | CPU on the same box (`large-v3-turbo` int8, `X264_PRESET=veryfast`) | GPU worker (RunPod / Modal / Vast) for Whisper + NVENC |
| Clip storage | local disk, served through an authenticated endpoint | Cloudflare R2 (S3 API, free egress), signed URLs |
| AI keys | **paid** Gemini API key + **Anthropic API key**, held by the SaaS, never shown to users | per-tenant spend caps, batch API for scoring |
| Billing | (milestone 3) Polar checkout links + webhook → `plan` / `period_end` on the user | Gumroad adapter as fallback |
| Email | none in MVP | Resend / Brevo free tier for verification + "clips ready" |

### Live vs VOD

VOD mode (scan the channel's past broadcasts every 30 min after a stream ends, use YouTube Most Replayed, chat
replay, Twitch viewer clips, audio peaks) is cheap: only the flagged sections are downloaded. **Live mode** records a
1080p ring buffer for the whole stream (~3 GB/hour ingress, 1 ffmpeg process, CPU for chat parsing) and is what
makes 24/7 cost grow. So: VOD for every plan, live recording only on Creator/Pro with a cap on concurrent live
channels per server.

### Engine changes needed (small, upstream-friendly, go to `main` too)

1. `processor/ai.py`: when `ANTHROPIC_API_KEY` is set, call Claude through the official `anthropic` SDK instead of
   the `claude` CLI. The CLI + personal subscription path stays for self-hosters. Model from `CLAUDE_MODEL`
   (`sonnet` → `claude-sonnet-5-5`). A personal Claude subscription may not be used to serve paying customers.
2. `processor/ai.py`: optional `GEMINI_MEDIA_RESOLUTION=low` for the proxy upload (roughly 3x fewer video tokens).
3. Nothing else: TryPost and `EXPORT_DIR` are already optional (clips stay `ready` with a schedule slot).

## 3. Cost per clip

Assumptions: a candidate excerpt is ~90 s; Gemini rejects ~75% of candidates, so **~4 Gemini verifications per
finished clip**; Gemini Flash paid input ~$0.375-0.75 per 1M tokens (intro price through 2026-12-31, then higher;
check ai.google.dev/gemini-api/docs/pricing before launch); video at 1 fps ≈ 290 tokens/s at default resolution,
~100 tokens/s at low resolution. Claude Sonnet 5.5 is $2 / $10 per 1M tokens.

| Step | Per finished clip |
|---|---|
| Gemini verify, 4 × 90 s × 290 tok/s ≈ 105k tokens | $0.04-0.08 (≈ $0.015-0.03 with low resolution) |
| Gemini scoring (text, ~3k tokens × 2 incl. rejects) | < $0.005 |
| Claude final check (~2.5k in / 250 out, passing clips only) | ≈ $0.008 |
| CPU: Whisper (~20 s) + render (~90 s) + downloads, on a $40 8-vCPU box doing ~3,000 clips/month | ≈ $0.013 |
| R2 storage, 30 MB × 30 days | ≈ $0.0005 |
| **Total** | **≈ $0.05-0.10** (target ≤ $0.06 with low-res proxies) |

Live recording adds roughly $0.01-0.02 per stream-hour of CPU and bandwidth on a flat-rate server; it is capped
per plan rather than metered.

### Fixed monthly costs at launch

| Item | $/month |
|---|---|
| Hetzner CCX23 / CPX41 class (8 vCPU, 16-32 GB RAM, 240 GB disk) | 30-45 |
| Cloudflare (DNS, proxy, R2 first 10 GB free) | 0-2 |
| Domain (virlo.studio, already owned) | ~1 |
| Polar | 0 fixed (4% + $0.40 per payment) |
| **Total** | **≈ $35-50** |

One box handles ~3,000 clips/month (≈ 40-60 paying users at average use) before a GPU worker is needed.

## 4. Pricing and margins

| Plan | Price | Clips / month | Channels | Live recording | Extras |
|---|---|---|---|---|---|
| Free trial | $0 | 5 (once) | 1 | no | watermark "made with AutoClipper" |
| **Starter** | $19 | 30 | 1 | no | VOD mode, all layouts, captions, schedule |
| **Creator** | $49 | 120 | 3 | 1 live channel | campaign rules, custom profile, Drive export |
| **Pro** | $99 | 300 | 10 | 3 live channels | priority queue, TryPost auto-posting, API |

Unused clips don't roll over. Overage: $5 per extra 20 clips (Polar one-time product).

| Plan | Revenue | Polar fee | AI + compute (at $0.08/clip, full use) | Gross margin |
|---|---|---|---|---|
| Starter | $19 | $1.16 | $2.40 | **81%** |
| Creator | $49 | $2.36 | $9.60 + ~$3 live | **70%** |
| Pro | $99 | $4.36 | $24 + ~$8 live | **63%** (typical use ~60% of quota → ~78%) |

Break-even on the $50 server: **3 Starter users**. "Clip" = a finished, rendered clip; rejected candidates are free
for the user but count toward a hidden fair-use cap (e.g. 15 candidates per clip of quota) so a dead channel can't
burn AI spend.

## 5. Quotas and abuse limits (enforced by the SaaS, not the engine)

- Clips per billing period (rendered clips counted from the tenant's `clipper.db`).
- Candidates per period (AI spend guard) and a global daily AI-spend kill switch (`MAX_DAILY_AI_USD`).
- Channels per plan, live channels per plan, concurrent jobs per tenant = 1.
- Max VOD age scanned: 7 days; max VOD windows per stream: from profile (`max_vod_windows`).
- Disk: tenant workspace capped (raw clips deleted after processing, finished clips moved to R2, 30-day retention).
- Trial: email verification + 1 trial per email domain/IP block to stop farming.

## 6. Legal and copyright stance

You chose **"anyone, any channel"**. That is how OpusClip, Vizard and similar tools work (paste any link), and it
is lawful for the tool itself, but the user is the one publishing, so the terms and the product have to make that
responsibility explicit. Measures:

1. **Terms of Service + Acceptable Use**: the user warrants they own the stream or have the right to clip it
   (their own channel, written permission, a paid clipping campaign such as Content Rewards, or another basis they
   are responsible for). Repost farming of a creator who objects is prohibited. Users indemnify the service.
2. **Rights attestation per channel**: adding a channel requires picking a basis ("my channel", "I have
   permission", "clipping campaign: link", "other, I take responsibility"); stored with timestamp + IP.
   "My channel" can later be verified with Twitch/YouTube OAuth for a "verified owner" badge.
3. **Creator opt-out list**: any streamer can ask (form + email) not to be clipped; their channels are blocked for
   every tenant (`blocked_channels` table checked when a channel is added and before every engine run).
4. **DMCA / takedown process**: public `/dmca` page with a designated contact email, a form for notices, removal of
   the clip within 48 h, notice forwarded to the user, counter-notice path, and a **repeat-infringer policy**
   (3 valid notices → account terminated). Optionally register a DMCA agent with the US Copyright Office ($6) to
   strengthen safe-harbor position for US-hosted content.
5. **Credit by default**: captions credit the streamer (the engine already does this); users can't turn it off on
   clips of channels they didn't verify as their own.
6. **No posting on the user's behalf in MVP**: we render and store; the user downloads and posts. TryPost
   auto-posting comes later, only to the user's own connected accounts.
7. **Payment-provider risk**: Polar (like every merchant of record) can drop products that facilitate
   infringement. Before launch, read Polar's acceptable-use policy and describe the product as a clip editor for
   your own streams and clipping campaigns (which is true). Marketing should lead with own-channel and campaign use,
   never with "repost big streamers". Keep the Gumroad adapter ready.
8. **Platform ToS**: downloading with yt-dlp/streamlink is a grey area under YouTube/Twitch terms (the same for
   every clipper tool). Keep request volume low (VOD sections only, no full downloads), cache, and respect 429s.
9. **Privacy**: GDPR-style privacy policy (what we store, retention 30 days for clips, deletion on request);
   Gemini paid tier does not use inputs for training, which also fixes the free-tier caveat in the README.

*Not legal advice; have the ToS/AUP reviewed once there is revenue.*

## 7. MVP scope (milestone 1, this branch)

In:
- `saas/` package: control DB (users, sessions, tenants, channels, jobs, usage, blocked_channels), FastAPI app.
- Signup / login / logout (email + password), per-user tenant + workspace folder.
- Add / pause / remove a channel (Twitch, Kick, YouTube) with the rights attestation and opt-out check.
- Worker: for each tenant with active channels, every 30 min run the engine (VOD mode) in a subprocess with
  `CLIPPER_ROOT=workspaces/<tenant>`; one job per tenant at a time, N global slots.
- Clips page: the tenant's clips (status, score, hook, caption, schedule slot), authenticated download.
- Plan field + clip quota check before each run (no payments yet; plans set by an admin command).
- Engine: Anthropic API backend in `ai.py` (§2).
- Docs: how to run the SaaS locally (`python -m saas`), env vars, nothing secret in git (`saas.env` ignored).

Out (later milestones): payments, R2, live mode, email, OAuth verification, admin UI, auto-posting.

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| M1 | **MVP skeleton** (this branch) | two test users each add a channel and see only their own clips, produced by the unchanged engine |
| M2 | Deploy + storage | runs on the Hetzner box behind Cloudflare; clips on R2 with signed URLs; backups of `saas.db` |
| M3 | Billing | Polar products for 3 tiers + overage; webhook sets plan/period; quota resets per period; trial |
| M4 | Legal + trust | ToS, AUP, privacy, `/dmca` page + notice form, opt-out form, repeat-infringer counter |
| M5 | Live mode | per-tenant live workers for Creator/Pro, concurrency caps, live status in the dashboard |
| M6 | Growth | landing page, onboarding emails, referral codes, verified-owner OAuth, TryPost auto-posting |
| M7 | Scale | Postgres, Redis queue, GPU worker pool, second box |

Rough effort: M1 2-3 days, M2 1 day, M3 1-2 days, M4 1 day, M5 2 days.

## 9. Secrets and the public repo

- The repo goes public: **no keys in git**. SaaS secrets live in `saas.env` (gitignored) or the server's env:
  `GEMINI_API_KEY` (paid project), `ANTHROPIC_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_WEBHOOK_SECRET`,
  `SESSION_SECRET`, R2 keys. A `saas.env.example` with empty values is committed.
- Tenant workspaces and `saas.db` live under `saas_data/` (gitignored), never in `data/`.
- Your local setup (`.env`, `data/`, `config/streamers.json`) is not touched by anything on this branch.

## 10. Open questions for later

- Product name on virlo.studio: "Virlo Studio" (clipper as part of Virlo) or keep "AutoClipper Cloud" as the name.
- Whether the open-source README's "Hosted version: waitlist" link should point to the landing page at M2.
- Trial watermark: yes/no.
