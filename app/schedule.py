"""Per-camera capture settings: when each camera captures, and what it detects.

Stored as JSON in the settings table:
    {"cameras": {"1": {"enabled": bool,             # false = capture all the time
                       "rules": [{"days": [0..6],   # Monday = 0
                                  "start": "HH:MM", "end": "HH:MM"}],  # end <= start wraps midnight
                       "paused_until": epoch | null,                   # manual pause, overrides
                       "detect": {"person": bool,   # anyone (incl. people too far to judge clothing)
                                  "white": bool,    # white shirt
                                  "black": bool,    # black shirt
                                  "none": bool},    # no shirt (also enables the no-shirt alert)
                       "zones": [[[x, y], ...], ...]},  # detection areas, 0-1 of the frame; [] = whole view
                 "2": {...}}}
"""

import re
import time
from datetime import datetime

DETECT_KEYS = ("person", "white", "black", "none")
MAX_ZONES = 10
MAX_ZONE_POINTS = 50
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def camera_default() -> dict:
    return {"enabled": False, "rules": [], "paused_until": None, "detect": {k: True for k in DETECT_KEYS},
            "zones": []}


def default(channels: list[int]) -> dict:
    return {"cameras": {str(c): camera_default() for c in channels}}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _migrate(data: dict, channels: list[int]) -> dict:
    """Convert the first (global) format {"enabled", "rules": [{..., "channels"}], "paused_until"}."""
    cams = {}
    for c in channels:
        rules = [{k: r[k] for k in ("days", "start", "end")}
                 for r in data.get("rules") or [] if c in r.get("channels", [])]
        cams[str(c)] = {"enabled": bool(data.get("enabled")), "rules": rules,
                        "paused_until": data.get("paused_until"),
                        "detect": {k: True for k in DETECT_KEYS}, "zones": []}
    return {"cameras": cams}


def _zones(raw, cam: str) -> list:
    """Polygons as [[x, y], ...] with coordinates as fractions (0-1) of the frame."""
    zones = []
    for poly in raw or []:
        if not isinstance(poly, list) or not 3 <= len(poly) <= MAX_ZONE_POINTS:
            raise ValueError(f"CH{cam}: each area needs 3 to {MAX_ZONE_POINTS} points")
        zones.append([[round(min(max(float(x), 0.0), 1.0), 4), round(min(max(float(y), 0.0), 1.0), 4)]
                      for x, y in poly])
    if len(zones) > MAX_ZONES:
        raise ValueError(f"CH{cam}: at most {MAX_ZONES} areas")
    return zones


def validate(data: dict, channels: list[int]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("schedule must be an object")
    if "cameras" not in data:
        data = _migrate(data, channels)
    out = default(channels)
    for key, cam in (data.get("cameras") or {}).items():
        if str(key) not in out["cameras"] or not isinstance(cam, dict):
            continue
        rules = []
        for r in cam.get("rules") or []:
            days = sorted({int(d) for d in r.get("days", []) if 0 <= int(d) <= 6})
            start, end = str(r.get("start", "")), str(r.get("end", ""))
            if not days:
                raise ValueError(f"CH{key}: each time range needs at least one day")
            if not _TIME.match(start) or not _TIME.match(end):
                raise ValueError(f"CH{key}: times must be HH:MM")
            rules.append({"days": days, "start": start, "end": end})
        paused = cam.get("paused_until")
        detect = cam.get("detect") or {}
        out["cameras"][str(key)] = {"enabled": bool(cam.get("enabled")), "rules": rules,
                                    "paused_until": float(paused) if paused else None,
                                    "detect": {k: bool(detect.get(k, True)) for k in DETECT_KEYS},
                                    "zones": _zones(cam.get("zones"), key)}
    return out


def _rule_matches(rule: dict, weekday: int, minute: int) -> bool:
    s, e = _minutes(rule["start"]), _minutes(rule["end"])
    days = rule["days"]
    if s == e:                       # whole day
        return weekday in days
    if s < e:
        return weekday in days and s <= minute < e
    # Wraps past midnight: the part after midnight belongs to the previous day's rule.
    return (weekday in days and minute >= s) or ((weekday - 1) % 7 in days and minute < e)


def capture_allowed(sched: dict, channel: int, ts: float | None = None) -> tuple[bool, str]:
    ts = time.time() if ts is None else ts
    cam = sched.get("cameras", {}).get(str(channel)) or camera_default()
    paused = cam.get("paused_until")
    if paused and ts < paused:
        return False, "paused"
    if not cam.get("enabled"):
        return True, "always"
    dt = datetime.fromtimestamp(ts)
    minute = dt.hour * 60 + dt.minute
    if any(_rule_matches(r, dt.weekday(), minute) for r in cam.get("rules", [])):
        return True, "scheduled"
    return False, "outside schedule"


def active_channels(sched: dict, channels: list[int], ts: float | None = None) -> list[int]:
    return [c for c in channels if capture_allowed(sched, c, ts)[0]]


def detect_settings(sched: dict) -> dict:
    return {c: cam["detect"] for c, cam in sched.get("cameras", {}).items()}


def zone_settings(sched: dict) -> dict:
    return {c: cam.get("zones", []) for c, cam in sched.get("cameras", {}).items()}
