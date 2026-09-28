from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .channel import Route, from_event, set_route
from .profiles import ProfileStore


class Integration:
    """Lifecycle contract for one optional target-plugin integration."""

    name = "integration"
    target_plugin = ""

    def __init__(self, context: Any, config: dict[str, Any], profiles: ProfileStore):
        self.context = context
        self.config = config
        self.profiles = profiles
        self.target: Any = None
        self.active = False
        self._originals: list[tuple[Any, str, Any]] = []

    def initialize(self) -> bool:
        self.target = self._resolve_target()
        if self.target is None:
            return False
        try:
            self.active = bool(self.install())
        except Exception:
            logger.exception("[%s] failed to install integration", self.name)
            self.restore()
            self.active = False
        return self.active

    def install(self) -> bool:
        raise NotImplementedError

    def on_event(self, event: Any, command: str, raw: str) -> bool:
        return False

    def on_llm_request(self, event: Any, request: Any) -> None:
        return None

    def profile(self, event: Any) -> tuple[str, dict[str, Any]]:
        return self.profiles.for_channel(from_event(event))

    def activate_route(self, event: Any) -> None:
        name, _ = self.profile(event)
        set_route(Route(event, from_event(event), name))

    def replace(self, obj: Any, name: str, value: Any) -> None:
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, original in reversed(self._originals):
            try:
                setattr(obj, name, original)
            except Exception:
                logger.exception("[%s] failed to restore %s.%s", self.name, type(obj).__name__, name)
        self._originals.clear()

    async def terminate(self) -> None:
        self.restore()

    def _resolve_target(self) -> Any:
        getter = getattr(self.context, "get_registered_star", None)
        if not self.target_plugin or not callable(getter):
            logger.warning("[%s] target lookup unavailable; integration disabled", self.name)
            return None
        try:
            target = getter(self.target_plugin)
        except Exception as exc:
            logger.warning("[%s] target %s unavailable: %s", self.name, self.target_plugin, exc)
            return None
        if target is None:
            logger.info("[%s] target %s is not loaded; integration disabled", self.name, self.target_plugin)
        return target

    def _unsupported(self, message: str) -> bool:
        logger.error("[%s] incompatible target: %s; integration disabled", self.name, message)
        return False
