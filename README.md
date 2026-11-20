# AutoClipper

Self-hosted, **$0/month** stream clipper. It watches Twitch, Kick and YouTube streamers (live and past
VODs), finds the moments chat or viewers went crazy for, turns them into 1080×1920 vertical clips with
word-by-word karaoke captions and a hook banner, and schedules them to YouTube Shorts, Instagram Reels
and TikTok through a self-hosted [TryPost](https://github.com/trypostit/trypost).

```
 Twitch / Kick / YouTube
        │  live: streamlink → 30-min ring buffer      VOD: "Most Replayed" heatmap / viewer clip clusters / audio peaks
        ▼
 cheap local signals (chat velocity, emotes, clip clusters, loudness)  ← filters out ~95% before any AI runs
        ▼
 Gemini (free key) watches a 480p · 1fps proxy → is it viral? exact cut points, facecam box
        ▼
 faster-whisper transcript → Claude (your Claude subscription via Claude Code) scores 1-10 + writes the hook
        ▼
 FFmpeg: stacked facecam / speaker crop / blur-fit + karaoke captions + 3.5 s hook pill
        ▼
 TryPost (self-hosted) schedules to Shorts, Reels, TikTok with spacing + daily caps
        ▲
 OpenBot (optional): daily health digest, auto-restarts, "add xqc on twitch" in plain English
```

## What it costs

| Piece | Cost |
|---|---|
| Gemini 2.5 Flash | **$0** with a free [AI Studio key](https://aistudio.google.com/apikey) (free tier, no billing; ~10 req/min). Note: Google may use free-tier inputs to improve its models. |
| Claude | **$0 extra**: runs `claude -p` (Claude Code headless) on the Claude Pro/Max plan you already pay for. No Anthropic API key. If Claude isn't set up, Gemini does the scoring. |
| TryPost, FFmpeg, faster-whisper, yt-dlp, streamlink | **$0**, open source, run locally |
| Twitch / Kick developer apps | **$0** (optional) |
| Public HTTPS for OAuth callbacks | **$0**: Cloudflare Tunnel (needs a domain you already own) or an ngrok free static domain |

Hardware: any PC that can stay on. CPU-only is fine (a clip takes a few minutes to render). Budget ~20 GB disk.

## Easy mode (Windows, 2 minutes)

No Docker, no TryPost, no developer accounts. Clips land in the dashboard with **Download** and **Copy caption** buttons; you post them yourself.

1. Download this repo (Code → Download ZIP, or `git clone`) and double-click **`start.bat`**.
2. First run installs everything, then asks you to paste a free Gemini key (it opens the page for you).
3. If Claude Code is installed, it checks you're logged in to Claude and helps you log in once. Without it, Gemini writes the hooks.
4. Say **Y** to "start with Windows". The dashboard opens at http://localhost:8090. Add streamers there.

Want hands-off posting later? Follow the full setup below to add TryPost.

## Full setup with auto-posting (Docker + TryPost)

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free for personal use), Python 3.10+, Git.

```bash
git clone https://github.com/<you>/autoclipper && cd autoclipper
python scripts/setup.py --public-url https://post.yourdomain.com
```

1. **Gemini**: put your free key in `.env` → `GEMINI_API_KEY=`.
2. **Claude (subscription, no API key)**:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude setup-token
   ```
   Paste the token into `.env` → `CLAUDE_CODE_OAUTH_TOKEN=`. Usage counts against your plan's normal limits.
3. **Start everything**: `docker compose up -d --build`
4. **TryPost first run**:
   ```bash
   docker compose exec trypost php artisan db:seed --force
   docker compose exec trypost php artisan db:seed --class=UserSeeder --force
   ```
   Open http://localhost:8000, log in as `admin@trypost.it` / `password`, **change the password**.
5. **Public URL** (needed for YouTube/Instagram/TikTok login): point a Cloudflare Tunnel or ngrok static
   domain at `localhost:8000`. With Cloudflare: put `CLOUDFLARE_TUNNEL_TOKEN=` in `.env`, then
   `docker compose --profile tunnel up -d`.
6. **Social apps**: see [docs/SOCIAL_SETUP.md](docs/SOCIAL_SETUP.md). Put the client IDs/secrets in
   `trypost.env`, run `docker compose up -d trypost`, then connect your accounts inside TryPost.
7. **TryPost API key**: TryPost → Settings → API → Generate. Put it in `.env` → `TRYPOST_API_KEY=` and run
   `docker compose up -d clipper`.
8. **Add streamers**: open http://localhost:8090 and add channels, or edit `config/streamers.json`.

That's it. From now on it runs by itself and restarts after reboots (Docker Desktop: enable *Start when you sign in*).

### OpenBot (optional manager)
Paste the prompt in [openbot/ROUTINES.md](openbot/ROUTINES.md) into OpenBot for a daily digest,
self-healing and chat-style channel management.

## Niche profiles

Each streamer uses a profile in `config/profiles/` (`gaming`, `gta6`, `podcast`, `default`). A profile
controls the topic filter, layout, clip length, score gate, posting cadence, emote weights and caption
style. Copy one to start a new niche. Every key you leave out uses the default in `clipper/config.py`.

| Key | Meaning |
|---|---|
| `game_or_topic_filter` | Only clip when the category/title matches (`A\|B\|C`). Empty = everything. |
| `layout_mode` | `auto`, `stacked_facecam` (webcam top 35% + gameplay), `speaker_crop`, `blur_fit` |
| `min_viral_score` | Claude's score gate (1-10) |
| `max_posts_per_day`, `post_spacing_hours` | Posting cadence per profile |
| `publish.mode` | `scheduled` (auto-post), `draft` (lands in TryPost for you to approve), `off` |
| `publish.tiktok_privacy` | Keep `SELF_ONLY` until TikTok approves your app audit, then `PUBLIC_TO_EVERYONE` |

## Test without any streams

```bash
pip install -r requirements.txt
python scripts/get_fonts.py
python -m clipper process-file path/to/any_clip.mp4 gaming
```
Runs Gemini → Claude → render on a local file and prints where the vertical MP4 landed.
Or queue any YouTube/Twitch VOD section from the dashboard API: `POST /api/manual {"url","start","end"}`.

## Platform notes

- **Twitch**: anonymous chat + streamlink. Add free Twitch app keys for the category filter and the viewer clip-cluster signal (also used to find peaks in finished VODs).
- **YouTube**: live chat via chat-downloader. Finished streams use YouTube's "Most Replayed" heatmap, so only the peak minutes are ever downloaded.
- **Kick**: live only. Live status comes from Kick's official public API (free app keys) or streamlink. Highlights are found from audio loudness spikes, since Kick has no public chat feed without webhooks.
- Full VODs are never downloaded. Live buffers keep 30 minutes and overwrite themselves.

## Use it responsibly

Reposting someone else's stream is their content. Clip streamers who allow it (many run clipping
programs and pay clippers), credit them (the caption includes the streamer's name), and expect
YouTube and TikTok to demonetize or strike unoriginal reuploads. You are responsible for what your
accounts post. Start with `publish.mode: "draft"` until you trust the output.

## Layout

```
clipper/
  ingest/      live.py (per-streamer worker) · ringbuffer.py · spikes.py · platforms.py · vod.py
  processor/   ai.py (Gemini + Claude CLI) · render.py (whisper + ASS + ffmpeg) · publish.py (TryPost) · pipeline.py
  dashboard/   app.py (FastAPI) · static/index.html
config/        streamers.json · profiles/*.json
data/          state/clipper.db · ring_buffers · candidates · ready_clips
```

MIT licensed. TryPost is AGPL-3.0 and runs as its own container.
