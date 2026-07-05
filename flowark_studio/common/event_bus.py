from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

from flowark_studio.common.models import StudioEvent


@dataclass(slots=True, unsafe_hash=True)
class _Subscriber:
    queue: asyncio.Queue[StudioEvent | None]
    task_id: str | None


class EventBus:
    """In-memory event fan-out for Studio SSE streams."""

    def __init__(self, *, history_limit: int = 2000) -> None:
        self._history_limit = max(1, int(history_limit))
        self._task_history: dict[str, deque[StudioEvent]] = {}
        self._global_history: deque[StudioEvent] = deque(maxlen=self._history_limit)
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: StudioEvent) -> None:
        async with self._lock:
            self._global_history.append(event)
            if event.task_id not in self._task_history:
                self._task_history[event.task_id] = deque(maxlen=self._history_limit)
            self._task_history[event.task_id].append(event)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            if sub.task_id is not None and sub.task_id != event.task_id:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = sub.queue.get_nowait()
                except Exception:
                    pass
                try:
                    sub.queue.put_nowait(event)
                except Exception:
                    pass

    async def subscribe_task(self, task_id: str, *, replay_last: int = 200) -> AsyncIterator[StudioEvent]:
        queue: asyncio.Queue[StudioEvent | None] = asyncio.Queue(maxsize=2000)
        sub = _Subscriber(queue=queue, task_id=task_id)
        async with self._lock:
            self._subscribers.add(sub)
            history = list(self._task_history.get(task_id, ()))
        for ev in history[-max(0, replay_last) :]:
            yield ev
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            async with self._lock:
                self._subscribers.discard(sub)

    async def subscribe_all(self, *, replay_last: int = 200) -> AsyncIterator[StudioEvent]:
        queue: asyncio.Queue[StudioEvent | None] = asyncio.Queue(maxsize=2000)
        sub = _Subscriber(queue=queue, task_id=None)
        async with self._lock:
            self._subscribers.add(sub)
            history = list(self._global_history)
        for ev in history[-max(0, replay_last) :]:
            yield ev
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            async with self._lock:
                self._subscribers.discard(sub)

    async def close_task_streams(self, task_id: str) -> None:
        async with self._lock:
            subscribers = [s for s in self._subscribers if s.task_id == task_id]
        for sub in subscribers:
            try:
                sub.queue.put_nowait(None)
            except Exception:
                pass
