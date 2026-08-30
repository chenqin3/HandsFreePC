# HandsFreePC 技术调研与选型

> 调研快照：2026-08-30。本文把厂商/上游项目公开资料中的**已核实事实**与 HandsFreePC 的**工程选择**分开陈述。外部能力会变化，发布前应重新核对链接和本机版本。

## 结论先行

HandsFreePC 不应把“常开麦克风、理解命令、操控桌面”全部交给一个云端 Agent。更稳妥的组合是：

1. 本机常驻进程只用本地模型监听唤醒词；
2. 唤醒后在本机转写一条命令；
3. 常见命令先走确定性解析和白名单动作；
4. 只有无法确定解析、且用户显式允许云规划时，才把**转写文本**交给 Codex 或 Claude；
5. 无论计划来自哪里，都必须经过本地 Schema、风险策略和目标核验；
6. Windows 操作按“原生 API → UI Automation → 受控兜底”的顺序执行，并检查结果；
7. 默认用不抢焦点的大字遮罩反馈，可切换为本机语音、两者同时或静默。

这意味着 Codex/Claude 是可替换的“规划器”，不是常驻麦克风、权限管理器或直接点击桌面的唯一执行器。准确性主要来自确定性路径解析、可访问性树定位、歧义拒绝和动作后的验证，而不是单纯换一个更大的模型。

## 1. Windows 自带能力是否够用

### 已核实事实

- Windows 11 22H2 及以后提供“语音访问”（Voice Access），可离线控制电脑和输入文本；官方当前列出的语言包括中文。首次设置和下载语言文件仍需要网络。[微软：语音访问命令列表](https://support.microsoft.com/zh-cn/accessibility/windows/voice-access/voice-access-command-list)、[微软：设置语音访问](https://support.microsoft.com/zh-cn/accessibility/windows/voice-access/set-up-voice-access)
- Voice Access 能打开/切换应用、按控件名称或编号操作屏幕、控制鼠标键盘和听写文本。
- Microsoft UI Automation（UIA）就是给辅助技术、语音识别和测试工具读取并调用 UI 元素的官方接口；它暴露控件属性、控件模式和事件，而不只是屏幕坐标。[Microsoft UI Automation client overview](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-clientsoverview)

### 工程选择

Voice Access 很适合作为系统级故障后备，但不作为本项目核心。原因不是它“不好用”，而是 HandsFreePC 需要自定义中文唤醒句、受限动作 Schema、路径消歧、Codex/Claude 专用流程、可测试的后置条件和统一隐私开关；这些需要自己的状态机。

项目复用 Windows 的底层 UIA 和文件关联能力，而不是复刻一个坐标点击器。Windows 自带语音访问可与本项目二选一运行；不建议两套听写同时占用麦克风。

## 2. 本地语音链路

| 候选 | 已核实能力 | 适合位置 | 本项目选择 |
|---|---|---|---|
| Vosk `vosk-model-small-cn-0.22` | 离线中文模型约 42 MB；Vosk 支持运行时 grammar/短语集合；模型列表标注 Apache-2.0 | 低资源、常开、有限词表的唤醒/停止词检测 | 首选唤醒层 |
| sherpa-onnx SenseVoiceSmall INT8 | 官方预训练页列出普通话、粤语、英语、日语、韩语，支持 `use_itn` 标点/文本归一化以及麦克风/VAD 示例；INT8 包约 228 MB | 唤醒后的一整句命令 | 首选命令 ASR |
| faster-whisper | 基于 CTranslate2 的本地 Whisper 实现，支持 CPU/GPU 和量化配置；资源消耗明显高于小型 KWS | 可作为另一种本地 ASR 或异常后备 | 默认不安装、不启用；0.1.0 后备只在 SenseVoice `transcribe()` 抛异常时触发 |
| Silero VAD v6.2.1 | 本地 ONNX 语音活动检测，上游 MIT 许可；sherpa-onnx 可直接加载模型 | 更稳的起止点检测、减少环境噪声误切句 | 默认起止点检测；自适应能量门限作为无模型后备 |

官方资料与下载入口：

- [Vosk 模型列表与许可](https://alphacephei.com/vosk/models)；[small-cn-0.22 模型包](https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip)；[Vosk grammar API](https://github.com/alphacep/vosk-api/blob/master/src/vosk_api.h)
- [sherpa-onnx SenseVoice 预训练模型](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)；[2024-07-17 INT8 模型包](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2)
- [SYSTRAN faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Silero VAD v6.2.1](https://github.com/snakers4/silero-vad/tree/v6.2.1)

### 工程选择

首版音频格式为 16 kHz、单声道 PCM。`ARMED`/`PAUSED` 状态只让 Vosk 用很小的 grammar 识别唤醒词和停止词；唤醒后才启用 SenseVoice。默认由 sherpa-onnx 加载固定为 v6.2.1 的 Silero ONNX 做话语起止点检测；若用户明确把 `speech.vad.backend` 配成 `energy`，则使用可校准的自适应能量门限后备。

这一分层比“让大模型一直听”更省资源，也让原始音频默认不必离开机器。公开默认 `speech.fallback.backend: none`，普通安装只装 `audio` 与 `windows` extras；`-WithWhisper` 才安装 faster-whisper。启用者应先显式预下载 `large-v3-turbo`，因为它会产生 GB 级网络下载/缓存和明显资源开销。当前实现不按空文本、低置信度或长句自动切换，只在已经构造 SenseVoice 后、某次 `transcribe()` 抛异常时延迟构造 Whisper；SenseVoice 启动/模型加载失败不能由这条后备补救，自动化套件也尚未覆盖该分支。

发布机已实际通过 SenseVoice 官方样例（转写为“开饭时间早上9点至下午5点。”）、Vosk 合成唤醒/停止、Silero 官方样例、16 kHz 真实麦克风读取和完整本地运行时启动/停止。它们证明默认链路在该机器可运行，但不等于远场、家庭噪声、婴儿声、方言或其他音频设备均已验证。

### 模型许可提醒

Vosk 官方模型页把 small-cn-0.22 标为 Apache-2.0，但该模型 zip 可能只有 README、没有完整许可文本；再分发时应同时保存 [Vosk v0.3.45 COPYING](https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING)、模型来源和下载哈希。

sherpa-onnx 运行时代码与 SenseVoice 权重不是同一许可对象。发布脚本不得把 SenseVoice 权重打包成项目自有资产；模型包内的短 LICENSE 可能只是链接，下载后应额外保存完整 [FunASR Model License](https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE)，注明 SenseVoiceSmall、FunASR/FunAudioLLM 和 Alibaba Group，并保留模型名。

## 3. “底层灵敏”应该怎样实现

### 已核实事实

Windows Service 从 Vista 起运行在隔离的 Session 0，不适合直接与当前用户桌面交互。[Microsoft：Interactive Services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

### 工程选择

HandsFreePC 是登录用户会话中的普通常驻进程，不是系统服务，也不要求管理员权限：

- 登录后自启；麦克风采集和 UI 操作都留在当前交互会话；
- 休眠态仅保存内存中的短预卷环形缓冲，不写音频文件；
- 唤醒窗口有超时，超时回到仅监听唤醒/停止短语；
- `ARMED`/`PAUSED` 中由 Vosk 识别唤醒/停止短语；`AWAKE`、`DICTATION`、`CONFIRMING` 的 endpoint 录音也会把每个音频 block 同时喂给本地 Vosk，只要命中配置的停止短语就立即中断当前录音并进入 `PAUSED`，无需等 SenseVoice 完整转写。取消确认等其他控制语仍走完整话语转写；同步执行和 TTS 期间仍不可抢占；
- 每个真实 OS/UI 动作前用 `OpenInputDesktop` 读取当前输入桌面的 `UOI_NAME`，只有 `Default` 才继续；锁屏、Winlogon 或 UAC secure desktop 连 `open_path` 也阻断。0.1.0 仍没有会话事件监听器，麦克风不会因锁屏自动进入 `PAUSED`，所以锁屏前主动暂停或退出仍更稳妥；
- TTS 播放时 PortAudio callback 仍写入有界输入队列/预卷，但运行时暂停识别与命令处理；`speaking` 覆盖整个待播队列，全部播放后两处缓冲一起丢弃，避免系统把自己的反馈重新当成命令。0.1.0 不能用停止词打断正在播放的 TTS 队列，因此播报必须保持为短句。

## 4. Windows 操作技术路线

| 层级 | 已核实事实 | 本项目用法 |
|---|---|---|
| 原生 Windows handler | ShellExecute 可按系统文件关联打开文档和目录。[Microsoft Shell launch](https://learn.microsoft.com/en-us/windows/win32/shell/launch) | 路径存在性、唯一性和扩展名风险校验后直接打开；不拼接 shell 命令 |
| UI Automation | UIA 可按语义读取元素并调用控件模式 | 激活窗口、定位项目/对话/输入框、点击按钮；输入前后都核验 |
| pywinauto | Python 的 Win32/UIA 自动化库，支持 `uia` 与 `win32` 后端。[pywinauto 文档](https://pywinauto.readthedocs.io/en/latest/) | 首版 UIA 适配器，便于与 Python 状态机集成 |
| WinApp CLI | 微软工具可搜索、调用、设值、等待、截图并输出 JSON；官方页面仍标为 Public Preview，Electron 支持有限。[WinApp UI Automation](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation) | 后续可选适配器；固定经测试版本，不能作为唯一依赖 |
| 局部视觉 | Windows 可捕获单个窗口画面。[Windows Graphics Capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture) | 未来兜底；只截目标窗口/区域，不允许模型对整张桌面任意点坐标 |

`SetForegroundWindow` 受 Windows 防抢焦点规则限制，所以“请求激活”失败必须是正常错误分支；不能激活后仍盲目发按键。[SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow) `SendInput` 还受 UIPI 完整性级别限制；普通权限进程不能可靠地向更高权限窗口注入输入。[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

Windows 提供 [`OpenInputDesktop`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop) 和 [`GetUserObjectInformation`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw) 查询当前输入桌面。0.1.0 把名称严格等于 `Default` 作为每个真实 OS/UI 动作的前置条件；这是本项目的工程门禁，不代表 Windows 会替任意应用自动采用同一策略。

### 工程选择

执行顺序固定为：

1. 路径和应用能用确定性原生 handler 完成时，绝不调用 LLM 或视觉；
2. UNC / `//server`、URI 和 Win32 device namespace 在任何文件系统访问前阻断，只接受本地路径合同；
3. 首版应用内选择先枚举可见、启用的 UIA 后代，再按控件类型和可访问名称做唯一/模糊匹配；`AutomationId` / `RuntimeId` 主要作为证据，并用于固定已聚焦的听写目标，而不是通用初始 selector；
4. 多个候选一律拒绝并失败关闭，不按第一个猜测；0.1.0 不展示候选问答，用户需重新说得更具体；
5. 模糊名称、别名或 planner 路径先解析成最终路径，再按最终扩展名重新做一次本地风险判级；
6. 激活阶段验证允许的进程名/标题；进入听写后，每段输入/提交前复核同一 foreground HWND 和已固定的非密码 Edit/Document 身份，不重新检查进程镜像、签名或完整性级别；
7. 命名 UIA 点击要求观察到选中/焦点/元素消失或 UI 树变化，搜索热键流程最终重新找到选中的命名项；听写每段输入和提交前会重验同一个非密码 Edit/Document 与前台窗口。`TYPE_TEXT` 的后置证据只证明 `SendInput` 接受全部 UTF-16 单元并保持前台，不能证明控件值已变化；`SEND_PROMPT` 只证明 Enter 已发送并保持前台，不能证明 composer 清空、消息出现或网络接受；`open_path` 只确认 Windows 接受调度；
8. 视觉仅用于 UIA 确实看不到的局部区域，首版不承诺通用视觉点击。

## 5. Codex `exec` 与 Claude `-p`

### 已核实事实

| 维度 | Codex CLI | Claude Code |
|---|---|---|
| 正确的非交互入口 | `codex exec`，官方标为 Stable | `claude -p` / `--print` |
| 结构化输出 | `--output-schema <file>`；`--json` 可输出 JSONL 事件 | `--json-schema '<schema>'`；`--output-format json/stream-json` |
| 限权相关能力 | `--sandbox read-only`、`--ephemeral`、独立工作目录；但 read-only 仍允许模型生成的只读 shell 命令 | `--tools ""`、`--safe-mode`、`--no-session-persistence`、权限模式 |
| 容易混淆的参数 | `codex -p` 是 **profile**，不是 prompt | `claude -p` 才是 print 模式 |

来源：[OpenAI Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)、[OpenAI Codex CLI reference](https://developers.openai.com/codex/cli/reference)、[Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)。

官方文档没有提供一个可直接证明“哪个 CLI 更擅长操控本机 Codex/Claude 桌面 UI”的同条件基准。因此不把主观印象写成事实。

### 工程选择

- 默认适配 `codex exec`，原因是它有稳定的非交互入口、JSON Schema 输出、临时工作目录和 ephemeral 运行选项，并与本项目的默认使用环境一致。
- 同时提供 Claude `-p` 适配器。其空工具集比 Codex read-only shell 提供更窄的本机工具面，但仍需要有效的 Claude OAuth/登录。用户可以按已有订阅、额度、延迟和个人效果切换；项目不承诺订阅一定覆盖所有调用，也不承诺固定模型或额度。
- 两个规划器都只能返回不超过 8 步的受限 JSON 动作；本地代码重新解析并从 planner 声明风险起步，只能保持或升高。云 planner 返回 `TYPE_TEXT` / `SEND_PROMPT` 会被直接阻断，只能规划进入/聚焦听写等前置动作；确认摘要也从已校验动作本地生成，不信任 plan `summary`。
- 两者的 JSON 计划都不能让 HandsFreePC 执行任意 shell、坐标点击、密码读取、确认绕过或 UAC 同意。需要单独说明的是，Codex CLI 自身仍保留受 `read-only` sandbox 约束的模型 shell；临时工作目录、`--ignore-user-config` 和 prompt 中“不要使用工具”的指令，都不能证明它只看得到 HandsFreePC 提供的最小 context。开启 Codex planner 前应把主机只读文件可见性视为残余风险。
- 两个 adapter 都使用空临时工作目录，避免自动带入项目级上下文；启动/超时错误返回泛化信息，不回显原始 prompt 或 provider stderr。Claude 空 tools 提供更窄工具面；Codex read-only shell 仍可能读取当前用户可见文件。
- 云规划默认关闭。打开后，命令转写文本和最小必要上下文会发送到所选提供商；**原始音频仍不发送，但文本会离开本机**。详见根目录的 `PRIVACY.md`。

2026-08-30 的发布机 smoke test 中，Codex 通过现有 ChatGPT 订阅登录完成了结构化规划；Claude 订阅模式因本机 OAuth 已过期而在认证阶段失败，程序没有改用环境中的 API key 兜底。这个结果只证明当时的认证/调用路径，不是两个模型能力高低的对照实验，也不能替代目标 UI 的 live test。

## 6. 反馈模式

首版定义四种模式，均可用语音切换：

- `overlay`：默认。屏幕顶层显示高对比大字，不抢输入焦点；适合看得到屏幕但不希望出声。
- `voice`：用 Windows 已安装的本机语音合成引擎读出短反馈；实际离线性和中文声音取决于本机已安装语音，需要安装后实测。
- `both`：同时显示并朗读。
- `silent`：只保留必要状态，不主动打扰；风险确认仍必须有可感知提示。

当前识别反馈会把完整转写显示在 `overlay` / `both`，并在 `voice` / `both` 中朗读，因此口述路径/项目名可能被旁观或旁听。TTS 是半双工的：播放期间暂停识别/命令处理，全部队列结束后丢弃同期麦克风缓冲；用户必须等提示播完再说下一句，否则可能被丢弃。SAPI worker/COM 错误当前也不会传播成 UI/退出失败，纯 `voice` 模式可能静默；每台机器需人工听测，默认 overlay 更稳妥。

进入听写时，默认由 HandsFreePC 自己转写并写入已核验的输入框；只有带控制前缀且整句精确匹配的“电脑发送提示”才提交，否定句不会提交。全局停止短语是高优先级子串控制，所以听写内容说出完整停止短语仍可能暂停。只有明确说“打开应用内语音”时，才尝试已校准的按钮/热键；公开 Codex/Claude 配置的语音按钮名为空、热键为 `null`，当前桌面 selector 尚未验证。`start_native_voice` 必须是计划中唯一一次且位于最后一步，不能和反馈模式切换组合；非法组合在执行前阻断并回 `ARMED`。合法计划经确认后先等此前 TTS 队列清空；一旦进入执行尝试，执行中/成功/失败提示都 overlay-only，成功或失败均保守进入 `PAUSED`。项目仍无法验证第三方麦克风何时真正停止。

HandsFreePC 的 blocked keyword 和动作 Schema 只约束本地计划。一旦用户明确提交 composer 中已有 prompt，下游 Codex/Claude agent 的能力由它自己的 sandbox、approval 和 permissions 决定；这是另一条权限边界，部署时应单独最小化。

## 7. 精确性验收边界

公开版不能用“能打开一次”代替可靠性。首版测试目标应包括：

- 精确路径样例全部打开正确目标；同名候选全部触发消歧；
- 任何前台窗口变化注入都不得把文字输入错误窗口；
- UAC、高完整性窗口、密码控件和非 `Default` 输入桌面全部失败关闭；锁屏时 `open_path` 也不得调度；
- Codex/Claude 应用升级后重新检查 UIA 树和选择器；
- 不同 DPI、主题、单/双屏、最小化、遮挡、睡眠恢复和麦克风拔插有独立 smoke test；
- 使用真实家庭噪声样本统计误唤醒和漏唤醒，而不是只在安静房间测试；
- live 测试必须显式启用，因为它可能抢焦点、打开窗口或使用麦克风。

当前项目是 Windows 11 alpha，不宣称已经覆盖任意第三方应用、任意自绘控件或安全桌面。发布机已通过短 SAPI、遮罩可见且不抢焦点、公开 `examples` 目录 Explorer dispatch、`Default` 输入桌面和 Codex 订阅 planner；Claude 清洗环境后的订阅认证不可用。Codex/Claude 项目/对话/Design/原生语音 selector、多 DPI 和真实家庭噪声尚未验证。正式验收需要逐动作采用与上文相符的证据边界，不能把按键调度或 Shell dispatch 写成下游应用端到端成功。
