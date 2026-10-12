import asyncio
import hmac
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from auth import COOKIE, Auth, LoginRequired, client_ip, is_https, safe_next
from capture import Capturer, is_jpeg, people_thumbnail, remove_files, retention_loop, safe_path, store_files
from db import Database
from export import zip_events
from hub import Hub, sse
import schedule
from xvr import EventListener, StreamStatus

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
db = Database(config.DB_PATH)
hub = Hub()
stream_status = StreamStatus()
auth = Auth()
detector = {"last_heartbeat": None, "info": None, "online": False}
capture_schedule = schedule.default(config.CHANNELS)
retention = {"days": config.RETENTION_DAYS, "max_gb": config.RETENTION_MAX_GB}
storage_usage: dict = {"used_bytes": None, "measured_at": None}
retention_wake = asyncio.Event()
DETECTOR_TIMEOUT = 30


def event_out(row: dict) -> dict:
    detections = row.get("detections")
    return {
        "id": row["id"],
        "ts": row["ts"],
        "channel": row["channel"],
        "code": row["code"],
        "action": row["action"],
        "source": row["source"],
        "image": f"/snapshots/{row['image']}" if row["image"] else None,
        "thumb": f"/snapshots/{row['thumb']}" if row["thumb"] else None,
        "persons": row.get("persons"),
        "shirtless": None if row.get("shirtless") is None else bool(row["shirtless"]),
        "shirtless_score": row.get("shirtless_score"),
        "reviewed": bool(row.get("reviewed")),
        "detections": json.loads(detections) if detections else None,
    }


def midnight_today() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def detector_status() -> dict:
    info = detector["info"] or {}
    return {
        "online": detector["online"],
        "last_heartbeat": detector["last_heartbeat"],
        "inference_ms": info.get("inference_ms"),
        "channels": info.get("channels"),
    }


def capture_status() -> dict:
    now = time.time()
    cameras = {}
    for c in config.CHANNELS:
        active, reason = schedule.capture_allowed(capture_schedule, c, now)
        paused = capture_schedule["cameras"][str(c)].get("paused_until")
        cameras[str(c)] = {"active": active, "reason": reason,
                           "paused_until": paused if paused and paused > now else None}
    return {"active_channels": [c for c in config.CHANNELS if cameras[str(c)]["active"]], "cameras": cameras}


def _status_sync() -> dict:
    today, today_shirtless = db.count_since(midnight_today())
    return {
        "connected": stream_status.connected,
        "connected_since": stream_status.connected_since,
        "last_error": stream_status.last_error,
        "last_message": stream_status.last_message,
        "last_event_time": db.last_event_ts(),
        "today_count": today,
        "today_shirtless": today_shirtless,
        "event_codes": config.EVENT_CODES,
        "detector": detector_status(),
        "capture": capture_status(),
    }


async def get_status() -> dict:
    return await asyncio.to_thread(_status_sync)


async def broadcast_status() -> None:
    hub.publish("status", await get_status())


def schedule_status_broadcast() -> None:
    asyncio.get_running_loop().create_task(broadcast_status())


def validate_retention(data) -> dict:
    try:
        days, max_gb = int(data["days"]), float(data["max_gb"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("days and max_gb must be numbers")
    if not 0 <= days <= 3650:
        raise ValueError("days must be between 0 and 3650")
    if not 0 <= max_gb <= 100_000:
        raise ValueError("max_gb must be between 0 and 100000")
    if days == 0 and max_gb == 0:
        raise ValueError("set at least one limit, or the disk will eventually fill up")
    return {"days": days, "max_gb": max_gb}


def _storage_sync() -> dict:
    events, oldest = db.stats()
    disk = shutil.disk_usage(config.DATA_DIR)
    return {**retention, **storage_usage, "events": events, "oldest_ts": oldest,
            "disk_total_bytes": disk.total, "disk_free_bytes": disk.free}


def on_retention_done(usage: dict) -> None:
    storage_usage.update(usage)
    schedule_status_broadcast()  # counters may have dropped


def on_saved(row: dict) -> None:
    hub.publish("event", event_out(row))
    schedule_status_broadcast()


async def status_watchdog() -> None:
    """Notice the detector going offline and schedule boundaries passing."""
    last_capture = capture_status()
    while True:
        await asyncio.sleep(10)
        changed = False
        last = detector["last_heartbeat"]
        online = last is not None and time.time() - last < DETECTOR_TIMEOUT
        if online != detector["online"]:
            detector["online"] = online
            log.warning("Detector %s", "online" if online else "OFFLINE (no heartbeat)")
            changed = True
        cap = capture_status()
        if cap != last_capture:
            log.info("Capture active on channels %s", cap["active_channels"])
            last_capture = cap
            changed = True
        if changed:
            await broadcast_status()


@asynccontextmanager
async def lifespan(app: FastAPI):
    stream_status.changed = schedule_status_broadcast
    stored = await asyncio.to_thread(db.get_setting, "schedule")
    if stored:
        capture_schedule.update(schedule.validate(json.loads(stored), config.CHANNELS))
        if "cameras" not in json.loads(stored):  # saved in the old global format: store migrated
            await asyncio.to_thread(db.set_setting, "schedule", json.dumps(capture_schedule))
    stored = await asyncio.to_thread(db.get_setting, "retention")
    if stored:
        try:
            retention.update(validate_retention(json.loads(stored)))
        except ValueError as exc:
            log.warning("Ignoring stored retention settings: %s", exc)
    capturer = Capturer(db, on_saved, lambda ch: schedule.capture_allowed(capture_schedule, ch)[0])
    listener = EventListener(stream_status, capturer.handle)
    log.info("Channels=%s EventCodes=%s Cooldown=%ss Retention=%sd/%sGB XVR=%s",
             config.CHANNELS, config.EVENT_CODES, config.EVENT_COOLDOWN_SECONDS,
             retention["days"], retention["max_gb"], config.XVR_BASE)
    tasks = [
        asyncio.create_task(listener.run(), name="event-stream"),
        asyncio.create_task(retention_loop(db, lambda: retention, retention_wake, on_retention_done),
                            name="retention"),
        asyncio.create_task(status_watchdog(), name="status-watchdog"),
    ]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await capturer.close()


app = FastAPI(title="CCTV Monitor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(LoginRequired, auth=auth)


@app.get("/login")
async def login_page():
    return FileResponse(config.STATIC_DIR.parent / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    target = safe_next(form.get("next"))
    ip = client_ip(request)
    if auth.locked_out(ip):
        log.warning("Login blocked for %s (too many failed attempts)", ip)
        return RedirectResponse(f"/login?error=locked&next={quote(target)}", status_code=303)
    if not auth.check_password(form.get("password", "")):
        auth.record_failure(ip)
        log.warning("Failed login from %s", ip)
        await asyncio.sleep(1)
        return RedirectResponse(f"/login?error=1&next={quote(target)}", status_code=303)
    auth.clear_failures(ip)
    log.info("Login from %s", ip)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(COOKIE, auth.make_token(), max_age=auth.max_age, httponly=True,
                        secure=is_https(request), samesite="lax", path="/")
    return response


@app.post("/api/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


def check_internal_token(request: Request) -> None:
    token = request.headers.get("x-internal-token", "")
    if not config.INTERNAL_TOKEN or not hmac.compare_digest(token.encode(), config.INTERNAL_TOKEN.encode()):
        raise HTTPException(403, "bad internal token")


@app.post("/internal/detections")
async def internal_detections(request: Request):
    """The detector posts a frame in which a new person (or a shirtless person) appeared."""
    check_internal_token(request)
    meta = json.loads(request.headers.get("x-meta", "{}"))
    jpeg = await request.body()
    channel, ts, people = int(meta["channel"]), float(meta["ts"]), meta.get("people") or []
    replace_id = meta.get("replace_id")
    if not replace_id and not schedule.capture_allowed(capture_schedule, channel, ts)[0]:
        return {"skipped": "outside capture schedule"}
    scores = [p["shirtless_score"] for p in people if p.get("shirtless_score") is not None]
    detect = capture_schedule["cameras"].get(str(channel), {}).get("detect", {})
    # "No shirt" unticked for this camera: still store the person, but no no-shirt flag/alert.
    shirtless = detect.get("none", True) and any(p.get("shirtless") for p in people)
    thumb = await asyncio.to_thread(people_thumbnail, jpeg, [p["box"] for p in people])
    image, thumb_rel = await asyncio.to_thread(store_files, ts, channel, jpeg, thumb)
    if replace_id:
        # A sharper frame of the same people: swap it into the existing event.
        res = await asyncio.to_thread(db.replace_image, int(replace_id), image, thumb_rel, len(people),
                                      int(shirtless), max(scores, default=None), json.dumps(people))
        if not res:  # event was deleted meanwhile
            await asyncio.to_thread(remove_files, {"image": image, "thumb": thumb_rel})
            return {"skipped": "event gone"}
        old, new = res
        await asyncio.to_thread(remove_files, old)
        log.info("Event #%s: replaced with a sharper frame (%s)", replace_id, meta.get("reason"))
        hub.publish("updated", event_out(new))
        return {"id": new["id"]}
    row = await asyncio.to_thread(
        db.insert_event, ts, channel, "Person", meta.get("reason"), image, thumb_rel, "ai", None,
        len(people), int(shirtless), max(scores, default=None), json.dumps(people))
    log.info("Saved event #%s channel=%s people=%d shirtless=%s", row["id"], channel, len(people), shirtless)
    on_saved(row)
    return {"id": row["id"]}


@app.post("/internal/heartbeat")
async def internal_heartbeat(request: Request):
    check_internal_token(request)
    detector["info"] = await request.json()
    detector["last_heartbeat"] = time.time()
    if not detector["online"]:
        detector["online"] = True
        log.info("Detector online")
        await broadcast_status()
    # The detector only analyses cameras that are inside the capture schedule.
    return {"ok": True, "active_channels": schedule.active_channels(capture_schedule, config.CHANNELS),
            "detect": schedule.detect_settings(capture_schedule),
            "zones": schedule.zone_settings(capture_schedule)}


@app.get("/api/schedule")
async def api_get_schedule():
    return {**capture_schedule, "capture": capture_status()}


@app.put("/api/schedule")
async def api_put_schedule(request: Request):
    try:
        new = schedule.validate(await request.json(), config.CHANNELS)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc))
    capture_schedule.clear()
    capture_schedule.update(new)
    await asyncio.to_thread(db.set_setting, "schedule", json.dumps(new))
    log.info("Capture schedule updated: %s", ", ".join(
        f"CH{c}: {'scheduled' if v['enabled'] else '24/7'} {len(v['rules'])} ranges"
        f" detect={'+'.join(k for k, on in v['detect'].items() if on) or 'nothing'}"
        f" areas={len(v['zones']) or 'whole view'}"
        + (" paused" if v["paused_until"] and v["paused_until"] > time.time() else "")
        for c, v in new["cameras"].items()))
    await broadcast_status()
    return {**capture_schedule, "capture": capture_status()}


@app.get("/api/cameras/{channel}/frame.jpg")
async def api_camera_frame(channel: int):
    """A current full-resolution frame, as the background for drawing detection areas."""
    if channel not in config.CHANNELS:
        raise HTTPException(404, "unknown channel")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{config.GO2RTC_URL}/api/frame.jpeg", params={"src": f"ch{channel}_hd"})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"no frame from CH{channel}: {exc}")
    if not is_jpeg(resp.content):
        raise HTTPException(502, f"no frame from CH{channel}")
    return Response(resp.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/events/{event_id}/not-shirtless")
async def api_not_shirtless(event_id: int):
    """A person reviewed a 'no shirt' alert and marked it as wrong."""
    row = await asyncio.to_thread(db.mark_not_shirtless, event_id)
    if not row:
        raise HTTPException(404, "event not found")
    hub.publish("updated", event_out(row))
    await broadcast_status()
    return event_out(row)


@app.get("/api/auth")
async def auth_check():
    """nginx auth_request target for the go2rtc paths; LoginRequired already ran."""
    return Response(status_code=204)


@app.get("/healthz")
async def healthz():
    await asyncio.to_thread(db.ping)
    return {"ok": True, "event_stream_connected": stream_status.connected}


@app.get("/api/config")
async def api_config():
    offset = datetime.now().astimezone().utcoffset()
    return {
        "channels": [{"id": c, "name": f"CH{c}", "stream": f"ch{c}", "stream_hd": f"ch{c}_hd"}
                     for c in config.CHANNELS],
        "tz": config.TZ_NAME,
        "utc_offset": int(offset.total_seconds()) if offset else 0,
        "event_codes": config.EVENT_CODES,
    }


@app.get("/api/storage")
async def api_get_storage():
    return await asyncio.to_thread(_storage_sync)


@app.put("/api/storage")
async def api_put_storage(request: Request):
    try:
        new = validate_retention(await request.json())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    retention.update(new)
    await asyncio.to_thread(db.set_setting, "retention", json.dumps(new))
    log.info("Retention updated: %s days, %s GB", new["days"], new["max_gb"])
    retention_wake.set()  # apply now rather than at the next hourly run
    return await asyncio.to_thread(_storage_sync)


@app.get("/api/status")
async def api_status():
    return await get_status()


@app.get("/api/events")
async def api_events(
    channel: int | None = None,
    start: float | None = None,
    end: float | None = None,
    before: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    shirtless: bool | None = None,
):
    rows, has_more = await asyncio.to_thread(db.list_events, channel, start, end, before, limit, shirtless)
    return {"events": [event_out(r) for r in rows], "has_more": has_more}


@app.delete("/api/events/{event_id}")
async def api_delete_event(event_id: int):
    row = await asyncio.to_thread(db.delete_event, event_id)
    if not row:
        raise HTTPException(404, "event not found")
    await asyncio.to_thread(remove_files, row)
    hub.publish("deleted", {"id": event_id})
    await broadcast_status()
    return {"ok": True}


@app.post("/api/events/delete")
async def api_delete_many(request: Request):
    """Bulk delete: body {"ids": [1, 2, ...]} (max 1000 per request)."""
    body = await request.json()
    try:
        ids = sorted({int(i) for i in body.get("ids", [])})
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "ids must be a list of integers")
    if not ids or len(ids) > 1000:
        raise HTTPException(400, "send between 1 and 1000 ids")
    return await delete_matching(ids=ids)


def parse_filters(data: dict) -> dict:
    """Filters shared by range summary / delete / export: start, end, channel, type, ids."""
    f = {}
    try:
        if data.get("ids"):
            raw = data["ids"]
            f["ids"] = sorted({int(i) for i in (raw.split(",") if isinstance(raw, str) else raw)})
        for key in ("start", "end"):
            if data.get(key) not in (None, ""):
                f[key] = float(data[key])
        if data.get("channel") not in (None, ""):
            f["channel"] = int(data["channel"])
    except (TypeError, ValueError):
        raise HTTPException(400, "bad filter value")
    kind = data.get("type") or ""
    if kind == "shirtless":
        f["shirtless"] = True
    elif kind:
        raise HTTPException(400, "type must be shirtless")
    if "ids" not in f and ("start" not in f or "end" not in f or f["end"] <= f["start"]):
        raise HTTPException(400, "choose a start and end time (end after start)")
    return f


async def delete_matching(**filters) -> dict:
    rows = await asyncio.to_thread(db.delete_events, **filters)
    for row in rows:
        await asyncio.to_thread(remove_files, row)
    deleted = [r["id"] for r in rows]
    log.info("Deleted %d events (%s)", len(deleted), {k: v for k, v in filters.items() if k != "ids"} or "selection")
    hub.publish("deleted_many", {"ids": deleted})
    await broadcast_status()
    return {"deleted": deleted}


@app.get("/api/events/summary")
async def api_events_summary(request: Request):
    """How many events (and how many MB of images) a date/time range covers."""
    f = parse_filters(dict(request.query_params))

    def summarise():
        rows = db.find_events(**f)
        size = 0
        for r in rows:
            for key in ("image", "thumb"):
                p = safe_path(r.get(key))
                if p and p.exists():
                    size += p.stat().st_size
        return {"count": len(rows), "bytes": size,
                "first": rows[0]["ts"] if rows else None, "last": rows[-1]["ts"] if rows else None}

    return await asyncio.to_thread(summarise)


@app.post("/api/events/delete_range")
async def api_delete_range(request: Request):
    f = parse_filters(await request.json())
    if "ids" in f:
        raise HTTPException(400, "use /api/events/delete for a selection")
    return await delete_matching(**f)


@app.post("/api/events/export")
async def api_export(request: Request):
    """Download events as a zip. Form fields: ids=1,2,3  or  start, end[, channel, type]."""
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    f = parse_filters(form)
    rows = await asyncio.to_thread(db.find_events, **f)
    if not rows:
        raise HTTPException(404, "no events to export")
    first, last = datetime.fromtimestamp(rows[0]["ts"]), datetime.fromtimestamp(rows[-1]["ts"])
    name = f"cctv_{first:%Y-%m-%d_%H%M}_to_{last:%Y-%m-%d_%H%M}_{len(rows)}events.zip"
    log.info("Exporting %d events as %s", len(rows), name)
    return StreamingResponse(zip_events(rows), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/live")
async def api_live(request: Request):
    queue = hub.subscribe()
    initial = await get_status()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            yield sse("status", initial)
            while hub.is_subscribed(queue):
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    msg = sse("ping", {"t": time.time()})
                if await request.is_disconnected():
                    break
                yield msg
        finally:
            hub.unsubscribe(queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


app.mount("/snapshots", StaticFiles(directory=config.SNAPSHOT_DIR), name="snapshots")
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="ui")
