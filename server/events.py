"""In-process pub/sub for live run events (SSE).

Phase 1: single-process only. Multiple uvicorn workers would each have
their own independent broker, so a client's SSE connection would only see
events from whichever worker happens to be running that pipeline's
background task. Fine for one process; a multi-worker deployment would
need a real message bus (e.g. Redis pub/sub) instead.
"""

import asyncio
from collections import defaultdict
from typing import Any


class RunEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[run_id].append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs and q in subs:
            subs.remove(q)

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        for q in list(self._subscribers.get(run_id, [])):
            await q.put(event)


broker = RunEventBroker()
