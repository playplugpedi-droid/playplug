import argparse
import sys
from pathlib import Path

from social_publisher import __version__
from social_publisher.models import VideoMeta
from social_publisher.platforms import PUBLISHERS, get_publisher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="social-publisher",
        description="Publish a video to YouTube, TikTok, Facebook, and/or Instagram.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Run the OAuth flow for a platform and save its token")
    auth.add_argument("platform", choices=sorted(PUBLISHERS))

    publish = sub.add_parser("publish", help="Publish a video file to one or more platforms")
    publish.add_argument("video", type=Path, help="Path to the video file (MP4)")
    publish.add_argument(
        "--platforms",
        default="youtube,tiktok,facebook,instagram",
        help="Comma-separated platforms to publish to (default: all configured)",
    )
    publish.add_argument("--title", default="", help="Video title")
    publish.add_argument("--description", default="", help="Video description / caption")
    publish.add_argument("--tags", default="", help="Comma-separated tags (YouTube only)")
    publish.add_argument(
        "--privacy",
        default="public",
        choices=["public", "private", "unlisted"],
        help="Privacy level (default: public)",
    )
    publish.add_argument(
        "--ig-url",
        dest="ig_url",
        default="",
        help="Public URL of the video, required for Instagram (their API cannot upload local files)",
    )

    status = sub.add_parser("status", help="Show which platforms are configured and ready")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "auth":
        publisher = get_publisher(args.platform)
        if not publisher.is_configured():
            print(f"{args.platform}: credentials missing in .env — see .env.example and README.md")
            return 1
        publisher.auth()
        print(f"{args.platform}: token saved to {str(Path('tokens') / (args.platform + '.json'))}")
        return 0

    if args.command == "status":
        for name in sorted(PUBLISHERS):
            publisher = get_publisher(name)
            state = "ready" if publisher.is_configured() else "not configured (missing .env values)"
            print(f"{name:10s} {state}")
        return 0

    if args.command == "publish":
        video_path: Path = args.video
        if not video_path.is_file():
            print(f"error: video file not found: {video_path}")
            return 1
        requested = [p.strip() for p in args.platforms.split(",") if p.strip()]
        configured = []
        for name in requested:
            publisher = get_publisher(name)
            if not publisher.is_configured():
                print(f"skip {name}: not configured (missing .env values)")
                continue
            configured.append(publisher)
        if not configured:
            print("error: no configured platforms to publish to")
            return 1

        meta = VideoMeta(
            title=args.title,
            description=args.description,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()],
            privacy=args.privacy,
            ig_video_url=args.ig_url,
        )

        failures = 0
        for publisher in configured:
            print(f"publishing to {publisher.name}...")
            result = publisher.publish(video_path, meta)
            if result.ok:
                print(f"  ok: {result.url or result.video_id}")
                if result.error:
                    print(f"  note: {result.error}")
            else:
                failures += 1
                print(f"  FAILED: {result.error}")
        return 1 if failures else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
