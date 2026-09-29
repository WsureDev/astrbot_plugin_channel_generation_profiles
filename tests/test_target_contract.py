"""Optional exact-source checks; no target module imports or real HTTP calls.

Set PROFILE_TARGET_SOURCE_ROOT to a read-only directory containing both plugins.
"""
from __future__ import annotations
import ast
import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from tests.support import ComfyTarget, Event, channel, context, module, route, store
from tests.test_image_generation import Target

SOURCE = os.environ.get("PROFILE_TARGET_SOURCE_ROOT")

def extract(path, name, namespace=None, decorators=False):
    namespace = dict(namespace or {})
    tree = ast.parse(path.read_text())
    node = next(item for item in ast.walk(tree)
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    if not decorators and not isinstance(node, ast.ClassDef):
        node.decorator_list = []
    for item in tree.body:
        if isinstance(item, ast.Assign):
            try:
                value = ast.literal_eval(item.value)
            except (ValueError, TypeError):
                continue
            for target in item.targets:
                if isinstance(target, ast.Name): namespace[target.id] = value
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    script = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(script, str(path), "exec"), namespace)
    return namespace[name]

class Response:
    status = 200
    def __init__(self, data=None, binary=None): self.data, self.binary = data, binary
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.data
    async def read(self): return self.binary

class OfflineHTTP:
    def __init__(self): self.requests = []
    def ClientSession(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    def post(self, url, *, json):
        self.requests.append(json["prompt"])
        return Response({"prompt_id": str(len(self.requests))})
    def get(self, url):
        if "/history/" in url:
            prompt_id = url.rsplit("/", 1)[1]
            outputs = {name: {"images": [{"filename": name + ".png", "subfolder": "", "type": "output"}]}
                       for name in ("qq-out", "tg-out", "9")}
            return Response({prompt_id: {"outputs": outputs}})
        if "/view?" in url:
            return Response(binary=parse_qs(urlsplit(url).query)["filename"][0].encode())
        raise AssertionError("Unexpected HTTP request: " + url)

@unittest.skipUnless(SOURCE, "set PROFILE_TARGET_SOURCE_ROOT for installed-source contracts")
class InstalledSourceContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root_temp = tempfile.TemporaryDirectory()
        self.root = Path(self.root_temp.name)
        self.source = Path(SOURCE)
        self.store = store(self.root)
        self.integrations = []
    async def asyncTearDown(self):
        channel.set_route(None)
        for item in self.integrations: await item.terminate()
        self.root_temp.cleanup()
    def comfy(self):
        path = self.source / "astrbot_plugin_comfyui_pro/comfyui_api.py"
        http = OfflineHTTP()
        async def fast_sleep(seconds): await asyncio.sleep(0)
        ns = dict(Path=Path, json=json, os=os, re=re, random=random, time=time,
                  logger=logging.getLogger("upstream-test"), aiohttp=http,
                  asyncio=types.SimpleNamespace(sleep=fast_sleep), inspect_workflow_models=lambda data: {})
        ns["_normalize_server_address"] = extract(path, "_normalize_server_address", ns)
        api_class = extract(path, "ComfyUI", ns)
        target = ComfyTarget(self.root)
        for file in target.workflow_dir.glob("*.json"):
            file.write_text(json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                                        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                                        "9": {"class_type": "SaveImage", "inputs": {}}}))
        target.api = api_class(target.config, data_dir=self.root)
        original = target.api
        item = module("integrations.comfyui").ComfyUIIntegration(context(target), {}, self.store)
        self.assertTrue(item.initialize())
        self.integrations.append(item)
        return target, original, http
    async def test_actual_api_submit_and_wait_use_channel_nodes(self):
        target, original, http = self.comfy()
        async def request(platform):
            route(self.store, platform)
            return await target.api.generate(platform + " prompt", None, "negative")
        qq, tg = await asyncio.gather(request("qq"), request("telegram"))
        self.assertEqual(qq, (b"qq-out.png", None))
        self.assertEqual(tg, (b"tg-out.png", None))
        self.assertEqual({p["6"]["inputs"]["text"] for p in http.requests}, {"qq prompt", "telegram prompt"})
        self.assertTrue(all(p["7"]["inputs"]["text"] == "negative" for p in http.requests))
        self.assertEqual(original.wf_filename, "global.json")
    async def test_actual_usage_and_catalog_report_same_default(self):
        target, _, _ = self.comfy()
        source = self.source / "astrbot_plugin_comfyui_pro/main.py"
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "comfyui_txt2img_tool.md").write_text("fixture documentation")
        target._check_access = lambda event: (True, "")
        usage = extract(source, "comfyui_usage", {"PLUGIN_DIR": self.root, "json": json})
        catalog = extract(source, "_get_workflow_catalog")
        route(self.store)
        result = await usage(target, Event())
        self.assertIn('"default_workflow": "qq.json"', result)
        self.assertEqual(len(catalog(target)["workflows"]), 3)
        self.assertEqual(catalog(target, "telegram.json")["default_workflow"], "qq.json")
    async def test_actual_comfy_command_preserves_admin_and_index_checks(self):
        target, original, _ = self.comfy()
        command = extract(self.source / "astrbot_plugin_comfyui_pro/main.py", "cmd_comfy_use",
                          {"logger": logging.getLogger("upstream-test")})
        route(self.store)
        event = Event(admin=False)
        event.plain_result = lambda value: value
        event.message_str = "comfy_use 1"
        result = [item async for item in command(target, event)]
        self.assertIn("权限不足", result[0])
        event.admin = True
        event.message_str = "comfy_use 0"
        result = [item async for item in command(target, event)]
        self.assertIn("序号错误", result[0])
        event.message_str = "comfy_use 3"
        result = [item async for item in command(target, event)]
        self.assertIn("telegram.json", result[0])
        self.assertEqual(target.api.wf_filename, "telegram.json")
        self.assertEqual(original.wf_filename, "global.json")
    async def test_actual_image_factory_and_model_command_keep_queued_model(self):
        path = self.source / "astrbot_plugin_image_generation/main.py"
        create = extract(path, "create_generation_task", {"GenerationTaskCreationError": RuntimeError})
        command = extract(path, "model_command")
        class ActualTarget(Target):
            create_generation_task = create
            normalize_image_count = staticmethod(lambda count: count)
            _handle_generation_task_terminal = Target.terminal
        target = ActualTarget()
        self.store.update("qq", "image_generation", {"model": "provider/qq"})
        item = module("integrations.image_generation").ImageGenerationIntegration(
            context(target, "astrbot_plugin_image_generation"), {}, self.store)
        self.assertTrue(item.initialize())
        self.integrations.append(item)
        route(self.store)
        args = dict(source="command", prompt="fixture", images_data=None, unified_msg_origin="fixture",
                    aspect_ratio="1:1", resolution="1K", image_count=1, is_usage_limit_admin=False, preset="")
        target.create_generation_task(task_id="before", **args)
        event = Event()
        event.plain_result = lambda value: value
        response = [value async for value in command(target, event, "3")]
        self.assertIn("provider/tg", response[0])
        target.create_generation_task(task_id="after", **args)
        await item.terminate()
        channel.set_route(None)
        self.assertEqual(await target.task_manager.run("before"), ("qq", "qq"))
        self.assertEqual(await target.task_manager.run("after"), ("tg", "tg"))
        self.assertEqual(target.config_manager.saved, [])
