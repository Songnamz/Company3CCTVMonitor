"""RTSP cameras that are not on the XVR (e.g. TP-Link VIGI/Tapo), added from the Settings tab.

Stored as JSON in the settings table:
    {"next_id": 6,
     "cameras": [{"id": 5, "name": "Gate", "host": "192.168.1.7", "port": 554,
                  "user": "admin", "password": "...", "main": "/stream1", "sub": "/stream2"}]}

Each camera gets a channel number after the XVR's (never reused, so old events keep
pointing at the right camera) and go2rtc streams chN (sub) / chN_hd (main), registered
through go2rtc's API. go2rtc keeps them only in memory (its config is read-only), so
they are registered again whenever go2rtc restarts.
"""

import asyncio
import json
import logging
import re
from urllib.parse import quote

import httpx

import config

log = logging.getLogger("cameras")

SYNC_SECONDS = 15
MAX_CAMERAS = 16
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_PATH = re.compile(r"^/[\x21-\x7e]{0,255}$")  # printable, no spaces


def rtsp_url(cam: dict, path: str) -> str:
    # Credentials are percent-encoded, so passwords with @ : / # etc. work.
    cred = f"{quote(cam['user'], safe='')}:{quote(cam['password'], safe='')}@" if cam["user"] else ""
    return f"rtsp://{cred}{cam['host']}:{cam['port']}{path}"


def public(cam: dict) -> dict:
    """A camera as shown to the browser: never the password."""
    return {k: v for k, v in cam.items() if k != "password"} | {"has_password": bool(cam["password"])}


def validate(data: dict, old: dict | None = None) -> dict:
    """Check a camera from the Settings form. An empty password keeps the old one."""
    if not isinstance(data, dict):
        raise ValueError("camera must be an object")
    host = str(data.get("host") or "").strip()
    if not _HOST.match(host):
        raise ValueError("enter the camera's IP address or host name")
    try:
        port = int(data.get("port") or 554)
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    paths = {}
    for key, label in (("main", "Main stream path"), ("sub", "Sub stream path")):
        p = str(data.get(key) or "").strip()
        if p and not p.startswith("/"):
            p = "/" + p
        if not _PATH.match(p):
            raise ValueError(f"{label} must start with / and have no spaces")
        paths[key] = p
    name = str(data.get("name") or "").strip()[:40]
    user = str(data.get("user") or "").strip()[:64]
    password = str(data.get("password") or "")[:128]
    if not password and old and user == old["user"]:
        password = old["password"]
    return {"name": name, "host": host, "port": port, "user": user, "password": password, **paths}


async def probe(cam: dict) -> dict:
    """Open both streams with ffprobe: {"main": {...}, "sub": {...}} with codec/size or an error."""
    async def one(path: str) -> dict:
        cmd = ["ffprobe", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               "-timeout", "8000000", "-select_streams", "v:0",
               "-show_entries", "stream=codec_name,width,height,avg_frame_rate", "-of", "json",
               rtsp_url(cam, path)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 20)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "no answer within 20 s"}
        streams = json.loads(out or b"{}").get("streams") if proc.returncode == 0 else None
        if not streams:
            lines = err.decode(errors="replace").strip().splitlines()
            msg = lines[-1] if lines else f"ffprobe exit {proc.returncode}"
            # ffprobe repeats the URL in its errors; keep the password out of the reply.
            msg = msg.replace(rtsp_url(cam, path), path)
            if "401" in msg:
                msg = "wrong username or password (401 Unauthorized)"
            return {"ok": False, "error": msg}
        s = streams[0]
        num, _, den = str(s.get("avg_frame_rate", "0/1")).partition("/")
        fps = round(int(num) / int(den)) if num.isdigit() and den.isdigit() and int(den) else None
        return {"ok": True, "codec": s.get("codec_name"), "width": s.get("width"),
                "height": s.get("height"), "fps": fps}

    main, sub = await asyncio.gather(one(cam["main"]), one(cam["sub"]))
    return {"main": main, "sub": sub}


class Cameras:
    def __init__(self, db):
        self.db = db
        self.items: list[dict] = []
        self.next_id = max(config.CHANNELS, default=0) + 1
        self.registered: dict[str, str] = {}  # go2rtc stream name -> source we registered
        self.wake = asyncio.Event()

    def load(self) -> None:
        stored = self.db.get_setting("cameras")
        if stored:
            data = json.loads(stored)
            self.items = data.get("cameras", [])
            self.next_id = max(data.get("next_id", 0), self.next_id)

    def _save(self) -> None:
        self.db.set_setting("cameras", json.dumps({"next_id": self.next_id, "cameras": self.items}))

    def ids(self) -> list[int]:
        return [c["id"] for c in self.items]

    def channels(self) -> list[int]:
        """Every camera: the XVR's channels, then the added ones."""
        return config.CHANNELS + self.ids()

    def get(self, cid: int) -> dict | None:
        return next((c for c in self.items if c["id"] == cid), None)

    def name(self, channel: int) -> str:
        cam = self.get(channel)
        return f"CH{channel} · {cam['name']}" if cam and cam["name"] else f"CH{channel}"

    def add(self, cam: dict) -> dict:
        if len(self.items) >= MAX_CAMERAS:
            raise ValueError(f"at most {MAX_CAMERAS} added cameras")
        cam = {"id": self.next_id, **cam}
        self.next_id += 1
        self.items.append(cam)
        self._save()
        self.wake.set()
        return cam

    def update(self, cid: int, cam: dict) -> dict:
        old = self.get(cid)
        old.update(cam)
        self._save()
        self.wake.set()
        return old

    def remove(self, cid: int) -> None:
        self.items = [c for c in self.items if c["id"] != cid]
        self._save()
        self.wake.set()

    def wanted_streams(self) -> dict[str, str]:
        out = {}
        for c in self.items:
            out[f"ch{c['id']}"] = rtsp_url(c, c["sub"])
            out[f"ch{c['id']}_hd"] = rtsp_url(c, c["main"])
        return out

    async def sync_loop(self) -> None:
        """Keep go2rtc's streams matching the added cameras."""
        api = f"{config.GO2RTC_URL}/api/streams"
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                self.wake.clear()
                try:
                    present = set((await client.get(api)).json() or {})
                    wanted = self.wanted_streams()
                    # PUT/DELETE answer 400 "read-only file system" (go2rtc can't save them to its
                    # config file) but do take effect, so the reply status is not checked.
                    for name in [n for n in self.registered if n not in wanted or wanted[n] != self.registered[n]]:
                        if name in present:
                            await client.delete(api, params={"src": name})
                            present.discard(name)
                        del self.registered[name]
                        log.info("Removed go2rtc stream %s", name)
                    for name, src in wanted.items():
                        if name not in present or name not in self.registered:
                            if name in present:  # left from before an app restart: replace it
                                await client.delete(api, params={"src": name})
                            await client.put(api, params={"name": name, "src": src})
                            if name not in self.registered:
                                log.info("Registered go2rtc stream %s", name)
                            self.registered[name] = src
                except Exception as exc:
                    log.warning("Syncing cameras with go2rtc failed: %s", exc)
                try:
                    await asyncio.wait_for(self.wake.wait(), SYNC_SECONDS)
                except asyncio.TimeoutError:
                    pass
