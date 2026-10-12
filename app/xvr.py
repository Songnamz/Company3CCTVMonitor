"""Dahua eventManager.cgi multipart event stream client."""

import asyncio
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

import config

log = logging.getLogger("xvr")

MAX_BUFFER = 1024 * 1024


@dataclass
class XvrEvent:
    code: str
    action: str
    index: int | None
    data: object = None
    raw: str = ""

    @property
    def channel(self) -> int | None:
        return None if self.index is None else self.index + 1


@dataclass
class StreamStatus:
    connected: bool = False
    last_error: str | None = None
    connected_since: float | None = None
    last_message: float | None = None
    url: str = ""
    changed: Callable[[], None] = field(default=lambda: None, repr=False)

    def set(self, connected: bool, error: str | None = None) -> None:
        if connected:
            if self.connected:
                return
            self.connected_since = time.time()
        else:
            if not self.connected and error == self.last_error:
                return
            self.connected_since = None
            self.last_error = error
        self.connected = connected
        self.changed()


def boundary_from_content_type(content_type: str | None) -> str:
    m = re.search(r'boundary\s*=\s*"?([^";]+)"?', content_type or "", re.I)
    b = m.group(1).strip() if m else "myboundary"
    return b[2:] if b.startswith("--") else b


class MultipartParser:
    """Incremental multipart/x-mixed-replace parser.

    Uses Content-Length when present so an event is emitted as soon as its body
    arrives (instead of waiting for the next boundary), but never reads past the
    next boundary in case the device reports a wrong length.
    """

    def __init__(self, boundary: str):
        self.delim = b"--" + boundary.encode()
        self.buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            start = self.buf.find(self.delim)
            if start < 0:
                if len(self.buf) > MAX_BUFFER:  # garbage; keep a possible partial delimiter
                    self.buf = self.buf[-len(self.delim):]
                break
            if start:
                self.buf = self.buf[start:]
            pos = len(self.delim)
            # Skip line break(s) after the delimiter.
            while self.buf[pos:pos + 1] in (b"\r", b"\n"):
                pos += 1
            if len(self.buf) - pos < 9:  # wait until we can tell headers from body
                break

            headers: dict[str, str] = {}
            rest = self.buf[pos:pos + 64].lstrip()
            if rest.lower().startswith(b"content-"):
                end, sep = self._header_end(pos)
                if end < 0:
                    if len(self.buf) > MAX_BUFFER:
                        self.buf = b""
                    break
                for line in self.buf[pos:end].decode("latin-1").splitlines():
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
                body_start = end + sep
            else:
                body_start = pos

            nxt = self.buf.find(self.delim, body_start)
            length = headers.get("content-length")
            if length and length.isdigit():
                body_end = body_start + int(length)
                if 0 <= nxt < body_end:
                    body_end = nxt
                elif len(self.buf) < body_end:
                    break
            else:
                if nxt < 0:
                    break
                body_end = nxt

            out.append(self.buf[body_start:body_end])
            self.buf = self.buf[body_end:]
        return out

    def _header_end(self, pos: int) -> tuple[int, int]:
        crlf = self.buf.find(b"\r\n\r\n", pos)
        lf = self.buf.find(b"\n\n", pos)
        if crlf >= 0 and (lf < 0 or crlf <= lf):
            return crlf, 4
        if lf >= 0:
            return lf, 2
        return -1, 0


def parse_events(text: str) -> list[XvrEvent]:
    """Parse 'Code=X;action=Start;index=0;data={...}' (possibly several per part)."""
    events = []
    for chunk in re.split(r"[\r\n]+(?=Code=)", text.strip()):
        chunk = chunk.strip()
        if not chunk.startswith("Code="):
            continue
        head, data = chunk, None
        i = chunk.find(";data=")
        if i >= 0:
            head, raw = chunk[:i], chunk[i + len(";data="):].strip()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw
        fields = {}
        for kv in head.split(";"):
            k, _, v = kv.partition("=")
            fields[k.strip().lower()] = v.strip()
        try:
            index = int(fields.get("index", ""))
        except ValueError:
            index = None
        events.append(XvrEvent(fields.get("code", ""), fields.get("action", ""), index, data, chunk))
    return events


def xvr_tls(verify: bool) -> ssl.SSLContext:
    """TLS settings for the XVR's HTTPS interface.

    Dahua firmware only offers TLS 1.2 with RSA key exchange (e.g. AES256-GCM-SHA384),
    which Python's default cipher list excludes; OpenSSL's DEFAULT list still allows it.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def describe_error(exc: Exception) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class EventListener:
    def __init__(self, status: StreamStatus, on_event: Callable[[XvrEvent], Awaitable[None]]):
        self.status = status
        self.on_event = on_event
        codes = ",".join(config.EVENT_CODES)
        self.url = (f"{config.XVR_BASE}/cgi-bin/eventManager.cgi"
                    f"?action=attach&codes=[{codes}]&heartbeat={config.HEARTBEAT_SECONDS}")
        status.url = self.url

    async def run(self) -> None:
        backoff = 1.0
        auth = httpx.DigestAuth(config.XVR_USER, config.XVR_PASS)
        timeout = httpx.Timeout(config.READ_TIMEOUT_SECONDS, connect=10)
        while True:
            try:
                log.info("Connecting to XVR event stream %s", self.url)
                async with httpx.AsyncClient(auth=auth, timeout=timeout, verify=xvr_tls(config.XVR_VERIFY_TLS)) as client:
                    async with client.stream("GET", self.url) as resp:
                        if resp.status_code != 200:
                            hint = ""
                            if resp.is_redirect:
                                hint = f" (redirect to {resp.headers.get('location')} - set XVR_SCHEME/XVR_HTTP_PORT)"
                            raise RuntimeError(f"HTTP {resp.status_code} from eventManager.cgi{hint}")
                        ctype = resp.headers.get("content-type")
                        boundary = boundary_from_content_type(ctype)
                        log.info("Event stream connected (Content-Type: %s)", ctype)
                        self.status.set(True)
                        parser = MultipartParser(boundary)
                        async for chunk in resp.aiter_bytes():
                            for body in parser.feed(chunk):
                                backoff = 1.0
                                await self._handle_part(body)
                        raise RuntimeError("event stream closed by XVR")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = describe_error(exc)
                log.warning("Event stream error: %s (reconnecting in %.0fs)", err, backoff)
                self.status.set(False, err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, config.MAX_BACKOFF_SECONDS)

    async def _handle_part(self, body: bytes) -> None:
        text = body.decode("utf-8", "replace").strip()
        if not text:
            return
        self.status.last_message = time.time()
        if text.startswith("Heartbeat"):
            log.debug("Heartbeat")
            return
        events = parse_events(text)
        if not events:
            log.info("Unrecognised event part: %r", text[:300])
        for ev in events:
            log.info("XVR event code=%s action=%s index=%s", ev.code, ev.action, ev.index)
            log.debug("XVR event data: %s", ev.raw[:2000])
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("Error handling event %s", ev.code)
