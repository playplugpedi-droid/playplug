import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from social_publisher import config
from social_publisher.models import PublishResult, VideoMeta
from social_publisher.platforms.base import PlatformPublisher

API_BASE = "https://open.tiktokapis.com/v2"
CHUNK_SIZE = 64 * 1024 * 1024
SCOPES = "user.info.basic,video.publish"


class TikTokPublisher(PlatformPublisher):
    name = "tiktok"

    def is_configured(self) -> bool:
        return bool(config.env("TIKTOK_CLIENT_KEY")) and bool(config.env("TIKTOK_CLIENT_SECRET"))

    def _redirect_uri(self) -> str:
        return config.env("TIKTOK_REDIRECT_URI", "http://localhost:8080/")

    def _saved_token(self) -> dict | None:
        token = config.load_token(self.name) or {}
        if not token.get("access_token"):
            return None
        if token.get("expires_at", 0) > time.time() + 60:
            return token
        refresh_token = token.get("refresh_token", "")
        if not refresh_token:
            return None
        response = requests.post(
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": config.env("TIKTOK_CLIENT_KEY"),
                "client_secret": config.env("TIKTOK_CLIENT_SECRET"),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if response.status_code != 200:
            return None
        data = response.json().get("data", {})
        refreshed = {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": time.time() + int(data.get("expires_in", 86400)),
        }
        config.save_token(self.name, refreshed)
        return refreshed

    def _access_token(self) -> str:
        token = self._saved_token()
        if token is None:
            raise SystemExit(
                "No valid TikTok user token. Run: social-publisher auth tiktok "
                "(posting requires a user access token with the video.publish scope, "
                "not a client-credentials app token)"
            )
        return token["access_token"]

    def auth(self) -> None:
        redirect_uri = self._redirect_uri()
        state = secrets.token_urlsafe(16)
        auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(
            {
                "client_key": config.env("TIKTOK_CLIENT_KEY"),
                "scope": SCOPES,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        result: dict = {}

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result["code"] = query.get("code", [None])[0]
                result["state"] = query.get("state", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"TikTok authorized. You can close this tab.")

            def log_message(self, *args) -> None:
                pass

        port = urllib.parse.urlparse(redirect_uri).port or 80
        server = http.server.HTTPServer(("localhost", port), Callback)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        print("Opening TikTok authorization page in your browser...")
        print(f"If it does not open, visit this URL manually:\n{auth_url}")
        webbrowser.open(auth_url)
        thread.start()
        server.server_close()
        if result.get("state") != state:
            raise SystemExit("TikTok OAuth state mismatch; aborting")
        if not result.get("code"):
            raise SystemExit("TikTok OAuth callback did not include an authorization code")
        response = requests.post(
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": config.env("TIKTOK_CLIENT_KEY"),
                "client_secret": config.env("TIKTOK_CLIENT_SECRET"),
                "code": result["code"],
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        config.save_token(
            self.name,
            {
                "access_token": data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": time.time() + int(data.get("expires_in", 86400)),
            },
        )

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
