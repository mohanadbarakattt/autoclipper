# OpenBot overseer routines

AutoClipper already runs on its own (Docker restarts it, it cleans up after itself every 6 h).
OpenBot sits on top as the "manager": daily report, health fixes, and plain-English channel control.

Paste this into your OpenBot workspace once (replace the path with where you cloned the repo):

```
You manage my AutoClipper install at C:\path\to\autoclipper (dashboard API: http://localhost:8090,
TryPost: http://localhost:8000). Set up two persistent routines:

1. Daily health & digest (09:00 every day)
   - GET http://localhost:8090/api/health and http://localhost:8090/api/clips?limit=100
   - If the API doesn't answer, or `docker compose ps` (run inside the repo folder) shows a service
     that is not "running"/"healthy", run `docker compose up -d` there, wait 60 s and check again.
   - If disk_free_gb < 15, tell me before anything else.
   - Send me a short digest: clips rendered / scheduled / rejected in the last 24 h, the top 3 hooks
     with their scores, any status "failed" or "publish_failed" with the error, TryPost account count,
     and disk usage. Do NOT delete or re-publish anything without asking me.

2. Channel manager (whenever I ask)
   When I say things like "add xqc on twitch with the gaming profile", "pause kick:adinross",
   "remove @somepodcast", or "switch twitch:foo to the gta6 profile", use the dashboard API:
     add:    POST   http://localhost:8090/api/streamers        {"platform","channel","profile"}
     pause:  POST   http://localhost:8090/api/streamers/{platform}:{channel}/toggle
     edit:   PATCH  http://localhost:8090/api/streamers/{platform}:{channel}  {"profile": "..."}
     remove: DELETE http://localhost:8090/api/streamers/{platform}:{channel}
   Then confirm with GET /api/streams and tell me the result.
```

Optional: TryPost also exposes an MCP server at `http://localhost:8000/mcp/trypost` (OAuth sign-in),
so OpenBot or Claude can read post status and analytics directly.

Run OpenBot on the same computer: the dashboard and TryPost answer only on `localhost` by default. If it
runs on another machine, see [who can open the dashboard](../README.md#who-can-open-it) (and `TRYPOST_HOST`
in `.env.example`).
