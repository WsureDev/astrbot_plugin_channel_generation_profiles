import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.support import ComfyTarget, Event, channel, context, module, route, store

class ComfyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = store(self.root)
        self.target = ComfyTarget(self.root)
        self.original = self.target.api
        self.integration = module("integrations.comfyui").ComfyUIIntegration(context(self.target), {}, self.store)
        self.assertTrue(self.integration.initialize())
    async def asyncTearDown(self):
        channel.set_route(None)
        await self.integration.terminate()
        self.temp.cleanup()
    async def test_direct_reads_and_bound_methods_agree(self):
        route(self.store)
        self.assertEqual(self.target.api.wf_filename, "qq.json")
        self.assertEqual(self.target.api.workflow_path.name, "qq.json")
        self.assertTrue(self.target.api.get_workflow_info()["is_default"])
        self.assertEqual(self.original.wf_filename, "global.json")
    async def test_catalog_keeps_all_entries_and_actual_default(self):
        route(self.store)
        catalog = self.target._get_workflow_catalog()
        self.assertEqual(len(catalog["workflows"]), 3)
        self.assertEqual(catalog["default_workflow"], "qq.json")
        selected = self.target._get_workflow_catalog("telegram.json")
        self.assertEqual(selected["default_workflow"], "qq.json")
        self.assertFalse(selected["workflows"][0]["is_default"])
    async def test_parallel_requests_do_not_change_original(self):
        async def request(platform):
            route(self.store, platform)
            result, error = await self.target.api.generate("test")
            self.assertIsNone(error)
            return result
        qq, tg = await asyncio.gather(request("qq"), request("telegram"))
        self.assertEqual(qq[:2], ("qq.json", "qq-out"))
        self.assertEqual(tg[:2], ("telegram.json", "tg-out"))
        self.assertEqual(self.original.wf_filename, "global.json")
    async def test_submission_keeps_snapshot_after_channel_switch(self):
        route(self.store)
        prompt_id, error = await self.target.api.submit("snapshot")
        self.assertIsNone(error)
        ok, _ = self.target.api.reload_config("telegram.json", output_id="new-out")
        self.assertTrue(ok)
        result, _ = await self.target.api.wait_for_result(prompt_id, 42)
        self.assertEqual(result, ("qq.json", "qq-out", 42))
    async def test_reload_validates_and_preserves_empty_node_values(self):
        route(self.store)
        ok, _ = self.target.api.reload_config("missing.json")
        self.assertFalse(ok)
        self.assertEqual(self.store.get("qq")["comfyui"]["workflow"], "qq.json")
        ok, _ = self.target.api.reload_config("qq.json", output_id="", neg_node_id="")
        self.assertTrue(ok)
        self.assertEqual(self.target.api.output_id, "")
        self.assertEqual(self.target.api.neg_node_id, "")
    async def test_original_commands_are_not_intercepted(self):
        event = Event(admin=False)
        self.assertFalse(self.integration.on_event(event, "comfy_use", "comfy_use 0"))
    async def test_no_route_delegates_and_unload_restores_original(self):
        channel.set_route(None)
        self.assertEqual(self.target.api.wf_filename, "global.json")
        await self.integration.terminate()
        self.assertIs(self.target.api, self.original)
    async def test_split_wait_survives_unload_and_restores_bridge(self):
        route(self.store)
        original_wait_function = type(self.original).wait_for_result
        prompt_id, _ = await self.target.api.submit("pending")
        await self.integration.terminate()
        channel.set_route(None)
        result, _ = await self.target.api.wait_for_result(prompt_id)
        self.assertEqual(result[:2], ("qq.json", "qq-out"))
        self.assertIs(self.original.wait_for_result.__func__, original_wait_function)
    async def test_wait_cancellation_releases_submission(self):
        route(self.store)
        prompt_id, _ = await self.target.api.submit("cancel")
        gate = asyncio.Event()
        async def blocked(*args, **kwargs):
            await gate.wait()
        self.integration._submissions[prompt_id].api.wait_for_result = blocked
        task = asyncio.create_task(self.target.api.wait_for_result(prompt_id))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(prompt_id, self.integration._submissions)
    async def test_expired_unclaimed_submission_is_bounded(self):
        route(self.store)
        prompt_id, _ = await self.target.api.submit("abandoned")
        entry = self.integration._submissions[prompt_id]
        entry.timer.cancel()
        await self.integration.terminate()
        self.integration._expire(prompt_id, entry)
        self.assertEqual(self.integration._submissions, {})
        self.assertEqual(self.integration._originals, [])
    async def test_unload_during_submission_keeps_split_wait_route(self):
        route(self.store)
        api = self.integration.runtime()
        started, proceed = asyncio.Event(), asyncio.Event()
        original = api.submit
        async def delayed(*args, **kwargs):
            started.set()
            await proceed.wait()
            return await original(*args, **kwargs)
        api.submit = delayed
        task = asyncio.create_task(self.target.api.submit("in-flight"))
        await started.wait()
        await self.integration.terminate()
        proceed.set()
        prompt_id, _ = await task
        channel.set_route(None)
        result, _ = await self.target.api.wait_for_result(prompt_id)
        self.assertEqual(result[:2], ("qq.json", "qq-out"))
        self.assertEqual(self.integration._originals, [])
    async def test_can_recover_from_deleted_default_workflow(self):
        route(self.store)
        (self.target.workflow_dir / "qq.json").unlink()
        ok, _ = self.target.api.reload_config("telegram.json")
        self.assertTrue(ok)
        self.assertEqual(self.target.api.wf_filename, "telegram.json")
