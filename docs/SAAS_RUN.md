# Running Virlo Studio (hosted AutoClipper) locally

The `saas/` package is the multi-user web app. It reuses the `clipper/` engine unchanged: each user gets a
workspace folder, and the engine runs on it in its own process (`python -m saas.run_tenant`).

## Start

```bash
python -m venv .venv-saas
.venv-saas/Scripts/python -m pip install -r requirements.txt -r saas/requirements.txt   # Windows path; bin/ on Linux
cp saas.env.example saas.env        # add a paid Gemini key (and an Anthropic key for paid plans)
.venv-saas/Scripts/python -m saas   # web + worker on http://localhost:8100
```

Other modes: `python -m saas web`, `python -m saas worker` (run them as two services in production).

Admin commands:

```bash
python -m saas set-plan someone@example.com creator     # free | starter | creator | pro (until payments land)
python -m saas block-channel twitch somestreamer "opt-out request"   # creator opt-out / takedown
```

## What happens

1. Sign up → onboarding: paste a channel link, pick a style, confirm rights.
2. The worker queues a scan right away, then every `SAAS_SCAN_MINUTES` while the user has clips left.
3. A scan = `vod.poll_all()` + `pipeline.run_once()` in the user's workspace, capped by the plan's remaining
   clips and AI checks. Finished clips and checked moments are recorded in `usage` (the quota source).
4. Clips, activity and channel status are read straight from the user's engine database.

## Layout

```
saas/
  settings.py     paths, env, plans (Free 10 / Starter 40 / Creator 120 / Pro 300 clips)
  store.py        accounts DB: users, sessions, channels, jobs, usage, blocked_channels
  auth.py         scrypt passwords, hashed session tokens, rate limiter
  workspace.py    channel links, engine config sync, reading clips, file access checks
  run_tenant.py   one engine run for one workspace (own process: CLIPPER_ROOT is per user)
  worker.py       scheduler + job pool, quota checks, retries with backoff
  web.py          FastAPI routes; templates/ and static/ hold the UI
saas_data/        (gitignored) saas.db + workspaces/u<id>/
```

## Tests

```bash
.venv-saas/Scripts/python -m pytest tests -q
```

Covers auth, channel parsing, plan limits, quota accounting, workspace sync, opt-out blocking, per-user clip
isolation (another user's clip or file is a 404), path checks and the cross-site POST guard.
