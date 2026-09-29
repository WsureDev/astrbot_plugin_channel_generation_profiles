from __future__ import annotations

import inspect
from dataclasses import dataclass
from astrbot.api import logger
from .channel import Route, current_route, from_event, set_route

@dataclass(eq=False)
class Patch:
    target: object
    name: str
    original: object
    replacement: object
    own_attribute: bool

class Integration:
    """Optional feature lifecycle; target-specific behavior lives in adapters."""
    name = "integration"
    target_plugin = ""

    def __init__(self, context, config, profiles):
        self.context, self.config, self.profiles = context, config, profiles
        self.target = None
        self.active = False
        self._originals = []
        self._runtime_key = object()

    def initialize(self):
        target = self._resolve_target()
        if self.active:
            return target is self.target
        if target is None:
            return False
        self.target = target
        try:
            self.active = bool(self.install())
        except Exception:
            logger.exception("[%s] integration installation failed", self.name)
            self.active = False
        if not self.active:
            self.restore()
        return self.active

    def install(self):
        raise NotImplementedError

    def on_event(self, event, command, raw):
        return False

    def profile(self, event):
        return self.profiles.for_channel(from_event(event))

    def route_for(self, event):
        name, profile = self.profile(event)
        return Route(event, from_event(event), name, profile=profile)

    def activate_route(self, event):
        route = self.route_for(event)
        set_route(route)
        return route

    def refresh_route(self):
        route = current_route()
        if route is not None:
            set_route(Route(route.event, route.channel, route.profile_name,
                            profile=self.profiles.get(route.profile_name)))

    def settings(self, route, section):
        profile = route.profile if route.profile is not None else self.profiles.get(route.profile_name)
        settings = profile.get(section, {})
        if not isinstance(settings, dict):
            raise ValueError(f"profile {route.profile_name}: {section} must be an object")
        return settings

    def replace(self, obj, name, value):
        patch = Patch(obj, name, getattr(obj, name), value,
                      name == "__class__" or name in getattr(obj, "__dict__", {}))
        setattr(obj, name, value)
        self._originals.append(patch)
        return patch

    def restore(self, exclude=()):
        retained = []
        for patch in reversed(self._originals):
            if patch in exclude:
                retained.append(patch)
                continue
            try:
                if getattr(patch.target, patch.name) is not patch.replacement:
                    continue
                if patch.own_attribute:
                    setattr(patch.target, patch.name, patch.original)
                else:
                    delattr(patch.target, patch.name)
            except Exception:
                logger.exception("[%s] failed to restore %s", self.name, patch.name)
        self._originals = list(reversed(retained))

    def require_call(self, function, *args, **kwargs):
        if not callable(function):
            raise TypeError("required target method is missing")
        if self.config.get("strict_version", True):
            inspect.signature(function).bind(*args, **kwargs)

    async def terminate(self):
        self.active = False
        self.restore()

    def _resolve_target(self):
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(self.target_plugin)
        except Exception:
            logger.exception("[%s] target lookup failed", self.name)
            return None
        if metadata is None or not getattr(metadata, "activated", False):
            return None
        return getattr(metadata, "star_cls", None)

    def _unsupported(self, message):
        logger.error("[%s] incompatible target; integration disabled: %s", self.name, message)
        return False
