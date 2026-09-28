# astrbot_plugin_channel_generation_profiles

按渠道隔离 AstrBot 生图能力的 integration 插件。

## 结构

```text
core/
  channel.py       渠道识别与当前请求路由上下文
  profiles.py      通用 profile 合并、绑定和持久化
  integration.py   可选 integration 的生命周期与目标解析契约
integrations/
  comfyui.py       ComfyUI 专用 hook
  image_generation.py  生图插件专用 hook
main.py            只负责装配 feature 和转发事件
```

底座不依赖任何具体目标插件。每个 integration 自己声明目标插件、检查接口、安装 patch、处理指令和恢复运行时修改。主插件只根据 `features` 装配 integration；目标插件未加载或接口不兼容时，该 integration 不会进入 active 列表。

## 配置

```json
{
  "features": ["comfyui", "image_generation"],
  "profiles": {
    "qq": {
      "platforms": ["aiocqhttp", "qq_official", "qq_official_webhook"],
      "comfyui": {
        "workflow": "healthy.json",
        "input_node_id": "6",
        "neg_node_id": "7",
        "output_node_id": "9"
      },
      "image_generation": {"model": "safe-provider/safe-model"}
    },
    "telegram": {
      "platforms": ["telegram"],
      "comfyui": {
        "workflow": "unrestricted.json",
        "input_node_id": "10",
        "neg_node_id": "11",
        "output_node_id": "12"
      },
      "image_generation": {"model": "unrestricted-provider/unrestricted-model"}
    },
    "default": {"platforms": [], "comfyui": {}, "image_generation": {}}
  }
}
```

配置命令：

```text
/comfy_use <序号> [正向节点] [负向节点] [输出节点]
/生图模型 <序号>
/渠道生图配置
```

切换结果写入本插件自己的 `plugin_data/astrbot_plugin_channel_generation_profiles/profiles.json`，不会调用原插件的全局配置写入逻辑。

## 扩展方式

新增能力时，只需在 `integrations/` 增加一个 `Integration` 子类，实现 `install()`、必要的 `on_event()` 或 `on_llm_request()`，再在 `main.py` 的 composition registry 注册 feature。公共底座不增加目标插件判断，也不保存目标插件字段。

## 边界

生图模型必须已经存在于原生生图插件的 provider 配置中，插件只选择已配置模型，不复制密钥。任务历史、配额和图片处理继续复用原插件；模型适配器和 executor 按 profile 创建。

本项目只修改自己的运行时对象和持久化目录，不修改 AstrBot 容器、原插件源码或原插件配置。升级 AstrBot 或目标插件后，应验证两个渠道并发提交任务、不同工作流节点和后台任务模型是否保持隔离。
