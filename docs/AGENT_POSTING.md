# Let an AI agent post your clips (Grokbot, OpenBot, ...)

AutoClipper doesn't need platform API keys for this: an agent that can **run commands on your PC** and
**use your browser** reads the posting queue from AutoClipper and uploads each clip through YouTube Studio
(or TikTok / Instagram) in the browser where you're already signed in.

## The posting queue

While AutoClipper runs, it serves:

```
GET  http://localhost:8090/api/posting-queue?platform=youtube&limit=3
```

```json
{"platform": "youtube", "count": 1, "items": [{
  "id": 87,
  "file": "C:\\Users\\you\\AutoClipper\\data\\ready_clips\\clip_87.mp4",
  "title": "Adin Ross insults NPC wife and immediately regrets it #Shorts",
  "description": "Adin couldn't stop laughing...\n\n#gta6 #adinross #gaming",
  "post_at_local": {"date": "2026-11-22", "time": "19:00", "pretty": "Sun 22 Nov 2026, 07:00 PM"},
  "in_past": false,
  "mark_posted": "POST /api/clips/87/posted  body: {\"url\": \"<link to the youtube post>\"}"
}]}
```

Clips already posted on that platform are skipped, so the same queue works for YouTube, then TikTok
(`platform=tiktok`), then Instagram (`platform=instagram`). After an upload the agent reports the link with
`POST /api/clips/{id}/posted` (`{"url": "..."}`). AutoClipper then tracks the views and learns from them.

## Grokbot routine (copy-paste)

Turn on Grokbot's local tools and computer use, open Chrome **signed in to the YouTube channel you post to**,
then paste:

```
You post my AutoClipper clips to my YouTube channel. Set this up as a routine that runs
every day at 10:00 and whenever I say "post my clips".

1. Run on my PC:  curl -s "http://localhost:8090/api/posting-queue?platform=youtube&limit=3"
   If it fails, AutoClipper isn't running: tell me and stop.
2. For each item, in my Chrome (already signed in to my channel), go to https://studio.youtube.com
   → Create → Upload videos → choose the file at item.file.
   - Title: item.title (exactly). Description: item.description (exactly).
   - Audience: "No, it's not made for kids".
   - Visibility: if item.in_past is true choose Public; otherwise choose Schedule and set
     item.post_at_local.date and item.post_at_local.time.
   - Wait until the upload finishes processing, then copy the video link
     (https://youtube.com/shorts/... or https://youtu.be/...).
3. After each successful upload run:
   curl -s -X POST "http://localhost:8090/api/clips/<item.id>/posted" -H "Content-Type: application/json" -d "{\"url\": \"<the link>\"}"
4. Never upload the same item.id twice. If YouTube shows a copyright claim, a "reused content" warning,
   or any error, stop and tell me instead of retrying.
5. When done, send me a short summary: title, scheduled time and link for each clip.
```

**Tips**
- Start with `limit=1` and watch the first run.
- YouTube limits new channels to a small number of uploads per day (and asks for phone verification to
  schedule or upload longer videos). Verify the channel once at https://www.youtube.com/verify.
- The same routine works for TikTok (`tiktok.com/upload`) and Instagram (`instagram.com` → Create → Reel):
  change `platform=` in step 1 and the upload page in step 2.
- Only post clips you have the right to post (your own streams, streamers who allow clipping, or paid
  clipping campaigns), and credit the streamer. The caption does this by default.
