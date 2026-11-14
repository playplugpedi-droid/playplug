import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKENS_DIR = Path(os.getenv("TOKENS_DIR", PROJECT_DIR / "tokens"))
TOKENS_DIR.mkdir(parents=True, exist_ok=True)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def token_path(platform: str) -> Path:
    return TOKENS_DIR / f"{platform}.json"


def save_token(platform: str, token: dict) -> None:
    token_path(platform).write_text(json.dumps(token, indent=2))


def load_token(platform: str) -> dict | None:
    path = token_path(platform)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


class ConfigError(Exception):
    pass


def require_env(platform: str, names: list[str]) -> dict:
    values = {}
    missing = []
    for name in names:
        value = env(name)
        if value:
            values[name] = value
        else:
            missing.append(name)
    if missing:
        raise ConfigError(
            f"{platform} is not configured. Set these in .env: {', '.join(missing)}. "
            f"See .env.example and README.md for how to get them."
        )
    return values
