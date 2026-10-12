import os
from pathlib import Path


def _list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


XVR_SCHEME = os.getenv("XVR_SCHEME", "http").lower()
XVR_HOST = os.getenv("XVR_HOST", "192.168.1.20")
XVR_HTTP_PORT = int(os.getenv("XVR_HTTP_PORT", "443" if XVR_SCHEME == "https" else "80"))
# Dahua devices ship self-signed certificates, so verification is off by default.
XVR_VERIFY_TLS = os.getenv("XVR_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
XVR_USER = os.getenv("XVR_USER", "")
XVR_PASS = os.getenv("XVR_PASS", "")
XVR_BASE = f"{XVR_SCHEME}://{XVR_HOST}:{XVR_HTTP_PORT}"

CHANNELS = sorted({int(c) for c in _list("CHANNELS", "1,2,3,4")})
EVENT_CODES = _list("EVENT_CODES", "FaceDetection")
EVENT_COOLDOWN_SECONDS = float(os.getenv("EVENT_COOLDOWN_SECONDS", "3"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))

GO2RTC_URL = os.getenv("GO2RTC_URL", "http://go2rtc:1984").rstrip("/")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "events.db"
STATIC_DIR = Path(__file__).parent / "static"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TZ_NAME = os.getenv("TZ", "Asia/Bangkok")

# Web UI login. Empty password = nobody can log in (fail closed).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))

# Shared secret between the detector container and the app's /internal/ endpoints.
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")

HEARTBEAT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30
MAX_BACKOFF_SECONDS = 60
THUMB_WIDTH = 480
RETENTION_INTERVAL_SECONDS = 6 * 3600
