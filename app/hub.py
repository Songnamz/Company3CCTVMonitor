import asyncio
import json
import logging

log = logging.getLogger("hub")


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class Hub:
    """Fan-out of server-sent events to every connected browser."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def is_subscribed(self, q: asyncio.Queue) -> bool:
        return q in self._subs

    def publish(self, event: str, data) -> None:
        msg = sse(event, data)
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow client: drop it; its generator notices and closes the stream.
                log.warning("SSE client too slow, dropping it")
                self._subs.discard(q)
