# astrbot_plugin_channel_generation_profiles

为 AstrBot 的 ComfyUI、生图插件提供按渠道隔离的工作流和模型配置。
目标插件的源码、配置文件、任务管理与权限规则保持由原插件管理。

## 结构

- `core/channel.py`：平台归一化、请求上下文与请求级 profile 快照。
- `core/profiles.py`：配置合并、绑定校验和原子持久化。
- `core/integration.py`：可选 integration、接口检查、带所有权检查的安装和恢复。
- `core/resources.py`：请求与排队任务引用计数、旧运行时延迟释放。
- `integrations/comfyui.py`：渠道 API 视图、实际 API 实例及提交/回收快照。
- `integrations/image_generation.py`：渠道配置读取、模型运行时和任务提交边界。
- `main.py`：装配和 AstrBot 生命周期事件，不解析或截断目标插件指令。

## 隔离机制

### ComfyUI

原插件的 `self.api` 被替换为渠道视图。视图将属性访问和方法调用委派给完整的真实 ComfyUI API 实例，方法内部的 `self` 也属于该实例。工作流名称、节点 ID、路径和相关缓存因此保持一致。

`comfyui_usage`、系统提示词、工作流列表、LoRA/sidecar 读取与执行使用同一请求快照。不修改 workflow catalog：省略参数仍列出全部工作流，显式查询不会改变默认工作流标记。

保留原 `/comfy_use` handler，由原插件检查管理员和序号，再通过渠道版 `reload_config()` 保存设置。提交后以 prompt ID 保留 API 引用；切换渠道配置、并发调用或卸载本插件不会改写已提交任务的节点设置。未被回收的提交快照在一小时后清理；正在等待的任务不会被这个清理时限中断。卸载期间，原 API 仅暂留结果路由桥接，最后一个任务结束或快照过期后自动恢复。

### Telegram 图片直发

默认情况下，Telegram 的 `/画图`、`/绘画` 自动使用与 `/画图no` 相同的逐张直发规则，避免产生平台不支持的合并转发节点。单图与多图均生效；命令内容、工作流和提示词参数不被改写，权限、敏感词和冷却检查仍由原插件执行。

命令的全部结果（包括拒绝和错误提示）交付后，调用 `event.stop_event()` 终止后续传播，避免消息继续进入其他 handler，或发送失败后被默认 LLM 当作聊天提示词处理。不会在图片交付前停止事件；LLM 工具入口不停止事件，以保留正常的 Agent 回合收尾。该命令消费规则仅作用于 `comfyui_direct_send_platforms` 中的平台。

LLM 调用 `comfyui_txt2img` 时，通过 AstrBot 的 `on_using_llm_tool` 钩子将实际调用参数设为 `direct_send=true`，不依赖模型主动选择正确参数。批量任务的等待阶段继承该设置，原工具仍返回完成或提交状态，保持 Agent 回合的原有流程。

`comfyui_direct_send_platforms` 默认 `["telegram"]`，`telegram_bot` 别名也生效。可以添加其他已确认需要直发的平台，例如 `["telegram", "discord"]`；设为 `[]` 可关闭本功能。未列入的平台（包括默认配置下的 QQ）保留原发送规则。此设置仅在 `features` 包含 `comfyui` 时生效，修改后重新加载本插件。

### 生图

按渠道创建真实 generator、executor 和配置快照，继续共用原任务队列、配额、审核以及请求并发额度。

ConfigManager 通过仅作用于该实例的子类属性提供渠道模型视图，已持有同一 manager 引用的结果处理器也能读取正确模型；原 ConfigManager 类没有修改。原模型命令的配置写入改为保存本插件的 profile，原配置文件不变。

原 `create_generation_task` 方法绑定到一个浅复制的任务接收对象，其 generator/executor/config 是固定引用。原方法创建的延迟闭包因此不会在出队时重新读取全局 executor。队列边界同时固定路由上下文与任务记录中的模型。旧实例会等请求和任务引用释放后再关闭；取消尚未执行的任务也会释放引用。

### 生命周期

目标不存在、未激活或接口不兼容时，对应 integration 不启用。通过 `on_astrbot_loaded`、`on_plugin_loaded`、`on_plugin_unloaded` 处理晚加载及热更新；重复通知不会重复安装。

卸载只恢复仍由本插件持有的替换，不覆盖其他插件之后安装的替换。已排队生图任务持有独立快照，可在本插件卸载后继续执行；原目标插件自身卸载时，仍遵循它原有的任务取消逻辑。

## 配置

AstrBot v4.28 的 `profiles` 字段使用 JSON 文本，示例内容：

```json
{
  "qq": {
    "platforms": ["qq"],
    "comfyui": {"workflow": "healthy.json", "input_node_id": "6", "neg_node_id": "7", "output_node_id": "9"},
    "image_generation": {"model": "provider/model-a"}
  },
  "telegram": {
    "platforms": ["telegram"],
    "comfyui": {"workflow": "alternate.json"},
    "image_generation": {"model": "provider/model-b"}
  },
  "default": {"platforms": [], "comfyui": {}, "image_generation": {}}
}
```

- `features` 默认包含两个 integration；显式空列表表示全部关闭。
- `strict_version` 为兼容既有配置保留，控制额外的调用签名检查，**不是版本号白名单**；必要接口检查始终执行。
- QQ 的 aiocqhttp/OneBot/NapCat/QQ 官方别名统一为 `qq`；Telegram 为 `telegram`。本版本按平台绑定，同平台多个机器人实例共享该平台 profile。
- 未填写的字段继承原实例默认设置，并固定在当前请求中；明确填写的空负向/输出节点 ID 不会被默认值覆盖。
- 模型必须来自原插件已配置的 `provider/model` 选项；不复制或持久化 API 密钥。错误模型不会悄悄回退到其他 provider。
- 非法 JSON、重复平台绑定和损坏的状态文件会明确报错，不会静默清空配置。保存失败时，旧文件和内存状态均保留。

## 使用和升级

继续使用原命令：

```text
/comfy_use <序号> [正向节点] [负向节点] [输出节点]
/生图模型 <序号>
/渠道生图配置
```

原插件自身的权限、提示和参数校验保持生效。平台唤醒前缀由 AstrBot 处理，本插件不再自行重解析命令。

渠道选择保存在本插件的 `plugin_data/astrbot_plugin_channel_generation_profiles/profiles.json`。v0.1.x 的配置和持久化文件可直接沿用。移除了旧实现的 `渠道工作流`/`渠道模型` 自定义别名，统一使用上面的原命令，以保留原插件校验。

原插件 WebUI 显示的仍是全局配置；查看渠道状态可用 `/渠道生图配置`，ComfyUI 工具查询则返回当前渠道的有效运行态。不再向用户 prompt 追加另一份渠道配置说明。

工作流、LoRA、sidecar 等资源文件仍共享：两个渠道选择同一个文件时，对该文件的管理修改仍会同时影响它们。这是配置选择与运行时隔离，不是独立资源目录或权限租户系统。

## 测试

无需安装 AstrBot 或网络依赖：

```bash
python3 -B -m unittest discover -s tests -v
```

可指定只读的目标插件源码目录，执行实际源代码契约测试：

```bash
PROFILE_TARGET_SOURCE_ROOT=/path/to/data/plugins python3 -B -m unittest discover -s tests -v
```

若同时设置 `ASTRBOT_SOURCE_ROOT=/path/to/AstrBot`（本地源码 checkout），还会执行真实调度器、命令处理阶段和默认 LLM 入口的离线传播测试，覆盖多图交付、权限拒绝、发送失败及后续插件请求 LLM 的情况。

契约测试提取目标源码中的 API、工具和命令方法，在临时文件、假 HTTP 和假队列上执行；不导入目标 main 模块，不连接生产服务，不发送消息或生成图片。

2026-09-29 的验证基于 AstrBot v4.28.0 挂载的两个插件源码。源码契约验证不等于生产端到端验收；更新安装后仍需检查 QQ/TG 各一次查询与生图、并发任务、切换模型以及目标热更新。
