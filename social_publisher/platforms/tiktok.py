import os
import time
from pathlib import Path

import requests

from social_publisher import config
from social_publisher.models import PublishResult, VideoMeta
from social_publisher.platforms.base import PlatformPublisher

API_BASE = "https://open.tiktokapis.com/v2"
CHUNK_SIZE = 64 * 1024 * 1024


class TikTokPublisher(PlatformPublisher):
    name = "tiktok"

    def is_configured(self) -> bool:
        return bool(config.env("TIKTOK_CLIENT_KEY")) and bool(config.env("TIKTOK_CLIENT_SECRET"))

    def _access_token(self) -> str:
        token = config.load_token(self.name) or {}
        if token.get("expires_at", 0) > time.time() + 60 and token.get("access_token"):
            return token["access_token"]
        response = requests.post(
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": config.env("TIKTOK_CLIENT_KEY"),
                "client_secret": config.env("TIKTOK_CLIENT_SECRET"),
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        config.save_token(
            self.name,
            {
                "access_token": data.get("access_token", ""),
                "expires_at": time.time() + int(data.get("expires_in", 7200)),
            },
        )
        return data["access_token"]

    def auth(self) -> None:
        self._access_token()

    def publish(self, video_path: Path, meta: VideoMeta) -> PublishResult:
        try:
            access_token = self._access_token()
            file_size = video_path.stat().st_size
            headers = {"Authorization": f"Bearer {access_token}"}
            init_response = requests.post(
                f"{API_BASE}/post/publish/video/init/",
                headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
                json={
                    "post_info": {
                        "title": meta.title or video_path.stem,
                        "privacy_level": {
                            "public": "PUBLIC_TO_EVERYONE",
                            "private": "SELF_ONLY",
                        }.get(meta.privacy, "SELF_ONLY"),
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": file_size,
                        "chunk_size": CHUNK_SIZE,
                        "total_chunk_count": (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE,
                    },
                },
                timeout=30,
            )
            init_response.raise_for_status()
            init_data = init_response.json().get("data", {})
            upload_url = init_data.get("upload_url")
            publish_id = init_data.get("publish_id")
            if not upload_url:
                error = init_response.json().get("error", {})
                return PublishResult(
                    platform=self.name,
                    ok=False,
                    error=f"init failed: {error.get('message', init_response.text)}",
                )
            with video_path.open("rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    upload = requests.put(
                        upload_url,
                        data=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {f.tell() - len(chunk)}-{f.tell() - 1}/{file_size}",
                        },
                        timeout=300,
                    )
                    upload.raise_for_status()
            for _ in range(30):
                status_response = requests.post(
                    f"{API_BASE}/post/publish/status/fetch/",
                    headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
                    json={"publish_id": publish_id},
                    timeout=30,
                )
                status_data = status_response.json().get("data", {})
                if status_data.get("status") == "PUBLISH_COMPLETE":
                    return PublishResult(
                        platform=self.name,
                        ok=True,
                        video_id=publish_id,
                        url="https://www.tiktok.com/",
                    )
                if status_data.get("status") == "FAILED":
                    return PublishResult(
                        platform=self.name,
                        ok=False,
                        error=f"publish failed: {status_data.get('fail_reason', 'unknown')}",
                    )
                time.sleep(5)
            return PublishResult(
                platform=self.name,
                ok=True,
                video_id=publish_id,
                error="upload accepted; status still processing (check the TikTok app)",
            )
        except Exception as exc:
            return PublishResult(platform=self.name, ok=False, error=str(exc))
