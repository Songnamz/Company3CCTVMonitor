"""Video clips around events, copied from the XVR's own recording (RTSP playback), or for
cameras not on the XVR, cut from a rolling recording of their main stream (RingRecorder).

The events table is the queue: new events start with clip_status 'pending', so clips
still missing after a restart are fetched then. A clip is fetched CLIP_DELAY_SECONDS
after its window ends, because the XVR only serves footage once it is on its disk.
Events on one camera whose windows overlap share one clip; each event plays it from
its own moment (clip_start tells where the clip begins).
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import quote

import config
from db import Database

log = logging.getLogger("clips")

STALL_SECONDS = 10        # no new video for this long = the XVR has nothing more to send
FIRST_DATA_SECONDS = 25   # playback normally starts within 5-8 s
MIN_SHARE = 0.5           # keep a clip that covers at least half of the requested time

# Rolling recording of cameras not on the XVR: segments of at least RING_SEGMENT_SECONDS
# (cut at keyframes, so as long as the camera's keyframe interval), kept RING_KEEP_SECONDS.
RING_SEGMENT_SECONDS = 4
RING_KEEP_SECONDS = 600
RING_DELAY_SECONDS = 15   # first try this long after a clip's end; waits for the segment to close
RING_WAIT_SECONDS = 120   # give up waiting for a segment to close after this long


class NotYet(Exception):
    """The recording doesn't reach the end of the clip yet; try again shortly."""


def ring_dir(channel: int):
    return config.RING_DIR / f"ch{channel}"


@dataclass
class Group:
    channel: int
    start: float
    end: float
    ids: list[int] = field(default_factory=list)


def group_events(rows: list[dict]) -> list[Group]:
    """Merge events whose clip windows overlap, per channel (rows sorted by channel, ts)."""
    groups: list[Group] = []
    for row in rows:
        start = row["ts"] - config.CLIP_BEFORE_SECONDS
        end = row["ts"] + config.CLIP_AFTER_SECONDS
        g = groups[-1] if groups else None
        if g and g.channel == row["channel"] and start <= g.end and end - g.start <= config.CLIP_MAX_SECONDS:
            g.end = max(g.end, end)
            g.ids.append(row["id"])
        else:
            groups.append(Group(row["channel"], start, end, [row["id"]]))
    return groups


def playback_url(channel: int, start: int, end: int) -> str:
    fmt = "%Y_%m_%d_%H_%M_%S"  # XVR local time (same TZ as this container)
    return (f"rtsp://{quote(config.XVR_USER, safe='')}:{quote(config.XVR_PASS, safe='')}"
            f"@{config.XVR_HOST}:{config.XVR_RTSP_PORT}/cam/playback?channel={channel}"
            f"&starttime={datetime.fromtimestamp(start):{fmt}}&endtime={datetime.fromtimestamp(end):{fmt}}")


async def record(url: str, seconds: float, out: str) -> tuple[float, str]:
    """Copy `seconds` of the playback stream into a Matroska file (readable even if ffmpeg
    has to be killed). Returns (seconds recorded, ffmpeg's last error line).

    The XVR sends the footage at about real time, then keeps the connection open without
    sending anything, so ffmpeg is stopped once the video stalls."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1",
           "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url,
           "-map", "0:v", "-t", f"{seconds:.2f}", "-c", "copy", "-y", out]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    errors = asyncio.create_task(proc.stderr.read())
    got, last_change = 0.0, time.monotonic()
    deadline = last_change + seconds + 60
    try:
        while time.monotonic() < deadline:
            limit = STALL_SECONDS if got else FIRST_DATA_SECONDS
            if time.monotonic() - last_change > limit:
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), 1)
            except asyncio.TimeoutError:
                continue
            if not line:  # ffmpeg finished
                break
            if line.startswith(b"out_time_us="):
                value = line.split(b"=", 1)[1].strip()
                if value.isdigit() and int(value) / 1e6 > got:
                    got, last_change = int(value) / 1e6, time.monotonic()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    lines = (await errors).decode(errors="replace").strip().splitlines()
    return got, lines[-1] if lines else ""


async def remux(src: str, dst: str) -> None:
    """Matroska -> MP4 with the index first, so browsers can start playing straight away."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src,
        "-c", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        lines = err.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"remux failed: {lines[-1] if lines else proc.returncode}")


async def run_ffmpeg(*args: str) -> str:
    """Run ffmpeg/ffprobe; returns stdout, raises with its last error line."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        lines = err.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"{args[0]} failed: {lines[-1] if lines else proc.returncode}")
    return out.decode(errors="replace")


async def ring_clip(channel: int, start: float, end: float, dst: str) -> tuple[float, float]:
    """Join the recorded segments covering start..end into an MP4.
    Returns (time of its first frame, seconds)."""
    segs = sorted((int(p.stem), p) for p in ring_dir(channel).glob("*.ts") if p.stem.isdigit())
    if not segs or segs[-1][0] <= end:  # the newest segment is still being written
        if time.time() - end > RING_WAIT_SECONDS:
            raise RuntimeError("the camera's recording stopped (is go2rtc receiving it?)")
        raise NotYet()
    # Segment i runs from its start until segment i+1 starts.
    picked = [(t, p) for (t, p), (nxt, _) in zip(segs, segs[1:]) if nxt > start and t < end]
    covered = min(end, segs[-1][0]) - max(start, picked[0][0]) if picked else 0
    if covered < (end - start) * MIN_SHARE:
        raise RuntimeError(f"only {max(covered, 0):.0f}s of {end - start:.0f}s recorded")
    # (MPEG-TS: ffprobe prints the codec once per program as well, hence split())
    codecs = (await run_ffmpeg("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=codec_name", "-of", "csv=p=0", str(picked[0][1]))).split()
    tag = ["-tag:v", "hvc1"] if "hevc" in codecs else []  # Safari only plays H.265 MP4s tagged hvc1
    await run_ffmpeg("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-i", "concat:" + "|".join(str(p) for _, p in picked),
                     "-map", "0:v", "-c", "copy", *tag, "-movflags", "+faststart", "-f", "mp4", "-y", dst)
    first = picked[0][0]
    nxt = next(t for t, _ in segs if t > picked[-1][0])
    return float(first), float(nxt - first)


class RingRecorder:
    """Keeps the last RING_KEEP_SECONDS of a camera's main stream (read from go2rtc, so the
    camera has one connection) as MPEG-TS segments named after their start time."""

    def __init__(self, channel: int):
        self.channel = channel

    def prune(self) -> None:
        cutoff = time.time() - RING_KEEP_SECONDS
        for p in ring_dir(self.channel).glob("*.ts"):
            if not p.stem.isdigit() or int(p.stem) < cutoff:
                p.unlink(missing_ok=True)

    async def run(self) -> None:
        folder = ring_dir(self.channel)
        folder.mkdir(parents=True, exist_ok=True)
        url = f"{config.GO2RTC_URL}/api/stream.mp4?src=ch{self.channel}_hd"
        backoff = 5
        while True:
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rw_timeout", "15000000",
                "-i", url, "-map", "0:v", "-c", "copy", "-f", "segment",
                "-segment_time", str(RING_SEGMENT_SECONDS), "-segment_format", "mpegts",
                "-strftime", "1", str(folder / "%s.ts"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            errors = asyncio.create_task(proc.stderr.read())
            try:
                while True:
                    self.prune()
                    try:
                        await asyncio.wait_for(asyncio.shield(proc.wait()), 30)
                        break
                    except asyncio.TimeoutError:
                        pass
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            lines = (await errors).decode(errors="replace").strip().splitlines()
            log.warning("Recording CH%s stopped: %s", self.channel, lines[-1] if lines else "stream ended")
            if time.monotonic() - started > 60:
                backoff = 5
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


class ClipFetcher:
    def __init__(self, db: Database, on_updated: Callable[[list[dict]], None],
                 ring_channels: Callable[[], list[int]]):
        self.db = db
        self.on_updated = on_updated
        self.ring_channels = ring_channels
        self.wake = asyncio.Event()
        self.busy: set[int] = set()                  # event ids being fetched right now
        self.retry: dict[int, tuple[int, float]] = {}  # event id -> (failed attempts, retry at)
        self.slots = asyncio.Semaphore(config.CLIP_PARALLEL)
        self.tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        shutil.rmtree(config.CLIP_TMP_DIR, ignore_errors=True)
        config.CLIP_TMP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            while True:
                self.wake.clear()
                wait = 600.0
                try:
                    rows = await asyncio.to_thread(self.db.pending_clips)
                    now = time.time()
                    ring = set(self.ring_channels())
                    for g in group_events([r for r in rows if r["id"] not in self.busy]):
                        delay = RING_DELAY_SECONDS if g.channel in ring else config.CLIP_DELAY_SECONDS
                        due = max([g.end + delay]
                                  + [self.retry[i][1] for i in g.ids if i in self.retry])
                        if due > now:
                            wait = min(wait, due - now)
                            continue
                        self.busy.update(g.ids)
                        task = asyncio.create_task(self.fetch(g))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                except Exception:
                    log.exception("Clip queue failed")
                try:
                    await asyncio.wait_for(self.wake.wait(), max(wait, 1))
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def fetch(self, g: Group) -> None:
        start, end = int(g.start), int(g.end + 0.999)
        dt = datetime.fromtimestamp(start)
        rel = f"{dt:%Y/%m/%d}/{dt:%H%M%S}_ch{g.channel}_{g.ids[0]}_clip.mp4"
        tmp = config.CLIP_TMP_DIR / f"{g.ids[0]}.mkv"
        dst = config.SNAPSHOT_DIR / rel
        part = dst.with_suffix(".part")
        try:
            started = time.monotonic()
            if g.channel not in config.CHANNELS and g.channel not in self.ring_channels():
                for i in g.ids:  # no retries: the camera is gone
                    self.retry[i] = (len(config.CLIP_RETRY_SECONDS), 0)
                raise RuntimeError("camera removed")
            if g.channel in self.ring_channels():
                dst.parent.mkdir(parents=True, exist_ok=True)
                clip_start, got = await ring_clip(g.channel, g.start, g.end, str(part))
            else:
                # Ask for a few seconds more than needed: the XVR stalls at the end of what it
                # sends, so the -t limit is what normally finishes the download.
                url = playback_url(g.channel, start, end + 4)
                async with self.slots:
                    got, err = await record(url, end - start, str(tmp))
                if got < (end - start) * MIN_SHARE:
                    raise RuntimeError(f"only {got:.1f}s of {end - start}s from the XVR"
                                       + (f" ({err})" if err else ""))
                dst.parent.mkdir(parents=True, exist_ok=True)
                await remux(str(tmp), str(part))
                clip_start = float(start)
            os.replace(part, dst)
            rows = await asyncio.to_thread(self.db.set_clip, g.ids, rel, clip_start, "ok")
            if not rows:  # all events were deleted meanwhile
                dst.unlink(missing_ok=True)
            for i in g.ids:
                self.retry.pop(i, None)
            log.info("Clip CH%s %s %.0fs for %d event(s), %.1f MB in %.0fs", g.channel, f"{dt:%H:%M:%S}",
                     got, len(g.ids), dst.stat().st_size / 1e6 if rows else 0, time.monotonic() - started)
            self.on_updated(rows)
        except asyncio.CancelledError:
            raise
        except NotYet:
            for i in g.ids:
                self.retry[i] = (self.retry.get(i, (0, 0))[0], time.time() + 5)
        except Exception as exc:
            await self.failed(g, exc)
        finally:
            tmp.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            self.busy.difference_update(g.ids)
            self.wake.set()

    async def failed(self, g: Group, exc: Exception) -> None:
        attempts = max(self.retry.get(i, (0, 0))[0] for i in g.ids)
        if attempts < len(config.CLIP_RETRY_SECONDS):
            delay = config.CLIP_RETRY_SECONDS[attempts]
            for i in g.ids:
                self.retry[i] = (attempts + 1, time.time() + delay)
            log.warning("Clip CH%s for events %s failed (%s); retrying in %ds", g.channel, g.ids, exc, delay)
            return
        for i in g.ids:
            self.retry.pop(i, None)
        log.error("Clip CH%s for events %s failed for good: %s", g.channel, g.ids, exc)
        self.on_updated(await asyncio.to_thread(self.db.set_clip, g.ids, None, None, "failed"))
