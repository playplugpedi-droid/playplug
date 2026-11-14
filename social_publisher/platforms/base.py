from abc import ABC, abstractmethod
from pathlib import Path

from social_publisher.models import PublishResult, VideoMeta


class PlatformPublisher(ABC):
    name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def publish(self, video_path: Path, meta: VideoMeta) -> PublishResult:
        ...
