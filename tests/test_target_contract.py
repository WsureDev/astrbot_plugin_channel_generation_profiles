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
import traceback
import types
import unittest
import uuid
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

class DeliveryChain:
    def __init__(self, chain=None): self.chain = list(chain or [])
    def message(self, text):
        self.chain.append(DeliveryPlain(text))
        return self

class DeliveryImage:
    def __init__(self, path): self.path = path
    @classmethod
    def fromFileSystem(cls, path): return cls(path)

class DeliveryPlain:
    def __init__(self, text): self.text = text

class DeliveryNode:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)

class DeliveryNodes:
    def __init__(self, nodes): self.nodes = nodes

class DeliveryEvent(Event):
    def __init__(self, platform, admin=True):
        super().__init__(platform, admin)
        self.extras, self.sent = {}, []
        self.message_str = '/画图 a cat -c 2 -wf example -n lowres'
    def get_extra(self, key): return self.extras.get(key)
    def set_extra(self, key, value): self.extras[key] = value
    def plain_result(self, text): return DeliveryChain().message(text)
    def chain_result(self, chain): return DeliveryChain(chain)
    async def send(self, result): self.sent.append(result)

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
    def comfy(self, target_class=ComfyTarget):
        path = self.source / "astrbot_plugin_comfyui_pro/comfyui_api.py"
        http = OfflineHTTP()
        async def fast_sleep(seconds): await asyncio.sleep(0)
        ns = dict(Path=Path, json=json, os=os, re=re, random=random, time=time,
                  logger=logging.getLogger("upstream-test"), aiohttp=http,
                  asyncio=types.SimpleNamespace(sleep=fast_sleep), inspect_workflow_models=lambda data: {})
        ns["_normalize_server_address"] = extract(path, "_normalize_server_address", ns)
        api_class = extract(path, "ComfyUI", ns)
        target = target_class(self.root)
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
    def delivery(self):
        source = self.source / "astrbot_plugin_comfyui_pro/main.py"
        namespace = dict(asyncio=asyncio, logger=logging.getLogger("upstream-test"),
                         traceback=traceback, uuid=uuid, re=re,
                         Image=DeliveryImage, Plain=DeliveryPlain, Node=DeliveryNode,
                         Nodes=DeliveryNodes, MessageChain=DeliveryChain)
        class DeliveryTarget(ComfyTarget):
            _MAX_REPEATED_IMAGES_PER_REQUEST = 16
            _BATCH_WAIT_TIMEOUT_SECONDS = 1
            _BATCH_WAIT_STATE_EXTRA = "comfy_batch_wait_state"
            count = 1
            _handle_paint_logic = extract(source, "_handle_paint_logic", namespace)
            comfyui_txt2img = extract(source, "comfyui_txt2img", namespace)
            cmd_paint = extract(source, "cmd_paint", namespace)
            cmd_paint_no = extract(source, "cmd_paint_no", namespace)
            _run_batch_wait_stage = extract(source, "_run_batch_wait_stage", namespace)
            def __init__(self, root):
                super().__init__(root)
                self.output_dir = root / "output"
                self.output_dir.mkdir()
            def _check_access(self, event): return event.admin, "denied"
            def _extract_command_prompt(self, event): return event.message_str
            def _parse_draw_command_payload(self, message):
                return "a cat", None, self.count, None
            def _extract_lora_control_tags(self, prompt): return prompt, []
            def _check_sensitive(self, prompt, event): return True, []
            def _check_cooldown(self, event): return True, 0
            def _remember_draw_failure(self, *args, **kwargs): pass
            def _clear_draw_failures(self, event): pass
            def _get_forward_user_id(self, event):
                if event.get_platform_name() != "qq":
                    raise AssertionError("Telegram must not build forwarding nodes")
                return 123
            async def _submit_repeated_batch(self, prompt, count, **kwargs):
                return [{"index": i, "status": "submitted", "prompt_id": str(i)}
                        for i in range(1, count + 1)]
            async def _collect_submitted_batch_result(self, item, **kwargs):
                await asyncio.sleep(0)
                return {**item, "status": "success", "path": self.output_dir / "fixture.png"}
        target, _, _ = self.comfy(DeliveryTarget)
        return target, self.integrations[-1]
    async def test_actual_paint_commands_use_direct_tg_images_and_preserve_qq_forwarding(self):
        target, _ = self.delivery()
        for platform in ("telegram", "telegram_bot", "qq"):
            for count in (1, 2):
                for command in ("cmd_paint", "cmd_paint_no"):
                    with self.subTest(platform=platform, count=count, command=command):
                        target.count = count
                        event = DeliveryEvent(platform)
                        before = event.message_str
                        results = [value async for value in getattr(target, command)(event)]
                        components = [part for result in results for part in result.chain]
                        if platform != "qq" or command == "cmd_paint_no":
                            self.assertEqual(len(components), count)
                            self.assertTrue(all(isinstance(part, DeliveryImage) for part in components))
                        else:
                            self.assertEqual(len(components), 1)
                            expected = DeliveryNode if count == 1 else DeliveryNodes
                            self.assertIsInstance(components[0], expected)
                        self.assertEqual(event.message_str, before)
    async def test_actual_tool_direct_images_and_batch_state_preserve_agent_completion(self):
        target, integration = self.delivery()
        tool = types.SimpleNamespace(name="comfyui_txt2img")
        for platform in ("telegram", "qq"):
            for count in (1, 2):
                with self.subTest(platform=platform, count=count):
                    event = DeliveryEvent(platform)
                    args = {"prompt": "a cat", "count": count, "direct_send": False}
                    integration.on_using_llm_tool(event, tool, args)
                    results = [value async for value in target.comfyui_txt2img(event, **args)]
                    self.assertTrue(results)
                    self.assertTrue(all(isinstance(value, str) for value in results))
                    if count > 1:
                        state = event.get_extra(target._BATCH_WAIT_STATE_EXTRA)
                        self.assertEqual(state["direct_send"], platform == "telegram")
                        await target._run_batch_wait_stage(event, state)
                    components = [part for result in event.sent for part in result.chain]
                    if platform == "telegram":
                        self.assertEqual(sum(isinstance(part, DeliveryImage) for part in components), count)
                        self.assertFalse(any(isinstance(part, (DeliveryNode, DeliveryNodes)) for part in components))
                    else:
                        self.assertEqual(len(components), 1)
                        self.assertIsInstance(components[0], DeliveryNode if count == 1 else DeliveryNodes)
    async def test_actual_paint_permission_check_still_runs_on_telegram(self):
        target, _ = self.delivery()
        event = DeliveryEvent("telegram", admin=False)
        results = [value async for value in target.cmd_paint(event)]
        self.assertEqual(results[0].chain[0].text, "denied")
        self.assertEqual(list(target.output_dir.iterdir()), [])
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
