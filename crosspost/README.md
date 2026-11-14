# crosspost - automatic video reposting helper

Watches your YouTube, TikTok, Facebook and Instagram for **new videos** and queues them
for reposting to the other platforms. Title and description are reused when they exist;
when one is missing, Claude (optional) writes it. You approve each repost.

## What you need (one time)

1. **Python 3.9 or newer** from python.org. On Windows, tick "Add Python to PATH" in the installer.
2. Put `crosspost.py` in its own folder.
3. Three keys, saved in a file called `.env` next to the script (see `.env.example`):
   - `WINDSOR_API_KEY` - from your Windsor.ai account (you already connected Facebook, Instagram, TikTok and YouTube there).
   - `AYRSHARE_API_KEY` - from an Ayrshare account. In Ayrshare, link the accounts you want to post TO
     (YouTube, TikTok, Facebook, Instagram). Check that your Ayrshare plan covers them.
   - `ANTHROPIC_API_KEY` - *optional*, only for AI-written titles/descriptions.
   Never share the `.env` file or paste these keys into a chat.
4. *Optional:* install `yt-dlp` if you want the tool to fetch YouTube/TikTok video files itself.
   Without it, Facebook and Instagram videos still work (their file link is provided), and for
   YouTube/TikTok you give the tool a file path or public video URL when you review (choice `m`).

## Use it

```
python crosspost.py init       # creates config.json and .env.example
python crosspost.py check      # tests your keys, sources and linked accounts - fix every FAIL
python crosspost.py scan       # first run only RECORDS existing videos (your old videos are not reposted)
python crosspost.py scan       # later runs queue brand-new videos
python crosspost.py review --dry-run   # shows exactly what would be sent, posts nothing
python crosspost.py review     # approve / edit / schedule / skip each queued video
python crosspost.py watch      # scan every 15 minutes (Ctrl+C to stop)
python crosspost.py status
python crosspost.py demo       # offline self-test with fake data
```

In `review`: `p` post now, `s` schedule, `e` edit title/description/hashtags, `d` change
destinations, `m` attach a video file or URL, `x` skip for good, `l` later, `q` quit.

`watch --auto-publish` posts without asking, but only videos whose text **you** wrote, scheduled
15 minutes ahead so you can cancel in Ayrshare. Add `--include-ai-drafts` to include Claude-drafted ones.

## Safety built in

- `scan` never posts. The first scan only records what already exists.
- Loop protection: a video the tool posted is recognised when it appears on the other platform
  and is not reposted again. Facebook crossposts are skipped. A video you already posted by hand
  on a destination is not duplicated there.
- Limits: `max_jobs_per_scan` (5) and `max_posts_per_day` (10) in `config.json`.
- Keys are only read from `.env` / environment and are hidden in error messages.
- Edit `config.json` to change which platforms post to which, the brand notes Claude uses,
  and per-platform options.

## Honest limitations

- **Tested offline only.** The demo and tests use fake responses. The real Windsor, Ayrshare and
  Claude API calls follow their documented formats from memory, so run `check` and
  `review --dry-run` first and post one test video before trusting it. If a request is rejected,
  the error text is printed - send it to Claude and it can be fixed.
- It reads titles, descriptions and links. **It cannot see the video**, so AI-written text is based
  only on the text that already exists; with no text at all you must write it yourself.
- Detection polls on a timer (default 15 minutes) and only runs while the script is running on your computer.
- Windsor read access depends on your Windsor trial/plan; TikTok shows no title (caption only).
- Downloaded TikTok videos may carry a watermark. Only repost videos you own, and follow each platform's rules.
- YouTube Community posts cannot be created by any API, so they are not covered.

## Optional: post with your own `social-publisher` tool ("direct" backend)

By default `crosspost.py` posts through Ayrshare. To use your Mistral-built tool instead (or for only some platforms), put the fixed `social-publisher` folder next to this one, run `pip install -e .` inside it, fill in its `.env`, then edit `config.json`:

    "backend": {"default": "ayrshare", "per_platform": {"facebook": "direct", "youtube": "direct"}},
    "direct_cli_dir": "../social-publisher"

Notes: direct posting cannot schedule (publish-now only); it needs the video as a local file (yt-dlp downloads it); Instagram needs a public video URL; TikTok needs `python -m social_publisher.cli auth tiktok` once (user login, `video.publish` scope) and stays private until TikTok audits your app. Tested offline with a stand-in CLI only; the Meta/TikTok calls have not been run against the real APIs.
