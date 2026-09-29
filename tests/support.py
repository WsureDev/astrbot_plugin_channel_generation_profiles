from __future__ import annotations

import asyncio
import copy
import importlib
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("profile_plugin")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("profile_plugin", package)
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("profiles-test")
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)
api.AstrBotConfig = dict
event_api = types.ModuleType("astrbot.api.event")
event_api.AstrMessageEvent = object
def decorator(*args, **kwargs):
    return lambda function: function
event_api.filter = types.SimpleNamespace(
    EventMessageType=types.SimpleNamespace(ALL="all"),
    event_message_type=decorator, command=decorator, on_llm_request=decorator,
    on_astrbot_loaded=decorator, on_plugin_loaded=decorator, on_plugin_unloaded=decorator)
sys.modules.setdefault("astrbot.api.event", event_api)
star_api = types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self, context): self.context = context
star_api.Star = Star
star_api.Context = object
star_api.StarTools = types.SimpleNamespace(get_data_dir=lambda name: None)
sys.modules.setdefault("astrbot.api.star", star_api)

def module(name):
    return importlib.import_module("profile_plugin." + name)

channel = module("core.channel")
ProfileStore = module("core.profiles").ProfileStore

class Event:
    def __init__(self, platform="qq", admin=True):
        self.platform = platform
        self.admin = admin
        self.message_str = ""
        self.unified_msg_origin = platform + ":GroupMessage:test"
    def get_platform_name(self): return self.platform
    def get_platform_id(self): return self.platform + "-instance"
    def get_sender_id(self): return "admin" if self.admin else "member"
    def stop_event(self): raise AssertionError("Original command must handle the event")

def route(store, platform="qq"):
    event = Event(platform)
    name, values = store.for_channel(channel.from_event(event))
    try:
        selected = channel.Route(event, channel.from_event(event), name, profile=values)
    except TypeError:
        selected = channel.Route(event, channel.from_event(event), name)
    channel.set_route(selected)
    return selected

def context(target, name="astrbot_plugin_comfyui_pro"):
    metadata = types.SimpleNamespace(activated=True, star_cls=target, name=name)
    return types.SimpleNamespace(get_registered_star=lambda key: metadata if key == name else None)

class FakeComfy:
    def __init__(self, config, data_dir=None):
        self.config = copy.deepcopy(config)
        wf = config["workflow_settings"]
        self.wf_filename = wf["json_file"]
        self.input_id = wf.get("input_node_id", "6")
        self.neg_node_id = wf.get("neg_node_id", "7")
        self.output_id = wf.get("output_node_id", "9")
        self.data_dir = Path(data_dir)
        self.workflow_dir = self.data_dir / "workflow"
        self.workflow_path = self.workflow_dir / self.wf_filename
        self.url = "http://fake.invalid"
        self.lora_control_enabled = False
    def resolve_workflow_filename(self, workflow_filename=None):
        name = workflow_filename or self.wf_filename
        if Path(name).name != name or not (self.workflow_dir / name).is_file():
            raise FileNotFoundError(name)
        return name
    def get_workflow_info(self, workflow_filename=None):
        name = self.resolve_workflow_filename(workflow_filename)
        return {"workflow": name, "is_default": name == self.wf_filename}
    def reload_config(self, filename, input_id=None, output_id=None, neg_node_id=None):
        self.wf_filename = filename
        self.workflow_path = self.workflow_dir / filename
        for key, value in (("input_id", input_id), ("output_id", output_id), ("neg_node_id", neg_node_id)):
            if value is not None: setattr(self, key, value)
        return self.workflow_path.exists(), filename
    async def submit(self, prompt, lora_selections=None, negative_prompt=None, workflow_filename=None):
        await asyncio.sleep(0)
        name = self.resolve_workflow_filename(workflow_filename)
        return name + ":" + prompt, None
    async def wait_for_result(self, prompt_id, timeout_seconds=120):
        await asyncio.sleep(0)
        return (self.wf_filename, self.output_id, timeout_seconds), None
    async def generate(self, prompt, lora_selections=None, negative_prompt=None, workflow_filename=None):
        prompt_id, error = await self.submit(prompt, lora_selections, negative_prompt, workflow_filename)
        return await self.wait_for_result(prompt_id)

class ComfyTarget:
    def __init__(self, root):
        self.config = {"workflow_settings": {"json_file": "global.json", "input_node_id": "6", "neg_node_id": "7", "output_node_id": "9"}}
        self.api = FakeComfy(self.config, root)
        self.workflow_dir = self.api.workflow_dir
        self.admin_user_ids = ["admin"]
        self.workflow_dir.mkdir()
        for name in ("global.json", "qq.json", "telegram.json"):
            (self.workflow_dir / name).write_text("{}")
    def _list_workflow_files(self): return sorted(p.name for p in self.workflow_dir.glob("*.json"))
    def _is_workflow_aux_file(self, name): return False
    def _get_workflow_catalog(self, workflow=None):
        names = [self.api.resolve_workflow_filename(workflow)] if workflow else self._list_workflow_files()
        return {"default_workflow": self.api.wf_filename, "workflows": [self.api.get_workflow_info(n) for n in names]}

def store(root):
    return ProfileStore({"profiles": {"qq": {"platforms": ["qq"], "comfyui": {"workflow": "qq.json", "output_node_id": "qq-out"}}, "telegram": {"platforms": ["telegram"], "comfyui": {"workflow": "telegram.json", "output_node_id": "tg-out"}}}}, root / "profiles")
