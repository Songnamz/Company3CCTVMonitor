"""Single-password login with stateless signed session cookies.

The signing key is derived from a random per-install secret (stored in /data) and the
password itself, so changing APP_PASSWORD logs out every existing session.
"""

import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

import config

log = logging.getLogger("auth")

COOKIE = "cctv_session"
PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}

FAIL_WINDOW = 15 * 60
MAX_FAILS_PER_IP = 5
MAX_FAILS_TOTAL = 30


def _install_secret() -> bytes:
    path = config.DATA_DIR / "session_secret"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def client_ip(request: Request) -> str:
    # Behind the Cloudflare tunnel every request comes from localhost; Cloudflare
    # sets CF-Connecting-IP to the real visitor address.
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-real-ip")
            or (request.client.host if request.client else "?"))


def is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") == "https"
            or '"https"' in request.headers.get("cf-visitor", ""))


def safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class Auth:
    def __init__(self):
        self.password = config.APP_PASSWORD
        self.key = hmac.new(_install_secret(), self.password.encode(), hashlib.sha256).digest()
        self.max_age = config.SESSION_DAYS * 86400
        self.fails: dict[str, list[float]] = {}
        if not self.password:
            log.warning("APP_PASSWORD is not set - the web UI cannot be logged into")

    def _sign(self, msg: str) -> str:
        return hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest()

    def make_token(self) -> str:
        exp = str(int(time.time()) + self.max_age)
        return f"{exp}.{self._sign(exp)}"

    def valid_token(self, token: str | None) -> bool:
        if not self.password or not token:
            return False
        exp, _, sig = token.partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(exp))

    def check_password(self, password: str) -> bool:
        return bool(self.password) and hmac.compare_digest(password.encode(), self.password.encode())

    # --- brute-force protection ---

    def _recent(self, ip: str) -> list[float]:
        cutoff = time.time() - FAIL_WINDOW
        for key in list(self.fails):
            self.fails[key] = [t for t in self.fails[key] if t > cutoff]
            if not self.fails[key]:
                del self.fails[key]
        return self.fails.get(ip, [])

    def locked_out(self, ip: str) -> bool:
        per_ip = len(self._recent(ip))
        total = sum(len(v) for v in self.fails.values())
        return per_ip >= MAX_FAILS_PER_IP or total >= MAX_FAILS_TOTAL

    def record_failure(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.time())

    def clear_failures(self, ip: str) -> None:
        self.fails.pop(ip, None)


class LoginRequired:
    """ASGI middleware (not BaseHTTPMiddleware, so SSE streaming is unaffected)."""

    def __init__(self, app, auth: Auth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        # /internal/ is for the detector container (token-checked; blocked in nginx).
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS or scope["path"].startswith("/internal/"):
            return await self.app(scope, receive, send)
        request = Request(scope)
        if self.auth.valid_token(request.cookies.get(COOKIE)):
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith(("/api/", "/snapshots/")) or request.method != "GET":
            response = JSONResponse({"detail": "login required"}, status_code=401)
        else:
            response = RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        await response(scope, receive, send)
