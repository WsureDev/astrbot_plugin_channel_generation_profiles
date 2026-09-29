from __future__ import annotations

import asyncio
from dataclasses import dataclass
from astrbot.api import logger

@dataclass(eq=False)
class Resource:
    value: object
    references: int = 0
    retired: bool = False
    closed: bool = False

class ResourcePool:
    """Keep retired clients alive while requests or queued jobs still own them."""
    def __init__(self, close):
        self.close = close
        self.resources = set()
        self.pins = {}
        self.closing = set()

    def add(self, value):
        resource = Resource(value)
        self.resources.add(resource)
        return resource

    def hold(self, resource):
        if resource.closed:
            raise RuntimeError("runtime has already been released")
        resource.references += 1
        released = False
        def release():
            nonlocal released
            if released:
                return
            released = True
            resource.references -= 1
            self._collect(resource)
        return release

    def pin(self, resource):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            return
        if task not in self.pins:
            self.pins[task] = {}
            task.add_done_callback(self._unpin)
        if resource not in self.pins[task]:
            self.pins[task][resource] = self.hold(resource)

    def _unpin(self, task):
        for release in self.pins.pop(task, {}).values():
            release()

    def retire(self, resource):
        resource.retired = True
        self._collect(resource)

    def _collect(self, resource):
        if not resource.retired or resource.references or resource.closed:
            return
        resource.closed = True
        self.resources.discard(resource)
        async def finish():
            try:
                await self.close(resource.value)
            except Exception:
                logger.exception("Failed to close retired channel runtime")
        task = asyncio.create_task(finish())
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)

    async def shutdown(self):
        for resource in list(self.resources):
            self.retire(resource)
        if self.closing:
            await asyncio.gather(*list(self.closing))
