from __future__ import annotations

import asyncio
from pathlib import Path
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from .core.channel import Route, current_route, from_event, set_route
from .core.profiles import ProfileStore
from .integrations.comfyui import ComfyUIIntegration
from .integrations.image_generation import ImageGenerationIntegration

PLUGIN = "astrbot_plugin_channel_generation_profiles"

class ChannelGenerationProfiles(Star):
    """Composition and framework lifecycle only; business hooks live in adapters."""
    INTEGRATIONS = {"comfyui": ComfyUIIntegration, "image_generation": ImageGenerationIntegration}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self.profiles = ProfileStore(self.config, Path(StarTools.get_data_dir(PLUGIN)))
        self.integrations = {}
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False

    async def initialize(self):
        selected = self.config.get("features", list(self.INTEGRATIONS))
        if not isinstance(selected, list):
            raise ValueError("features must be a list")
        for name in dict.fromkeys(selected):
            if name not in self.INTEGRATIONS:
                logger.warning("[%s] unknown feature %s; skipped", PLUGIN, name)
                continue
            self.integrations[name] = self.INTEGRATIONS[name](self.context, self.config, self.profiles)
        await self._reconcile()

    async def _reconcile(self, plugin_name=None):
        async with self._lifecycle_lock:
            if self._stopping:
                return
            for name, integration in list(self.integrations.items()):
                if plugin_name and integration.target_plugin != plugin_name:
                    continue
                target = integration._resolve_target()
                if integration.active and target is integration.target:
                    continue
                # Fresh generations keep old queued jobs and submission maps
                # separate from a newly loaded target instance.
                await integration.terminate()
                integration = self.INTEGRATIONS[name](self.context, self.config, self.profiles)
                self.integrations[name] = integration
                integration.initialize()
            logger.info("[%s] active integrations: %s", PLUGIN,
                        [name for name, item in self.integrations.items() if item.active])

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self._reconcile()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        name = metadata if isinstance(metadata, str) else getattr(metadata, "name", None)
        if name and name != PLUGIN:
            await self._reconcile(name)

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata):
        name = metadata if isinstance(metadata, str) else getattr(metadata, "name", None)
        async with self._lifecycle_lock:
            for feature, integration in list(self.integrations.items()):
                if integration.target_plugin == name:
                    await integration.terminate()
                    self.integrations[feature] = self.INTEGRATIONS[feature](self.context, self.config, self.profiles)

    async def terminate(self):
        async with self._lifecycle_lock:
            self._stopping = True
            for integration in reversed(list(self.integrations.values())):
                await integration.terminate()
            self.integrations.clear()
        set_route(None)

    def _route(self, event, preserve=False):
        if not any(item.active for item in self.integrations.values()):
            set_route(None)
            return
        previous = current_route()
        if preserve and previous is not None and previous.event is event:
            return
        channel = from_event(event)
        name, profile = self.profiles.for_channel(channel)
        set_route(Route(event, channel, name, profile=profile))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10_000)
    async def route_event(self, event: AstrMessageEvent):
        self._route(event)

    @filter.on_llm_request(priority=10_000)
    async def route_llm_request(self, event: AstrMessageEvent, request):
        # Bind execution state; do not append a second competing prompt.
        self._route(event, preserve=True)

    @filter.on_using_llm_tool(priority=10_000)
    async def route_llm_tool(self, event: AstrMessageEvent, tool, tool_args):
        self._route(event, preserve=True)
        for integration in self.integrations.values():
            if integration.active:
                integration.on_using_llm_tool(event, tool, tool_args)

    @filter.command("渠道生图配置")
    async def show_profile(self, event: AstrMessageEvent):
        name, profile = self.profiles.for_channel(from_event(event))
        status = {key: item.active for key, item in self.integrations.items()}
        yield event.plain_result(f"profile={name}\n{profile}\nintegrations={status}")
