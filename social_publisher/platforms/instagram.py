import time
from pathlib import Path

import requests

from social_publisher import config
from social_publisher.models import PublishResult, VideoMeta
from social_publisher.platforms.base import PlatformPublisher

GRAPH = "https://graph.facebook.com/v21.0"


class InstagramPublisher(PlatformPublisher):
    name = "instagram"

    def is_configured(self) -> bool:
        return bool(config.env("INSTAGRAM_USER_ID")) and bool(config.env("INSTAGRAM_ACCESS_TOKEN"))

    def _token(self) -> dict:
        return {"access_token": config.env("INSTAGRAM_ACCESS_TOKEN")}

    def auth(self) -> None:
        raise SystemExit(
            "Instagram uses a long-lived access token for a Business/Creator account linked to a "
            "Facebook Page. Get one via the Graph API explorer (see README.md) and set "
            "INSTAGRAM_USER_ID / INSTAGRAM_ACCESS_TOKEN in .env."
        )

    def publish(self, video_path: Path, meta: VideoMeta) -> PublishResult:
        if not meta.ig_video_url:
            return PublishResult(
                platform=self.name,
                ok=False,
                error=(
                    "Instagram's API cannot upload a local file; it requires a video at a public URL. "
                    "Pass --ig-url https://example.com/video.mp4 (MP4, 3-90s for Reels)."
                ),
            )
        try:
            ig_user_id = config.env("INSTAGRAM_USER_ID")
            caption = f"{meta.title}\n\n{meta.description}".strip()
            create = requests.post(
                f"{GRAPH}/{ig_user_id}/media",
                data={
                    **self._token(),
                    "media_type": "REELS",
                    "video_url": meta.ig_video_url,
                    "caption": caption,
                },
                timeout=60,
            )
            create.raise_for_status()
            creation_id = create.json()["id"]
            for _ in range(60):
                status = requests.get(
                    f"{GRAPH}/{creation_id}",
                    params={**self._token(), "fields": "status_code"},
                    timeout=30,
                )
                status.raise_for_status()
                status_code = status.json().get("status_code")
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    return PublishResult(
                        platform=self.name,
                        ok=False,
                        error=f"processing failed: {status.json()}",
                    )
                time.sleep(5)
            else:
                return PublishResult(
                    platform=self.name,
                    ok=False,
                    error="timed out waiting for Instagram to finish processing the video",
                )
            publish = requests.post(
                f"{GRAPH}/{ig_user_id}/media_publish",
                data={**self._token(), "creation_id": creation_id},
                timeout=30,
            )
            publish.raise_for_status()
            media_id = publish.json()["id"]
            url = f"https://www.instagram.com/reel/{media_id}/"
            try:
                pl = requests.get(f"{GRAPH}/{media_id}", params={**self._token(), "fields": "permalink"}, timeout=30)
                url = pl.json().get("permalink") or url
            except Exception:
                pass
            return PublishResult(
                platform=self.name,
                ok=True,
                video_id=media_id,
                url=url,
            )
        except Exception as exc:
            return PublishResult(platform=self.name, ok=False, error=str(exc))
