# HandsFreePC 架构

一句话：本地离线的语音前端，加一个执行者（Kimi Code CLI）。

```text
audio.py           麦克风采集、Silero VAD、Vosk 控制词与 over 检测、SenseVoice / faster-whisper 转写
runtime.py         唤醒 -> 会话 -> over 分段 -> FIFO -> 结果反馈；控制词本地处理
session.py         SessionState / WorkerState、PromptAssembler、单线程 FIFO CommandWorker
kimi_agent.py      KimiAgentController：kimi -p 子进程、stream-json 解析、超时与取消
control.py         Controller 协议与 ControlResult
feedback.py        自适应大小的屏幕大字提示、Windows SAPI 语音
mic_guard.py       读取 Windows 麦克风使用记录，别的程序开麦时暂停
diagnostics.py     固定字段的本地 JSONL 事件日志
transcripts.py     可选的识别原文记录
config.py          app / privacy / speech / kimi 四段配置
cli.py             run / doctor / exec / download-models / logs 等命令
downloads.py       官方语音模型下载
normalize.py       文本归一化与控制词匹配
```

## 线程

- **麦克风线程**（`VoiceRuntime.run_microphone`）：唯一持有麦克风的线程。待命时等控制词，词表检测器一命中就从该词的起点（Vosk 词时间戳，环形缓冲保留 4 秒）录到句末交给正文转写，默认要求转写以同一控制词开头才算数（`strict_wake_phrase`）；会话中听一句话，按 `over` 的样本边界切段并转写；语音播报只在这条线程的话语边界上播放，避免打断用户。
- **命令线程**（`CommandWorker`）：严格 FIFO，一次只跑一条；把 `QueuedCommand` 交给 `KimiAgentController.run`，结果回到 `VoiceRuntime._on_control_outcome`。
- **提示框线程**（`Overlay`）：tkinter 窗口，不抢焦点、鼠标穿透。

## 会话状态

`ARMED`（待命）→ 说唤醒词 → `ACTIVE`（接收指令）→ 说结束词 → `DRAINING`（排空队列）→ 队列空 → `ARMED`。停止词随时把状态置为 `PAUSED` 并取消一切，再说唤醒词或恢复词重新开始。

队列策略：`failure_policy: continue` 时一条失败不影响后面的；`pause` 时队列停在失败处，说「继续队列」才继续。

## Kimi 契约

- 命令行：`kimi -p "<前言>\n用户指令：<文本>" --output-format stream-json`，可选 `--model`、`--skills-dir`、`--session`。`-p` 模式本身不需要审批，且不能与 `--yolo` 同用。
- 前言（`DEFAULT_PREAMBLE`）要求 Kimi 用 gui-control 技能、不反问、遵守「不要发送」、Claude 默认 Code 页签，并以两行固定格式收尾：`RESULT: <成功|失败> - <说明>` 与 `SCREENSHOT: <路径>`。
- 解析：`parse_stream_line` 从 `{"role":"assistant","tool_calls":[…]}` 统计工具调用（记录工具名和前 200 字参数），从最后一条 `assistant` 文本取结论，从 `{"role":"meta","session_id":…}` 取会话号。
- 结果映射：`RESULT: 成功` → 成功；`RESULT: 失败` → `KIMI_REPORTED_FAILURE`；没有结论 → `KIMI_NO_VERDICT`；非零退出且无结论 → `KIMI_EXIT_ERROR`；超时 → `KIMI_TIMEOUT`（终止整棵进程树）；取消 → `CANCELLED`。
- 进度：每次工具调用通过 `on_progress` 回调更新屏幕提示「Kimi 第 N 步：工具名」。

## 事件日志

阶段只有 `runtime`、`transcribe`、`kimi_agent`。常见错误码：`MICROPHONE_READY`、`VOICE_SESSION_STARTED`、`COMMAND_ENQUEUED`、`CONTROL_STARTED`、`KIMI_STARTED`、`KIMI_TOOL_CALL`、`KIMI_COMPLETED`、`CONTROL_COMPLETED`、`KIMI_TIMEOUT`、`MICROPHONE_GUARD_PAUSED`、`MICROPHONE_GUARD_RESUMED`。
