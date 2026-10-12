# CCTV Monitor

Self-hosted web UI for a Dahua XVR (DH-XVR5104HS-X1): live view of all channels and
a gallery of face-detection snapshots with real-time notifications.

```
Browser ──https──► Cloudflare ──tunnel──► cloudflared (host) ──► 127.0.0.1:8080
                                                                     │ nginx
                                     ┌───────────────────────────────┴───────────────┐
                           /stream/api/ws, /stream/video-*.js              everything else
                                     ▼                                                 ▼
                                 go2rtc:1984 ◄── RTSP ── XVR :554           app:8000 (FastAPI)
                                     ▲                                       │  ▲
                                     └──── frame.jpeg (snapshot fallback) ───┘  └── eventManager.cgi / snapshot.cgi ── XVR :80
```

| Container | Role |
|-----------|------|
| `go2rtc`  | Pulls RTSP from the XVR (`chN` = sub stream, `chN_hd` = main stream) and serves it to browsers as MSE over a websocket. Its RTSP and WebRTC servers are disabled; it has no published ports. |
| `detector`| Local AI (CPU). Reads each analysed camera's HD stream from go2rtc (up to 5 frames/s), finds people with YOLO11s and checks each person for a shirt with CLIP ViT-B/16 zero-shot. Posts an event to the app when a new person appears or someone is seen without a shirt. |
| `app`     | Python/FastAPI. Holds a long-lived connection to the XVR event stream, grabs a snapshot on every face event, stores it in SQLite + `./data/snapshots`, pushes events to browsers via SSE, serves the UI. |
| `nginx`   | The only published port (`127.0.0.1:8080`). Exposes just the go2rtc websocket and player JS under `/stream/`; every other `/stream/` path returns 403 so the go2rtc admin UI/API is never public. |

## Setup

1. **Create a view-only user on the XVR** (see below).
2. **Configure:**
   ```bash
   cd ~/apps/CCTVMonitor
   cp .env.example .env
   chmod 600 .env
   nano .env            # XVR_USER / XVR_PASS, APP_PASSWORD etc.
   mkdir -p data        # must be owned by your user (the app runs as uid 1000)
   ```
   The password must be **letters and digits only** – it is embedded in the RTSP URL.
   If your user isn't uid/gid 1000, add `APP_UID=…` and `APP_GID=…` to `.env`.
3. **Start:**
   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs -f app      # every received event code is logged here
   ```
4. Open http://localhost:8080 (or https://cctv.songnam.xyz through the tunnel).

The header dot is **green** when the app is connected to the XVR event stream, **red**
when not (hover for the error), **amber** when the browser lost its connection to the server.

### Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `XVR_HOST` / `XVR_HTTP_PORT` / `XVR_RTSP_PORT` | `192.168.1.20` / `80` / `554` | |
| `XVR_SCHEME` | `http` | `https` (with port 443) if HTTPS is enabled on the XVR |
| `XVR_VERIFY_TLS` | `false` | Verify the XVR certificate (Dahua's is self-signed) |
| `XVR_USER` / `XVR_PASS` | – | Used by the app (HTTP digest) and go2rtc (RTSP) |
| `CHANNELS` | `1,2,3,4` | Channels shown in the UI and accepted for events. go2rtc streams are defined in `go2rtc/go2rtc.yaml` |
| `EVENT_CODES` | `FaceDetection` | Comma-separated Dahua event codes; `All` for debugging |
| `EVENT_COOLDOWN_SECONDS` | `3` | Minimum time between snapshots per channel |
| `RETENTION_DAYS` | `30` | Events/files older than this are deleted every 6 h; `0` disables |
| `CLIPS_ENABLED` | `true` | Save a video clip around each event (see *Video clips*) |
| `CLIP_BEFORE_SECONDS` / `CLIP_AFTER_SECONDS` | `5` / `5` | Clip length before / after the event |
| `CLIP_DELAY_SECONDS` | `120` | Wait this long after a clip's end before copying it from the XVR |
| `APP_PASSWORD` | – | **Required.** Password for the web login page. Empty = nobody can log in |
| `SESSION_DAYS` | `30` | How long a login stays valid |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs heartbeats and full event payloads |

After changing `.env`: `docker compose up -d` (recreates affected containers).
After editing `go2rtc/go2rtc.yaml` or `nginx/nginx.conf`: `docker compose restart go2rtc` / `nginx`.

### Data

Everything lives in `./data`: `events.db` (SQLite, WAL) and `snapshots/YYYY/MM/DD/*.jpg`
(full image + `_thumb.jpg`) and `*_clip.mp4` (video clips). Back up that folder to back up everything.

## Create a dedicated view-only XVR user

Don't use `admin` – the credentials sit in `.env` on this server.

1. XVR web UI (http://192.168.1.20) or local GUI → **Main Menu → System → Account**
   (on some firmware: *Setting → System → Account*).
2. **Group** tab → *Add Group*, e.g. `viewers`, with only **Live** for all channels
   (Playback is optional) and every *Setting/Maintenance/System* permission **unchecked**. Some firmwares need the
   "Log search"/"Event" permission for `eventManager.cgi`; if the event stream returns
   `HTTP 401/403` with the new user but works with `admin`, enable that one too.
3. **User** tab → *Add User*: name e.g. `viewer`, password letters+digits only,
   group `viewers`. If there is a "Reusable"/multiple-login option, enable it (go2rtc
   and the app log in concurrently).
4. Put the user/password in `.env` and `docker compose up -d`.

Tip: Dahua locks accounts after repeated failed logins. If you see a burst of 401s in the
logs, fix the password then wait for the lockout (usually 30 min) or unlock it in the UI.

## People & no-shirt detection

This XVR's built-in AI only works on one channel at a time, and its motion detection
misses people standing still, so person detection runs locally in the `detector`
container instead:

1. Every `DETECT_INTERVAL_SECONDS` (1 s) per camera it takes the latest frame of the HD
   stream and runs YOLO person detection (people under `PERSON_MIN_HEIGHT` px are ignored
   as too far away).
2. People are tracked between samples. An event is saved when a **new** person appears
   (at most one per camera per `PERSON_COOLDOWN_SECONDS`), so someone standing still for
   10 minutes produces one event, not hundreds.
3. Each person fully in view and at least `SHIRT_MIN_HEIGHT` px tall gets a CLIP image
   embedding. A small classifier *trained on labelled people from these cameras*
   (`detector/shirt_probe.pt`) turns it into a no-shirt probability. At or above
   `SHIRTLESS_THRESHOLD` (0.7) the person is flagged; flagged events bypass the cooldown
   and show a red **NO SHIRT** toast. A tracked person is re-checked every 15 s.

4. **Best shot:** for ~8 s after an event (`BEST_SHOT_SECONDS`) the detector checks every
   frame every `BEST_SHOT_INTERVAL` (0.5 s; frames are decoded at up to `DECODE_FPS`, 5 a second) and swaps in a frame where the same people are at least 20% sharper (measured
   by Laplacian variance), since people walking are often motion-blurred in the first frame.

In the **Events** tab: filter by camera, date and type (*No shirt* – events with
at least one person without a shirt), click an event to see every detected person
boxed (green = person, red = no shirt with confidence; `B` toggles boxes). If an alert is
wrong press **Not shirtless**: it is unflagged and drops out of the "no shirt" count.
The header shows today's events, today's no-shirt alerts, and an **AI** chip (green =
detector running, red = offline; hover for per-camera status).

**Tuning** (in `.env`, then `docker compose up -d`):

| Symptom | Change |
|---|---|
| False "no shirt" alerts | Mark them **Not shirtless**, then retrain (below); or raise `SHIRTLESS_THRESHOLD` to 0.8 |
| Shirtless people missed | Lower `SHIRTLESS_THRESHOLD` to 0.5 – expect more false alerts |
| Too many events from distant crowds | Raise `PERSON_MIN_HEIGHT` (e.g. 160) or `PERSON_COOLDOWN_SECONDS` |
| CPU too high | Raise `DETECT_INTERVAL_SECONDS`, or lower `AI_CPUS` / `AI_THREADS` |

**Accuracy** (measured on 178 hand-labelled people from these cameras, day and night,
leave-one-out): the trained classifier caught 34/37 shirtless people with 5/141 false
alarms at 0.5, and 31/37 with 3/141 at 0.7. Generic zero-shot prompts flagged ~12% of
clothed people – mostly white shirts that look skin-coloured in shade or evening light – so
the trained classifier is used. It is still an AI guess: treat alerts as "please check".

**Retraining from your reviews.** Every event you mark **Not shirtless** becomes a
training example. To learn from them (takes ~1 minute):

```bash
docker compose run --rm -v ./data:/data:ro -v ./detector:/out detector python train_probe.py
docker compose up -d --build detector
```

It prints the new leave-one-out accuracy before saving. Training data is stored only as
CLIP numbers in `detector/training/features.pt` – no images are kept.

CPU: the detector is limited to 2 of the 4 cores (`AI_CPUS`). Roughly 0.2 s per frame for
YOLO plus ~0.45 s per newly seen person for CLIP. All processing stays on this server;
no images are sent anywhere. Models are baked into the image at build time.

## Camera settings (Settings tab)

Each camera has its own settings: **what** to detect and **when** to capture.

**Detect** – an event is saved when someone matches a ticked item:

| Tick | Captures |
|---|---|
| **Person** | everyone, including people too far away to judge clothing |
| **No shirt** | people without a shirt; also turns the red no-shirt alert on for this camera |

E.g. *Person* unticked + *No shirt* ticked = the camera only records rule breakers. A person
too far away to judge is captured later if they come close enough to be classified.

**Detection area** – by default the whole view is used. To watch only part of it (the stairs,
a doorway), press **Draw area** on the camera's still picture, click/tap the corners, then tap the
first corner or **Finish area**. A camera can have up to 10 areas; drag a corner to adjust, **Delete**
one, or **Clear (whole view)**. **Refresh picture** takes a new frame. Press **Save settings** to apply.

A person counts only if **at least half of their body** (their box) is inside the areas
(`ZONE_MIN_OVERLAP`, default 0.5). Checking only the feet doesn't work here: the cameras look
down from above, so someone on the corridor behind a railing has their legs hidden, and the
bottom of their box lands inside an area drawn over the stairs below. Draw the area around
where people *appear* in the picture, with some room above the floor for their bodies.
People outside the areas are ignored
completely: no event, no box, no no-shirt alert. The rest of the picture is still saved with
the event. Areas apply to the AI detector only, not to XVR face events (`EVENT_CODES`).
**Copy to all cameras** doesn't copy areas, because every camera's view is different.

**When**:

- **All cameras** (top): pause every camera for 1 hour, 8 hours or until you resume, or
  resume them all.
- **Per camera**: its own status (*Capturing 24/7*, *Capturing (in schedule)* or *Not
  capturing* with the reason), its own pause/resume buttons, and *Only capture during these
  times* with its own time ranges (days + start → end). Unticked = 24/7. An end time earlier
  than the start runs past midnight (22:00 → 06:00); the same start and end means the whole
  day. **Copy to all cameras** copies one camera's detect and time settings to the others.
- Edits apply when you press **Save settings**; pause/resume apply immediately and
  don't save unfinished edits.

Outside a camera's schedule the detector stops analysing that camera completely (saves
CPU) and no events are stored for it; its live view still works. The header shows
**Capture off: CHx** and the live tile says *not capturing*. Changes take effect within ~10 s.

## Image quality

The XVR main streams run at the maximum 4096 kbps H.264. Captured frames are the camera's
1920×1080 frames saved at JPEG quality ~95, the best
of several frames (see *Best shot*). Remaining softness comes from the cameras: 2 MP analog
cameras, people a few metres away (~200–400 px tall), and motion blur from slow shutter
speeds indoors and at night. The cameras' shutter/exposure is not reachable through the
XVR's API (only gain is); it can be set in the camera's own on-screen menu from the XVR's
local monitor (*Camera → PTZ → Menu* on HDCVI cameras), if the cameras offer it. Click/tap an image in the event viewer to zoom 2.5× and move to
pan. Thumbnails are cropped around the people at 480 px wide.

## Video clips

Each event also gets a short video, from 5 s before to 5 s after it (e.g. an event at
21:10:10 gets 21:10:05–21:10:15). It is copied, unchanged (1080p H.264, ~0.5 MB/s), from the
XVR's own recording over RTSP playback, so the XVR must be recording those cameras. The XVR
only serves footage once it is written to its disk, about 100 s later, so the app copies a
clip 2 minutes after the event; until then the card shows **⏳ Video**, then **▶ Video**.
Events on one camera less than 10 s apart share one clip (up to 60 s long), and each plays
it from its own moment.

Click **▶ Video** on a card, or open an event and press **▶ Video** (or `V`) to switch
between the photo and the video; ‹ › then steps through the videos. A clip the XVR could not
deliver is retried after 1, 5 and 30 minutes, then shown as *No video*. Clips count towards
the storage limit, and zips include them (`events.csv` lists each event's video and the
second it happens at).

### Downloading and deleting many events

**Selection** – press **Select**, tap images (shift-click selects a range) or **Select all**
(everything loaded so far), then **Download zip** or **Delete N**. Changing a filter clears
the selection, so nothing hidden is affected.

**Date/time range** – press **Date/time range…**, pick *From* / *To* (date and time, Bangkok
time) and optionally a camera and type (no shirt). It shows how many events
and MB the range covers. Then **Download zip**, **Delete N** (one confirmation that repeats
the exact range) or **Show in gallery**.

Zips contain the full-size images in per-day folders, named
`2026-10-11_19-30-01_CH3_noshirt_428.jpg`, plus `events.csv` (time, camera, type, number of
people, no-shirt score) that opens in Excel. They are streamed, so even a
whole day (~250 MB) starts downloading immediately.

## Login

Every page, API call, snapshot and live-video websocket requires logging in with
`APP_PASSWORD` (`/login`). The app checks the session cookie itself; nginx asks the app
(`auth_request`) before proxying anything to go2rtc.

- Sessions are signed cookies valid for `SESSION_DAYS`. **Changing `APP_PASSWORD` and
  running `docker compose up -d` logs everybody out.** (The signing secret is in
  `data/session_secret`; deleting it does the same.)
- 5 wrong passwords from one IP (or 30 in total) within 15 minutes lock logins for
  15 minutes. The real visitor IP comes from Cloudflare's `CF-Connecting-IP`; failed logins
  are logged by the `app` container.
- nginx marks every response `Cache-Control: private` so Cloudflare never caches
  snapshots or pages at its edge.
- Log out with the ⏻ button in the header.

## Put Cloudflare Access in front of cctv.songnam.xyz (optional, recommended)

The built-in password is a single shared secret. For a second layer, such as per-person
email login, put Cloudflare Access (Zero Trust, free for up to 50 users) in front of it:

1. https://one.dash.cloudflare.com → **Access → Applications → Add an application →
   Self-hosted**.
2. Application domain: `cctv.songnam.xyz` (path empty, so the whole host is covered,
   including `/stream/api/ws` and `/api/live`).
3. Session duration: e.g. 1 month for convenience on phones.
4. Add a policy: Action **Allow**, Include → **Emails** → your address(es)
   (login method: One-time PIN, or Google if configured under *Settings → Authentication*).
5. Save. Visit https://cctv.songnam.xyz in a private window – you should see the
   Cloudflare login page first.

Websockets (live video) and Server-Sent Events work through Access and the tunnel without
extra settings. Nothing in the cloudflared config needs to change.

## Troubleshooting

**Red status dot / no events**
- `HTTP 302 from eventManager.cgi (redirect to https://…)` → HTTPS is enabled on the XVR
  and it redirects plain HTTP. Set `XVR_SCHEME=https` and `XVR_HTTP_PORT=443` (this XVR is set up this way).
- `docker compose logs -f app`. `HTTP 401` → wrong credentials or locked account.
  `ConnectError`/`ReadTimeout` → XVR unreachable from Docker (`docker compose exec app python -c "import httpx; print(httpx.get('http://192.168.1.20').status_code)"`).
- **Event code names differ by firmware.** If the dot is green but nothing arrives when a
  face is in view, set `EVENT_CODES=All` in `.env`, `docker compose up -d`, walk in front
  of a camera and watch for lines like
  `XVR event code=FaceDetection action=Start index=0` – some firmwares use
  `FaceRecognition`, `SmartMotionHuman`, `CrossLineDetection`, etc. Put the right code(s)
  in `EVENT_CODES` (comma-separated) and restart. Note that with `All` every event
  (motion, video loss…) is captured, so switch back afterwards.
- Make sure Face Detection is enabled *per channel* on the XVR, and that the channel is
  listed in `CHANNELS`.

**Live view: HD (fullscreen) doesn't play but the grid does**
The main stream is probably **H.265**, which most browsers can't play over MSE. On the
XVR: *Main Menu → Camera → Encode* → Main Stream compression **H.264** (turn off
"Smart Codec"/H.264+ if playback stutters). Sub streams should be H.264 as well.

**Live view shows nothing at all** – check `docker compose logs go2rtc` for RTSP errors
(wrong password, special characters in the password, too many connections). The XVR limits
concurrent RTSP sessions; go2rtc shares one session per stream across viewers and stops
it when nobody watches.

**Snapshots come from go2rtc instead of the XVR** (`source=go2rtc` in logs) – the XVR's
`snapshot.cgi` failed; the reason is logged as a warning just before. The fallback needs a
moment to start the RTSP stream, so the frame can be ~1 s later than the event.

**Keeps returning to the login page** – check `APP_PASSWORD` is set (`docker compose logs app`
warns if it's empty), and that cookies are allowed for the site.

**AI chip red** – `docker compose logs detector`. "stream stopped" lines mean go2rtc
can't read that camera's HD stream (check `docker compose logs go2rtc`). If its main loop
stalls for 3 minutes the detector exits and Docker restarts it ("Main loop stalled" in the log).
Events the detector can't hand to the app (e.g. while `app` restarts) are queued and retried
for up to 10 minutes ("delivered after retry" / "dropped queued … event").

**Status dot amber** – the browser can't reach the server-sent events endpoint; the
page reconnects automatically and reloads the gallery when it's back.

## Development / testing without the XVR

`tests/` contains a mock Dahua server (digest auth, multipart event stream, JPEG
snapshots; channel 3 deliberately returns a non-JPEG to exercise the go2rtc fallback)
and a compose override that runs an isolated stack on `127.0.0.1:18080` with synthetic
go2rtc test patterns. It never touches `.env` or `./data`.

```bash
mkdir -p tests/data
docker compose -f docker-compose.yml -f tests/docker-compose.test.yml -p cctvtest up -d --build
curl "localhost:18081/trigger?channel=1"                 # face event (style=multiline|nolength|split|double)
curl "localhost:18081/drop"                              # drop the event stream (reconnect test)
# test login password is "testpass":
curl -c /tmp/c -d password=testpass localhost:18080/api/login
curl -b /tmp/c -N localhost:18080/api/live               # watch SSE
docker compose -f docker-compose.yml -f tests/docker-compose.test.yml -p cctvtest down
rm -rf tests/data
```

### API

| | |
|---|---|
| `GET /api/config` | channels, timezone |
| `GET /api/schedule` / `PUT /api/schedule` | per-camera settings: schedule, detect, `zones` (polygons, 0–1 of the frame) |
| `GET /api/cameras/{ch}/frame.jpg` | a current full-resolution frame (for drawing areas) |
| `GET /api/status` | event-stream connected, last error, last event time, today's count |
| `GET /api/events?channel=&start=&end=&before=&limit=&shirtless=` | epoch seconds; newest first; page with `before=<last id>`; returns `has_more` |
| `DELETE /api/events/{id}` | delete event + files |
| `POST /api/events/delete` | bulk delete, body `{"ids": [...]}` (max 1000) |
| `GET /api/events/summary?start=&end=&channel=&type=` | count + MB for a range |
| `POST /api/events/delete_range` | body `{"start", "end", "channel"?, "type"?}` |
| `POST /api/events/export` | form `ids=1,2,3` or `start=&end=&channel=&type=` → zip |
| `GET /api/live` | SSE: `event`, `deleted`, `status`, `ping` (15 s) |
| `GET /healthz` | container healthcheck (no login needed) |
| `POST /api/login` / `POST /api/logout` | form field `password`; sets / clears the session cookie |

All endpoints except `/login`, `/api/login` and `/healthz` return 401 (API) or redirect to
`/login` (pages) without a valid session.
