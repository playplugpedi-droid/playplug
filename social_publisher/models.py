from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VideoMeta:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "public"
    ig_video_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    platform: str
    ok: bool
    video_id: str = ""
    url: str = ""
    error: str = ""
