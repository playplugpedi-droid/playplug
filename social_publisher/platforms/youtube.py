from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from social_publisher import config
from social_publisher.models import PublishResult, VideoMeta
from social_publisher.platforms.base import PlatformPublisher

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_NAME = "youtube"
API_VERSION = "v3"
CHUNK_SIZE = 8 * 1024 * 1024


class YouTubePublisher(PlatformPublisher):
    name = "youtube"

    def is_configured(self) -> bool:
        return bool(config.env("YOUTUBE_CLIENT_ID")) and bool(config.env("YOUTUBE_CLIENT_SECRET"))

    def _credentials(self) -> Credentials:
        token = config.load_token(self.name) or {}
        creds = Credentials(
            token=token.get("token"),
            refresh_token=token.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=config.env("YOUTUBE_CLIENT_ID"),
            client_secret=config.env("YOUTUBE_CLIENT_SECRET"),
            scopes=SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            config.save_token(self.name, {"token": creds.token, "refresh_token": creds.refresh_token})
        return creds

    def auth(self) -> None:
        client_config = {
            "installed": {
                "client_id": config.env("YOUTUBE_CLIENT_ID"),
                "client_secret": config.env("YOUTUBE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        config.save_token(self.name, {"token": creds.token, "refresh_token": creds.refresh_token})

    def publish(self, video_path: Path, meta: VideoMeta) -> PublishResult:
        try:
            creds = self._credentials()
            service = build(API_NAME, API_VERSION, credentials=creds)
            body = {
                "snippet": {
                    "title": meta.title or video_path.stem,
                    "description": meta.description,
                    "tags": meta.tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": meta.privacy if meta.privacy in ("public", "private", "unlisted") else "private",
                    "selfDeclaredMadeForKids": False,
                },
            }
            media = MediaFileUpload(str(video_path), chunked=True, resumable=True, mimetype="video/*")
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"  youtube: {int(status.progress() * 100)}% uploaded")
            return PublishResult(
                platform=self.name,
                ok=True,
                video_id=response["id"],
                url=f"https://www.youtube.com/watch?v={response['id']}",
            )
        except Exception as exc:
            return PublishResult(platform=self.name, ok=False, error=str(exc))
