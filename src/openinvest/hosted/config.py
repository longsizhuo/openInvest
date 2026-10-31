"""Hosted configuration is separate from INVEST_HOME and production credentials."""
from dataclasses import dataclass
from pathlib import Path
import base64
import os
from urllib.parse import urlsplit

REQUIRED_ENV = (
    "HOSTED_DATA_DIR", "HOSTED_ORIGIN", "HOSTED_AUTH0_ISSUER", "HOSTED_AUTH0_CLIENT_ID",
    "HOSTED_AUTH0_CLIENT_SECRET", "HOSTED_ENCRYPTION_KEY", "HOSTED_SESSION_SECRET",
    "HOSTED_ACTION_SECRET", "HOSTED_ADMIN_TOKEN",
)


@dataclass(frozen=True)
class Settings:
    root: Path
    origin: str
    issuer: str
    audience: str
    client_id: str
    client_secret: str
    session_secret: str
    encryption_key: bytes
    image: str = "openinvest-hosted-worker:local"
    policy_version: str = "2026-10-30-v1"
    workers: int = 2
    idle_seconds: int = 300
    queue_bytes: int = 128 * 1024 * 1024
    portal: Path | None = None
    cos_region: str = "ap-singapore"
    cos_bucket: str = ""
    action_secret: str = ""
    registration_open: bool = False
    admin_token: str = ""

    @classmethod
    def from_env(cls):
        def required(name):
            result = os.environ.get(name, "").strip()
            if not result:
                raise ValueError(f"Missing {name}")
            return result
        origin = required("HOSTED_ORIGIN").rstrip("/")
        issuer = required("HOSTED_AUTH0_ISSUER").rstrip("/") + "/"
        for url in (origin, issuer):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise ValueError("Hosted public origins must use HTTPS")
        key = base64.b64decode(required("HOSTED_ENCRYPTION_KEY"), validate=True)
        secret = required("HOSTED_SESSION_SECRET")
        if len(key) != 32 or min(len(secret), len(required("HOSTED_ACTION_SECRET")), len(required("HOSTED_ADMIN_TOKEN"))) < 32:
            raise ValueError("Use independent 32-byte encryption and session secrets")
        root = Path(required("HOSTED_DATA_DIR")).expanduser().resolve()
        if (root / "pyproject.toml").exists():
            raise ValueError("HOSTED_DATA_DIR must be a dedicated directory")
        if os.getenv("HOSTED_REGISTRATION_OPEN") == "1" and not os.getenv("HOSTED_COS_BUCKET"):
            raise ValueError("Configure research archive and backups before public registration")
        return cls(root, origin, issuer, os.getenv("HOSTED_AUDIENCE", origin + "/mcp"),
                   required("HOSTED_AUTH0_CLIENT_ID"), required("HOSTED_AUTH0_CLIENT_SECRET"),
                   secret, key, image=os.getenv("HOSTED_WORKER_IMAGE", cls.image),
                   portal=Path(os.environ["HOSTED_PORTAL_DIR"]) if os.getenv("HOSTED_PORTAL_DIR") else None,
                   cos_bucket=os.getenv("HOSTED_COS_BUCKET", ""),
                   action_secret=required("HOSTED_ACTION_SECRET"),
                   admin_token=required("HOSTED_ADMIN_TOKEN"),
                   registration_open=os.getenv("HOSTED_REGISTRATION_OPEN") == "1")
