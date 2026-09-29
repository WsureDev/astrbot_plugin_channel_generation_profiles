import asyncio
import copy
import tempfile
import types
import unittest
from pathlib import Path
from tests.support import Event, channel, context, module, route, store

class Manager:
    def __init__(self):
        self.saved = []
        self._all_provider_configs = []
        self._plugin_config = types.SimpleNamespace(adapter_config=self.selected("provider/global"))
        self.max_concurrent_tasks = 2
    def selected(self, name):
        return types.SimpleNamespace(name="provider", model=name.split("/", 1)[1],
                                     available_models=["provider/global", "provider/qq", "provider/tg"], api_keys=["fake"])
    @property
    def adapter_config(self): return self._plugin_config.adapter_config
    def _select_adapter_config(self, providers, model): return self.selected(model)
    def save_model_setting(self, model): self.saved.append(model)
    def reload(self): return self._plugin_config

class Generator:
    instances = []
    def __init__(self, config):
        self.adapter_config = config
        self.adapter = types.SimpleNamespace(model=config.model)
        self.closed = False
        self.instances.append(self)
    async def update_adapter(self, config):
        self.adapter_config = config
        self.adapter.model = config.model
    async def close(self): self.closed = True

class Executor:
    def __init__(self, *, config_manager, **kwargs):
        self.config_manager = config_manager
        self.generator = None
        self.request_semaphore = asyncio.Semaphore(2)
    def update_generator(self, generator): self.generator = generator
    def refresh_request_semaphore(self): self.request_semaphore = asyncio.Semaphore(2)
    async def generate_and_send_image_async(self, **kwargs):
        await asyncio.sleep(0)
        return self.generator.adapter.model, self.config_manager.adapter_config.model

class Queue:
    def __init__(self): self.jobs = {}; self.records = {}
    def create_generation_task(self, coro_factory, **kwargs):
        task_id = kwargs["task_id"]
        record = types.SimpleNamespace(**kwargs, status="queued")
        self.jobs[task_id] = coro_factory
        self.records[task_id] = record
        return record
    async def run(self, task_id):
        record = self.records[task_id]
        try:
            result = await self.jobs[task_id]()
            record.status = "succeeded"
            return result
        finally:
            callback = record.terminal_callback
            if callback: callback(record)
    def cancel(self, task_id):
        record = self.records[task_id]
        record.status = "cancelled"
        if record.terminal_callback: record.terminal_callback(record)

class Target:
    def __init__(self):
        self.context = object()
        self.config_manager = Manager()
        self.generator = Generator(self.config_manager.adapter_config)
        self.generation_executor = Executor(config_manager=self.config_manager)
        self.generation_executor.update_generator(self.generator)
        self.task_manager = Queue()
        self.image_processor = object()
        self.usage_manager = object()
        self.safety_auditor = object()
        self.llm_result_handler = types.SimpleNamespace(config_manager=self.config_manager)
        self.global_reloads = 0
    def reload_runtime_settings(self): self.global_reloads += 1
    def create_generation_task(self, *, task_id, source_event=None, **kwargs):
        def factory():
            return self.generation_executor.generate_and_send_image_async(task_id=task_id)
        config = self.config_manager.adapter_config
        return self.task_manager.create_generation_task(factory, task_id=task_id,
            model=config.name + "/" + config.model, terminal_callback=self.terminal)
    def terminal(self, record): pass
    async def change_model(self, model):
        self.config_manager.save_model_setting(model)
        self.config_manager.reload()
        self.reload_runtime_settings()
        await self.generator.update_adapter(self.config_manager.adapter_config)
        self.generation_executor.update_generator(self.generator)

class ImageIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store(Path(self.temp.name))
        self.store.update("qq", "image_generation", {"model": "provider/qq"})
        self.store.update("telegram", "image_generation", {"model": "provider/tg"})
        self.target = Target()
        self.original_generator = self.target.generator
        self.original_executor = self.target.generation_executor
        name = "astrbot_plugin_image_generation"
        self.integration = module("integrations.image_generation").ImageGenerationIntegration(context(self.target, name), {}, self.store)
        self.assertTrue(self.integration.initialize())
    async def asyncTearDown(self):
        channel.set_route(None)
        await self.integration.terminate()
        self.temp.cleanup()
    async def test_queued_jobs_capture_executor_and_model_metadata(self):
        route(self.store, "qq")
        self.target.create_generation_task(task_id="qq")
        route(self.store, "telegram")
        self.target.create_generation_task(task_id="tg")
        channel.set_route(None)
        self.assertEqual(await self.target.task_manager.run("qq"), ("qq", "qq"))
        self.assertEqual(await self.target.task_manager.run("tg"), ("tg", "tg"))
        self.assertEqual(self.target.task_manager.records["qq"].model, "provider/qq")
        self.assertEqual(self.target.task_manager.records["tg"].model, "provider/tg")
    async def test_original_model_command_updates_only_channel(self):
        route(self.store)
        self.target.create_generation_task(task_id="old")
        await self.target.change_model("provider/tg")
        self.target.create_generation_task(task_id="new")
        self.assertEqual(self.target.config_manager.saved, [])
        self.assertEqual(self.target.global_reloads, 0)
        self.assertEqual(await self.target.task_manager.run("old"), ("qq", "qq"))
        self.assertEqual(await self.target.task_manager.run("new"), ("tg", "tg"))
        self.assertEqual(self.original_generator.adapter.model, "global")
    async def test_existing_config_manager_references_are_routed(self):
        route(self.store)
        self.assertEqual(self.target.llm_result_handler.config_manager.adapter_config.model, "qq")
    async def test_invalid_model_does_not_fall_back_or_persist(self):
        route(self.store)
        with self.assertRaises(ValueError):
            await self.target.change_model("missing/provider")
        self.assertEqual(self.store.get("qq")["image_generation"]["model"], "provider/qq")
    async def test_unload_does_not_change_already_queued_job(self):
        route(self.store)
        self.target.create_generation_task(task_id="old")
        await self.integration.terminate()
        channel.set_route(None)
        self.assertIs(self.target.generator, self.original_generator)
        self.assertIs(self.target.generation_executor, self.original_executor)
        self.assertEqual(await self.target.task_manager.run("old"), ("qq", "qq"))
    async def test_explicit_source_event_overrides_ambient_route(self):
        route(self.store, "telegram")
        self.target.create_generation_task(task_id="qq", source_event=Event("qq"))
        channel.set_route(None)
        self.assertEqual(await self.target.task_manager.run("qq"), ("qq", "qq"))
    async def test_global_request_limit_is_shared(self):
        route(self.store)
        first = self.integration.runtime().value.executor.request_semaphore
        route(self.store, "telegram")
        second = self.integration.runtime().value.executor.request_semaphore
        self.assertIs(first, self.original_executor.request_semaphore)
        self.assertIs(second, first)
    async def test_cancelled_queued_task_releases_retired_adapter(self):
        async def submit():
            route(self.store)
            self.target.create_generation_task(task_id="cancel")
            return self.integration.runtime().value.generator
        generator = await asyncio.create_task(submit())
        await asyncio.sleep(0)
        await self.integration.terminate()
        self.assertFalse(generator.closed)
        self.target.task_manager.cancel("cancel")
        await asyncio.sleep(0)
        self.assertTrue(generator.closed)
    async def test_finished_job_releases_retired_adapter(self):
        async def submit():
            route(self.store)
            self.target.create_generation_task(task_id="finished")
            return self.integration.runtime().value.generator
        generator = await asyncio.create_task(submit())
        await asyncio.sleep(0)
        await self.integration.terminate()
        self.assertFalse(generator.closed)
        channel.set_route(None)
        self.assertEqual(await self.target.task_manager.run("finished"), ("qq", "qq"))
        await asyncio.sleep(0)
        self.assertTrue(generator.closed)
