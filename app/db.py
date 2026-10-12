import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    channel INTEGER NOT NULL,
    code    TEXT    NOT NULL,
    action  TEXT,
    image   TEXT,
    thumb   TEXT,
    source  TEXT,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_channel_id ON events(channel, id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release (applied with ALTER TABLE when missing).
MIGRATIONS = {
    "persons": "INTEGER",
    "shirtless": "INTEGER",          # 1 = someone without a shirt, 0 = no, NULL = not analysed
    "shirtless_score": "REAL",       # highest per-person score
    "detections": "TEXT",            # JSON list of {box, conf, shirtless_score, shirtless}
    "reviewed": "INTEGER DEFAULT 0", # 1 = a person marked the shirtless flag as wrong
}


class Database:
    """Thin synchronous SQLite wrapper; call from a worker thread (asyncio.to_thread)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            have = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
            for col, decl in MIGRATIONS.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_shirtless ON events(shirtless, id)")

    def insert_event(self, ts, channel, code, action, image, thumb, source, data,
                     persons=None, shirtless=None, shirtless_score=None, detections=None) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, channel, code, action, image, thumb, source, data,"
                " persons, shirtless, shirtless_score, detections)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, channel, code, action, image, thumb, source, data,
                 persons, shirtless, shirtless_score, detections),
            )
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    @staticmethod
    def _where(channel=None, start=None, end=None, before=None, shirtless=None,
               ids=None) -> tuple[str, list]:
        where, args = [], []
        if ids is not None:
            where.append(f"id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        if shirtless is not None:
            where.append("shirtless = ?")
            args.append(1 if shirtless else 0)
        if channel is not None:
            where.append("channel = ?")
            args.append(channel)
        if start is not None:
            where.append("ts >= ?")
            args.append(start)
        if end is not None:
            where.append("ts < ?")
            args.append(end)
        if before is not None:
            where.append("id < ?")
            args.append(before)
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_events(self, channel=None, start=None, end=None, before=None, limit=50,
                    shirtless=None) -> tuple[list[dict], bool]:
        where, args = self._where(channel, start, end, before, shirtless)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", [*args, limit + 1]).fetchall()]
        return rows[:limit], len(rows) > limit

    def find_events(self, **filters) -> list[dict]:
        """All events matching the filters (see _where), oldest first."""
        where, args = self._where(**filters)
        with self._lock:
            return [dict(r) for r in self._conn.execute(f"SELECT * FROM events{where} ORDER BY ts", args)]

    def delete_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row:
                self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return dict(row) if row else None

    def delete_events(self, **filters) -> list[dict]:
        """Delete all events matching the filters (see _where); returns the deleted rows."""
        where, args = self._where(**filters)
        if not where:
            raise ValueError("refusing to delete without a filter")
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(f"SELECT * FROM events{where}", args)]
            self._conn.execute(f"DELETE FROM events{where}", args)
        return rows

    def delete_older_than(self, ts: float) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM events WHERE ts < ?", (ts,)).fetchall()]
            self._conn.execute("DELETE FROM events WHERE ts < ?", (ts,))
        return rows

    def oldest_events(self, limit: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM events ORDER BY ts LIMIT ?", (limit,)).fetchall()]

    def stats(self) -> tuple[int, float | None]:
        """(event count, oldest event ts)."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*), MIN(ts) FROM events").fetchone()
        return row[0], row[1]

    def count_since(self, ts: float) -> tuple[int, int]:
        """(all events, shirtless events) since ts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(shirtless = 1), 0) FROM events WHERE ts >= ?", (ts,)).fetchone()
        return row[0], row[1]

    def replace_image(self, event_id: int, image, thumb, persons, shirtless, shirtless_score,
                      detections) -> tuple[dict, dict] | None:
        """Swap in a better frame for an event. Returns (old_row, new_row)."""
        with self._lock:
            old = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not old:
                return None
            if old["reviewed"]:  # a person already said "not shirtless": keep that verdict
                shirtless = 0
            self._conn.execute(
                "UPDATE events SET image = ?, thumb = ?, persons = ?, shirtless = ?, shirtless_score = ?,"
                " detections = ? WHERE id = ?",
                (image, thumb, persons, shirtless, shirtless_score, detections, event_id))
            new = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(old), dict(new)

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                               " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def mark_not_shirtless(self, event_id: int) -> dict | None:
        with self._lock:
            self._conn.execute("UPDATE events SET shirtless = 0, reviewed = 1 WHERE id = ?", (event_id,))
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def last_event_ts(self) -> float | None:
        with self._lock:
            return self._conn.execute("SELECT MAX(ts) FROM events").fetchone()[0]

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
