from pathlib import Path

import requests

from social_publisher import config
from social_publisher.models import PublishResult, VideoMeta
from social_publisher.platforms.base import PlatformPublisher

GRAPH = "https://graph.facebook.com/v21.0"
CHUNK_SIZE = 4 * 1024 * 1024


class FacebookPublisher(PlatformPublisher):
    name = "facebook"

    def is_configured(self) -> bool:
        return bool(config.env("FACEBOOK_PAGE_ID")) and bool(config.env("FACEBOOK_PAGE_TOKEN"))

    def _token(self) -> dict:
        # Graph API takes the token as a request parameter (not a header named access_token).
        return {"access_token": config.env("FACEBOOK_PAGE_TOKEN")}

    def auth(self) -> None:
        raise SystemExit(
            "Facebook uses a Page access token, not an OAuth flow from this tool. "
            "Generate one at https://developers.facebook.com/tools/explorer/ with the "
            "pages_manage_posts and pages_read_engagement permissions, then exchange it for a "
            "long-lived Page token (see README.md) and set FACEBOOK_PAGE_ID / FACEBOOK_PAGE_TOKEN in .env."
        )

    def publish(self, video_path: Path, meta: VideoMeta) -> PublishResult:
        try:
            page_id = config.env("FACEBOOK_PAGE_ID")
            file_size = video_path.stat().st_size
            start = requests.post(
                f"{GRAPH}/{page_id}/videos",
                data={
                    **self._token(),
                    "upload_phase": "start",
                    "file_size": file_size,
                },
                timeout=30,
            )
            start.raise_for_status()
            start_data = start.json()
            session_id = start_data["upload_session_id"]
            video_id = start_data["video_id"]
            with video_path.open("rb") as f:
                offset = 0
                while offset < file_size:
                    chunk = f.read(CHUNK_SIZE)
                    transfer = requests.post(
                        f"{GRAPH}/{page_id}/videos",
                        data={
                            **self._token(),
                            "upload_phase": "transfer",
                            "upload_session_id": session_id,
                            "start_offset": offset,
                        },
                        files={"video_file_chunk": ("chunk.bin", chunk)},
                        timeout=600,
                    )
                    transfer.raise_for_status()
                    next_offset = int(transfer.json().get("start_offset", offset + len(chunk)))
                    if next_offset <= offset:
                        next_offset = offset + len(chunk)
                    offset = next_offset
            finish = requests.post(
                f"{GRAPH}/{page_id}/videos",
                data={
                    **self._token(),
                    "upload_phase": "finish",
                    "upload_session_id": session_id,
                    "description": f"{meta.title}\n\n{meta.description}".strip(),
                    "title": meta.title[:255],
                },
                timeout=30,
            )
            finish.raise_for_status()
            return PublishResult(
                platform=self.name,
                ok=True,
                video_id=video_id,
                url=f"https://www.facebook.com/{video_id}",
            )
        except Exception as exc:
            return PublishResult(platform=self.name, ok=False, error=str(exc))
