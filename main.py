from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.channel import from_event, set_route
from .core.profiles import ProfileStore
from .integrations.comfyui import ComfyUIIntegration
from .integrations.image_generation import ImageGenerationIntegration


PLUGIN = "astrbot_plugin_channel_generation_profiles"


class ChannelGenerationProfiles(Star):
    """Composition root for optional channel-generation integrations."""

    # Composition only. Target IDs and target APIs live in integration modules.
    INTEGRATIONS = {
        "comfyui": ComfyUIIntegration,
        "image_generation": ImageGenerationIntegration,
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.profiles = ProfileStore(self.config, Path(StarTools.get_data_dir(PLUGIN)))
        self.integrations: list[Any] = []

    async def initialize(self) -> None:
        selected = self.config.get("features") or list(self.INTEGRATIONS)
        for name in selected:
            factory = self.INTEGRATIONS.get(str(name))
            if factory is None:
                logger.warning("[%s] unknown feature %s; skipped", PLUGIN, name)
                continue
            integration = factory(self.context, self.config, self.profiles)
            if integration.initialize():
                self.integrations.append(integration)
        logger.info("[%s] active integrations: %s", PLUGIN, [item.name for item in self.integrations])

    async def terminate(self) -> None:
        for integration in reversed(self.integrations):
            await integration.terminate()
        self.integrations.clear()
        set_route(None)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10_000)
    async def route_event(self, event: AstrMessageEvent) -> None:
        if not self.integrations:
            return
        for integration in self.integrations:
            integration.activate_route(event)
            raw = (getattr(event, "message_str", "") or "").strip()
            command_text = raw[1:] if raw.startswith("/") else raw
            command = command_text.split(maxsplit=1)[0].lower() if command_text else ""
            if integration.on_event(event, command, raw):
                return

    @filter.on_llm_request(priority=90)
    async def inject_profile_context(self, event: AstrMessageEvent, request: Any) -> None:
        if not self.integrations:
            return
        name, profile = self.profiles.for_channel(from_event(event))
        active_names = {item.name for item in self.integrations}
        sections = []
        if "comfyui" in active_names:
            sections.append(f"comfyui={profile.get('comfyui', {})}")
        if "image_generation" in active_names:
            sections.append(f"image_generation={profile.get('image_generation', {})}")
        if hasattr(request, "add_user_text"):
            request.add_user_text(
                "<channel_generation_profile>\n"
                f"profile={name}\n" + "\n".join(sections) +
                "\n</channel_generation_profile>",
                slot="channel_generation_profile",
            )
        for integration in self.integrations:
            integration.on_llm_request(event, request)

    @filter.command("渠道生图配置")
    async def show_profile(self, event: AstrMessageEvent):
        name, profile = self.profiles.for_channel(from_event(event))
        yield event.plain_result(f"profile={name}\n{profile}")
