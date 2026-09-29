from __future__ import annotations

import asyncio
import importlib
import inspect
from typing import Any

from astrbot.api import logger

from ..core.channel import current_route
from ..core.integration import Integration


class ImageGenerationIntegration(Integration):
    """Channel isolation for the image-generation plugin only."""

    name = "image_generation"
    target_plugin = "astrbot_plugin_image_generation"

    def __init__(self, context, config, profiles):
        super().__init__(context, config, profiles)
        self._executors: dict[str, Any] = {}

    def install(self) -> bool:
        if not callable(getattr(self.target, "create_generation_task", None)):
            return self._unsupported("create_generation_task is missing")
        if not hasattr(self.target, "generation_executor"):
            return self._unsupported("generation_executor is missing")
        original_create = self.target.create_generation_task

        def create_generation_task(*args, **kwargs):
            event = kwargs.get("source_event")
            route = current_route()
            if event is None and route:
                event = route.event
            if event is None:
                return original_create(*args, **kwargs)
            profile_name, _ = self.profile(event)
            executor = self._executor(profile_name)
            if executor is None:
                return original_create(*args, **kwargs)
            previous = self.target.generation_executor
            self.target.generation_executor = executor
            try:
                return original_create(*args, **kwargs)
            finally:
                self.target.generation_executor = previous

        self.replace(self.target, "create_generation_task", create_generation_task)
        return True

    def _executor(self, profile_name: str) -> Any:
        if profile_name in self._executors:
            return self._executors[profile_name]
        settings = self.profiles.get(profile_name).get("image_generation", {}) or {}
        model = settings.get("model")
        if not model:
            return self.target.generation_executor
        try:
            # AstrBot loads plugins under its registered package path, which is
            # commonly `data.plugins.<plugin_name>`, not a top-level package.
            # Derive the package from the live target class instead of guessing
            # an import name.
            target_module = type(self.target).__module__
            package = target_module.rsplit(".", 1)[0]
            generator_module = importlib.import_module(
                f"{package}.core.adapters.generator"
            )
            executor_module = importlib.import_module(
                f"{package}.core.generation.executor"
            )
            ImageGenerator = generator_module.ImageGenerator
            GenerationExecutor = executor_module.GenerationExecutor

            manager = self.target.config_manager
            selected = manager._select_adapter_config(
                getattr(manager, "_all_provider_configs", []), str(model)
            )
            if selected is None:
                raise ValueError(f"model is not configured: {model}")
            executor = GenerationExecutor(
                context=self.target.context,
                config_manager=manager,
                image_processor=self.target.image_processor,
                task_manager=self.target.task_manager,
                usage_manager=self.target.usage_manager,
                safety_auditor=self.target.safety_auditor,
            )
            executor.update_generator(ImageGenerator(selected))
            executor.refresh_request_semaphore()
            self._executors[profile_name] = executor
            return executor
        except Exception as exc:
            logger.error("[%s] cannot create executor for %s: %s", self.name, profile_name, exc)
            return None

    def on_event(self, event: Any, command: str, raw: str) -> bool:
        if command not in {"生图模型", "渠道模型"}:
            return False
        manager = getattr(self.target, "config_manager", None)
        if manager is None:
            return False
        models = list(getattr(manager.adapter_config, "available_models", []) or [])
        profile_name, profile = self.profile(event)
        command_text = raw.strip()
        if command_text.startswith("/"):
            command_text = command_text[1:].lstrip()
        argument = command_text.partition(" ")[2].strip()
        if not argument:
            current = (profile.get("image_generation", {}) or {}).get("model")
            lines = [f"渠道 {profile_name} 可用模型："]
            lines.extend(f"{index}. {model}{' ✓' if model == current else ''}" for index, model in enumerate(models, 1))
            asyncio.create_task(event.send(event.plain_result("\n".join(lines))))
            event.stop_event()
            return True
        try:
            model = models[int(argument) - 1]
        except (ValueError, IndexError):
            asyncio.create_task(event.send(event.plain_result("模型序号无效")))
            event.stop_event()
            return True
        self.profiles.update(profile_name, "image_generation", {"model": model})
        logger.info("[%s] channel=%s profile=%s model=%s", self.name, getattr(event, "unified_msg_origin", ""), profile_name, model)
        self._executors.pop(profile_name, None)
        event.stop_event()
        return True

    async def terminate(self) -> None:
        for executor in self._executors.values():
            generator = getattr(executor, "generator", None)
            close = getattr(generator, "close", None)
            if close:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("[%s] failed to close profile generator", self.name)
        await super().terminate()
