from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ..core.channel import current_route
from ..core.integration import Integration


class ComfyUIIntegration(Integration):
    """Channel isolation for the ComfyUI plugin only."""

    name = "comfyui"
    target_plugin = "astrbot_plugin_comfyui_pro"

    def __init__(self, context, config, profiles):
        super().__init__(context, config, profiles)
        self.api = None
        self._lock = asyncio.Lock()
        self._prompt_profiles: dict[str, dict[str, Any]] = {}

    def install(self) -> bool:
        self.api = getattr(self.target, "api", None)
        if self.api is None or not callable(getattr(self.api, "submit", None)):
            return self._unsupported("api.submit is missing")

        original_submit = self.api.submit
        original_wait = getattr(self.api, "wait_for_result", None)
        original_reload = getattr(self.api, "reload_config", None)
        self.api._channel_original_submit = original_submit
        if original_wait:
            self.api._channel_original_wait = original_wait

        async def submit(prompt, *args, **kwargs):
            route = current_route()
            if route is None:
                return await original_submit(prompt, *args, **kwargs)
            return await self._submit_for(route.profile_name, prompt, kwargs)

        self.replace(self.api, "submit", submit)

        if original_wait:
            async def wait(prompt_id, *args, **kwargs):
                profile = self._prompt_profiles.get(prompt_id)
                if profile is None:
                    return await original_wait(prompt_id, *args, **kwargs)
                return await self._wait_for(profile, prompt_id, kwargs)
            self.replace(self.api, "wait_for_result", wait)

        if original_reload:
            def reload_config(filename, input_id=None, output_id=None, neg_node_id=None):
                route = current_route()
                if route is None:
                    return original_reload(filename, input_id=input_id, output_id=output_id, neg_node_id=neg_node_id)
                values = {"workflow": str(filename)}
                for key, value in (("input_node_id", input_id), ("neg_node_id", neg_node_id), ("output_node_id", output_id)):
                    if value is not None:
                        values[key] = str(value)
                self.profiles.update(route.profile_name, "comfyui", values)
                return True, f"已为渠道 {route.profile_name} 保存工作流：{filename}"
            self.replace(self.api, "reload_config", reload_config)
        return True

    async def _submit_for(self, profile_name: str, prompt: str, kwargs: dict[str, Any]):
        profile = self.profiles.get(profile_name)
        settings = profile.get("comfyui", {}) or {}
        workflow = kwargs.pop("workflow_filename", None) or settings.get("workflow") or self.api.wf_filename
        original = self._settings_snapshot()
        async with self._lock:
            self.api.wf_filename = str(workflow)
            self.api.input_id = str(settings.get("input_node_id") or self.api.input_id)
            self.api.neg_node_id = str(settings.get("neg_node_id") or self.api.neg_node_id)
            self.api.output_id = str(settings.get("output_node_id") or self.api.output_id)
            try:
                result = await self.api._channel_original_submit(prompt, workflow_filename=str(workflow), **kwargs)
                if result and result[0]:
                    self._prompt_profiles[result[0]] = profile
                return result
            finally:
                self._restore_snapshot(original)

    async def _wait_for(self, profile: dict[str, Any], prompt_id: str, kwargs: dict[str, Any]):
        settings = profile.get("comfyui", {}) or {}
        original = self._settings_snapshot()
        async with self._lock:
            self.api.output_id = str(settings.get("output_node_id") or self.api.output_id)
            self.api.wf_filename = str(settings.get("workflow") or self.api.wf_filename)
            try:
                return await self.api._channel_original_wait(prompt_id, **kwargs)
            finally:
                self._restore_snapshot(original)

    def _settings_snapshot(self) -> tuple[str, str, str, str]:
        return self.api.wf_filename, self.api.input_id, self.api.neg_node_id, self.api.output_id

    def _restore_snapshot(self, snapshot: tuple[str, str, str, str]) -> None:
        self.api.wf_filename, self.api.input_id, self.api.neg_node_id, self.api.output_id = snapshot

    def on_event(self, event: Any, command: str, raw: str) -> bool:
        if command not in {"comfy_use", "渠道工作流"}:
            return False
        if command == "渠道工作流":
            args = raw.split()
            if len(args) < 2:
                asyncio.create_task(event.send(event.plain_result("用法：/渠道工作流 <文件名> [正向节点] [负向节点] [输出节点]")))
                event.stop_event()
                return True
            filename = args[1]
            values = {"workflow": filename}
            for key, value in zip(("input_node_id", "neg_node_id", "output_node_id"), args[2:5]):
                values[key] = value
        else:
            args = raw.split()
            try:
                files = sorted(f.name for f in self.api.workflow_dir.glob("*.json") if not self.target._is_workflow_aux_file(f.name))
                filename = files[int(args[1]) - 1]
            except (IndexError, ValueError, OSError):
                asyncio.create_task(event.send(event.plain_result("工作流序号无效")))
                event.stop_event()
                return True
            values = {"workflow": filename}
            for key, value in zip(("input_node_id", "neg_node_id", "output_node_id"), args[2:5]):
                values[key] = value
        name, _ = self.profile(event)
        self.profiles.update(name, "comfyui", values)
        event.stop_event()
        return True

    def on_llm_request(self, event: Any, request: Any) -> None:
        return None
