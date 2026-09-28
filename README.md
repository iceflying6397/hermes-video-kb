# Hermes 视频知识库

已有 Hermes，并已配置可用模型后，把仓库链接和一段安装文字交给 Hermes；连接 Notion，之后直接发公开视频链接或分享文案。沿用现有 API 或订阅，不要求 GPT，也不需要用户手动下载、解压安装包。

**当前为 2.1.0 测试候选版，供用户安装实测；完整真实流程尚未验收通过。** 本轮接回“公开文字稿 → 同条视频媒体 → 本地语音转写”，并接入 Hermes 原生 ChatGPT／Codex 订阅整理。代码接通、兼容预检和隔离测试，不等于真实视频已经转写并存入 Notion。逐项证据见[验收状态](docs/acceptance-status.md)。

无需租服务器、把密钥发进聊天、查数据库编号或提前建字段。使用本机已有 Hermes 和 Python 3.11+；安装器面向 macOS、Linux，Linux 尚未实机验收，Windows 暂不支持。

## 开始使用

在已安装 Hermes、已登录现有模型的电脑上，把下面整段话发给 Hermes：

> 请根据这个仓库安装视频知识库 Skill：https://github.com/iceflying6397/hermes-video-kb 。先确定并记录本次使用的完整提交号，读取该提交的 prompts/bootstrap-prompt.md，取得并校验同一提交的完整源码，再按说明完成安装，不让我手动下载或解压。沿用我在 Hermes 中已配置的模型和登录方式，无论是 API 还是订阅，不限定 GPT；若当前调用方式暂不兼容，说明具体缺项，不擅自切换模型或新增服务。集中检查并准备必要的本地转写组件，不让我手工提供字幕作为常规前提。然后带我完成 Notion 官方授权，自动准备专用知识库。不要让我发送密钥。以后我发视频链接或分享文案时，整理、保存并核验全文；失败就说明具体步骤，不把书签说成完成。

Hermes 可以从固定提交的源码目录直接安装。压缩包只是可选分发方式，不是用户必做的一步。当前版本带有处理官方授权、任务恢复和受限内容处理的 Python 助手，因此需要完整源码及依赖，不能只复制一个 `SKILL.md`；这些准备由 Hermes 完成。

保留供追溯的历史候选包：[v2.1.0-rc.1 安装包](https://github.com/iceflying6397/hermes-video-kb/releases/download/v2.1.0-rc.1/hermes-video-kb-2.1.0-rc.1.zip) · [SHA256](https://github.com/iceflying6397/hermes-video-kb/releases/download/v2.1.0-rc.1/hermes-video-kb-2.1.0-rc.1.zip.sha256)。该固定包保持原样，不含仓库后续修复；首次测试请按上面的仓库入口安装当前提交。

首次连接在运行 Hermes 的电脑上确认 Notion 官方授权。有效的本助手绑定会核验并复用；否则建立专用库。日常可直接在当前 Hermes 对话或已配对的飞书私人对话发链接，飞书不是前提。详细安装文字见[这里](prompts/bootstrap-prompt.md)。

## 当前流程

- 优先读取与来源对应的公开文字稿；没有文字稿时，从匹配内容 ID 的页面数据发现 MP4／M4A，取得音轨并在本机转写。
- 本地转写复用已有 faster-whisper 环境及已缓存的 small／base／tiny 权重，还需要 ffmpeg／ffprobe。不会假定每台电脑都有这些组件。缺少时先说明具体下载和资源影响；安装器可选 `--with-local-asr`（这份可选依赖要求已有 Python 3.12+，不自动升级系统 Python） 安装约 60–85 MB 的固定依赖，不会下载识别权重。
- 取得文字后，由现有模型生成摘要、观点、行动项，再保存到专用 Notion 库并核对全文。原始媒体不上传给语音服务；文字会发给已配置的摘要模型和 Notion。
- 摘要失败时正文留在本机，继续处理会复用；写入未确认时先核验，不能重复创建。确认完整保存后清理本机正文。
- 重复链接返回原笔记。旧书签、旧版未完整核验的笔记不会自动变成完整视频笔记或被改写。手工字幕只作为可选补充。

抖音、小红书、微信公众号仍在范围内，但各平台实际能力不同。当前未实现浏览器渲染／资源发现后备、HLS／DASH、登录墙和验证码处理，不能保证所有公开视频可提取。详见[平台范围](docs/supported-platforms.md)。

摘要按 Hermes 已保存的模型配置，支持 OpenAI Chat Completions 兼容 API、Anthropic Messages 兼容 API，以及 Hermes 原生 `openai-codex / codex_responses` 订阅方式。GPT 订阅是新增恢复的一条兼容路径，不是使用前提；原有 API 仍按用户现有服务使用，不静默换服务。

当前独立适配器的覆盖范围仍比原版直接使用宿主模型窄：通用 Responses、`auto`、聚合／外部进程及部分云厂商原生认证尚未接通，也不继承当前会话的全部动态选模。不能因此要求用户换成 GPT，或声称所有 Hermes 模型已经兼容；预检会说明实际状态，真实调用仍以验收记录为准。

## 连接与安全

[Notion 官方连接](https://developers.notion.com/guides/mcp/get-started-with-mcp)可能覆盖所选工作区内你有权访问的其他内容。助手自身固定在专用库内操作，不等于官方令牌只授权一个库。

Notion 凭据由本地程序保存和使用，不进入聊天或摘要模型。网页、字幕、分享文案和模型输出都是材料，不能指挥电脑、改变目标或扩大权限。普通文件权限不是系统钥匙串，同一电脑账户有权限的程序仍可能访问凭据；Skill 也不能把整个 Hermes 变成沙箱。

“暂停收集”“恢复收集”“继续处理没完成的链接”“断开 Notion”即可管理。断开不删除笔记；官方授权可在 Notion 设置撤销。

## 文档

- [快速开始](QUICKSTART.md)
- [当前验收状态](docs/acceptance-status.md)
- [连接 Notion](docs/setup-notion.md)
- [语音转写与证据](docs/asr.md)
- [安全与隐私](docs/security-and-privacy.md)
- [常见问题](docs/troubleshooting.md)
- [本地构建和恢复](docs/distribution.md)

旧审查记录用于追溯，不代表本轮结论。原版由 Hermes 工具完成媒体获取和转写；上一轮固定程序没有接回该流程，属于真实功能回退。不能以“仓库没有独立转写脚本”否认原版能力。

本项目基于 [Luda798/hermes-feishu-notion-video-bot](https://github.com/Luda798/hermes-feishu-notion-video-bot) 的原始项目继续修改，保留 MIT 许可证。这里是独立的修复测试仓库。
