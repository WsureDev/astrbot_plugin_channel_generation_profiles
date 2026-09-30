import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from tests.support import ComfyTarget, CommandEvent as Event, channel, context, module, route, store


class ComfyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = store(self.root)
        self.target = ComfyTarget(self.root)
        self.original_paint = self.target._handle_paint_logic
        self.integration = module("integrations.comfyui").ComfyUIIntegration(
            context(self.target), {}, self.store)
        self.assertTrue(self.integration.initialize())

    async def asyncTearDown(self):
        channel.set_route(None)
        await self.integration.terminate()
        self.temp.cleanup()

    async def configure(self, platforms):
        await self.integration.terminate()
        self.integration = module("integrations.comfyui").ComfyUIIntegration(
            context(self.target), {"comfyui_direct_send_platforms": platforms}, self.store)
        self.assertTrue(self.integration.initialize())

    async def paint(self, event, direct_send=False):
        return [value async for value in self.target._handle_paint_logic(event, direct_send)]

    async def test_telegram_and_alias_use_direct_delivery(self):
        route(self.store, "qq")
        for platform in ("telegram", "telegram_bot"):
            with self.subTest(platform=platform):
                event = Event(platform)
                event.message_str = '/画图 <lora picks="example:0.8"> -c 2'
                before = event.message_str
                self.assertEqual(await self.paint(event), [True])
                self.assertEqual(event.message_str, before)

    async def test_other_platforms_preserve_requested_mode(self):
        route(self.store, "telegram")
        for platform in ("qq", "aiocqhttp", "discord"):
            with self.subTest(platform=platform):
                self.assertEqual(await self.paint(Event(platform)), [False])
                self.assertEqual(await self.paint(Event(platform), True), [True])

    async def test_telegram_command_stops_only_after_last_reply(self):
        event = Event("telegram")
        replies = self.target._handle_paint_logic(event, False)
        self.assertTrue(await anext(replies))
        self.assertFalse(event.is_stopped(), "A yielded reply must reach the sender")
        with self.assertRaises(StopAsyncIteration):
            await anext(replies)
        self.assertTrue(event.is_stopped(), "A consumed command must not reach later handlers")

    async def test_tool_call_does_not_stop_agent_round(self):
        event = Event("telegram")
        self.integration.on_using_llm_tool(
            event, types.SimpleNamespace(name="comfyui_txt2img"), {"prompt": "a cat"})
        self.assertFalse(event.is_stopped())

    async def test_concurrent_platforms_do_not_share_delivery_mode(self):
        tg, qq = await asyncio.gather(self.paint(Event("telegram")), self.paint(Event("qq")))
        self.assertEqual((tg, qq), ([True], [False]))

    async def test_tool_hook_changes_only_comfy_delivery_argument(self):
        tool = types.SimpleNamespace(name="comfyui_txt2img")
        for direct in (None, False, True):
            args = {"prompt": "a cat", "count": 3, "workflow": "telegram.json"}
            if direct is not None:
                args["direct_send"] = direct
            self.integration.on_using_llm_tool(Event("telegram"), tool, args)
            self.assertEqual(args, {"prompt": "a cat", "count": 3,
                                    "workflow": "telegram.json", "direct_send": True})

    async def test_tool_hook_leaves_other_tools_and_platforms_unchanged(self):
        for platform, name in (("qq", "comfyui_txt2img"),
                               ("telegram", "comfyui_workflows"),
                               ("telegram", "other_txt2img")):
            args = {"prompt": "a cat"}
            self.integration.on_using_llm_tool(
                Event(platform), types.SimpleNamespace(name=name), args)
            self.assertEqual(args, {"prompt": "a cat"})

    async def test_empty_platform_list_disables_command_and_tool_policy(self):
        await self.configure([])
        self.assertEqual(await self.paint(Event("telegram")), [False])
        args = {"direct_send": False}
        self.integration.on_using_llm_tool(
            Event("telegram"), types.SimpleNamespace(name="comfyui_txt2img"), args)
        self.assertFalse(args["direct_send"])

    async def test_platform_list_can_be_extended_and_normalizes_aliases(self):
        await self.configure([" TELEGRAM_BOT ", "discord"])
        self.assertEqual(await self.paint(Event("telegram")), [True])
        self.assertEqual(await self.paint(Event("discord")), [True])
        self.assertEqual(await self.paint(Event("qq")), [False])

    async def test_unload_restores_original_delivery_and_repeated_load_is_idempotent(self):
        wrapped = self.target._handle_paint_logic
        self.assertTrue(self.integration.initialize())
        self.assertIs(self.target._handle_paint_logic, wrapped)
        await self.integration.terminate()
        self.assertEqual(self.target._handle_paint_logic, self.original_paint)
        self.assertEqual(await self.paint(Event("telegram")), [False])
