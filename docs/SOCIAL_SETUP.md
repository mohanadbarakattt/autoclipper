# Connecting YouTube, Instagram and TikTok to self-hosted TryPost

All three are free. Each needs your **public** TryPost URL (`APP_URL` in `trypost.env`), referred to below as
`https://post.yourdomain.com`. After you change `trypost.env`, run `docker compose up -d trypost`.

## YouTube Shorts
1. [Google Cloud Console](https://console.cloud.google.com/) → new project → enable **YouTube Data API v3**.
2. OAuth consent screen → External → add scope `youtube.upload` → **Publish app** (in "Testing" mode, tokens expire after 7 days).
3. Credentials → OAuth client ID → *Web application* → redirect URI
   `https://post.yourdomain.com/accounts/youtube/callback`
4. `trypost.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
5. Free quota: about 6 uploads/day per project (10,000 units, 1,600 per upload). That's enough for the default 6 posts/day.

## Instagram Reels
1. Switch the IG account to **Creator** or **Business**.
2. [Meta for Developers](https://developers.facebook.com/) → Create app (Business) → add **Instagram API** with
   `instagram_content_publish`.
3. Redirect URI: `https://post.yourdomain.com/accounts/instagram/callback`
4. `trypost.env`: `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET`.
5. Instagram fetches the video from TryPost's public URL, so the tunnel must be up.

## TikTok
1. [TikTok for Developers](https://developers.tiktok.com/) → Create app → add **Login Kit** + **Content Posting API** (`video.publish`).
2. Redirect URI: `https://post.yourdomain.com/accounts/tiktok/callback`
3. `trypost.env`: `TIKTOK_CLIENT_ID` (the client key), `TIKTOK_CLIENT_SECRET`.
4. **Until TikTok approves your app audit, posts can only be private** (`SELF_ONLY`). Keep
   `publish.tiktok_privacy: "SELF_ONLY"` in your profile until then. TikTok decides whether to approve the audit.

## Multiple niches
A TryPost API key belongs to one workspace. For separate niche pages, either list the account IDs
per profile (`publish.account_ids`; get them from `GET http://localhost:8000/api/social-accounts`) or run a
second clipper container with another workspace's key.
