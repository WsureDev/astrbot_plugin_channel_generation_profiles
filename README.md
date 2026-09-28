# astrbot_plugin_channel_generation_profiles

按渠道隔离 AstrBot 的画图和生图配置。

## 当前状态

这是一个独立的 hook/proxy 插件，不修改 `astrbot_plugin_comfyui_pro` 或 `astrbot_plugin_image_generation` 的源码、配置文件或容器。ComfyUI 工作流代理和生图模型代理都按渠道保存配置；后台生图任务在入队时绑定 profile 对应的 executor。

ComfyUI 通过提交和结果回收代理使用 profile 的工作流及节点 ID，并用锁保护原 API 对象的短暂注入。生图插件通过复制配置管理器、创建 profile 专属 `ImageGenerator` 与 `GenerationExecutor`，让任务闭包保留正确的模型，不在运行中切换共享生成器。

## 配置示例

配置 `profiles` 后，使用：

```text
/渠道工作流 moody_krea_v7_fast_API.json 6 7 9
/渠道模型 provider/model-name
/渠道生图配置
```

示例：

```json
{
  "profiles": {
    "qq": {
      "platforms": ["aiocqhttp", "qq_official", "qq_official_webhook"],
      "comfyui": {"workflow": "healthy.json", "input_node_id": "6", "neg_node_id": "7", "output_node_id": "9"},
      "image_generation": {"model": "safe-provider/safe-model"}
    },
    "telegram": {
      "platforms": ["telegram"],
      "comfyui": {"workflow": "unrestricted.json", "input_node_id": "10", "neg_node_id": "11", "output_node_id": "12"},
      "image_generation": {"model": "unrestricted-provider/unrestricted-model"}
    },
    "default": {"platforms": [], "comfyui": {}, "image_generation": {}}
  }
}
```

`/渠道工作流` 和 `/渠道模型` 写入插件自己的 `plugin_data/astrbot_plugin_channel_generation_profiles/profiles.json`，不会调用原插件的 `/comfy_use` 或 `/生图模型` 全局写配置命令。

## 已知边界

原 ComfyUI 自动画图路径最终也经过 API `submit`，因此会使用渠道工作流。显式传入的 `workflow` 参数仍会被原工具传给代理；当前 profile 默认值用于未指定 workflow 的调用。

生图 profile 的模型必须已经出现在原生生图插件的 `api_providers` 配置中，且对应凭据已经配置。插件不会读取或复制密钥到自己的配置。任务历史、配额和图片处理继续复用原插件对象，因此这些资源是共享的；模型适配器、模型选择和 executor 是按 profile 独立的。

这是针对当前 AstrBot 和两个目标插件内部接口的适配层。启动时如果目标对象或关键接口缺失，会记录兼容性错误；升级 AstrBot 或目标插件后应重新执行并发双渠道验证。

## 安装与验证

将本仓库作为插件安装后，确认目标插件已经加载，再执行 `/渠道生图配置`。先分别在 QQ 和 Telegram 执行 `/渠道工作流`、`/生图模型`，随后检查显示值并并发提交两张图。插件不会操作 AstrBot 容器。
