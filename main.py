from __future__ import annotations

import asyncio
import copy
import contextvars
import functools
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


COMFY = "astrbot_plugin_comfyui_pro"
IMAGE = "astrbot_plugin_image_generation"
PLUGIN = "astrbot_plugin_channel_generation_profiles"
_ACTIVE_ROUTE: contextvars.ContextVar[tuple["ChannelGenerationProfiles", AstrMessageEvent] | None] = contextvars.ContextVar(
    "channel_generation_active_route", default=None
)


@dataclass(frozen=True)
class ChannelKey:
    platform: str
    bot_id: str

    def text(self) -> str:
        return f"{self.platform}:{self.bot_id}" if self.bot_id else self.platform


def channel_key(event: AstrMessageEvent) -> ChannelKey:
    platform = str(event.get_platform_name() or "unknown").strip().lower()
    bot_id = ""
    for attr in ("get_self_id", "get_bot_id"):
        getter = getattr(event, attr, None)
        if callable(getter):
            try:
                bot_id = str(getter() or "").strip()
            except Exception:
                pass
            if bot_id:
                break
    return ChannelKey(platform, bot_id)


class ProfileStore:
    """Persistent profile state; profile data never enters the target plugins' config."""

    def __init__(self, config: dict[str, Any], data_dir: Path):
        self.config = config or {}
        self.data_dir = data_dir
        self.path = data_dir / "profiles.json"
        self.profiles = copy.deepcopy(self.config.get("profiles") or {})
        self.bindings = self._build_bindings(self.profiles)
        self.state: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _build_bindings(profiles: dict[str, Any]) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for name, value in profiles.items():
            for platform in (value or {}).get("platforms", []) or []:
                bindings[str(platform).strip().lower()] = str(name)
        return bindings

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.state = raw
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def name_for(self, event: AstrMessageEvent) -> str:
        key = channel_key(event)
        return self.bindings.get(key.platform, self.bindings.get("*", "default"))

    def profile(self, event: AstrMessageEvent) -> dict[str, Any]:
        name = self.name_for(event)
        configured = copy.deepcopy(self.profiles.get(name) or {})
        current = copy.deepcopy(self.state.get(name) or {})
        return _deep_merge(configured, current)

    def update(self, event: AstrMessageEvent, section: str, values: dict[str, Any]) -> None:
        name = self.name_for(event)
        self.state.setdefault(name, {})
        self.state[name].setdefault(section, {}).update(values)
        self.save()


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class ComfyProfileProxy:
    """A per-profile view over the target ComfyUI API object."""

    def __init__(self, api: Any, profile: dict[str, Any], owner: "ChannelGenerationProfiles"):
        self._api = api
        self._profile = profile
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def _workflow(self) -> dict[str, Any]:
        return self._profile.get("comfyui", {}) or {}

    @property
    def wf_filename(self) -> str:
        return str(self._workflow().get("workflow") or self._api.wf_filename)

    @property
    def input_id(self) -> str:
        return str(self._workflow().get("input_node_id") or self._api.input_id)

    @property
    def neg_node_id(self) -> str:
        return str(self._workflow().get("neg_node_id") or self._api.neg_node_id)

    @property
    def output_id(self) -> str:
        return str(self._workflow().get("output_node_id") or self._api.output_id)

    async def submit(self, prompt: str, **kwargs: Any):
        """Snapshot node settings before delegating; submit resolves the workflow per call."""
        workflow = kwargs.pop("workflow_filename", None) or self.wf_filename
        async with self._owner._comfy_lock:
            original = (self._api.wf_filename, self._api.input_id, self._api.neg_node_id, self._api.output_id)
            self._api.wf_filename, self._api.input_id = workflow, self.input_id
            self._api.neg_node_id, self._api.output_id = self.neg_node_id, self.output_id
            try:
                result = await self._api._channel_original_submit(prompt, workflow_filename=workflow, **kwargs)
                if result and result[0]:
                    self._owner._comfy_prompt_profiles[result[0]] = self._profile
                return result
            finally:
                self._api.wf_filename, self._api.input_id, self._api.neg_node_id, self._api.output_id = original

    async def generate(self, prompt: str, **kwargs: Any):
        result = await self.submit(prompt, **kwargs)
        if not result or not result[0]:
            return result
        return await self.wait_for_result(result[0])

    async def wait_for_result(self, prompt_id: str, **kwargs: Any):
        profile = self._owner._comfy_prompt_profiles.get(prompt_id, self._profile)
        workflow = profile.get("comfyui", {}) or {}
        async with self._owner._comfy_lock:
            original = (self._api.output_id, self._api.wf_filename)
            self._api.output_id = str(workflow.get("output_node_id") or self._api.output_id)
            self._api.wf_filename = str(workflow.get("workflow") or self._api.wf_filename)
            try:
                return await self._api._channel_original_wait(prompt_id, **kwargs)
            finally:
                self._api.output_id, self._api.wf_filename = original


class ChannelGenerationProfiles(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN))
        self.store = ProfileStore(self.config, self.data_dir)
        self._target_comfy: Any = None
        self._target_image: Any = None
        self._comfy_enabled = False
        self._image_enabled = False
        self._originals: list[tuple[Any, str, Any]] = []
        self._comfy_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._comfy_prompt_profiles: dict[str, dict[str, Any]] = {}
        self._image_executors: dict[str, Any] = {}
        self._origin_profiles: dict[str, str] = {}

    async def initialize(self) -> None:
        targets = self.config.get("targets") or {}
        self._target_comfy = self._find_target(targets.get("comfyui", COMFY), "ComfyUI")
        self._target_image = self._find_target(targets.get("image_generation", IMAGE), "image generation")
        self._comfy_enabled = self._patch_comfy()
        self._image_enabled = self._patch_image_generation()
        logger.info(
            "[%s] initialized; profiles=%s; comfy_hook=%s; image_hook=%s",
            PLUGIN,
            sorted(self.store.profiles),
            self._comfy_enabled,
            self._image_enabled,
        )

    def _find_target(self, name: str, label: str) -> Any:
        """Resolve a loaded Star without making a missing target fatal."""
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            logger.warning("[%s] %s target lookup is unavailable; hook disabled", PLUGIN, label)
            return None
        try:
            target = getter(str(name))
        except Exception as exc:
            logger.warning("[%s] %s target %s not found: %s; hook disabled", PLUGIN, label, name, exc)
            return None
        if target is None:
            logger.info("[%s] %s target %s is not loaded; hook disabled", PLUGIN, label, name)
            return None
        return target

    async def terminate(self) -> None:
        for executor in self._image_executors.values():
            generator = getattr(executor, "generator", None)
            close = getattr(generator, "close", None)
            if close:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("[%s] failed to close profile image generator", PLUGIN)
        for obj, name, original in reversed(self._originals):
            try:
                setattr(obj, name, original)
            except Exception:
                logger.exception("[%s] failed to restore %s.%s", PLUGIN, type(obj).__name__, name)
        self._originals.clear()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10_000)
    async def activate_channel_profile(self, event: AstrMessageEvent) -> None:
        """Set routing context before commands, tools, and automatic ComfyUI hooks run."""
        if not self._comfy_enabled and not self._image_enabled:
            return
        _ACTIVE_ROUTE.set((self, event))
        self._origin_profiles[str(event.unified_msg_origin)] = self.store.name_for(event)
        raw = (getattr(event, "message_str", "") or "").strip()
        if not raw.startswith("/"):
            return
        command = raw[1:].split(maxsplit=1)[0].lower()
        if command == "comfy_use" and self._comfy_enabled:
            await self._handle_workflow_command(event)
        elif command == "生图模型" and self._image_enabled:
            await self._handle_model_command(event, raw.partition(" ")[2].strip())

    def _patch_comfy(self) -> bool:
        plugin = self._target_comfy
        api = getattr(plugin, "api", None) if plugin else None
        if not plugin:
            logger.info("[%s] ComfyUI target is absent; ComfyUI hook disabled", PLUGIN)
            return False
        if not api or not callable(getattr(api, "submit", None)):
            self._compatibility_error("ComfyUI API unavailable")
            return False
        original_submit = api.submit
        original_wait = getattr(api, "wait_for_result", None)
        api._channel_original_submit = original_submit
        if original_wait:
            api._channel_original_wait = original_wait

        async def bound_submit(prompt, *args, **kwargs):
            route = _ACTIVE_ROUTE.get()
            if route is None:
                return await original_submit(prompt, *args, **kwargs)
            _, event = route
            proxy = ComfyProfileProxy(api, self.store.profile(event), self)
            return await proxy.submit(prompt, **kwargs)

        self._replace(api, "submit", bound_submit)
        if original_wait:
            async def bound_wait(prompt_id, *args, **kwargs):
                profile = self._comfy_prompt_profiles.get(prompt_id)
                if profile is None:
                    return await original_wait(prompt_id, *args, **kwargs)
                return await ComfyProfileProxy(api, profile, self).wait_for_result(prompt_id, **kwargs)
            self._replace(api, "wait_for_result", bound_wait)

        original_reload = getattr(api, "reload_config", None)
        if original_reload:
            def reload_for_channel(filename, input_id=None, output_id=None, neg_node_id=None):
                route = _ACTIVE_ROUTE.get()
                if route is None:
                    return original_reload(filename, input_id=input_id, output_id=output_id, neg_node_id=neg_node_id)
                _, event = route
                values = {"workflow": str(filename)}
                if input_id is not None:
                    values["input_node_id"] = str(input_id)
                if neg_node_id is not None:
                    values["neg_node_id"] = str(neg_node_id)
                if output_id is not None:
                    values["output_node_id"] = str(output_id)
                self.store.update(event, "comfyui", values)
                return True, f"已为渠道 {self.store.name_for(event)} 保存工作流：{filename}"
            self._replace(api, "reload_config", reload_for_channel)
        return True

    def _patch_image_generation(self) -> bool:
        plugin = self._target_image
        if plugin is None:
            logger.info("[%s] image generation target is absent; image hook disabled", PLUGIN)
            return False
        if not callable(getattr(plugin, "create_generation_task", None)) or not hasattr(plugin, "generation_executor"):
            self._compatibility_error("image generation task API unavailable")
            return False
        original_create = plugin.create_generation_task

        # create_generation_task closes over self.generation_executor. Temporarily selecting
        # a per-profile executor here makes the queued closure retain that executor.
        def create_for_channel(*args, **kwargs):
            event = kwargs.get("source_event")
            route = _ACTIVE_ROUTE.get()
            if event is None and route:
                event = route[1]
            if event is None:
                return original_create(*args, **kwargs)
            name = self.store.name_for(event)
            self._origin_profiles[str(kwargs.get("unified_msg_origin", event.unified_msg_origin))] = name
            executor = self._get_image_executor(name)
            if executor is None:
                return original_create(*args, **kwargs)
            previous = plugin.generation_executor
            plugin.generation_executor = executor
            try:
                return original_create(*args, **kwargs)
            finally:
                plugin.generation_executor = previous

        self._replace(plugin, "create_generation_task", create_for_channel)

        original_model = getattr(plugin, "model_command", None)
        if original_model:
            # The command registry may hold the original bound method, so the event hook
            # below handles the user-facing command. This wrapper is useful for direct calls.
            async def model_for_channel(event, model_index=""):
                return await self._handle_model_command(event, model_index)
            self._replace(plugin, "model_command", model_for_channel)
        return True

    def _get_image_executor(self, profile_name: str) -> Any:
        if profile_name in self._image_executors:
            return self._image_executors[profile_name]
        plugin = self._target_image
        try:
            from astrbot_plugin_image_generation.core.adapters.generator import ImageGenerator
            from astrbot_plugin_image_generation.core.generation.executor import GenerationExecutor
            profile = self.store.profiles.get(profile_name, {})
            model = (self.store.state.get(profile_name, {}).get("image_generation", {}) or {}).get("model")
            model = model or (profile.get("image_generation", {}) or {}).get("model")
            if not model:
                return plugin.generation_executor
            # ConfigManager is read-only for execution here. Reusing it avoids copying
            # internal locks/caches; the selected ImageGenerator remains profile-local.
            manager = plugin.config_manager
            providers = getattr(manager, "_all_provider_configs", [])
            selected = manager._select_adapter_config(providers, str(model))
            if selected is None:
                raise ValueError(f"model is not configured: {model}")
            generator = ImageGenerator(selected)
            executor = GenerationExecutor(
                context=plugin.context,
                config_manager=manager,
                image_processor=plugin.image_processor,
                task_manager=plugin.task_manager,
                usage_manager=plugin.usage_manager,
                safety_auditor=plugin.safety_auditor,
            )
            executor.update_generator(generator)
            executor.refresh_request_semaphore()
            self._image_executors[profile_name] = executor
            return executor
        except Exception as exc:
            logger.error("[%s] failed to create image executor for %s: %s", PLUGIN, profile_name, exc)
            return None

    def _replace(self, obj: Any, name: str, replacement: Any) -> None:
        self._originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, replacement)

    def _compatibility_error(self, message: str) -> None:
        if bool(self.config.get("strict_version", True)):
            logger.error("[%s] disabled target integration: %s", PLUGIN, message)
        else:
            logger.warning("[%s] target integration degraded: %s", PLUGIN, message)

    def _profile_text(self, event: AstrMessageEvent) -> str:
        name = self.store.name_for(event)
        profile = self.store.profile(event)
        return json.dumps({"profile": name, "config": profile}, ensure_ascii=False, indent=2)

    async def _handle_workflow_command(self, event: AstrMessageEvent) -> None:
        args = (event.message_str or "").split()
        if len(args) < 2:
            await event.send(event.plain_result("用法：/comfy_use <序号> [正向节点] [负向节点] [输出节点]"))
            event.stop_event()
            return
        plugin = self._target_comfy
        api = getattr(plugin, "api", None) if plugin else None
        if not api:
            await event.send(event.plain_result("ComfyUI API 未初始化"))
            event.stop_event()
            return
        try:
            files = sorted(f.name for f in api.workflow_dir.glob("*.json") if not plugin._is_workflow_aux_file(f.name))
            index = int(args[1])
            filename = files[index - 1]
        except (ValueError, IndexError, OSError) as exc:
            await event.send(event.plain_result(f"工作流序号无效：{exc}"))
            event.stop_event()
            return
        values = {"workflow": filename}
        for key, value in zip(("input_node_id", "neg_node_id", "output_node_id"), args[2:5]):
            values[key] = value
        self.store.update(event, "comfyui", values)
        await event.send(event.plain_result(f"已为渠道 {self.store.name_for(event)} 切换工作流：{filename}"))
        event.stop_event()

    async def _handle_model_command(self, event: AstrMessageEvent, model_index: str = "") -> None:
        plugin = self._target_image
        manager = getattr(plugin, "config_manager", None) if plugin else None
        if not manager:
            await event.send(event.plain_result("生图插件未初始化"))
            event.stop_event()
            return
        models = list(getattr(manager.adapter_config, "available_models", []) or [])
        if not model_index:
            current = (self.store.profile(event).get("image_generation", {}) or {}).get("model")
            await event.send(event.plain_result("可用模型：\n" + "\n".join(
                f"{index}. {model}{' ✓' if model == current else ''}" for index, model in enumerate(models, 1)
            )))
            event.stop_event()
            return
        try:
            model = models[int(model_index) - 1]
        except (ValueError, IndexError):
            await event.send(event.plain_result("模型序号无效"))
            event.stop_event()
            return
        self.store.update(event, "image_generation", {"model": model})
        self._image_executors.pop(self.store.name_for(event), None)
        await event.send(event.plain_result(f"已为渠道 {self.store.name_for(event)} 切换生图模型：{model}"))
        event.stop_event()

    @filter.on_llm_request(priority=90)
    async def inject_profile_context(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._comfy_enabled and not self._image_enabled:
            return
        _ACTIVE_ROUTE.set((self, event))
        self._origin_profiles[str(event.unified_msg_origin)] = self.store.name_for(event)
        sections = []
        profile = self.store.profile(event)
        if self._comfy_enabled:
            sections.append("comfyui=" + json.dumps(profile.get("comfyui", {}), ensure_ascii=False))
        if self._image_enabled:
            sections.append("image_generation=" + json.dumps(profile.get("image_generation", {}), ensure_ascii=False))
        text = "<channel_generation_profile>\n当前请求只允许使用本渠道绑定的配置。\n" + "\n".join(sections) + "\n</channel_generation_profile>"
        if hasattr(req, "add_user_text"):
            req.add_user_text(text, slot="channel_generation_profile")

    @filter.command("渠道生图配置")
    async def show_profile(self, event: AstrMessageEvent):
        yield event.plain_result(self._profile_text(event))

    @filter.command("渠道工作流")
    async def set_workflow(self, event: AstrMessageEvent):
        if not self._comfy_enabled:
            yield event.plain_result("ComfyUI 插件未加载，渠道工作流 hook 未启用")
            return
        args = (event.message_str or "").split()
        if len(args) < 2:
            yield event.plain_result("用法：/渠道工作流 <文件名> [正向节点] [负向节点] [输出节点]")
            return
        values = {"workflow": args[1]}
        for key, value in zip(("input_node_id", "neg_node_id", "output_node_id"), args[2:5]):
            values[key] = value
        self.store.update(event, "comfyui", values)
        yield event.plain_result(f"已为渠道 {self.store.name_for(event)} 保存工作流配置：{values}")

    @filter.command("渠道模型")
    async def set_model(self, event: AstrMessageEvent):
        if not self._image_enabled:
            yield event.plain_result("生图插件未加载，渠道模型 hook 未启用")
            return
        args = (event.message_str or "").split(maxsplit=1)
        if len(args) < 2 or "/" not in args[1]:
            yield event.plain_result("用法：/渠道模型 <供应商名/模型名>")
            return
        self.store.update(event, "image_generation", {"model": args[1].strip()})
        yield event.plain_result(f"已为渠道 {self.store.name_for(event)} 保存生图模型：{args[1].strip()}")
