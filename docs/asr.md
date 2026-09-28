# 视频转写与证据

2.1.0 已实现受限的媒体获取和本地识别路径，但真实样例是否通过，必须看[验收记录](acceptance-status.md)，不能仅凭函数存在或模拟测试宣布恢复完成。

## 自动处理路径

先读取与当前内容 ID 对应的公开内嵌文字稿；没有文字稿时，从本条笔记／视频的页面数据中发现公开 MP4／M4A，核验媒体域名和每次跳转，下载、解码并调用本地 faster-whisper。

复用已安装的运行环境和缓存的 small、base 或 tiny 权重，需要 ffmpeg／ffprobe。仅发现缓存文件不证明识别效果。没有组件时保留任务，说明具体缺项，不自动改用云端或另外付费的音频 API。GPT 订阅用于整理文字，不被当作音频 API 额度。

安装器可选 `--with-local-asr`（这份可选依赖要求已有 Python 3.12+，不自动升级系统 Python） 安装固定版本组件，约下载 60–85 MB，不下载模型权重。已有配置在升级时继承。新增识别权重或解码组件前，先查清下载体积、位置、资源影响并说明；不要求普通用户自己查技术配置，也不把手工字幕变成日常前提。

## 首次准备（由 Hermes 执行）

一次检查运行组件、模型缓存与 ffmpeg／ffprobe，集中说明全部缺项后按已有授权准备。缺权重时使用下述官方 base 快照；已有可用的 small／base／tiny 缓存则直接复用，不重复下载或替换。若三项都缺，转写组件加权重约需下载 208–233 MB，另加基础安装依赖与本机解码组件；ffmpeg 的体积、来源和是否需要系统权限应按当前平台核实。模型缓存约占 148 MB，安装及下载缓存还会占额外磁盘；识别使用 2 个 CPU 线程，不向云端发送音频。下载服务会接收模型文件请求与网络连接信息。

固定模型为 `Systran/faster-whisper-base`，完整提交号 `ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`。2026-09-28 只读核查[官方固定快照](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66)及[文件元数据](https://huggingface.co/api/models/Systran/faster-whisper-base/revision/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66?blobs=true)时，下列四个文件共 147,882,941 字节，约 148 MB；此核查没有下载权重或完成转写验收。

| 文件 | 字节数 |
| --- | ---: |
| `model.bin` | 145,217,532 |
| `config.json` | 2,309 |
| `tokenizer.json` | 2,203,239 |
| `vocabulary.txt` | 459,861 |

执行准备的 Hermes 应在本 Skill 已安装的转写环境，或确实可复用的 Hermes 转写环境中调用 `huggingface_hub.snapshot_download`：`repo_id="Systran/faster-whisper-base"`、`revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"`、`allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]`、`token=False`、`endpoint="https://huggingface.co"`、`max_workers=1`。它是公开模型，不需要用户登录 Hugging Face 或提供令牌；不要加载仓库代码或调用云端识别。[官方固定版本与文件筛选说明](https://huggingface.co/docs/huggingface_hub/guides/download)

使用标准 Hub 缓存，**不要传 `local_dir` 下载到任意目录**。将 `cache_dir` 设为运行 Hermes 时已有的 `HF_HUB_CACHE`，否则为已有 `HF_HOME` 下的 `hub`，均未配置时为 `~/.cache/huggingface/hub`；不要为此修改用户全局环境。下载返回位置应为该缓存下的 `models--Systran--faster-whisper-base/snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`，四个文件均须可读。官方元数据给出的 `model.bin` SHA256 为 `d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9`，下载后核对大小和该哈希；校验失败保留错误信息，不运行不完整模型。[官方缓存下载接口](https://huggingface.co/docs/huggingface_hub/package_reference/file_download#huggingface_hub.snapshot_download)

准备后，从实际 Skill 安装目录用 `"<已有Python绝对路径>" scripts/video_kb.py doctor` 复查；只有 `transcription.available=true` 才可报告本地转写条件齐备。下载完成、安装成功或 `ready_to_connect` 均不能替代此检查；该检查本身也不等于真实识别成功。随后仍须处理首条真实公开链接，区分媒体获取、ASR、摘要和 Notion 完整回读的实际结果。

## 限制与说明

- 单条媒体最多 256 MB、20 分钟；下载最多 3 分钟，音轨处理最多 90 秒，本地识别受 5 分钟运行上限及 CPU 限额约束。单任务执行，不无限并发。
- 媒体缓存按约 640 MB 总额预留空间；开始新任务时，只清理带本程序标记且原进程已结束的固定临时文件。未知文件、特殊文件或空间不足会保留并停止，不盲目删除。
- 音轨为本地临时文件，不发送给云端识别服务。识别文字会交给已有摘要模型，并写入 Notion。
- 标注“ASR 转写，非官方字幕”。识别可能漏词或认错专名；处理整段音轨不等于逐字准确率 100%。
- 音轨与视频时长明显不一致、文字超过 5 万字时标为局部，并说明缺失／截断。检测到沉默不能单独证明转写遗漏或完整。
- 公开内嵌文字稿也不自动称为覆盖全部声音；需用真实讲话抽查不同位置。
- 暂无 HLS／DASH、浏览器渲染／资源发现后备、图片 OCR、平台登录或验证码绕过。

只有标题、配文或简介时，不冒充视频转写。可保存降级内容并说明，但不能算核心视频验收通过。已发现媒体但缺少本地 ASR 时，任务保留待处理，不用“已收藏”掩盖缺项。

用户主动提供的 TXT、SRT、VTT（最多 5 万字符）可用 `collect URL --transcript-file 文件路径` 保存为独立补充笔记；同一补充去重，不覆盖旧笔记，也不搜索其他本地文件。
