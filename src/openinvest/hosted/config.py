"""Hosted configuration is separate from INVEST_HOME and production credentials."""
from dataclasses import dataclass
from pathlib import Path
import base64
import os
import re
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
    policy_version: str = "2026-11-01-v1"
    workers: int = 2
    idle_seconds: int = 300
    queue_bytes: int = 128 * 1024 * 1024
    portal: Path | None = None
    cos_region: str = "ap-singapore"
    cos_bucket: str = ""
    action_secret: str = ""
    registration_open: bool = False
    admin_token: str = ""
    archive_dir: Path | None = None
    archive_bytes: int = 2 * 1024**3
    worker_network: str = "bridge"
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_profile: str = ""
    worker_namespace: str = "ledger"

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
        if os.getenv("HOSTED_REGISTRATION_OPEN") == "1" and not (os.getenv("HOSTED_COS_BUCKET") or os.getenv("HOSTED_S3_BUCKET")):
            raise ValueError("Configure research archive and backups before public registration")
        archive_dir = Path(os.environ["HOSTED_ARCHIVE_DIR"]) if os.getenv("HOSTED_ARCHIVE_DIR") else None
        archive_bytes = int(os.getenv("HOSTED_ARCHIVE_BYTES", str(cls.archive_bytes)))
        network = os.getenv("HOSTED_WORKER_NETWORK", "bridge")
        namespace = os.getenv("HOSTED_WORKER_NAMESPACE", "ledger")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", namespace):
            raise ValueError("Invalid worker namespace")
        if archive_dir and (not archive_dir.is_absolute() or archive_dir.resolve() == root):
            raise ValueError("Choose a dedicated absolute archive directory or COS")
        if sum(bool(value) for value in (archive_dir, os.getenv("HOSTED_COS_BUCKET"), os.getenv("HOSTED_S3_BUCKET"))) > 1:
            raise ValueError("Choose one archive backend")
        s3_endpoint = os.getenv("HOSTED_S3_ENDPOINT", "")
        if os.getenv("HOSTED_S3_BUCKET"):
            endpoint = urlsplit(s3_endpoint)
            if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}:
                raise ValueError("S3 endpoint must be an HTTPS origin")
            if not os.getenv("HOSTED_S3_PROFILE"):
                required("HOSTED_S3_ACCESS_KEY_ID")
                required("HOSTED_S3_SECRET_ACCESS_KEY")
        if archive_bytes < 1 or network in {"host", "none"} or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", network):
            raise ValueError("Invalid archive capacity or worker network")
        return cls(root, origin, issuer, os.getenv("HOSTED_AUDIENCE", origin + "/mcp"),
                   required("HOSTED_AUTH0_CLIENT_ID"), required("HOSTED_AUTH0_CLIENT_SECRET"),
                   secret, key, image=os.getenv("HOSTED_WORKER_IMAGE", cls.image),
                   portal=Path(os.environ["HOSTED_PORTAL_DIR"]) if os.getenv("HOSTED_PORTAL_DIR") else None,
                   cos_bucket=os.getenv("HOSTED_COS_BUCKET", ""),
                   action_secret=required("HOSTED_ACTION_SECRET"),
                   admin_token=required("HOSTED_ADMIN_TOKEN"),
                   archive_dir=archive_dir, archive_bytes=archive_bytes, worker_network=network,
                   s3_bucket=os.getenv("HOSTED_S3_BUCKET", ""), s3_endpoint=s3_endpoint,
                   s3_region=os.getenv("HOSTED_S3_REGION", "auto"),
                   s3_profile=os.getenv("HOSTED_S3_PROFILE", ""),
                   registration_open=os.getenv("HOSTED_REGISTRATION_OPEN") == "1",
                   worker_namespace=namespace)
