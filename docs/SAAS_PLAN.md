# Virlo Studio: the hosted AutoClipper

Status: **draft v2 for approval** · branch `saas` · 2026-11-21 · website: **virlo.studio**

Virlo Studio is AutoClipper as a website. It works exactly like our AutoClipper (same engine, same clips), with a
proper user interface on top: sign up, paste a channel, get ready-to-post vertical clips. Nothing to install, no
API keys, no PC left running.

Goals, in order:
1. **Same engine, same results**: reuse `clipper/` unchanged; every engine improvement ships to the site.
2. **Great UI and functionality**: simple enough for a streamer who has never edited a clip.
3. **Cheap to use, generous free plan**, priced so every plan, free included, pays for itself.
4. **Robust and sustainable**: fails safely, can't run up an AI bill, cheap to keep running.

## 1. Decisions

| Topic | Decision |
|---|---|
| Who can use it | Anyone, any channel, with rights confirmation, a creator opt-out list and a takedown process (§7) |
| Plans | Free forever + $9 / $19 / $39 (§5) |
| Payments | Polar (lists Egypt as a payout country); Gumroad as fallback behind the same adapter |
| Website | `virlo.studio` (landing + pricing), `app.virlo.studio` (the app) |
| Hosting | one cheap server to start (§8); not a focus of this plan |

## 2. The user's journey

1. **Lands on virlo.studio**: demo clip playing, "Paste your channel, get viral clips", pricing, sign up free.
2. **Signs up**: email + password (Google sign-in later). No card for the free plan.
3. **Onboarding wizard (3 steps, under a minute)**
   1. Paste a Twitch / Kick / YouTube link → platform and channel detected automatically, avatar shown.
   2. Pick a style: **Gaming**, **GTA 6**, **Podcast / Talk**, **Arabic**, **General** (the existing profiles), with a
      preview of the caption look.
   3. Confirm rights ("my channel" / "I have permission" / "clipping campaign" / "other, my responsibility").
   → "We're scanning your latest streams. First clips usually arrive in 20-40 minutes." Progress shown live.
4. **Gets clips**: an email/notification when clips are ready; opens the clip library.
5. **Posts**: download, copy caption, mark as posted + paste the link (views are tracked, and future clips are scored
   to match what performed).
6. **Upgrades** when the free clips run out or they want more channels, live recording or campaign rules.

## 3. Screens

| Screen | What's on it |
|---|---|
| **Home / dashboard** | clips used this month (quota bar), channel cards (live now / last scanned / next scan), the latest 6 clips, a "what's happening" line (e.g. "Checking 3 moments from yesterday's stream") |
| **Clip library** | grid of vertical previews that play on hover/tap; filters: Ready, Scheduled, Posted, Rejected; search by hook/streamer; each card: score, hook, length, **Download**, **Copy caption**, **Mark posted** |
| **Clip detail** | full player; editable hook, title, caption, hashtags; **Re-render** with the new hook; "Why this was picked" (AI summary + score reason); source link + timestamp; delete |
| **Schedule** | week view of posting slots (3 a day by default, user's time zone), clips placed in slots, move a clip to another slot, copy caption, download; daily slot times editable |
| **Channels** | add / pause / remove; per channel: style, VOD scanning on/off, live recording on/off (paid), last result |
| **Channel style** (per channel) | caption font / size / colour / words per line with live preview, hook banner on/off, layout (auto / facecam stacked / speaker crop / blur), clip length, a **"Picky ↔ More clips"** slider (maps to `min_viral_score`), language; paid: campaign rules, required hashtags, caption suffix |
| **Activity** | every moment the AI looked at, with the verdict and reason ("rejected: ordinary driving, no reaction"), so users trust it and tune the slider |
| **Discover** (Creator+) | "find top live streams in my niche" (the engine's auto-discover) with one-click add |
| **Account & billing** | plan, usage this period, upgrade / downgrade (Polar checkout), invoices (Polar customer portal), delete account |
| **Admin** (us) | users, plans, clips and AI spend per day, failed jobs + retry, opt-out list, takedown notices |

UI principles: mobile-first (most streamers check on their phone), dark theme matching the current dashboard, every
action one click, plain words (no "candidates" or "profiles" in the UI), empty states that say what happens next.
Arabic UI (RTL) after launch, since the engine already supports Arabic captions.

## 4. What maps to the engine

| Site feature | Engine piece (unchanged) |
|---|---|
| Channels, VOD scanning | `ingest/vod.py` (Most Replayed, chat replay, Twitch viewer clips, audio peaks) |
| Live recording | `ingest/live.py`, `ringbuffer.py`, `spikes.py` |
| AI picking + scoring | `processor/ai.py` (Gemini verify + score, Claude final check) |
| Clip rendering | `processor/render.py` (moving crop, facecam stack, payoff zoom, karaoke captions, hook) |
| Schedule slots | `processor/publish.py` `next_slot` |
| Duplicates, view learning | `processor/learn.py` |
| Styles | `config/profiles/*.json` + per-channel overrides |
| Discover | `ingest/discover.py` |

**How it's reused without forking:** the engine takes all its paths from `CLIPPER_ROOT`. Each user gets a private
folder (`saas_data/workspaces/<user>/`) with their own channels, styles and clip database. The site writes the
user's settings into that folder, runs the engine on it in a separate process when work is due, and reads the
clips back. Users can never see each other's data, and one user's crash can't affect another.

Engine changes (small, also go to `main`):
1. Claude via the **Anthropic API** when `ANTHROPIC_API_KEY` is set (a personal Claude subscription can't serve
   customers); the CLI path stays for self-hosters.
2. Optional low-resolution Gemini video input (about 3x cheaper per clip).
3. A "re-render with a new hook" entry point (for the clip detail screen).

## 5. Plans (cheap, with a real free plan)

| | **Free** | **Starter** | **Creator** | **Pro** |
|---|---|---|---|---|
| Price | $0 forever | **$9/mo** | **$19/mo** | **$39/mo** |
| Clips / month | 10 | 40 | 120 | 300 |
| Channels | 1 | 2 | 5 | 15 |
| Live recording | - | - | 1 channel | 3 channels |
| AI | Gemini only | Gemini + Claude final check | same | same |
| Queue | standard | fast | fast | priority |
| Watermark | small "virlo.studio" | none | none | none |
| Style editor | presets only | full | full | full |
| Campaign rules, hashtags, caption suffix | - | - | yes | yes |
| Discover top streams | - | - | yes | yes |
| Clip history kept | 7 days | 30 days | 60 days | 90 days |

Yearly billing: 2 months free. Extra clips: $3 per 20.

### Why this is sustainable

Cost per finished clip ≈ **$0.03 free plan** (Gemini Flash-Lite, low-res, no Claude) and **≈ $0.05 paid**
(Gemini Flash low-res + Claude final check + CPU + storage). "Clip" means a finished clip; the moments the AI
rejects are free for users but capped behind the scenes so a dead channel can't burn money.

| Plan | Revenue | Polar fee | Cost at full use | Margin at full use | Margin at typical use (~50%) |
|---|---|---|---|---|---|
| Free | $0 | - | $0.30 | -$0.30/user | -$0.15/user |
| Starter | $9 | $0.76 | $2.00 | 69% | 80% |
| Creator | $19 | $1.16 | $6.00 + live | 55% | 72% |
| Pro | $39 | $1.96 | $15.00 + live | 45% | 66% |

With a ~$40/month server, **5 Starter users cover the server**, and every 10 free users cost about $1.50-3 a
month, so one $19 Creator pays for ~60 free users. A global daily AI-spend limit protects against surprises.

## 6. Robustness

- **Isolation**: one folder + one process per user run; crashes stay contained; the engine already resets stuck
  jobs on start.
- **Job queue with retries**: a run is retried up to 3 times with backoff, then shown as "failed" in Activity and in
  Admin with a retry button. A user only ever has one run at a time.
- **AI fallbacks**: Gemini model chain (already in the engine) → Claude final check is optional, so an outage
  degrades quality slightly instead of stopping clips.
- **Spend guards**: per-user monthly clip and moment caps, a free-plan daily cap, and a global `MAX_DAILY_AI_USD`
  kill switch.
- **Quotas checked before work**, not after: no run starts for a user who is out of clips.
- **Disk hygiene**: raw footage deleted after processing (engine), clips deleted after the plan's history window.
- **Backups**: nightly copy of the account database + user settings; clips are re-creatable and not backed up.
- **Health**: `/health` endpoint, failed-job counter, a daily summary email to us.
- **Tests**: unit tests for quotas, auth and workspace sync; one end-to-end test that runs the engine on a sample
  file (`process-file`) inside a test workspace.

## 7. Rights and takedowns (any channel)

- Rights confirmation when adding a channel (stored with time + IP); terms say the user is responsible for what they
  post.
- **Creator opt-out**: any streamer can ask not to be clipped; their channel is blocked for everyone.
- **Takedown page** (`virlo.studio/dmca`): contact + form, clip removed within 48 h, user notified,
  3 valid notices → account closed.
- Captions credit the streamer by default; no auto-posting to social accounts at launch (users post themselves).
- Marketing leads with "clip your streams" and paid clipping campaigns, which also keeps Polar comfortable.
- Privacy policy; the paid Gemini tier doesn't train on inputs.

*Not legal advice; have the terms reviewed once there's revenue.*

## 8. Tech and hosting (short)

- **App**: FastAPI (same as the engine) + server-rendered pages with HTMX and Tailwind: fast, works on phones, no
  front-end build step. JSON API underneath for a future mobile app.
- **Data**: SQLite for accounts (Postgres later if needed); each user's clip database is the engine's own.
- **Auth**: email + password (stdlib `scrypt`), secure session cookie; Google sign-in later.
- **Workers**: a scheduler picks users with work due and runs the engine; parallel runs = CPU cores / 4.
- **Hosting**: one ~$40/month 8-vCPU server behind Cloudflare (free TLS) at launch; clips on Cloudflare R2
  later. A GPU worker only once revenue covers it.
- **Secrets**: never in git (the repo is public). Paid Gemini key, Anthropic key, Polar keys, session secret live in
  `saas.env` (gitignored); `saas.env.example` is committed. User data lives in `saas_data/` (gitignored). Your local
  AutoClipper (`.env`, `data/`, `config/streamers.json`) is untouched.

## 9. Milestones

| # | Milestone | Done when |
|---|---|---|
| **M1** | **Working app (this branch)** | sign up, onboarding wizard, add channel, engine runs per user, dashboard + clip library + clip detail + channels + activity work locally; two test users see only their own clips |
| M2 | Plans + payments | Free/Starter/Creator/Pro limits enforced; Polar checkout + webhook; usage bar; watermark on free |
| M3 | Schedule + style editor | week view, slot editing, caption style live preview, re-render with new hook |
| M4 | Launch | landing page + pricing on virlo.studio, terms/privacy/takedown pages, server live, R2, backups, admin page |
| M5 | Live + Discover | live recording for Creator/Pro, discover screen |
| M6 | Growth | Google sign-in, notifications, referral codes, Arabic UI, verified-owner badge, auto-posting |

## 10. Open questions

- Product name: "Virlo Studio" for everything, or "AutoClipper by Virlo"?
- Free-plan watermark: yes (helps growth) or no?
