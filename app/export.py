"""Stream a set of events as a zip: one JPEG per event, their video clips, and an events.csv index."""

import csv
import io
import zipfile
from datetime import datetime
from typing import Iterator

from capture import safe_path


class _StreamSink:
    """File-like object for zipfile that can be drained while the zip is being written.

    zipfile seeks back to patch each entry's local header (sizes, CRC) once the entry is
    written; that header is still in the unflushed buffer because we only drain between
    entries. This keeps normal (non data-descriptor) entries that every unzip tool reads.
    """

    def __init__(self):
        self.base = 0          # bytes already handed out
        self.buf = bytearray()
        self.pos = 0

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return True

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 1:
            pos += self.pos
        elif whence == 2:
            pos += self.base + len(self.buf)
        if pos < self.base:
            raise OSError("cannot seek into data that was already sent")
        self.pos = pos
        return pos

    def write(self, data) -> int:
        off = self.pos - self.base
        if off > len(self.buf):
            self.buf.extend(b"\0" * (off - len(self.buf)))
        self.buf[off:off + len(data)] = data
        self.pos += len(data)
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = bytes(self.buf)
        self.base += len(self.buf)
        self.buf.clear()
        return data


def event_kind(row: dict) -> str:
    if row.get("shirtless"):
        return "noshirt"
    return row.get("code", "event").lower()


def zip_events(rows: list[dict]) -> Iterator[bytes]:
    sink = _StreamSink()
    index = io.StringIO()
    writer = csv.writer(index)
    writer.writerow(["file", "event_id", "time", "camera", "type", "people", "no_shirt_score",
                     "video", "video_seconds_in"])
    clips: dict[str, str] = {}  # clip path -> name in the zip (nearby events share a clip)
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:   # JPEGs/MP4s don't compress further
        for row in rows:
            path = safe_path(row.get("image"))
            if not path or not path.exists():
                continue
            dt = datetime.fromtimestamp(row["ts"])
            kind = event_kind(row)
            name = f"{dt:%Y-%m-%d}/{dt:%Y-%m-%d_%H-%M-%S}_CH{row['channel']}_{kind}_{row['id']}.jpg"
            zf.write(path, name)
            video = offset = ""
            clip = safe_path(row.get("clip"))
            if clip and clip.exists():
                if row["clip"] not in clips:
                    start = datetime.fromtimestamp(row["clip_start"])
                    clips[row["clip"]] = f"{start:%Y-%m-%d}/{start:%Y-%m-%d_%H-%M-%S}_CH{row['channel']}_video_{row['id']}.mp4"
                    zf.write(clip, clips[row["clip"]])
                video, offset = clips[row["clip"]], f"{row['ts'] - row['clip_start']:.1f}"
            writer.writerow([name, row["id"], f"{dt:%Y-%m-%d %H:%M:%S}", f"CH{row['channel']}", kind,
                             row.get("persons") or "",
                             row.get("shirtless_score") if row.get("shirtless_score") is not None else "",
                             video, offset])
            yield sink.drain()
        # UTF-8 BOM so Excel opens it with the right encoding.
        zf.writestr("events.csv", "﻿" + index.getvalue())
    yield sink.drain()
