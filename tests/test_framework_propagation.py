"""Optional scheduler contracts using a local, read-only AstrBot checkout."""
import inspect
import logging
import os
import tempfile
import types
import typing
import unittest
from collections.abc import AsyncGenerator
from pathlib import Path

from tests.support import ComfyTarget, Event, channel, context, module, store
from tests.test_target_contract import extract

SOURCE = os.environ.get("ASTRBOT_SOURCE_ROOT")


class Result:
    def __init__(self, value=None):
        self.chain = [] if value is None else [value]
        self.stopped = False
    def message(self, value):
        self.chain.append(value)
        return self
    def stop_event(self):
        self.stopped = True
        return self
    def is_stopped(self): return self.stopped


class ProviderRequest:
    pass


@unittest.skipUnless(SOURCE, "set ASTRBOT_SOURCE_ROOT for actual scheduler contracts")
class CommandPropagationContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = Path(SOURCE) / "astrbot/core"
        self.profiles = store(self.root)
        class Target(ComfyTarget):
            count = 2
            async def _handle_paint_logic(self, event, direct_send):
                if not event.admin:
                    yield Result("denied")
                    return
                for index in range(self.count):
                    yield Result(("image", index, direct_send))
        self.target = Target(self.root)
        self.integration = module("integrations.comfyui").ComfyUIIntegration(
            context(self.target), {}, self.profiles)
        self.assertTrue(self.integration.initialize())

    async def asyncTearDown(self):
        channel.set_route(None)
        await self.integration.terminate()
        self.temp.cleanup()

    async def run_pipeline(self, *, send_succeeds=True, later_plugin=False, admin=True):
        namespace = {"MessageEventResult": Result, "CommandResult": Result,
                     "ProviderRequest": ProviderRequest, "inspect": inspect,
                     "logger": logging.getLogger("framework-contract")}
        class PipelineEvent(Event):
            def __init__(self):
                super().__init__("telegram", admin=admin)
                self._result = None
                self._force_stopped = False
                self._has_send_oper = False
                self.call_llm = False
                self.is_at_or_wake_command = True
                self.extras = {}
                self.attempts, self.sent = [], []
                self.llm_calls, self.later_calls = 0, 0
            def get_extra(self, name): return self.extras.get(name)
            def set_extra(self, name, value): self.extras[name] = value
        # Use the real stop/result semantics, including _force_stopped surviving
        # clear_result(). No AstrBot imports or platform requests are needed.
        for name in ("set_result", "get_result", "clear_result", "stop_event", "is_stopped"):
            setattr(PipelineEvent, name, extract(
                self.source / "platform/astr_message_event.py", name, namespace))
        event = PipelineEvent()
        async def command(event):
            async for result in self.target._handle_paint_logic(event, False):
                yield result
        async def later(event):
            event.later_calls += 1
            yield ProviderRequest()
        handlers = [types.SimpleNamespace(handler=command, handler_full_name="paint",
                    handler_module_path="comfy", handler_name="cmd_paint")]
        if later_plugin:
            handlers.append(types.SimpleNamespace(handler=later, handler_full_name="later",
                            handler_module_path="later", handler_name="later"))
        event.set_extra("activated_handlers", handlers)
        event.set_extra("handlers_parsed_params", {})
        call_handler = extract(self.source / "pipeline/context_utils.py", "call_handler", namespace)
        star_process = extract(self.source / "pipeline/process_stage/method/star_request.py",
                               "process", {**namespace, "call_handler": call_handler,
                               "star_map": {name: types.SimpleNamespace(name=name)
                                            for name in ("comfy", "later")}})
        star_stage = type("StarStage", (), {"process": star_process})()
        class AgentStage:
            async def process(self, event):
                event.llm_calls += 1
                yield
        process = extract(self.source / "pipeline/process_stage/stage.py", "process", namespace)
        stage = type("ProcessStage", (), {"process": process})()
        stage.ctx = types.SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
        stage.star_request_sub_stage, stage.agent_sub_stage = star_stage, AgentStage()
        class ReplyStage:
            async def process(self, event):
                result = event.get_result()
                if result and result.chain:
                    # The actual scheduler must permit every yielded image to
                    # reach this point before the command stops propagation.
                    if event.is_stopped():
                        raise AssertionError("Stopped before delivering all command replies")
                    event.attempts.extend(result.chain)
                    if send_succeeds:
                        event.sent.extend(result.chain)
                        event._has_send_oper = True
                    event.clear_result()
        scheduler_method = extract(self.source / "pipeline/scheduler.py", "_process_stages",
                                   {**namespace, "AsyncGenerator": AsyncGenerator, "cast": typing.cast})
        scheduler = type("Scheduler", (), {"_process_stages": scheduler_method})()
        scheduler.stages = [stage, ReplyStage()]
        await scheduler._process_stages(event)
        return event

    async def test_unconsumed_successful_send_already_skips_default_llm(self):
        await self.integration.terminate()
        event = await self.run_pipeline()
        self.assertEqual(event.llm_calls, 0)
        self.assertFalse(event.is_stopped())

    async def test_unconsumed_command_still_reaches_later_plugin_llm(self):
        await self.integration.terminate()
        event = await self.run_pipeline(later_plugin=True)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual((event.later_calls, event.llm_calls), (1, 1))

    async def test_unconsumed_failed_send_falls_back_to_default_llm(self):
        await self.integration.terminate()
        event = await self.run_pipeline(send_succeeds=False)
        self.assertEqual(event.llm_calls, 1)

    async def test_completed_command_delivers_all_images_and_blocks_later_plugin(self):
        event = await self.run_pipeline(later_plugin=True)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual((event.later_calls, event.llm_calls), (0, 0))
        self.assertTrue(event.is_stopped())

    async def test_failed_send_does_not_reinterpret_command_as_llm_prompt(self):
        event = await self.run_pipeline(send_succeeds=False)
        self.assertEqual(len(event.attempts), 2)
        self.assertEqual(event.llm_calls, 0)
        self.assertTrue(event.is_stopped())

    async def test_denied_command_replies_then_stops(self):
        event = await self.run_pipeline(later_plugin=True, admin=False)
        self.assertEqual(event.sent, ["denied"])
        self.assertEqual((event.later_calls, event.llm_calls), (0, 0))
        self.assertTrue(event.is_stopped())

    async def test_command_with_no_results_still_stops(self):
        self.target.count = 0
        event = await self.run_pipeline(later_plugin=True)
        self.assertEqual((event.later_calls, event.llm_calls), (0, 0))
        self.assertTrue(event.is_stopped())
