from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from functools import wraps
from typing import Any
from astrbot.api import logger
from ..core.channel import current_route, normalize_platform
from ..core.integration import Integration

@dataclass
class Submission:
    api: Any
    timer: Any = None
    waiters: int = 0

class ChannelComfyAPI:
    """Bound methods execute on a real isolated client, not the shared API."""
    def __init__(self, integration):
        self._integration = integration

    def __getattr__(self, name):
        return getattr(self._integration.runtime(), name)

    def reload_config(self, filename, input_id=None, output_id=None, neg_node_id=None):
        return self._integration.reload_config(filename, input_id, output_id, neg_node_id)

    async def submit(self, *args, **kwargs):
        return await self._integration.submit(*args, **kwargs)

    async def wait_for_result(self, prompt_id, *args, **kwargs):
        return await self._integration.wait(prompt_id, *args, **kwargs)

    async def generate(self, *args, **kwargs):
        prompt_id, error = await self.submit(*args, **kwargs)
        if not prompt_id:
            return None, error
        return await self.wait_for_result(prompt_id)

class ComfyUIIntegration(Integration):
    name = "comfyui"
    target_plugin = "astrbot_plugin_comfyui_pro"
    # Expire abandoned submissions; actively awaited jobs are never expired.
    snapshot_ttl_seconds = 3600

    def __init__(self, context, config, profiles):
        super().__init__(context, config, profiles)
        self.api = None
        self._clients = {}
        self._submissions = {}
        self._submitting = 0
        self._wait_patch = None
        self._direct_send_platforms = frozenset()

    def install(self):
        self.api = getattr(self.target, "api", None)
        if self.api is None:
            return self._unsupported("api is missing")
        for field in ("wf_filename", "input_id", "neg_node_id", "output_id", "data_dir"):
            if not hasattr(self.api, field):
                return self._unsupported(f"api.{field} is missing")
        self.require_call(type(self.api), {}, data_dir=self.api.data_dir)
        self.require_call(self.api.submit, "prompt", lora_selections=None, negative_prompt=None, workflow_filename=None)
        self.require_call(self.api.wait_for_result, "id", timeout_seconds=120)
        self.require_call(self.api.reload_config, "workflow.json", input_id=None, output_id=None, neg_node_id=None)
        self.require_call(self.api.resolve_workflow_filename, None)
        platforms = self.config.get("comfyui_direct_send_platforms", ["telegram"])
        if not isinstance(platforms, list) or any(
            not isinstance(value, str) or not value.strip() for value in platforms
        ):
            return self._unsupported("comfyui_direct_send_platforms must be a list of platform names")
        self._direct_send_platforms = frozenset(normalize_platform(value) for value in platforms)
        if self._direct_send_platforms:
            original_paint = getattr(self.target, "_handle_paint_logic", None)
            self.require_call(original_paint, None, direct_send=False)

            @wraps(original_paint)
            async def handle_paint(event, direct_send):
                consume_command = self.force_direct_send(event)
                if consume_command:
                    direct_send = True
                async for result in original_paint(event, direct_send=direct_send):
                    yield result
                if consume_command:
                    # Each yield must finish the downstream send stage first.
                    # Stopping earlier would suppress images or truncate batches.
                    # This is the command boundary, never the LLM tool boundary.
                    event.stop_event()

            # Original command handlers still parse input and enforce access.
            # Route delivery and consume handled commands without rewriting input.
            self.replace(self.target, "_handle_paint_logic", handle_paint)
        self._original_wait = self.api.wait_for_result
        async def bridge(prompt_id, *args, **kwargs):
            return await self.wait(prompt_id, *args, **kwargs)
        # Split submit/wait calls can still finish correctly after our unload.
        self._wait_patch = self.replace(self.api, "wait_for_result", bridge)
        self.replace(self.target, "api", ChannelComfyAPI(self))
        return True

    def force_direct_send(self, event):
        return normalize_platform(event.get_platform_name()) in self._direct_send_platforms

    def on_using_llm_tool(self, event, tool, tool_args):
        # AstrBot keeps registered tool handlers independently of instance
        # methods. Its pre-call hook changes the actual invocation arguments.
        if (getattr(tool, "name", None) == "comfyui_txt2img"
                and isinstance(tool_args, dict) and self.force_direct_send(event)):
            tool_args["direct_send"] = True

    def _client_config(self, settings):
        config = copy.deepcopy(dict(self.target.config))
        workflow = config.setdefault("workflow_settings", {})
        for setting, attr, field in (
            ("workflow", "wf_filename", "json_file"),
            ("input_node_id", "input_id", "input_node_id"),
            ("neg_node_id", "neg_node_id", "neg_node_id"),
            ("output_node_id", "output_id", "output_node_id"),
        ):
            value = settings[setting] if setting in settings else getattr(self.api, attr)
            if value is None:
                raise ValueError(f"{setting} cannot be null")
            workflow[field] = str(value)
        return config

    def runtime(self):
        route = current_route()
        if route is None:
            return self.api
        if self._runtime_key in route.runtimes:
            return route.runtimes[self._runtime_key]
        if not self.active:
            return self.api
        config = self._client_config(self.settings(route, "comfyui"))
        fingerprint = json.dumps(config, sort_keys=True, ensure_ascii=False)
        cached = self._clients.get(route.profile_name)
        if cached is None or cached[0] != fingerprint:
            api = type(self.api)(config, data_dir=self.api.data_dir)
            self._clients[route.profile_name] = (fingerprint, api)
        else:
            api = cached[1]
        route.runtimes[self._runtime_key] = api
        return api

    def reload_config(self, filename, input_id=None, output_id=None, neg_node_id=None):
        route = current_route()
        if route is None:
            return self.api.reload_config(filename, input_id=input_id, output_id=output_id, neg_node_id=neg_node_id)
        try:
            api = self.runtime()
            filename = api.resolve_workflow_filename(str(filename))
            values = {"workflow": filename}
            for field, attr, value in (("input_node_id", "input_id", input_id),
                                       ("neg_node_id", "neg_node_id", neg_node_id),
                                       ("output_node_id", "output_id", output_id)):
                values[field] = str(getattr(api, attr) if value is None else value)
            self.profiles.update(route.profile_name, "comfyui", values)
            self.refresh_route()
        except (OSError, ValueError, TypeError) as exc:
            return False, f"渠道工作流切换失败：{exc}"
        logger.info("[%s] profile=%s workflow=%s", self.name, route.profile_name, filename)
        return True, (f"已为渠道 {route.profile_name} 切换至 {filename}\n"
                      f"Positive={values['input_node_id']}, Negative={values['neg_node_id']}, "
                      f"Output={values['output_node_id'] or '自动'}")

    async def submit(self, *args, **kwargs):
        api = self.runtime()
        self._submitting += 1
        try:
            result = await api.submit(*args, **kwargs)
            if result and result[0]:
                prompt_id = str(result[0])
                entry = Submission(api)
                previous = self._submissions.get(prompt_id)
                if previous and previous.timer:
                    previous.timer.cancel()
                self._submissions[prompt_id] = entry
                entry.timer = asyncio.get_running_loop().call_later(
                    self.snapshot_ttl_seconds, self._expire, prompt_id, entry)
            return result
        finally:
            self._submitting -= 1
            self._restore_wait_if_idle()

    def _expire(self, prompt_id, entry):
        if not entry.waiters and self._submissions.get(prompt_id) is entry:
            self._submissions.pop(prompt_id)
            logger.warning("[%s] expired unclaimed submission %s", self.name, prompt_id)
            self._restore_wait_if_idle()

    async def wait(self, prompt_id, *args, **kwargs):
        entry = self._submissions.get(str(prompt_id))
        if entry is None:
            return await self._original_wait(prompt_id, *args, **kwargs)
        entry.waiters += 1
        try:
            wait = self._original_wait if entry.api is self.api else entry.api.wait_for_result
            return await wait(prompt_id, *args, **kwargs)
        finally:
            entry.waiters -= 1
            if not entry.waiters:
                if self._submissions.get(str(prompt_id)) is entry:
                    self._submissions.pop(str(prompt_id))
                entry.timer.cancel()
                self._restore_wait_if_idle()

    def _restore_wait_if_idle(self):
        if not self.active and not self._submissions and not self._submitting:
            self.restore()

    async def terminate(self):
        self.active = False
        self.restore(exclude=(self._wait_patch,) if self._submissions or self._submitting else ())
        self._clients.clear()
