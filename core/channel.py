from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


PLATFORM_ALIASES = {
    "aiocqhttp": "qq",
    "onebot": "qq",
    "onebot11": "qq",
    "napcat": "qq",
    "qq": "qq",
    "qqbot": "qq",
    "qq_official": "qq",
    "qq_official_webhook": "qq",
    "telegram": "telegram",
    "telegram_bot": "telegram",
}


def normalize_platform(value: str) -> str:
    platform = str(value or "unknown").strip().lower()
    return PLATFORM_ALIASES.get(platform, platform)


@dataclass(frozen=True)
class Channel:
    platform: str
    bot_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.bot_id}" if self.bot_id else self.platform


def from_event(event: Any) -> Channel:
    platform = normalize_platform(event.get_platform_name())
    bot_id = ""
    for name in ("get_platform_id", "get_self_id", "get_bot_id"):
        getter = getattr(event, name, None)
        if callable(getter):
            try:
                bot_id = str(getter() or "").strip()
            except Exception:
                bot_id = ""
            if bot_id:
                break
    return Channel(platform, bot_id)


@dataclass(frozen=True)
class Route:
    event: Any
    channel: Channel
    profile_name: str
    profile: dict[str, Any] | None = None
    runtimes: dict[object, Any] = field(default_factory=dict, compare=False, repr=False)


_current_route: contextvars.ContextVar[Route | None] = contextvars.ContextVar(
    "channel_generation_route", default=None
)


def set_route(route: Route | None):
    return _current_route.set(route)


def current_route() -> Route | None:
    return _current_route.get()


@contextmanager
def bind_route(route: Route | None):
    token = set_route(route)
    try:
        yield
    finally:
        _current_route.reset(token)
