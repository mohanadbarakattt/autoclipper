# Clips inside Virlo Studio: integration contract

Decision (2026-11-21): the clipper is a **Clips tool inside Virlo Studio** (virlo.studio), not a separate site.
Virlo Studio (Next.js on Vercel, repo `virlo-studio`) owns accounts, login, points (نقط), payments
(Paymob / Paddle) and every screen. This repo's `saas/` package runs as the **clipping service** on the server
that can run Whisper + FFmpeg (Oracle free VM at first), reachable at e.g. `https://clips-api.virlo.studio`.

```
 browser ──► virlo.studio (Vercel, Next.js)
               │  /studio/clips pages, /api/clips/* (requireUser → user id)
               │  server-to-server, Bearer CLIPS_SERVICE_TOKEN
               ▼
        clips service (saas/, SAAS_MODE=service, BILLING=virlo)
               │  per-user engine workspace, worker, quotas
               │  before a run:  POST virlo /api/internal/clips/allowance  {userId}
               │  per clip:      POST virlo /api/internal/clips/charge     {userId, clipId, idempotencyKey}
               ▼
        clipper engine (unchanged)          browser ◄── signed media links (video / thumb / download)
```

## Why this split
- Vercel can't run multi-minute FFmpeg/Whisper jobs or hold large video files; the VM can.
- Videos go straight from the service to the browser through **signed, expiring links**, never through Vercel
  (Vercel function responses are size-limited).
- One account, one wallet: clips are paid in the same points as every other Virlo tool.

## Auth
- Virlo → service: `Authorization: Bearer $CLIPS_SERVICE_TOKEN` on every `/v1/*` call. The user is identified by
  Virlo's user id in the path (`/v1/users/{userId}/…`); the service trusts it because only Virlo holds the token.
- Service → Virlo: same bearer token on `/api/internal/clips/*`.
- Browser → service: only `/media/{uid}/{clip}/{kind}?exp=&sig=` links, HMAC-signed with `SAAS_MEDIA_SECRET`,
  valid 6 h, issued by the service inside `/v1` responses.

## Points
- `config/clips.json` in Virlo: `pointsPerClip: 15` at `sellPricePerCreditUsd 0.01` = $0.15 per clip, against
  an all-in cost of ≤ $0.045 per finished clip → 3.3x, exactly Virlo's margin rule (a Vitest test enforces it;
  keep `GEMINI_MEDIA_RESOLUTION=low` on the service to stay under that cost).
- Free tier (15 points/day, capped at 200/month) = 1 clip a day; Creator (1,200/mo) = 80 clips; Pro (3,500)
  ≈ 233; Studio (10,000) ≈ 666.
- Only **finished** clips cost points. Rejected moments are free for the user (capped per month by tier inside
  the service so a dead channel can't burn AI spend).
- **Allowance** before each run: `floor(balance / pointsPerClip)` (Virlo answers). The run stops at that many clips.
- **Charge** per finished clip, idempotent (`clip:{serviceUserId}:{clipId}`), via the existing ledger `debit`.
  If the balance ran out in the meantime (402), the clip is kept but **locked** (no download) and the charge is
  retried every few minutes; it unlocks as soon as the user tops up.

## Service API (`/v1`, JSON)
| Method & path | What |
|---|---|
| `PUT /v1/users/{uid}` `{email?, tier}` | create/update the user (tier: free/creator/pro/studio/business) |
| `GET /v1/users/{uid}/overview` | counts, scan status, channels, latest clips |
| `GET /v1/users/{uid}/channels` · `POST` `{link, platform?, style, rights, rightsNote?}` | list / add channel |
| `PATCH /v1/users/{uid}/channels/{id}` `{style?, picky?, paused?}` · `DELETE` | edit / remove channel |
| `POST /v1/users/{uid}/scan` | scan now |
| `GET /v1/users/{uid}/clips?status=&q=` · `GET …/clips/{cid}` | library / one clip (with signed media links when unlocked) |
| `PATCH …/clips/{cid}` `{title, caption, hashtags}` · `POST …/clips/{cid}/posted` `{url}` · `DELETE …/clips/{cid}` | edit / mark posted / delete |
| `GET /v1/users/{uid}/activity` | moments checked + scans |
| `GET /media/{uid}/{cid}/{video|thumb|download}?exp=&sig=` | signed media (no bearer) |

Errors: `{"error": code, "message": friendly English}`; Virlo maps codes to Egyptian Arabic copy.

## Virlo side (repo `virlo-studio`, branch `clips`)
- `config/clips.json`, `src/lib/clips/` (service client, tier limits, margin test).
- `app/api/clips/**` route handlers (requireUser → service).
- `app/api/internal/clips/allowance` + `charge` (bearer check → ledger `getBalance` / `debit`).
- `app/(studio)/studio/clips/**` pages + `components/StudioClips*.tsx`, nav link "كليباتي".
- Env on Vercel: `CLIPS_SERVICE_URL`, `CLIPS_SERVICE_TOKEN`.

## Status (2026-11-22)
- Service side: `saas/api.py`, `saas/billing.py` on branch `saas` of this repo (30 Python tests).
- Virlo side: commit `abb97ad` on branch `clips` of `virlo-studio` (local clone `C:\Users\user\virlo-studio`,
  not pushed). Typecheck + lint clean, 9 new Vitest tests; the existing suite passes (two PGlite tests can time out
  under full parallel load and pass alone, unrelated).
- Checked end to end locally (Virlo dev in DEMO_MODE + service): sign up → Clips page → add channel (user
  auto-registered, scan queued) → seeded finished clips → allowance 1 → first clip charged 15 points through the
  real ledger, second locked (402) → video plays from a signed link; locked clip shows "top up".
- Not yet done: a real stream clipped and rendered through the whole chain (needs paid Gemini/Anthropic keys and
  the server), deploying the service (Oracle VM + Cloudflare Tunnel), setting `CLIPS_SERVICE_URL` /
  `CLIPS_SERVICE_TOKEN` on Vercel, merging `clips` in virlo-studio.
