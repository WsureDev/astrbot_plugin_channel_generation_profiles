from __future__ import annotations

import contextvars
import copy
from contextlib import contextmanager
from dataclasses import dataclass
from astrbot.api import logger
from ..core.channel import bind_route, current_route
from ..core.integration import Integration
from ..core.resources import ResourcePool

@dataclass
class ImageRuntime:
    config: object
    generator: object
    executor: object
    model: str

class GeneratorView:
    def __init__(self, owner): self.owner = owner
    def __bool__(self): return self.owner.generator() is not None
    def __getattr__(self, name): return getattr(self.owner.generator(), name)
    async def update_adapter(self, config):
        resource = self.owner.runtime()
        if resource is None:
            return await self.owner._generator.update_adapter(config)
        if config != resource.value.config.adapter_config:
            raise ValueError("Change channel model through save_model_setting first")
        # The newly selected runtime already owns its adapter. Never close an
        # adapter still referenced by an older queued task.
    async def close(self):
        await self.owner._generator.close()

class ExecutorView:
    def __init__(self, owner): self.owner = owner
    def __getattr__(self, name):
        resource = self.owner.runtime()
        executor = resource.value.executor if resource else self.owner._executor
        return getattr(executor, name)
    def update_generator(self, generator):
        resource = self.owner.runtime()
        if resource is None:
            value = self.owner._generator if isinstance(generator, GeneratorView) else generator
            return self.owner._executor.update_generator(value)
        if not isinstance(generator, GeneratorView) and generator is not resource.value.generator:
            raise ValueError("Cannot mutate an immutable channel executor")
    def refresh_request_semaphore(self):
        if self.owner.runtime() is None:
            return self.owner._executor.refresh_request_semaphore()
        # Request limits remain shared with the original plugin.

class ImageGenerationIntegration(Integration):
    name = "image_generation"
    target_plugin = "astrbot_plugin_image_generation"

    def __init__(self, context, config, profiles):
        super().__init__(context, config, profiles)
        self._current = contextvars.ContextVar("image_task_runtime", default=None)
        self._cache = {}
        self._pool = ResourcePool(self._close_runtime)

    async def _close_runtime(self, runtime):
        await runtime.generator.close()

    def install(self):
        target = self.target
        required = ("config_manager", "generator", "generation_executor", "task_manager",
                    "context", "image_processor", "usage_manager", "safety_auditor")
        if any(getattr(target, name, None) is None for name in required):
            return self._unsupported("target runtime is not fully initialized")
        self._manager = target.config_manager
        self._manager_type = type(self._manager)
        self._adapter_property = getattr(self._manager_type, "adapter_config", None)
        if not isinstance(self._adapter_property, property):
            return self._unsupported("ConfigManager.adapter_config is not a property")
        for name in ("_plugin_config", "_all_provider_configs"):
            if not hasattr(self._manager, name):
                return self._unsupported(f"ConfigManager.{name} is missing")
        self._generator, self._executor = target.generator, target.generation_executor
        self.require_call(self._generator.close)
        self.require_call(self._generator.update_adapter, self._base_adapter())
        self.require_call(self._executor.update_generator, self._generator)
        if not hasattr(self._executor, "request_semaphore"):
            return self._unsupported("executor request semaphore is missing")
        self._original_create = target.create_generation_task
        if getattr(self._original_create, "__self__", None) is not target:
            return self._unsupported("create_generation_task is not an instance method")
        self.require_call(self._original_create, task_id="id", source="test", prompt="",
                          images_data=None, unified_msg_origin="", aspect_ratio="1:1",
                          resolution="1K", image_count=1, is_usage_limit_admin=False,
                          source_event=None)
        self.require_call(self._manager.save_model_setting, "provider/model")
        self.require_call(self._manager.reload)
        self.require_call(self._manager._select_adapter_config, [], "provider/model")
        self.require_call(target.reload_runtime_settings)
        self.require_call(target.task_manager.create_generation_task, lambda: None,
                          task_id="id", source="test", unified_msg_origin="", prompt="",
                          reference_image_count=0, requested_count=1, aspect_ratio="1:1",
                          resolution="1K", model="", terminal_callback=None)
        self.require_call(type(self._generator), self._base_adapter())
        self.require_call(type(self._executor), context=target.context,
                          config_manager=self._manager, image_processor=target.image_processor,
                          task_manager=target.task_manager, usage_manager=target.usage_manager,
                          safety_auditor=target.safety_auditor)
        original_save = self._manager.save_model_setting
        original_reload = self._manager.reload
        original_runtime_reload = target.reload_runtime_settings
        original_enqueue = target.task_manager.create_generation_task
        owner = self

        def adapter_config(manager):
            resource = owner.runtime()
            return resource.value.config.adapter_config if resource else owner._base_adapter()

        # Instance-scoped subclass: existing collaborators keep their manager
        # reference, while the global class and all other instances stay intact.
        routed_class = type("Channel" + self._manager_type.__name__, (self._manager_type,),
                            {"__slots__": (), "adapter_config": property(adapter_config)})
        self.replace(self._manager, "__class__", routed_class)

        def save_model_setting(model):
            route = current_route()
            if route is None:
                return original_save(model)
            selected = self._select(str(model))
            resource = self._build(selected)
            try:
                self.profiles.update(route.profile_name, "image_generation", {"model": str(model)})
            except BaseException:
                self._pool.retire(resource)
                raise
            self.refresh_route()
            route = current_route()
            self._publish(route, self._key(str(model)), resource)
            logger.info("[%s] profile=%s model=%s", self.name, route.profile_name, model)

        def reload_config():
            resource = self.runtime()
            return resource.value.config._plugin_config if resource else original_reload()

        def reload_runtime_settings():
            if self.runtime() is None:
                return original_runtime_reload()

        def create_generation_task(*args, **kwargs):
            event = kwargs.get("source_event")
            route = current_route()
            if event is not None and (route is None or route.event is not event):
                route = self.route_for(event)
            with bind_route(route):
                resource = self.runtime()
                if resource is None:
                    return self._original_create(*args, **kwargs)
                runtime = resource.value
                # Bind the ORIGINAL method to a shallow receiver snapshot. Its
                # deferred factory and terminal callback now close over real
                # immutable runtime references, even if hooks are later removed.
                receiver = copy.copy(target)
                receiver.config_manager = runtime.config
                receiver.generator = runtime.generator
                receiver.generation_executor = runtime.executor
                with self._bound(resource):
                    return self._original_create.__func__(receiver, *args, **kwargs)

        def enqueue(factory, *args, **kwargs):
            resource = self._current.get()
            if resource is None:
                return original_enqueue(factory, *args, **kwargs)
            route = current_route()
            release = self._pool.hold(resource)
            started = False
            callback = kwargs.get("terminal_callback")
            async def run_bound():
                nonlocal started
                started = True
                try:
                    with bind_route(route), self._bound(resource):
                        return await factory()
                finally:
                    release()
            def terminal(record):
                try:
                    with bind_route(route), self._bound(resource):
                        if callback:
                            callback(record)
                finally:
                    status = getattr(record.status, "value", record.status)
                    if not started and status in {"succeeded", "failed", "cancelled"}:
                        release()
            kwargs["model"] = resource.value.model
            kwargs["terminal_callback"] = terminal
            try:
                return original_enqueue(run_bound, *args, **kwargs)
            except BaseException:
                release()
                raise

        self.replace(self._manager, "save_model_setting", save_model_setting)
        self.replace(self._manager, "reload", reload_config)
        self.replace(target, "reload_runtime_settings", reload_runtime_settings)
        self.replace(target, "generator", GeneratorView(self))
        self.replace(target, "generation_executor", ExecutorView(self))
        self.replace(target.task_manager, "create_generation_task", enqueue)
        self.replace(target, "create_generation_task", create_generation_task)
        return True

    def _base_adapter(self):
        return self._adapter_property.__get__(self._manager, self._manager_type)

    def _select(self, model):
        base = self._base_adapter()
        if not model:
            if base is None:
                raise ValueError("Image model is not configured")
            return copy.deepcopy(base)
        choices = getattr(base, "available_models", ()) or ()
        if model not in choices:
            # Upstream selector silently falls back to its first provider.
            raise ValueError(f"渠道模型未配置：{model}")
        selected = self._manager._select_adapter_config(self._manager._all_provider_configs, model)
        if selected is None or f"{selected.name}/{selected.model}" != model:
            raise ValueError(f"渠道模型解析不一致：{model}")
        return copy.deepcopy(selected)

    def _key(self, model):
        return id(self._manager._plugin_config), model

    def _build(self, selected):
        config = copy.copy(self._manager)
        config.__class__ = self._manager_type
        # Do not copy our instance-installed write hooks into the snapshot.
        config.__dict__.pop("save_model_setting", None)
        config.__dict__.pop("reload", None)
        config._plugin_config = copy.deepcopy(self._manager._plugin_config)
        config._plugin_config.adapter_config = selected
        generator = type(self._generator)(selected)
        runtime = ImageRuntime(config, generator, None, f"{selected.name}/{selected.model}")
        resource = self._pool.add(runtime)
        try:
            target = self.target
            executor = type(self._executor)(context=target.context, config_manager=config,
                image_processor=target.image_processor, task_manager=target.task_manager,
                usage_manager=target.usage_manager, safety_auditor=target.safety_auditor)
            executor.update_generator(generator)
            executor.request_semaphore = self._executor.request_semaphore
            runtime.executor = executor
        except BaseException:
            self._pool.retire(resource)
            raise
        return resource

    def _publish(self, route, key, resource):
        old = self._cache.get(route.profile_name)
        self._cache[route.profile_name] = (key, resource)
        route.runtimes[self._runtime_key] = resource
        self._pool.pin(resource)
        if old and old[1] is not resource:
            self._pool.retire(old[1])

    def runtime(self):
        bound = self._current.get()
        if bound is not None:
            return bound
        route = current_route()
        if route is None:
            return None
        if self._runtime_key in route.runtimes:
            resource = route.runtimes[self._runtime_key]
            self._pool.pin(resource)
            return resource
        if not self.active:
            return None
        model = str(self.settings(route, "image_generation").get("model") or "")
        key = self._key(model)
        cached = self._cache.get(route.profile_name)
        if cached is None or cached[0] != key:
            resource = self._build(self._select(model))
            self._publish(route, key, resource)
        else:
            resource = cached[1]
            route.runtimes[self._runtime_key] = resource
            self._pool.pin(resource)
        return resource

    def generator(self):
        resource = self.runtime()
        return resource.value.generator if resource else self._generator

    @contextmanager
    def _bound(self, resource):
        token = self._current.set(resource)
        try:
            yield
        finally:
            self._current.reset(token)

    async def terminate(self):
        await super().terminate()
        self._cache.clear()
        await self._pool.shutdown()
