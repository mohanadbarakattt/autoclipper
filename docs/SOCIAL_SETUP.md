# Auto-posting setup: YouTube Shorts, Instagram Reels, TikTok (via self-hosted TryPost)

> **You don't need this to use AutoClipper.** Easy mode (`start.bat`) gives you finished clips with a
> Download and a Copy caption button. This guide is only for fully hands-off posting.
> Plan on **1-2 hours** the first time, plus waiting for platform approvals (days to weeks).

Everything here is free. Read the **"What will block you"** box for each platform before you start.

---

## Part 0 — Things you need first

| You need | Why | Free option |
|---|---|---|
| Docker Desktop running | TryPost runs in Docker | docker.com (free for personal use) |
| A **public HTTPS URL** that reaches TryPost | Google, Meta and TikTok send you back to it after login, and Instagram downloads the video from it | Cloudflare Tunnel (needs a domain you own) **or** ngrok free static domain |
| A **Privacy Policy URL** and **Terms of Service URL** | All three platforms refuse to create an app without them | A simple page on a site you already own (e.g. `yourdomain.com/privacy`) |
| A website/domain you own | TikTok verifies it; Meta and Google show it on the consent screen | The same domain |
| The social accounts you'll post to | — | Instagram must be **Creator or Business** |

### 0.1 Start TryPost locally

```bash
python scripts/setup.py
docker compose up -d --build
docker compose exec trypost php artisan db:seed --force
docker compose exec trypost php artisan db:seed --class=UserSeeder --force
```
Open http://localhost:8000, sign in as `admin@trypost.it` / `password`, and **change the password right away**.

### 0.2 Give TryPost a public HTTPS URL (pick ONE)

**Option A: Cloudflare Tunnel (best; needs a domain whose DNS is on a free Cloudflare account)**
1. Go to https://one.dash.cloudflare.com → **Networks → Tunnels → Create a tunnel** → choose **Cloudflared** → name it `trypost` → **Save**.
2. On the "Install connector" screen, choose **Docker** and copy only the long token after `--token`.
3. In `.env` add: `CLOUDFLARE_TUNNEL_TOKEN=<that token>`
4. Click **Next** → **Public hostname**: Subdomain `post`, Domain `yourdomain.com`, Service type **HTTP**, URL `trypost:80` → **Save tunnel**.
   (`trypost:80` works because the tunnel container runs inside the same Docker network.)
5. Start it: `docker compose --profile tunnel up -d`
6. Check that `https://post.yourdomain.com` shows the TryPost login page.

**Option B: ngrok (no domain needed)**
1. Sign up at https://ngrok.com (free) → **Domains** → claim your one free static domain, e.g. `lucky-cat-123.ngrok-free.app`.
2. Install ngrok, run `ngrok config add-authtoken <your token>`, then keep this running:
   `ngrok http --url=lucky-cat-123.ngrok-free.app 8000`
3. Note: on the free plan, browsers see an ngrok warning page the first time. Click **Visit Site**. It doesn't affect the platforms' servers.
4. Downside: the laptop must keep ngrok running whenever you connect accounts or post.

### 0.3 Tell TryPost its public URL
```bash
python scripts/setup.py --public-url https://post.yourdomain.com
docker compose up -d trypost
```
This sets `APP_URL` and all three callback URLs in `trypost.env`. The callback URLs you'll paste below are:

```
https://post.yourdomain.com/accounts/youtube/callback
https://post.yourdomain.com/accounts/instagram/callback
https://post.yourdomain.com/accounts/tiktok/callback
```
They must match **exactly**: `https`, no trailing slash, same subdomain.

---

## Part 1 — YouTube Shorts (Google Cloud)

> **What will block you**
> - **Uploads stay PRIVATE until Google audits your project.** Any API project created after July 2020
>   that hasn't passed the *YouTube API Services audit* can only upload private videos. Apply early (step 8).
> - In **Testing** mode your login expires every **7 days**. Switch the app to **In production** (step 4).
> - Free quota is 10,000 units/day, and one upload costs about 1,600, so **about 6 uploads per day** per project.

1. Open https://console.cloud.google.com → top bar project picker → **New Project** → name `AutoClipper` → **Create**, and make sure it's selected.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen** (newer UIs call it **Google Auth Platform → Branding / Audience / Data access**):
   - User type: **External**
   - App name: e.g. `AutoClipper`. User support email: your Gmail.
   - App domain: your homepage, Privacy Policy URL and Terms URL. Authorized domain: `yourdomain.com` (for ngrok: `ngrok-free.app`).
   - Developer contact email: your Gmail → **Save**.
4. **Audience**: click **Publish app → Confirm** so the status shows **In production**.
   Because `youtube.upload` is a *sensitive* scope, Google may say the app needs verification. You can still
   use it yourself: when you log in, click **Advanced → Go to AutoClipper (unsafe)**. That's fine for your own accounts.
5. **Data access → Add or remove scopes** → filter `youtube.upload` → tick
   `https://www.googleapis.com/auth/youtube.upload` → **Update → Save**.
6. **Clients (Credentials) → Create credentials → OAuth client ID**:
   - Application type: **Web application**, name `TryPost`
   - **Authorized redirect URIs → Add URI** → `https://post.yourdomain.com/accounts/youtube/callback`
   - **Create** → copy the **Client ID** and **Client secret**.
7. In `trypost.env`:
   ```
   GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
   ```
   Then `docker compose up -d trypost`.
8. **Remove the private lock (audit):** fill in the *YouTube API Services – Audit and Quota Extension Form*
   (search that name, or go via https://support.google.com/youtube/contact/yt_api_form). Describe it honestly:
   "Personal tool that uploads my own short-form clips to my own channel via a self-hosted scheduler."
   Until it's approved, YouTube makes the uploads private automatically. Nothing breaks, they just aren't public yet.

---

## Part 2 — Instagram Reels (Meta for Developers)

> **What will block you**
> - The account must be **Instagram Professional** (Creator or Business). Personal accounts can't post through the API.
> - While the Meta app is in **Development** mode it only works for accounts added as **testers**,
>   which is fine because you're posting to your own account. You never need App Review for that.
> - Instagram downloads the video from TryPost's public URL, so the tunnel (Part 0.2) **must be up** at posting time.

1. In the Instagram app: **Settings → Account type and tools → Switch to professional account** → choose **Creator** (or Business).
2. Go to https://developers.facebook.com → **My Apps → Create App**.
   - Use case: **Other** → **Next** → app type **Business** → name `AutoClipper`, your email → **Create app**.
     (If you're offered a use case called *"Manage messaging & content on Instagram"*, pick that instead. It's the same thing.)
3. App dashboard → **Add product → Instagram → Set up** → choose **API setup with Instagram login**.
4. Section **1. Generate access tokens**: you don't need to generate one, TryPost does it. Copy the
   **Instagram app ID** and **Instagram app secret** shown at the top. These are *not* the Meta app ID.
5. Section **3. Set up Instagram business login → Business login settings**:
   - **OAuth redirect URIs**: `https://post.yourdomain.com/accounts/instagram/callback` → **Save**.
6. **App roles → Roles → Add people → Instagram Tester** → enter your Instagram username.
   Then on Instagram (website): **Settings → Apps and websites → Tester invites → Accept**.
7. **App settings → Basic**: add the Privacy Policy URL, Terms URL, an app icon (1024×1024) and a category → **Save**.
   Keep the app in **Development** mode. That's enough for your own tester account.
8. In `trypost.env`:
   ```
   INSTAGRAM_CLIENT_ID=<Instagram app ID>
   INSTAGRAM_CLIENT_SECRET=<Instagram app secret>
   ```
   Then `docker compose up -d trypost`.
9. Permissions requested at login: `instagram_business_basic` and `instagram_business_content_publish`.
   Approve both on the Instagram login screen.
10. Limits: Instagram allows about **50 API posts per 24 h** and at most **5 hashtags** per post. AutoClipper's defaults stay under both.

*(TryPost can also connect Instagram through a Facebook Page. That route uses `FACEBOOK_CLIENT_ID/SECRET` and
`/accounts/facebook/callback`, and you only need it if you want licensed music on Reels.)*

---

## Part 3 — TikTok (TikTok for Developers)

> **What will block you**
> - **Until TikTok audits your app, every post is private (`SELF_ONLY`)**, only up to 5 users can post per day,
>   and TikTok may require the account itself to be set to private. You can still test end to end, and you open
>   the private posts in the TikTok app to make them public by hand. Apply for the audit once it works.
> - TikTok needs a **website, Terms URL and Privacy URL** on a domain you can verify.
> - The audit asks for a **screen recording** of the full flow (log in → connect TikTok in TryPost → post).

1. Go to https://developers.tiktok.com → log in with the TikTok account → **Manage apps → Connect an app**
   (individual developer is fine) → name `AutoClipper`.
2. **App details**: icon (1024×1024), category (e.g. *Entertainment*), description, **Terms of Service URL**,
   **Privacy Policy URL**, **Platform: Web**, **Website URL** `https://yourdomain.com`.
   If asked to verify the URL, download the signature file TikTok gives you and host it at your domain root,
   or add the TXT DNS record in Cloudflare.
3. **Add products**:
   - **Login Kit** → **Redirect URI** (Web): `https://post.yourdomain.com/accounts/tiktok/callback`
   - **Content Posting API** → turn on **Direct Post**.
4. **Scopes**: make sure `user.info.basic`, `video.upload` and `video.publish` are added.
5. Copy **Client key** and **Client secret** (top of the app page) into `trypost.env`:
   ```
   TIKTOK_CLIENT_ID=<Client key>
   TIKTOK_CLIENT_SECRET=<Client secret>
   ```
   Then `docker compose up -d trypost`.
6. Click **Submit for review**. Approval of the app itself usually takes a few days.
   While it's in review you can test with the **Sandbox** tab: add your TikTok account as a target user there.
7. Keep `"tiktok_privacy": "SELF_ONLY"` in your profile's `publish` block.
8. **Get the audit (to post publicly):** once posting works, record your screen showing the full flow, then on the
   Content Posting API page request the **audit**. When it's approved, change the profile to
   `"tiktok_privacy": "PUBLIC_TO_EVERYONE"`.

---

## Part 4 — Connect the accounts and turn on posting

1. Open TryPost **at your public URL** (`https://post.yourdomain.com`, not localhost, or the login redirect fails).
2. Go to the social accounts / channels page → **Connect** YouTube, then Instagram, then TikTok, and approve each login.
   All three should show as **connected**.
3. TryPost → **Settings → API → Generate API key** → copy it (it's shown only once) → put it in `.env`:
   `TRYPOST_API_KEY=...` → `docker compose up -d clipper`.
4. Open http://localhost:8090. The TryPost badge at the top should show **3 accounts**.
5. **Recommended first week:** set `"mode": "draft"` in your profile's `publish` block. Clips then land in TryPost as
   drafts and you approve each one. Switch to `"scheduled"` once you trust the output.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `redirect_uri_mismatch` / "URL blocked" | The callback in the developer console must exactly match the one above (https, subdomain, no trailing slash). Re-run `setup.py --public-url ...` and restart TryPost. |
| YouTube video uploaded but private | Your project hasn't passed the YouTube audit (Part 1 step 8). |
| YouTube login stops working after a week | Consent screen still in **Testing**. Publish it (Part 1 step 4) and reconnect. |
| Instagram "media could not be fetched" | Tunnel was down when it posted. Keep Cloudflare Tunnel or ngrok running. |
| Instagram account not offered at login | It's not Professional, or the tester invite wasn't accepted (Part 2 steps 1 and 6). |
| TikTok posts only visible to you | Expected until the TikTok audit is approved (Part 3 step 8). |
| Dashboard says "publish_failed" | Hover the error on the clip card. Most often a disconnected account or an expired login: reconnect it in TryPost, then click **Re-queue**. |
