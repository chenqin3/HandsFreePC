# 故障排查

先看这三样：

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml doctor --strict --check-kimi
.venv\Scripts\python.exe -m handsfree_pc diagnose-last
.venv\Scripts\python.exe -m handsfree_pc logs --tail 30
```

## 说「开始语音操作」没反应

- `doctor` 里 `models.wake.ready` 必须为 true，`audio_inputs` 非空。多个麦克风时在 `speech.input_device` 指定索引。
- 控制词必须完整地说，前后不要带别的字；说慢一点比说快更容易被词表检测到。
- 自定义了控制词却没加进 `speech.wake.grammar`（按「字 词 之间 加 空格」的形式）。
- 屏幕上出现过「X 正在使用麦克风，已暂停监听」：别的程序还占着麦克风；等它结束，或把它加进 `app.microphone_guard_ignore`。

## 聊天时误触发了「开始语音操作」

- 默认 `app.strict_wake_phrase: true`：词表检测器听到唤醒词后，还要由正文转写模型确认这句话以唤醒词开头，否则丢弃（事件日志里是 `CONTROL_PHRASE_UNCONFIRMED`）。如果仍误触发，看 `transcripts --tail` 里被确认的那句是什么，通常是真的说了这几个字。
- 可以把 `speech.wake.phrase_window_seconds` 再调小（默认 3 秒），词表检测器只把这个窗口内的词拼在一起。
- 也可以换一个更不常说的唤醒词，记得同时改 `speech.wake.grammar`。

## 说了唤醒词却不触发

- 严格模式要求唤醒词在句首：前面别带"嗯""那个"之类的话，说完停一下。
- 看 `transcripts --tail`：如果转写把唤醒词听成了别的字，把唤醒词加进 `speech.command.hotwords` 和 `initial_prompt`（默认已包含）。
- 临时放宽：`app.strict_wake_phrase: false`，词表检测器一听到就触发。

## `over` 经常漏

- `doctor` 里 `models.delimiter.ready` 必须为 true（英文 Vosk 小模型）。
- 清晰地说 over 并留一个很短的停顿；正文转写里独立出现的单词 over 也算，但不要指望它。
- 看 `privacy.save_transcripts: true` 后的 `transcripts --tail`，确认正文转写是否把 over 吞进了别的词。

## 指令进了队列，但没有动作

- `logs` 里找 `KIMI_NOT_AVAILABLE`：`kimi.executable` 没找到，改成 `kimi.exe` 的完整路径。
- `KIMI_NO_VERDICT`：Kimi 没有按「RESULT: …」格式收尾，通常是技能没加载或模型不看图。用 `exec` 跑一条，看 stderr 里是否有 `Skill` 调用。
- `KIMI_TIMEOUT`：`kimi.timeout_seconds` 太短；发文件这类任务实测 2 到 4 分钟。
- `KIMI_EXIT_ERROR`：先在终端手动跑 `kimi -p "打开记事本" --output-format stream-json`，看登录状态和报错。

## Kimi 做错了目标

- 转写不准：改 `speech.command` 的 `initial_prompt` / `hotwords`，把常用的应用名和会话名加进去；或换 faster-whisper。
- 定位不准：把你的项目地图、常用目录和应用约定写进 `gui-control` 技能，Kimi 会先查索引再找文件。
- 想改 Kimi 收到的总要求：`kimi.preamble_file` 指向你自己的前言文件（结尾两行格式不能变）。

## 一条失败后后面的不动了

`app.failure_policy: pause` 时队列会停在失败处，说「继续队列」或「取消所有操作」；想让后面的自动继续就改成 `continue`。

## 提示框没出现

- `feedback_mode` 是 `silent` 或 `voice`：说「切换到屏幕反馈」。
- 计划任务没有跑在桌面会话里：`install_autostart.ps1` 注册的是交互式任务，不要改成「不管用户是否登录都运行」。

## 开机后没有启动

- `Get-ScheduledTask HandsFreePC` 状态应为 Running；`%LOCALAPPDATA%\HandsFreePC\logs\run.log` 里看启动报错。
- 出现「already running」：上一个实例还在（锁文件 `logs\run.lock` 记录 pid），`Stop-ScheduledTask HandsFreePC` 或结束那个 python 进程。

## 语音播报把我的话吞了

播报期间是半双工，播完才继续听；需要连续快速下指令时用 `overlay`。
