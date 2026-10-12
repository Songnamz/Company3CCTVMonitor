"""Snapshot capture for face events, plus retention cleanup."""

import asyncio
import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image

import config
from db import Database
from xvr import XvrEvent, describe_error, xvr_tls

log = logging.getLogger("capture")

TRIGGER_ACTIONS = {"start", "pulse"}


def is_jpeg(data: bytes) -> bool:
    return len(data) > 4 and data[:2] == b"\xff\xd8"


def make_thumbnail(jpeg: bytes) -> bytes:
    with Image.open(io.BytesIO(jpeg)) as img:
        img = img.convert("RGB")
        img.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH * 4))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=75, optimize=True)
        return out.getvalue()


def people_thumbnail(jpeg: bytes, boxes: list[list[int]]) -> bytes:
    """Thumbnail cropped (16:9) around everyone detected, so small figures stay visible."""
    with Image.open(io.BytesIO(jpeg)) as img:
        img = img.convert("RGB")
        if boxes:
            W, H = img.size
            x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            pad = 0.15 * max(x2 - x1, y2 - y1)
            x1, y1, x2, y2 = x1 - pad, y1 - pad, x2 + pad, y2 + pad
            # Grow to 16:9, at least 480x270, centred on the people, kept inside the frame.
            w = max(x2 - x1, (y2 - y1) * 16 / 9, 480)
            h = min(w * 9 / 16, H)
            w = min(h * 16 / 9, W)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            left = min(max(cx - w / 2, 0), W - w)
            top = min(max(cy - h / 2, 0), H - h)
            img = img.crop((int(left), int(top), int(left + w), int(top + h)))
        img.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH * 4), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


def store_files(ts: float, channel: int, jpeg: bytes, thumb: bytes) -> tuple[str, str]:
    """Write image + thumbnail under SNAPSHOT_DIR/YYYY/MM/DD; returns their relative paths."""
    dt = datetime.fromtimestamp(ts)
    stem = f"{dt:%Y/%m/%d}/{dt:%H%M%S}_{dt.microsecond // 1000:03d}_ch{channel}"
    image, thumb_rel = f"{stem}.jpg", f"{stem}_thumb.jpg"
    for rel, content in ((image, jpeg), (thumb_rel, thumb)):
        path = config.SNAPSHOT_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
    return image, thumb_rel


def safe_path(rel: str | None) -> Path | None:
    if not rel:
        return None
    path = (config.SNAPSHOT_DIR / rel).resolve()
    root = config.SNAPSHOT_DIR.resolve()
    return path if path.is_relative_to(root) and path != root else None


def remove_files(row: dict) -> None:
    for key in ("image", "thumb"):
        path = safe_path(row.get(key))
        if path:
            path.unlink(missing_ok=True)


class Capturer:
    def __init__(self, db: Database, on_saved: Callable[[dict], None], allowed: Callable[[int], bool]):
        self.db = db
        self.on_saved = on_saved
        self.allowed = allowed
        self.codes = {c.lower() for c in config.EVENT_CODES}
        self.capture_all = "all" in self.codes
        self.last_trigger: dict[int, float] = {}
        self.tasks: set[asyncio.Task] = set()
        self.client = httpx.AsyncClient(
            auth=httpx.DigestAuth(config.XVR_USER, config.XVR_PASS),
            timeout=httpx.Timeout(10, connect=5),
            verify=xvr_tls(config.XVR_VERIFY_TLS),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def handle(self, ev: XvrEvent) -> None:
        """Called from the event stream loop; must return quickly."""
        if ev.action.lower() not in TRIGGER_ACTIONS:
            return
        if not self.capture_all and ev.code.lower() not in self.codes:
            return
        channel = ev.channel
        if channel not in config.CHANNELS:
            log.info("Ignoring %s on channel %s (not in CHANNELS)", ev.code, channel)
            return
        if not self.allowed(channel):
            log.debug("Channel %s outside capture schedule, skipping", channel)
            return
        now = time.monotonic()
        if now - self.last_trigger.get(channel, -1e9) < config.EVENT_COOLDOWN_SECONDS:
            log.debug("Cooldown active for channel %s, skipping", channel)
            return
        self.last_trigger[channel] = now
        task = asyncio.create_task(self.capture(ev, channel, time.time()))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _fetch(self, url: str, auth: bool) -> bytes:
        resp = await self.client.get(url, auth=httpx.USE_CLIENT_DEFAULT if auth else None)
        resp.raise_for_status()
        if not is_jpeg(resp.content):
            raise ValueError(f"not a JPEG ({resp.headers.get('content-type')}, {len(resp.content)} bytes)")
        return resp.content

    async def grab(self, channel: int) -> tuple[bytes, bytes, str] | None:
        # go2rtc first: a full-resolution frame from the main stream. The XVR's own
        # snapshot.cgi is only sub-stream resolution (352x288) on this model.
        sources = [
            ("go2rtc", f"{config.GO2RTC_URL}/api/frame.jpeg?src=ch{channel}_hd", False),
            ("xvr", f"{config.XVR_BASE}/cgi-bin/snapshot.cgi?channel={channel}", True),
        ]
        for name, url, auth in sources:
            try:
                jpeg = await self._fetch(url, auth)
                thumb = await asyncio.to_thread(make_thumbnail, jpeg)
                return jpeg, thumb, name
            except Exception as exc:
                log.warning("Snapshot from %s failed for channel %s: %s", name, channel, describe_error(exc))
        return None

    async def capture(self, ev: XvrEvent, channel: int, ts: float) -> None:
        try:
            started = time.monotonic()
            result = await self.grab(channel)
            image = thumb = source = None
            if result:
                jpeg, thumb_bytes, source = result
                image, thumb = await asyncio.to_thread(store_files, ts, channel, jpeg, thumb_bytes)
            else:
                log.error("No snapshot available for channel %s; storing event without image", channel)
            data = json.dumps(ev.data) if ev.data is not None else None
            row = await asyncio.to_thread(
                self.db.insert_event, ts, channel, ev.code, ev.action, image, thumb, source, data)
            log.info("Saved event #%s channel=%s code=%s source=%s in %.2fs",
                     row["id"], channel, ev.code, source, time.monotonic() - started)
            self.on_saved(row)
        except Exception:
            log.exception("Capture failed for channel %s", channel)


def run_retention(db: Database, days: int) -> None:
    cutoff = time.time() - days * 86400
    rows = db.delete_older_than(cutoff)
    for row in rows:
        remove_files(row)
    orphans = 0
    root = config.SNAPSHOT_DIR
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                orphans += 1
        for dirpath, _, _ in os.walk(root, topdown=False):
            if Path(dirpath) != root and not os.listdir(dirpath):
                os.rmdir(dirpath)
    log.info("Retention: removed %d events and %d orphan files older than %d days", len(rows), orphans, days)


async def retention_loop(db: Database, on_change: Callable[[], None]) -> None:
    if config.RETENTION_DAYS <= 0:
        log.info("Retention disabled (RETENTION_DAYS=%s)", config.RETENTION_DAYS)
        return
    while True:
        try:
            await asyncio.to_thread(run_retention, db, config.RETENTION_DAYS)
            on_change()
        except Exception:
            log.exception("Retention run failed")
        await asyncio.sleep(config.RETENTION_INTERVAL_SECONDS)
