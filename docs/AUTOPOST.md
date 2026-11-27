# Hands-off auto-posting (Docker + self-hosted TryPost)

> Optional. Easy mode (`start.bat` / the one-line installer) already gives you finished clips, a posting
> schedule and a Google Drive folder. Use this only if you want clips published automatically.

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free for personal use), Python 3.10+, Git.

```bash
git clone https://github.com/mohanadbarakattt/autoclipper && cd autoclipper
python scripts/setup.py --public-url https://post.yourdomain.com
```

1. **Gemini**: put your free key in `.env` → `GEMINI_API_KEY=`.
2. **Claude (your subscription, no API key)**:
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
6. **Social apps**: follow [SOCIAL_SETUP.md](SOCIAL_SETUP.md). Put the client IDs/secrets in
   `trypost.env`, run `docker compose up -d trypost`, then connect your accounts inside TryPost.
7. **TryPost API key**: TryPost → Settings → API → Generate. Put it in `.env` → `TRYPOST_API_KEY=` and run
   `docker compose up -d clipper`.
8. **Add streamers**: open http://localhost:8090 and add channels, or edit `config/streamers.json`.

From now on it runs by itself and restarts after reboots (Docker Desktop: enable *Start when you sign in*).

The dashboard (8090) and TryPost (8000, 8081) answer only on this computer. A Cloudflare Tunnel or ngrok
still reaches TryPost. To open them from your network, see `DASHBOARD_HOST` and `TRYPOST_HOST` in
[.env.example](../.env.example).

## OpenBot (optional manager)
Paste the prompt in [../openbot/ROUTINES.md](../openbot/ROUTINES.md) into OpenBot for a daily digest,
self-healing and chat-style channel management.
