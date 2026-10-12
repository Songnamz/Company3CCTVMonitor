"""Person + shirtless detector.

Reads each channel's HD stream from go2rtc (up to DECODE_FPS frames/s), runs YOLO person
detection and a CLIP zero-shot "shirt / no shirt" check on every person, tracks
people between samples and posts an event to the app when a new person appears
or someone is newly seen without a shirt.
"""

import io
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

import httpx
import numpy as np
import open_clip
import torch
from PIL import Image
from ultralytics import YOLO

log = logging.getLogger("detector")


def env_float(name, default):
    return float(os.getenv(name, default))


GO2RTC_URL = os.getenv("GO2RTC_URL", "http://go2rtc:1984").rstrip("/")
APP_URL = os.getenv("APP_URL", "http://app:8000").rstrip("/")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "")
CHANNELS = sorted({int(c) for c in os.getenv("CHANNELS", "1,2,3,4").split(",") if c.strip()})
SAMPLE_SECONDS = env_float("DETECT_INTERVAL_SECONDS", 1)
# Frames a second decoded per camera: every SAMPLE_SECONDS one is analysed, and while
# looking for a best shot as many as the CPU keeps up with.
DECODE_FPS = env_float("DECODE_FPS", 5)
PERSON_CONFIDENCE = env_float("PERSON_CONFIDENCE", 0.3)
PERSON_MIN_HEIGHT = int(os.getenv("PERSON_MIN_HEIGHT", "120"))     # px in the 1080p frame; smaller = far away
SHIRTLESS_THRESHOLD = env_float("SHIRTLESS_THRESHOLD", 0.7)
# Only judge clothing when enough of the body is visible: a head-only or tiny box
# gives CLIP nothing to go on (and produced false alarms in testing).
SHIRT_MIN_HEIGHT = int(os.getenv("SHIRT_MIN_HEIGHT", "150"))
SHIRT_MIN_ASPECT = env_float("SHIRT_MIN_ASPECT", 1.0)                # box height / width
# People cut off by the left/right/top of the frame are judged once fully in view.
EDGE_MARGIN = 6
# After an event, keep checking every frame for this long and swap in a sharper frame
# of the same people (people walking are often motion-blurred in the first frame).
BEST_SHOT_SECONDS = env_float("BEST_SHOT_SECONDS", 8)
BEST_SHOT_GAIN = 1.2      # replace only if at least 20% sharper
# Seconds between frames checked while looking for a sharper shot (people arrive every few
# seconds here, so the search runs most of the time; 0 would keep the AI's CPUs saturated).
BEST_SHOT_INTERVAL = env_float("BEST_SHOT_INTERVAL", 0.5)
# A tracked person's shirt check is reused for this long before CLIP runs again on them.
SHIRT_RECHECK_SECONDS = env_float("SHIRT_RECHECK_SECONDS", 15)
COOLDOWN_SECONDS = env_float("PERSON_COOLDOWN_SECONDS", 10)
# Share of a person's box that must be inside a camera's detection areas to count.
ZONE_MIN_OVERLAP = env_float("ZONE_MIN_OVERLAP", 0.5)
TRACK_TTL_SECONDS = env_float("TRACK_TTL_SECONDS", 15)
YOLO_IMGSZ = int(os.getenv("YOLO_IMGSZ", "640"))
THREADS = int(os.getenv("AI_THREADS", "2"))
MODEL_DIR = os.getenv("MODEL_DIR", "/models")
# Events the app couldn't take (e.g. while it restarts) are queued and retried in order;
# older than this they're dropped (the queue also holds at most OUTBOX_MAX frames).
OUTBOX_MAX_AGE = 600
OUTBOX_MAX = 50
OUTBOX_RETRY_SECONDS = 5
# The main loop touches this file; if it stops for STALL_SECONDS the process exits so
# Docker restarts it (the compose healthcheck reads the same file).
ALIVE_FILE = "/tmp/detector-alive"
STALL_SECONDS = 180

# Shirtless decision: a linear classifier on CLIP image features, trained on labelled
# people from this site's own cameras (see train_probe.py). If the file is missing, the
# zero-shot prompts below are used instead (less accurate: ~3x more false alarms here).
PROBE_PATH = os.getenv("SHIRT_PROBE", "/detector/shirt_probe.pt")

# Zero-shot fallback. The shirtless probability is the softmax mass on NO_SHIRT.
NO_SHIRT = [
    "a CCTV photo of a shirtless person",
    "a CCTV photo of a man with no shirt on and a bare chest",
    "a CCTV photo of a topless man showing his bare torso",
    "a CCTV photo of a person with a naked upper body",
]
SHIRT = [
    "a CCTV photo of a person wearing a shirt",
    "a CCTV photo of a person wearing a t-shirt",
    "a CCTV photo of a person wearing a polo shirt or a uniform",
    "a CCTV photo of a person wearing a jacket or hoodie",
    "a CCTV photo of a person wearing a tank top",
    "a CCTV photo of a person wearing a dress or a long-sleeved top",
]


# ---------------------------------------------------------------- models

class Models:
    def __init__(self):
        torch.set_num_threads(THREADS)
        self.yolo = YOLO(os.path.join(MODEL_DIR, "yolo11s.pt"))
        self.clip, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-16", pretrained="laion2b_s34b_b88k")
        self.clip.eval()
        tokenizer = open_clip.get_tokenizer("ViT-B-16")
        with torch.no_grad():
            text = self.clip.encode_text(tokenizer(NO_SHIRT + SHIRT))
            self.text = text / text.norm(dim=-1, keepdim=True)
        self.n_pos = len(NO_SHIRT)
        self.probe = None
        if os.path.exists(PROBE_PATH):
            self.probe = torch.load(PROBE_PATH)
            log.info("Shirt classifier: trained probe (%s examples, %s shirtless)",
                     self.probe.get("n"), self.probe.get("positives"))
        else:
            log.warning("Shirt classifier: %s not found, using zero-shot prompts", PROBE_PATH)
        self.lock = threading.Lock()

    def detect(self, img: Image.Image) -> list[dict]:
        with self.lock, torch.no_grad():
            res = self.yolo.predict(img, classes=[0], conf=PERSON_CONFIDENCE, imgsz=YOLO_IMGSZ, verbose=False)[0]
        people = []
        for (x1, y1, x2, y2), conf in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist()):
            if y2 - y1 >= PERSON_MIN_HEIGHT:
                people.append({"box": [round(x1), round(y1), round(x2), round(y2)], "conf": round(conf, 3),
                               "shirtless_score": None, "shirtless": False})
        return people

    def embed(self, crops: list[Image.Image]) -> torch.Tensor:
        with self.lock, torch.no_grad():
            feats = self.clip.encode_image(torch.stack([self.preprocess(c) for c in crops]))
        return feats / feats.norm(dim=-1, keepdim=True)

    def shirtless_scores(self, feats: torch.Tensor) -> torch.Tensor:
        if self.probe is not None:
            pr = self.probe
            return torch.sigmoid(feats @ pr["w"] * pr["scale"] + pr["b"])
        return (100.0 * feats @ self.text.T).softmax(dim=-1)[:, :self.n_pos].sum(-1)

    def judge(self, img: Image.Image, people: list[dict]) -> None:
        """Set shirtless_score / shirtless on each person."""
        if not people:
            return
        feats = self.embed([self._crop(img, p["box"]) for p in people])
        with torch.no_grad():
            scores = self.shirtless_scores(feats)
        for p, score in zip(people, scores.tolist()):
            p["shirtless_score"] = round(score, 3)
            p["shirtless"] = score >= SHIRTLESS_THRESHOLD

    def analyze(self, img: Image.Image) -> list[dict]:
        """Detect + judge everyone (no tracking); used for testing."""
        people = self.detect(img)
        self.judge(img, [p for p in people if self.body_visible(p["box"], img.size)])
        return people

    @staticmethod
    def body_visible(box, size) -> bool:
        """Enough of the person in view to judge clothing."""
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        inside = x1 > EDGE_MARGIN and y1 > EDGE_MARGIN and x2 < size[0] - EDGE_MARGIN
        return inside and h >= SHIRT_MIN_HEIGHT and h >= SHIRT_MIN_ASPECT * w

    @staticmethod
    def _crop(img: Image.Image, box) -> Image.Image:
        """Person crop padded to a square so CLIP sees the whole body undistorted."""
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        side = max(w, h) * 1.1
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        return img.crop((int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2)))


# ---------------------------------------------------------------- tracking

@dataclass
class Track:
    box: list[int]
    last_seen: float
    shirtless: bool = False
    score: float | None = None
    judged_at: float = float("-inf")
    matched: bool = False   # already reported as matching the camera's detect settings


@dataclass
class ChannelState:
    tracks: list[Track] = field(default_factory=list)
    last_saved: float = 0.0
    # Latest saved event, for best-shot replacement: {"id", "ts", "tracks", "quality"}
    event: dict | None = None


def sharpness(img: Image.Image, box) -> float:
    """Variance of the Laplacian of the person crop, scaled to a fixed height.

    Higher = sharper. Motion blur and small, upscaled figures both score low."""
    x1, y1, x2, y2 = box
    crop = img.crop((x1, y1, x2, y2)).convert("L")
    h = 256
    crop = crop.resize((max(8, round(crop.width * h / max(crop.height, 1))), h), Image.BILINEAR)
    a = np.asarray(crop, dtype=np.float32)
    lap = 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


def _match(a, b) -> bool:
    """Same person between two samples: overlapping boxes or a nearby centre."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    if union > 0 and inter / union > 0.2:
        return True
    dx = (a[0] + a[2] - b[0] - b[2]) / 2
    dy = (a[1] + a[3] - b[1] - b[3]) / 2
    return (dx * dx + dy * dy) ** 0.5 < 0.6 * max(a[3] - a[1], b[3] - b[1])


def _inside(x: float, y: float, poly) -> bool:
    """Point-in-polygon (ray casting)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def in_zones(box, size, zones) -> bool:
    """Is the person in one of the camera's detection areas? No areas = whole view.

    At least ZONE_MIN_OVERLAP of the person's box must be inside the areas (sampled on a
    10x10 grid). Just the feet isn't enough: these cameras look down from above, so
    someone on the corridor behind a railing has their legs hidden and the bottom of
    their box lands inside an area drawn over the stairs below."""
    if not zones:
        return True
    x1, y1, x2, y2 = box
    n = 10
    hits = 0
    for i in range(n):
        x = (x1 + (i + .5) * (x2 - x1) / n) / size[0]
        for j in range(n):
            y = (y1 + (j + .5) * (y2 - y1) / n) / size[1]
            hits += any(_inside(x, y, z) for z in zones)
    return hits >= ZONE_MIN_OVERLAP * n * n


def process_frame(models: Models, img: Image.Image, state: ChannelState, now: float, zones=None):
    """Detect people, match them to tracks, run the shirt check only where needed.

    People outside the camera's detection areas are ignored completely.
    Returns (people, tracks, new_person, new_shirtless); tracks[i] is people[i]'s track."""
    people = [p for p in models.detect(img) if in_zones(p["box"], img.size, zones)]
    state.tracks = [t for t in state.tracks if now - t.last_seen <= TRACK_TTL_SECONDS]
    free = list(state.tracks)
    pairs = []
    for p in people:
        track = next((t for t in free if _match(t.box, p["box"])), None)
        if track:
            free.remove(track)
        pairs.append((p, track))

    to_judge = [p for p, t in pairs if Models.body_visible(p["box"], img.size)
                and (t is None or now - t.judged_at >= SHIRT_RECHECK_SECONDS)]
    models.judge(img, to_judge)
    judged = {id(p) for p in to_judge}

    new_person = new_shirtless = False
    tracks = []
    for p, track in pairs:
        if track is None:
            track = Track(p["box"], now)
            state.tracks.append(track)
            new_person = True
        if id(p) in judged:
            track.score, track.judged_at = p["shirtless_score"], now
        elif track.score is not None:  # reuse the recent result for this person
            p["shirtless_score"] = track.score
            p["shirtless"] = track.score >= SHIRTLESS_THRESHOLD
        if p["shirtless"] and not track.shirtless:
            new_shirtless = track.shirtless = True
        track.box, track.last_seen = p["box"], now
        tracks.append(track)
    return people, tracks, new_person, new_shirtless


# ---------------------------------------------------------------- stream reading

class StreamReader(threading.Thread):
    """Runs ffmpeg on the go2rtc MP4 stream while the camera is analysed, keeping the latest JPEG.

    Every frame is decoded, not just keyframes (CH3 has one only every 2 s), and up to
    DECODE_FPS a second are passed on, so a fast walker leaves several frames for the
    best-shot search to pick the sharpest from."""

    def __init__(self, channel: int):
        super().__init__(daemon=True, name=f"reader-ch{channel}")
        self.channel = channel
        self.url = f"{GO2RTC_URL}/api/stream.mp4?src=ch{channel}_hd"
        self.latest: tuple[float, bytes] | None = None
        self.error: str | None = None
        self.frames = 0
        self.wanted = threading.Event()
        self.wanted.set()
        self.proc: subprocess.Popen | None = None

    def set_wanted(self, on: bool) -> None:
        """Stop decoding a camera nobody analyses (saves CPU; go2rtc then stops pulling it)."""
        if on == self.wanted.is_set():
            return
        if on:
            self.wanted.set()
        else:
            self.wanted.clear()
            if self.proc:
                self.proc.kill()

    def run(self):
        backoff = 2
        while True:
            self.wanted.wait()
            started = time.time()
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-rw_timeout", "15000000", "-i", self.url, "-an",
                   # at most DECODE_FPS frames a second; only drops frames, never duplicates
                   "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{1 / DECODE_FPS:.3f})'",
                   "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-"]
            proc = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if not self.wanted.is_set():
                proc.kill()
            try:
                self._read(proc.stdout)
            finally:
                proc.kill()
                err = proc.stderr.read().decode(errors="replace").strip().splitlines()
                self.error = err[-1] if err else "stream ended"
            if not self.wanted.is_set():
                self.latest, self.error = None, None
                log.info("CH%s not analysed, stream reader stopped", self.channel)
                continue
            log.warning("CH%s stream stopped: %s", self.channel, self.error)
            if time.time() - started > 60:
                backoff = 2
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _read(self, pipe):
        buf = b""
        while True:
            chunk = pipe.read1(1 << 16)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break
                self.latest = (time.time(), buf[start:end + 2])
                self.frames += 1
                self.error = None
                buf = buf[end + 2:]


# ---------------------------------------------------------------- main loop

ALL_DETECT = {"person": True, "none": True}


def matches(track: Track, detect: dict) -> bool:
    """Does this person match what the camera is set to detect?"""
    return bool(detect.get("person") or (detect.get("none") and track.shirtless))


def post_event(client: httpx.Client, channel: int, ts: float, jpeg: bytes, people: list[dict], reason: str,
               replace_id: int | None = None) -> int | None:
    """Send a frame to the app; returns the event id (None if the app skipped it)."""
    meta = {"channel": channel, "ts": ts, "people": people, "reason": reason, "replace_id": replace_id}
    r = client.post(f"{APP_URL}/internal/detections", content=jpeg,
                    headers={"Content-Type": "image/jpeg", "X-Meta": json.dumps(meta),
                             "X-Internal-Token": INTERNAL_TOKEN})
    r.raise_for_status()
    return r.json().get("id")


def send_event(client: httpx.Client, outbox: list[dict], ev: dict) -> int | None:
    """Post an event; if the app can't take it (or older ones are still queued), queue it."""
    if not outbox:
        try:
            return post_event(client, **ev)
        except Exception as exc:
            log.warning("CH%s: app didn't accept the event (%s); will retry", ev["channel"], exc)
    outbox.append(ev)
    if len(outbox) > OUTBOX_MAX:
        old = outbox.pop(0)
        log.warning("CH%s: dropped queued %s event, queue full", old["channel"], old["reason"])
    return None


def flush_outbox(client: httpx.Client, outbox: list[dict]) -> None:
    while outbox:
        ev = outbox[0]
        if time.time() - ev["ts"] > OUTBOX_MAX_AGE:
            outbox.pop(0)
            log.warning("CH%s: dropped queued %s event, app unreachable for too long", ev["channel"], ev["reason"])
            continue
        try:
            event_id = post_event(client, **ev)
        except Exception:
            return
        outbox.pop(0)
        log.info("CH%s event #%s: delivered after retry (%s)", ev["channel"], event_id, ev["reason"])


def watchdog() -> None:
    """Exit if the main loop stops touching ALIVE_FILE (hung stream read, model or HTTP call)."""
    while True:
        time.sleep(30)
        try:
            stalled = time.time() - os.path.getmtime(ALIVE_FILE)
        except OSError:
            continue
        if stalled > STALL_SECONDS:
            log.error("Main loop stalled for %.0fs, exiting so Docker restarts the detector", stalled)
            os._exit(1)


def main():
    # Libraries (httpx, open_clip via the root logger) only log warnings; ours logs at LOG_LEVEL.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.info("Loading models (threads=%s)...", THREADS)
    t = time.time()
    models = Models()
    log.info("Models loaded in %.1fs. Channels=%s interval=%ss shirtless>=%.2f",
             time.time() - t, CHANNELS, SAMPLE_SECONDS, SHIRTLESS_THRESHOLD)

    readers = {c: StreamReader(c) for c in CHANNELS}
    for r in readers.values():
        r.start()
    states = {c: ChannelState() for c in CHANNELS}
    last_done = {c: 0.0 for c in CHANNELS}
    stats = {"inference_ms": None}
    client = httpx.Client(timeout=15)
    last_heartbeat = 0.0
    active = set(CHANNELS)  # cameras inside the capture schedule (updated from the app's heartbeat reply)
    detect = {c: dict(ALL_DETECT) for c in CHANNELS}  # per-camera detect settings, also from the app
    zones = {c: [] for c in CHANNELS}                 # per-camera detection areas ([] = whole view)
    outbox: list[dict] = []
    last_retry = 0.0
    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()

    while True:
        with open(ALIVE_FILE, "w"):
            pass
        did_work = False
        for ch, reader in readers.items():
            # Outside its schedule, or nothing ticked under Detect: don't spend CPU on it.
            if ch not in active or not any(detect[ch].values()):
                continue
            st = states[ch]
            # Every BEST_SHOT_INTERVAL while looking for a sharper shot, otherwise every SAMPLE_SECONDS.
            in_best_shot = st.event is not None and time.time() - st.event["ts"] <= BEST_SHOT_SECONDS
            interval = BEST_SHOT_INTERVAL if in_best_shot else SAMPLE_SECONDS
            latest = reader.latest
            # (minus half a frame of jitter, so a 1 s interval doesn't wait for the frame after)
            if not latest or latest[0] <= last_done[ch] or time.time() - last_done[ch] < interval - 0.5 / DECODE_FPS:
                continue
            ts, jpeg = latest
            last_done[ch] = ts
            did_work = True
            try:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")
                t0 = time.time()
                people, tracks, _new_person, new_shirtless = process_frame(models, img, st, ts, zones[ch])
                stats["inference_ms"] = round((time.time() - t0) * 1000)
                quality = [sharpness(img, p["box"]) for p in people]
                if people:
                    log.debug("CH%s %d people %s sharpness %s (%sms)", ch, len(people),
                              [p["shirtless_score"] for p in people], [round(q) for q in quality],
                              stats["inference_ms"])
                det = detect[ch]
                new_shirtless = new_shirtless and det.get("none", True)
                new_match = []
                for t in tracks:
                    if not t.matched and matches(t, det):
                        t.matched = True
                        new_match.append(t)
                if new_shirtless or (new_match and ts - st.last_saved >= COOLDOWN_SECONDS):
                    reason = "shirtless" if new_shirtless else "person"
                    event_id = send_event(client, outbox, {"channel": ch, "ts": ts, "jpeg": jpeg,
                                                           "people": people, "reason": reason})
                    st.last_saved = ts
                    st.event = None if event_id is None else {
                        "id": event_id, "ts": ts, "tracks": {id(t) for t in tracks},
                        "quality": sum(quality) / len(quality)}
                    log.info("CH%s event #%s: %d people, shirtless scores %s (%s)", ch,
                             "queued" if outbox and event_id is None else event_id, len(people),
                             [p["shirtless_score"] for p in people], reason)
                elif st.event and ts - st.event["ts"] <= BEST_SHOT_SECONDS:
                    ev = st.event
                    same = [q for q, t in zip(quality, tracks) if id(t) in ev["tracks"]]
                    if len(same) == len(ev["tracks"]):
                        q = sum(same) / len(same)
                        if q > ev["quality"] * BEST_SHOT_GAIN:
                            post_event(client, ch, ts, jpeg, people, "sharper", replace_id=ev["id"])
                            log.info("CH%s event #%s: sharper frame (%.0f -> %.0f)", ch, ev["id"], ev["quality"], q)
                            ev["quality"] = q
            except Exception:
                log.exception("CH%s analysis failed", ch)

        if outbox and time.time() - last_retry > OUTBOX_RETRY_SECONDS:
            last_retry = time.time()
            flush_outbox(client, outbox)

        if time.time() - last_heartbeat > 10:
            last_heartbeat = time.time()
            hb = {"inference_ms": stats["inference_ms"], "channels": {
                str(c): {"last_frame": r.latest[0] if r.latest else None, "frames": r.frames, "error": r.error,
                         "idle": not r.wanted.is_set()}
                for c, r in readers.items()}}
            try:
                r = client.post(f"{APP_URL}/internal/heartbeat", json=hb, headers={"X-Internal-Token": INTERNAL_TOKEN})
                # An error reply (e.g. 403 for a wrong INTERNAL_TOKEN) must not be read as settings:
                # its missing "active_channels" would turn on every camera, ignoring schedules.
                r.raise_for_status()
                reply = r.json()
                now_active = set(reply.get("active_channels", CHANNELS))
                for c, d in (reply.get("detect") or {}).items():
                    if int(c) in detect and d != detect[int(c)]:
                        log.info("CH%s detect: %s", c, "+".join(k for k, on in d.items() if on) or "nothing")
                        detect[int(c)] = d
                        states[int(c)] = ChannelState()  # re-evaluate people under the new settings
                for c, z in (reply.get("zones") or {}).items():
                    if int(c) in zones and z != zones[int(c)]:
                        log.info("CH%s detection areas: %s", c, len(z) or "whole view")
                        zones[int(c)] = z
                        states[int(c)] = ChannelState()
                for ch in active - now_active:
                    states[ch] = ChannelState()  # start fresh when capture resumes
                if now_active != active:
                    log.info("Capture schedule: analysing channels %s", sorted(now_active) or "none")
                active = now_active
                for c, reader in readers.items():
                    reader.set_wanted(c in active and any(detect[c].values()))
            except Exception as exc:
                log.warning("Heartbeat to app failed: %s", exc)
        if not did_work:
            time.sleep(0.2)


if __name__ == "__main__":
    main()
