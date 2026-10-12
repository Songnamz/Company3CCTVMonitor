"""Stream a set of events as a zip: one JPEG per event plus an events.csv index."""

import csv
import io
import json
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
    shirts = sorted({d.get("shirt") for d in json.loads(row.get("detections") or "[]")} & {"white", "black"})
    return "+".join(shirts) if shirts else row.get("code", "event").lower()


def zip_events(rows: list[dict]) -> Iterator[bytes]:
    sink = _StreamSink()
    index = io.StringIO()
    writer = csv.writer(index)
    writer.writerow(["file", "event_id", "time", "camera", "type", "people", "shirts", "no_shirt_score"])
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:   # JPEGs don't compress further
        for row in rows:
            path = safe_path(row.get("image"))
            if not path or not path.exists():
                continue
            dt = datetime.fromtimestamp(row["ts"])
            kind = event_kind(row)
            name = f"{dt:%Y-%m-%d}/{dt:%Y-%m-%d_%H-%M-%S}_CH{row['channel']}_{kind}_{row['id']}.jpg"
            zf.write(path, name)
            dets = json.loads(row.get("detections") or "[]")
            writer.writerow([name, row["id"], f"{dt:%Y-%m-%d %H:%M:%S}", f"CH{row['channel']}", kind,
                             row.get("persons") or "", " ".join(d.get("shirt") or "?" for d in dets),
                             row.get("shirtless_score") if row.get("shirtless_score") is not None else ""])
            yield sink.drain()
        # UTF-8 BOM so Excel opens it with the right encoding.
        zf.writestr("events.csv", "﻿" + index.getvalue())
    yield sink.drain()
