#!/usr/bin/env python3
"""Mock Dahua XVR for local testing (not used in production).

Endpoints (HTTP Digest auth, like the real device):
  /cgi-bin/eventManager.cgi?action=attach&codes=[...]&heartbeat=N   multipart event stream
  /cgi-bin/snapshot.cgi?channel=N                                    JPEG snapshot
Control endpoints (no auth):
  /trigger?channel=1&code=FaceDetection&action=Start&style=normal|multiline|nolength|double
  /drop        close all open event streams (tests reconnect)
"""

import hashlib
import io
import json
import os
import queue
import re
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

USER = os.getenv("MOCK_USER", "admin")
PASS = os.getenv("MOCK_PASS", "test123")
REALM = "Login to MOCKXVR"
PORT = int(os.getenv("MOCK_PORT", "80"))
# Channels whose snapshot.cgi returns an HTML error instead of a JPEG (tests go2rtc fallback).
BAD_SNAPSHOT = {int(c) for c in os.getenv("MOCK_BAD_SNAPSHOT_CHANNELS", "3").split(",") if c}
BOUNDARY = "myboundary"

subscribers: set[queue.Queue] = set()
sub_lock = threading.Lock()
nonces: set[str] = set()


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def broadcast(item):
    with sub_lock:
        for q in subscribers:
            q.put(item)


def part(body: str, with_length=True) -> bytes:
    data = body.encode() + b"\r\n"
    head = f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n"
    if with_length:
        head += f"Content-Length: {len(data)}\r\n"
    return head.encode() + b"\r\n" + data


def face_body(channel: int, code: str, action: str, multiline: bool) -> str:
    data = {"Faces": [{"BoundingBox": [2000, 1500, 3000, 3200], "Center": [2500, 2350]}],
            "Name": "FaceDetection", "UTC": int(time.time()), "EventSeq": secrets.randbelow(10000)}
    js = json.dumps(data, indent=3 if multiline else None)
    return f"Code={code};action={action};index={channel - 1};data={js}"


def snapshot(channel: int) -> bytes:
    img = Image.new("RGB", (1280, 720), (20 + 50 * channel % 200, 40, 90))
    d = ImageDraw.Draw(img)
    d.rectangle([520, 200, 760, 500], outline=(255, 255, 0), width=6)
    d.text((40, 40), f"MOCK CH{channel}  {datetime.now():%Y-%m-%d %H:%M:%S}", fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[mock] {self.address_string()} {fmt % args}", flush=True)

    def send_plain(self, code: int, text: str, ctype="text/plain", extra=None):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        h = self.headers.get("Authorization", "")
        if h.startswith("Digest "):
            f = dict(re.findall(r'(\w+)="?([^",]*)"?', h[7:]))
            if f.get("username") == USER and f.get("nonce") in nonces:
                ha1 = md5(f"{USER}:{REALM}:{PASS}")
                ha2 = md5(f"{self.command}:{f.get('uri')}")
                expect = md5(f"{ha1}:{f['nonce']}:{f.get('nc')}:{f.get('cnonce')}:{f.get('qop')}:{ha2}")
                if f.get("response") == expect:
                    return True
        nonce = secrets.token_hex(16)
        nonces.add(nonce)
        self.send_plain(401, "Unauthorized", extra={
            "WWW-Authenticate": f'Digest realm="{REALM}", qop="auth", nonce="{nonce}", opaque="{secrets.token_hex(8)}"'})
        return False

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path == "/trigger":
            broadcast(("event", int(q.get("channel", 1)), q.get("code", "FaceDetection"),
                       q.get("action", "Start"), q.get("style", "normal")))
            return self.send_plain(200, "OK\n")
        if u.path == "/drop":
            broadcast(None)
            return self.send_plain(200, "dropped\n")

        if not self.authorized():
            return
        if u.path == "/cgi-bin/eventManager.cgi":
            return self.event_stream(q)
        if u.path == "/cgi-bin/snapshot.cgi":
            ch = int(q.get("channel", 1))
            if ch in BAD_SNAPSHOT:
                return self.send_plain(200, "<html>Error</html>", "text/html")
            jpeg = snapshot(ch)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)
            return
        self.send_plain(404, "Not found")

    def event_stream(self, q):
        print(f"[mock] event stream attached, raw path: {self.path}", flush=True)
        hb = float(q.get("heartbeat", 5))
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Connection", "close")
        self.end_headers()
        inbox: queue.Queue = queue.Queue()
        with sub_lock:
            subscribers.add(inbox)
        try:
            while True:
                try:
                    item = inbox.get(timeout=hb)
                except queue.Empty:
                    self.wfile.write(part("Heartbeat"))
                    self.wfile.flush()
                    continue
                if item is None:
                    return
                _, ch, code, action, style = item
                if style == "double":
                    # Two events in one part, as some firmwares do.
                    body = face_body(ch, code, action, False) + "\r\n" + face_body(ch % 4 + 1, code, action, False)
                    payload = part(body)
                elif style == "split":
                    # Deliver the part in tiny chunks to exercise incremental parsing.
                    payload = part(face_body(ch, code, action, True))
                    for i in range(0, len(payload), 7):
                        self.wfile.write(payload[i:i + 7])
                        self.wfile.flush()
                        time.sleep(0.005)
                    continue
                else:
                    payload = part(face_body(ch, code, action, style == "multiline"),
                                   with_length=style != "nolength")
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with sub_lock:
                subscribers.discard(inbox)
            print("[mock] event stream closed", flush=True)


if __name__ == "__main__":
    print(f"[mock] Dahua mock listening on :{PORT} (user={USER})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
