# social-publisher

A Python CLI that publishes one video to **YouTube**, **TikTok**, **Facebook (Page)**, and **Instagram (Reels)** using each platform's official API.

Every platform is optional: the tool skips any platform whose credentials are missing from `.env`, so you can start with just one and add the rest later.

## Install

```bash
cd social-publisher
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env             # then fill in what you have
```

## Usage

```bash
# See which platforms are ready
social-publisher status

# One-time OAuth for YouTube / TikTok (saves a token in tokens/)
social-publisher auth youtube
social-publisher auth tiktok

# Publish everywhere that is configured
social-publisher publish my-video.mp4 \
  --title "My video" \
  --description "Full description here" \
  --tags vlog,howto \
  --privacy public

# Publish to specific platforms only
social-publisher publish my-video.mp4 --platforms youtube,tiktok

# Instagram needs a public URL (see limitations below)
social-publisher publish my-video.mp4 --platforms instagram --ig-url https://example.com/my-video.mp4
```

Exit code is `0` if every requested platform succeeded, `1` otherwise — handy for scripts/CI.

## Getting credentials

### YouTube
1. [Google Cloud Console](https://console.cloud.google.com) → new project.
2. Enable **YouTube Data API v3**.
3. OAuth consent screen → **External** → add your Google account as a test user.
4. Credentials → **Create OAuth client ID → Desktop app** → copy the client ID and secret into `.env`.
5. Run `social-publisher auth youtube` once; it opens a browser and stores the token.
6. Note: unverified test apps have uploads set to **private** — flip videos to public manually or verify the app. Also, fresh API projects have limited upload quota (see "Quota" in the [API docs](https://developers.google.com/youtube/v3/getting-started#quota)).

### TikTok
1. [TikTok for Developers](https://developers.tiktok.com) → create an app.
2. Add the **Content Posting API** product.
3. In the app settings, register the redirect URI `http://localhost:8080/`.
4. Copy the client key and secret into `.env`, then run `social-publisher auth tiktok`.
5. Posting requires a **user** access token with the `video.publish` scope — the `auth` command runs the OAuth authorization-code flow in your browser (with a local callback server on port 8080) and stores the token, refreshing it automatically when it expires.
6. **Important:** until TikTok audits your app, posts are limited to **self-only (private)** visibility. After audit approval, public posting works.

### Facebook (Pages only)
The Graph API can only post to **Pages**, not personal profiles.
1. [Facebook for Developers](https://developers.facebook.com) → create a **Business** app.
2. Add the `pages_manage_posts` and `pages_read_engagement` permissions.
3. Get a Page access token in the [Graph API Explorer](https://developers.facebook.com/tools/explorer/), then exchange it for a long-lived token:

```bash
# user token → long-lived user token
curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
  --data-urlencode "grant_type=fb_exchange_token" \
  --data-urlencode "client_id=YOUR_APP_ID" \
  --data-urlencode "client_secret=YOUR_APP_SECRET" \
  --data-urlencode "fb_exchange_token=YOUR_SHORT_LIVED_TOKEN"
# then list your pages to get the never-expiring Page token
curl "https://graph.facebook.com/v21.0/me/accounts?access_token=LONG_LIVED_USER_TOKEN"
```

4. Put the **Page ID** and **Page access token** into `.env`.

### Instagram (Business/Creator + Reels)
1. Your Instagram account must be a **Business or Creator** account linked to the Facebook Page above.
2. Find your numeric IG user id: `curl "https://graph.facebook.com/v21.0/me/accounts?fields=instagram_business_account&access_token=PAGE_TOKEN"`.
3. Use the **Page access token** as `INSTAGRAM_ACCESS_TOKEN` and the IG user id as `INSTAGRAM_USER_ID`.
4. **Limitation:** Instagram's API does not accept file uploads — the video must already be at a **public URL** (MP4, 3–90 s for Reels, vertical ≤ 1080×1920). Pass it with `--ig-url`. A common pattern: upload the file to YouTube/Facebook first (this tool) or your own storage/CDN, then publish to Instagram with that URL.

## Project layout

```
social_publisher/
├── cli.py               # argparse CLI: auth / publish / status
├── config.py            # .env loading + token storage
├── models.py            # VideoMeta, PublishResult
└── platforms/
    ├── base.py          # PlatformPublisher interface
    ├── youtube.py       # OAuth + resumable upload (YouTube Data API v3)
    ├── tiktok.py        # user OAuth (auth code) + chunked upload (Content Posting API)
    ├── facebook.py      # Page token + resumable video upload (Graph API)
    └── instagram.py     # media container + publish flow (Graph API)
```

## Notes & limits

- **Video format:** MP4 everywhere; YouTube/TikTok/Facebook upload the local file, Instagram requires a public URL.
- **Tokens** are stored in `tokens/*.json` (git-ignored). Never commit `.env` or tokens.
- **Facebook upload** targets a Page; personal-profile posting is not supported by the API.
- **TikTok visibility** is private until your app passes TikTok's audit.
- API quotas apply per platform; a failed platform does not stop the others.
