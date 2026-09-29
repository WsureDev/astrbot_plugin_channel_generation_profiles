import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.support import ComfyTarget, Event, channel, module, ProfileStore

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = {}
        context = types.SimpleNamespace(get_registered_star=self.registry.get)
        self.main = module("main")
        self.get_dir = patch.object(self.main.StarTools, "get_data_dir", return_value=self.root / "state")
        self.get_dir.start()
        self.plugin = self.main.ChannelGenerationProfiles(context, {"features": ["comfyui"], "profiles": {"qq": {"platforms": ["qq"]}}})
        await self.plugin.initialize()
    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.get_dir.stop()
        self.temp.cleanup()
    async def load_target(self, directory):
        directory.mkdir()
        target = ComfyTarget(directory)
        metadata = types.SimpleNamespace(name="astrbot_plugin_comfyui_pro", activated=True, star_cls=target)
        self.registry[metadata.name] = metadata
        await self.plugin.on_plugin_loaded(metadata)
        return target, metadata
    async def test_late_load_and_idempotent_install(self):
        self.assertFalse(self.plugin.integrations["comfyui"].active)
        target, metadata = await self.load_target(self.root / "target")
        facade = target.api
        self.assertTrue(self.plugin.integrations["comfyui"].active)
        await self.plugin.on_astrbot_loaded()
        await self.plugin.on_plugin_loaded(metadata)
        self.assertIs(target.api, facade)
    async def test_target_unload_then_reload_uses_new_generation(self):
        first, metadata = await self.load_target(self.root / "first")
        integration = self.plugin.integrations["comfyui"]
        original = integration.api
        await self.plugin.on_plugin_unloaded(metadata)
        self.assertIs(first.api, original)
        second, _ = await self.load_target(self.root / "second")
        self.assertIsNot(self.plugin.integrations["comfyui"], integration)
        self.assertIs(self.plugin.integrations["comfyui"].target, second)
    async def test_disabled_metadata_does_not_install(self):
        self.registry["astrbot_plugin_comfyui_pro"] = types.SimpleNamespace(activated=False, star_cls=object())
        await self.plugin.on_astrbot_loaded()
        self.assertFalse(self.plugin.integrations["comfyui"].active)
    async def test_routing_does_not_stop_commands_or_inject_prompt(self):
        await self.load_target(self.root / "target")
        event = Event(admin=False)
        event.message_str = "comfy_use 0"
        await self.plugin.route_event(event)
        before = channel.current_route()
        request = types.SimpleNamespace(add_user_text=lambda *a, **k: self.fail("must not add competing prompt"))
        await self.plugin.route_llm_request(event, request)
        self.assertIs(channel.current_route(), before)
        self.assertEqual(before.channel.bot_id, "qq-instance")
    async def test_empty_feature_list_disables_all(self):
        plugin = self.main.ChannelGenerationProfiles(self.plugin.context, {"features": []})
        await plugin.initialize()
        self.assertEqual(plugin.integrations, {})
        await plugin.terminate()
    async def test_tool_hook_routes_by_call_event_and_respects_disabled_integration(self):
        _, metadata = await self.load_target(self.root / "target")
        tool = types.SimpleNamespace(name="comfyui_txt2img")
        args = {"prompt": "fixture", "direct_send": False}
        await self.plugin.route_llm_tool(Event("telegram"), tool, args)
        self.assertTrue(args["direct_send"])
        self.assertEqual(channel.current_route().channel.platform, "telegram")
        await self.plugin.on_plugin_unloaded(metadata)
        args["direct_send"] = False
        await self.plugin.route_llm_tool(Event("telegram"), tool, args)
        self.assertFalse(args["direct_send"])

class PatchOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_install_restores_partial_changes(self):
        Base = module("core.integration").Integration
        target = types.SimpleNamespace(value="original")
        class Broken(Base):
            target_plugin = "test"
            def install(self):
                self.replace(self.target, "value", "ours")
                return False
        metadata = types.SimpleNamespace(activated=True, star_cls=target)
        item = Broken(types.SimpleNamespace(get_registered_star=lambda _: metadata), {}, None)
        self.assertFalse(item.initialize())
        self.assertEqual(target.value, "original")
    async def test_restore_does_not_overwrite_later_owner(self):
        item = module("core.integration").Integration(None, {}, None)
        target = types.SimpleNamespace(value=object())
        item.replace(target, "value", object())
        later = object()
        target.value = later
        await item.terminate()
        self.assertIs(target.value, later)

class PersistenceTests(unittest.TestCase):
    def test_failed_write_does_not_change_memory_or_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore({}, Path(directory))
            store.update("qq", "comfyui", {"workflow": "old.json"})
            before = store._path.read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.update("qq", "comfyui", {"workflow": "new.json"})
            self.assertEqual(store.get("qq")["comfyui"]["workflow"], "old.json")
            self.assertEqual(store._path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
    def test_invalid_profile_config_does_not_silently_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ProfileStore({"profiles": "{broken"}, Path(directory))
    def test_duplicate_alias_bindings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ProfileStore({"profiles": {"one": {"platforms": ["qq"]}, "two": {"platforms": ["aiocqhttp"]}}}, Path(directory))
