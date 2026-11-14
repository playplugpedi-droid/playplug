from social_publisher.platforms.base import PlatformPublisher
from social_publisher.platforms.facebook import FacebookPublisher
from social_publisher.platforms.instagram import InstagramPublisher
from social_publisher.platforms.tiktok import TikTokPublisher
from social_publisher.platforms.youtube import YouTubePublisher

PUBLISHERS: dict[str, type[PlatformPublisher]] = {
    p.name: p for p in (YouTubePublisher, TikTokPublisher, FacebookPublisher, InstagramPublisher)
}


def get_publisher(name: str) -> PlatformPublisher:
    try:
        return PUBLISHERS[name]()
    except KeyError:
        known = ", ".join(sorted(PUBLISHERS))
        raise SystemExit(f"Unknown platform '{name}'. Available: {known}") from None
