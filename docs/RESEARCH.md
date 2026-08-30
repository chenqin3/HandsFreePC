# HandsFreePC 技术调研与选型

> 外部调研快照：2026-08-30；0.3 实现对齐：2026-08-31。本文把厂商/上游项目公开资料中的**已核实事实**、HandsFreePC 0.3 的**当前工程选择**和早期版本的**历史验证记录**分开陈述。外部能力会变化，发布前应重新核对链接和本机版本；标成“历史”的结果不能替 0.3 背书。
>
> 当前实现的规范性说明以 [0.3 架构](ARCHITECTURE.md)、[安全模型](SECURITY_MODEL.md) 和 [测试指南](TESTING.md) 为准。

## 结论先行

HandsFreePC 不应把“常开麦克风、理解命令、操控桌面、判断成功”全部交给一个云端 Agent。0.3 采用的组合是：

1. 单一麦克风采集留在本机，由 Vosk 识别控制词，Silero VAD 与 SenseVoice 转写正文；
2. “开始语音操作”进入连续会话，正文中的独立 `over` 完成一条 prompt，并进入有界 FIFO；
3. 完整命中的常见命令先走 `NativeSkillRouter`、确定性解析和本地白名单执行器；
4. 只有确定性 miss、用户在原句中肯定且只指定一个应用、并显式允许转写与屏幕上下文离机时，默认 Claude CLI 才规划**下一步**；Codex CLI 只作额外同意主机读取风险后的 best-effort 备选；
5. planner 每次只能返回一个结构化决定，不持有鼠标键盘，也不能通过动作 Schema 请求 shell、坐标点击或任意脚本；
6. 项目自有 `DesktopDriver` 对每个通用 planner 动作强制任务后置条件 false-before、执行一次、fresh observe 后 true-after，再由本地策略和 `DesktopVerifier` 验收；确定性 native skill 使用动作特定证据，并允许精确状态已成立时幂等成功；
7. 通用文本输入一律使用绑定到确切动作与界面快照的随机四位一次性确认；点击/按键上下文被本地词形识别为发送、删除、安装、上传/分享、关闭等副作用时也确认，但词表不是完整语义证明，重要任务仍需监督；静态“确认执行”无效；
8. 只有本地验收通过才返回 `LOCAL_VERIFIED_COMPLETION`；planner 的 `done` 和 driver 的 `accepted` 都不是成功证据；
9. 默认用不抢焦点的大字遮罩反馈，可切换为本机语音、两者同时或静默。

这意味着 Codex/Claude 是可替换的单步“规划器”，不是常驻麦克风、权限管理器、桌面执行器或成功裁判。准确性主要来自确定性路径解析、可访问性树定位、歧义拒绝、observation generation 绑定和动作后的本地验证，而不是单纯换一个更大的模型。旧 `legacy_codex_cli` 仍可显式选择，但同一 agent 自报的 `VERIFIED_COMPLETION` 不属于 0.3 的可信验收路径。

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
| Vosk `vosk-model-small-cn-0.22` | 离线中文模型约 42 MB；Vosk 支持运行时 grammar/短语集合；模型列表标注 Apache-2.0 | 低资源、常开、有限词表的开始/结束/急停/确认等控制词检测 | 首选控制词层 |
| sherpa-onnx SenseVoiceSmall INT8 | 官方预训练页列出普通话、粤语、英语、日语、韩语，支持 `use_itn` 标点/文本归一化以及麦克风/VAD 示例；INT8 包约 228 MB | 连续会话中的正文片段与 `over` 转写 | 首选正文 ASR |
| faster-whisper | 基于 CTranslate2 的本地 Whisper 实现，支持 CPU/GPU 和量化配置；资源消耗明显高于小型 KWS | 可作为另一种本地 ASR 或异常后备 | 默认不安装、不启用；当前兼容后备只在 SenseVoice `transcribe()` 抛异常时触发 |
| Silero VAD v6.2.1 | 本地 ONNX 语音活动检测，上游 MIT 许可；sherpa-onnx 可直接加载模型 | 更稳的起止点检测、减少环境噪声误切句 | 默认起止点检测；自适应能量门限作为无模型后备 |

官方资料与下载入口：

- [Vosk 模型列表与许可](https://alphacephei.com/vosk/models)；[small-cn-0.22 模型包](https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip)；[Vosk grammar API](https://github.com/alphacep/vosk-api/blob/master/src/vosk_api.h)
- [sherpa-onnx SenseVoice 预训练模型](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)；[2024-07-17 INT8 模型包](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2)
- [SYSTRAN faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Silero VAD v6.2.1](https://github.com/snakers4/silero-vad/tree/v6.2.1)

### 工程选择

0.3 仍使用 16 kHz、单声道 PCM，但会话协议已经不是“唤醒后只转写一条命令”。单一音频采集在本机同时支持控制词检测和正文切句：“开始语音操作”进入持续 `ACTIVE` 会话，SenseVoice fragment 交给 `PromptAssembler`；只有正文 ASR 识别到独立 `over` 后，前面的非空 prompt 才进入有界 FIFO。执行线程与采集线程分离，因此前一条执行时仍可继续接收下一条，普通任务由一个 worker 串行执行。

当前 `over` 仍依赖正文 SenseVoice，不是独立 KWS。0.3 只提供 `PromptAssembler.finalize()` 这一未来 KWS 可调用的 out-of-band seam；真正接入时必须在单一采集流后做 audio block fan-out、统一时间戳、命中前音频回灌和去重，不能另开第二个麦克风流。候选 sherpa-onnx KWS 模型的许可归属仍待澄清，所以 0.3 不自动下载或声称已经启用。

默认由 sherpa-onnx 加载固定为 v6.2.1 的 Silero ONNX 做话语起止点检测；若用户明确把 `speech.vad.backend` 配成 `energy`，则使用可校准的自适应能量门限后备。这一分层比“让大模型一直听”更省资源，也让原始音频默认不必离开机器。公开默认 `speech.fallback.backend: none`，普通安装只装 `audio` 与 `windows` extras；`-WithWhisper` 才安装 faster-whisper。启用者应先显式预下载 `large-v3-turbo`，因为它会产生 GB 级网络下载/缓存和明显资源开销。当前实现不按空文本、低置信度或长句自动切换，只在已经构造 SenseVoice 后、某次 `transcribe()` 抛异常时延迟构造 Whisper；SenseVoice 启动/模型加载失败不能由这条后备补救，自动化套件也尚未覆盖该分支。

### 历史验证记录（2026-08-30，早期运行时）

当时的发布机曾通过 SenseVoice 官方样例（转写为“开饭时间早上9点至下午5点。”）、Vosk 合成唤醒/停止、Silero 官方样例、16 kHz 真实麦克风读取和早期本地运行时启动/停止。这些结果只证明当时那台机器的组件链路可运行；它们没有覆盖 0.3 的持续会话、`over`/FIFO、项目自有桌面 agent，也不等于远场、家庭噪声、婴儿声、方言或其他音频设备已经验证。

### 模型许可提醒

Vosk 官方模型页把 small-cn-0.22 标为 Apache-2.0，但该模型 zip 可能只有 README、没有完整许可文本；再分发时应同时保存 [Vosk v0.3.45 COPYING](https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING)、模型来源和下载哈希。

sherpa-onnx 运行时代码与 SenseVoice 权重不是同一许可对象。发布脚本不得把 SenseVoice 权重打包成项目自有资产；模型包内的短 LICENSE 可能只是链接，下载后应额外保存完整 [FunASR Model License](https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE)，注明 SenseVoiceSmall、FunASR/FunAudioLLM 和 Alibaba Group，并保留模型名。

## 3. “底层灵敏”应该怎样实现

### 已核实事实

Windows Service 从 Vista 起运行在隔离的 Session 0，不适合直接与当前用户桌面交互。[Microsoft：Interactive Services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

### 工程选择

HandsFreePC 是登录用户会话中的普通常驻进程，不是系统服务，也不要求管理员权限：

- 麦克风采集、UI 观察和实际动作都留在当前交互用户会话；默认不写音频或转写文件；
- 会话状态以 `ARMED -> ACTIVE -> DRAINING/PAUSED` 为主：开始口令接收任务，结束口令拒绝新任务、丢弃未完成半条并排空已接受队列，急停请求取消当前任务并清空待处理任务；
- 音频采集和单 worker 执行位于不同线程；急停是 cooperative cancellation，不能撤回已经到达 Windows 或外部服务的一次副作用；
- Vosk 本地检测开始、结束、急停、确认和恢复等控制词；没有说话人识别，结束会话、`silent` 和 drain 都不等于关闭麦克风；
- 默认 Windows UIA driver 在真实动作前要求当前输入桌面为 interactive `Default`，并复核已配置目标窗口的 HWND 为前台；锁屏、Winlogon 和 UAC secure desktop 会被阻断，普通权限进程受 UIPI 限制而无法可靠输入更高完整性窗口时必须按动作失败处理；
- TTS 仍为半双工：播放期间暂停识别/命令处理，结束后清理同期缓冲，且语音急停不能打断正在播放的 SAPI。确认反馈若未真正显示或播报成功，运行时不会把它当成已交付确认提示。

### 历史实现说明（0.1.0）

早期实现以 `ARMED`、`AWAKE`、`DICTATION`、`CONFIRMING`、`PAUSED` 描述单轮唤醒、听写和应用内语音流程，并记录过停止词无法抢占同步执行/TTS、锁屏没有会话事件自动暂停等限制。这些细节仍可用于理解兼容的确定性听写路径，但不能用来描述 0.3 的主入口；0.3 的公开主流程是持续会话、`over` 分段、有界 FIFO 和 `DesktopAgentLoopController`。

## 4. Windows 操作技术路线

| 层级 | 已核实事实 | 本项目用法 |
|---|---|---|
| 原生 Windows handler | ShellExecute 可按系统文件关联打开文档和目录。[Microsoft Shell launch](https://learn.microsoft.com/en-us/windows/win32/shell/launch) | `NativeSkillRouter` 完整命中后，先做路径存在性、唯一性和最终扩展名风险校验，再交给本地白名单执行器；不拼接 shell 命令 |
| UI Automation | UIA 可按语义读取元素并调用控件模式 | 0.3 默认 `windows_uia` driver 读取不可变 observation、绑定元素 index、执行一个语义动作并 fresh observe |
| pywinauto | Python 的 Win32/UIA 自动化库，支持 `uia` 与 `win32` 后端。[pywinauto 文档](https://pywinauto.readthedocs.io/en/latest/) | 项目自有 Win32/UIA driver 的基础库；动作仍受本地 safety 和 verifier 约束 |
| WinApp CLI | 微软工具可搜索、调用、设值、等待、截图并输出 JSON；官方页面仍标为 Public Preview，Electron 支持有限。[WinApp UI Automation](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation) | 调研中的后续候选；0.3 不把它作为默认或唯一依赖 |
| 局部视觉 | Windows 可捕获单个窗口画面。[Windows Graphics Capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture) | 默认 driver 不依赖视觉；实验 MCP driver 可能在本地取得截图，但 safety 重建的 planner view 会移除截图，项目构造的云 prompt 不发送 PNG bytes 或真实截图可用性，也不允许纯坐标点击 |

`SetForegroundWindow` 受 Windows 防抢焦点规则限制，所以“请求激活”失败必须是正常错误分支；不能激活后仍盲目发按键。[SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow) `SendInput` 还受 UIPI 完整性级别限制；普通权限进程不能可靠地向更高权限窗口注入输入。[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

Qwen open-computer-use 0.2.3 虽暴露多种 MCP 动作，但当前 `get_app_state` 结果没有被适配成可安全绑定的结构化 element list。严格 Schema 要求动作引用 element index，因此 planner 驱动的点击/导航能力受限；通用 `type_text`/`set_value` 也会因找不到可验证的焦点元素而失败关闭。它只能作为实验兼容层，不能替代默认 `windows_uia` 的语义元素证据。

Windows 提供 [`OpenInputDesktop`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop) 和 [`GetUserObjectInformation`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw) 查询当前输入桌面。项目从 0.1.0 起把名称严格等于 `Default` 作为真实 OS/UI 动作的前置条件，0.3 默认 driver 继续保留这项门禁；这不代表 Windows 会替任意应用自动采用同一策略。

### 工程选择

0.3 的执行顺序固定为：

1. 路径、已配置应用或固定听写能被确定性 parser 完整覆盖时，由 `NativeSkillRouter` 先解析全部目标、完成风险分类并执行；parser 只匹配前半句时视为 miss，不能静默丢掉后半句；
2. UNC / `//server`、URI 和 Win32 device namespace 在任何文件系统访问前阻断，只接受本地路径合同；模糊路径解析到最终目标后按最终扩展名重新判级；
3. 确定性 miss 才进入通用 agent loop；用户原句必须肯定且只明确指定一个已配置应用。完整 observation 先在本地检查，再重建为只含本句肯定且精确点名控件的 task-authorized 子集，planner 只能从该子集选择一个 `observe`、`action`、`done` 或 `fail` 决定；
4. 默认 driver 只接受配置过的应用 profile，按进程名和标题寻找唯一窗口；多窗口只有唯一前台匹配时接受，否则拒绝歧义；
5. observation 的元素 index 绑定 `app + HWND + generation`。一次动作后旧 index 立即失效，必须 fresh observe；
6. 动作只允许 `click`、`perform_secondary_action`、`scroll`、`type_text`、`press_key` 和 `set_value`。已被本地词形/属性识别的认证、密码、terminal/shell、UAC/Windows Security、付款、隐私/公开链接 surface，以及剪贴板和无语义目标的坐标操作 fail closed；
7. 通用 `type_text`/`set_value` 全部需要确认；点击/按键上下文命中已知发送/提交、删除、安装/卸载、上传/共享、关闭词形时也先生成绑定参数、expectation 和 observation fingerprint 的 confirmation。副作用词表不是完整语义证明，未知词形可能漏分；确认前 fresh observe 并重绑定新 generation，再由 runtime 加上随机四位一次性码；
8. 每个通用动作先 fresh observe，确认任务相关 expectation 此时为 false，再执行一次；driver `accepted` 只说明动作已发出。随后 `DesktopVerifier` 要求更高 generation、同一应用、状态 fingerprint 变化且同一 expectation 为 true；`type_text`/`set_value` 还必须在新 UIA 状态中看到精确文本；
9. planner 的 `done` 只能给出本地可检查的有限 expectation。只有 `DesktopVerifier` 通过才终止为 `LOCAL_VERIFIED_COMPLETION`。

另有一个仅为兼容旧 `VoiceRuntime` 保留的顶层 `planner.enabled` one-shot fallback，不属于上述逐步 desktop planner。其云输出只可提出原句肯定、非引号/数据引用且精确授权的应用内导航（激活、项目/对话/tab/mode、听写、应用内语音）；反馈、暂停/恢复/等待、路径、文本和发送动作即使出现在云 plan 中也会被本地 safety 阻断。需要确认时，运行时绑定完整 plan/source、规范路径、stat 身份和普通文件 SHA-256，并在确认后重新 prepare、重新 safety、重新 binding；任何变化都取消。

早期 0.1.0 的 UIA 研究已经识别出“唯一匹配、前台 HWND、非密码输入、动作后检查”等正确方向，但当时 `SendInput` 接收、Enter 发送或 Shell dispatch 仍可能只是调度证据。0.3 的本地 verifier 提高了动作级证据强度；它仍不能把 UIA 文本变化等同于网络服务已经接受消息、文件已持久保存或其他外部业务后置条件。

确定性 `OPEN_PATH` 当前也采用 false-before/true-after，并要求动作后的前台 HWND 与动作前不同：Explorer 目录再用 Shell.Application 返回的规范化路径做精确比较；普通文件则只检查新前台窗口标题是否包含精确文件名，仍是 best-effort。复用同一 HWND 的查看器会保守失败；同名文件和不显示文件名的查看器仍需人工核对，这不是 exact-path 或 exact-content 证明。

确定性 `OPEN_MODE` 不把稳定语音名直接当作当前 UI 标签。只有 `apps.*.mode_names` 显式列出的 canonical key 才能进入 native route；labels 按配置顺序映射为版本相关的精确 accessible labels（如 `Chat and Cowork`、`Chat`）。缺少映射会在输入前拒绝，绝不把任意用户单词当作按钮名。执行器还拒绝 fuzzy-only 命中，并要求最终 mode 精确标签变为 selected；focus-only 只证明控件收到点击，不证明导航完成。

## 5. Codex `exec` 与 Claude `-p`

### 已核实事实

| 维度 | Codex CLI | Claude Code |
|---|---|---|
| 正确的非交互入口 | `codex exec`，官方标为 Stable | `claude -p` / `--print` |
| 结构化输出 | `--output-schema <file>`；`--json` 可输出 JSONL 事件 | `--json-schema '<schema>'`；`--output-format json/stream-json` |
| 限权相关能力 | `--sandbox read-only`、`--ephemeral`、独立工作目录和若干工具 disable；订阅 CLI 没有完整 no-tools 保证 | `--tools ""`、`--safe-mode`、`--restricted`、严格 MCP 配置、`--no-session-persistence`、权限模式 |
| 容易混淆的参数 | `codex -p` 是 **profile**，不是 prompt | `claude -p` 才是 print 模式 |

来源：[OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)、[OpenAI Codex CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)、[Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)。

官方文档没有提供一个可直接证明“哪个 CLI 更擅长操控本机 Codex/Claude 桌面 UI”的同条件基准。因此不把主观印象写成事实。

### 工程选择

- 0.3 默认 desktop planner 为 Claude `-p`，因为当前 CLI 能显式使用空 tools、restricted/safe mode 与严格 MCP 配置。Codex `exec` 保留为 `codex_cli_best_effort`，只有 `allow_codex_cli_host_read: true` 后才启用；项目不承诺任一订阅覆盖所有调用、固定模型或额度。
- 两个 planner 每次调用只能返回一个严格的 `observe`、`action`、`done` 或 `fail` 决定。一次 response 最多一个动作；多步任务由本地 agent loop 逐轮执行，并受 `max_steps` 与总 timeout 限制。最近最多 8 条**本地已验收历史**可进入上下文，这不是“一次返回 8 步计划”。
- 0.3 桌面动作 Schema 只允许 `click`、`perform_secondary_action`、`scroll`、`type_text`、`press_key` 和 `set_value`。所有通用文本输入都需要本轮随机四位一次性确认；执行前还必须通过当前 observation 绑定、本地界面/动作风险分类、false-before 和动作后 exact true-after 验收。planner 不能请求 shell、坐标字段、密码读取、确认绕过或 UAC 同意。
- Claude adapter 使用独立 system policy、空 tools、safe/restricted 模式、严格 MCP 配置、非交互权限模式和无会话持久化。Codex adapter 使用 ephemeral 空临时目录、结构化输出 Schema、忽略用户配置/规则、过滤环境变量、read-only sandbox 并尽量禁用当前已知工具。两者都没有 HandsFreePC 的 `DesktopDriver`。
- 启动、非零退出、超时、取消或不合 Schema 的输出全部失败关闭，并返回泛化错误，不回显原始 prompt 或 provider stderr。失败不会自动切换到 `legacy_codex_cli`。
- Codex CLI 自身仍是当前用户进程；read-only sandbox、deny list、空目录、环境过滤和 prompt 禁令都不是 no-tools 或主机级秘密隔离保证。Claude 空 tools 提供更窄工具面，但认证、遥测和服务端保留仍由提供商与账户设置决定。
- 云规划默认关闭。打开后，项目构造的 prompt 只包含：完成的一条命令转写、唯一明确授权的可见应用摘要、task-authorized observation generation、最近最多 8 条本地验收历史，以及**本句肯定且精确点名控件**的 index/name/control type/selected/focused/enabled 子集。原始窗口标题、进程 ID、未点名 UI/聊天正文、automation ID、element value、原始音频、截图字节和真实截图可用性不发送；完整 observation 留在本地做 freshness、重绑定与 after-state 验收。该范围只描述 HandsFreePC 主动组装的 prompt，CLI/provider 仍可能附加账户、网络、CLI/OS/runtime、临时工作目录、用量与诊断/遥测等自身元数据。详见根目录的 [PRIVACY.md](../PRIVACY.md)。

### 历史验证记录（2026-08-30，早期 planner）

当时的发布机 smoke test 中，Codex 通过现有 ChatGPT 订阅登录完成了旧结构化规划；Claude 订阅模式当时因本机 OAuth 已过期而在认证阶段失败，程序没有改用环境中的 API key 兜底。这个历史结果不是两个模型能力高低的对照实验，也没有验证 0.3 单步 adapter、项目自有 driver、fresh observation、LocalVerifier 或目标 UI。

2026-08-31 重新登录 Claude CLI 后，发布候选分别用真实 Claude/Codex 订阅 CLI 调用了 0.3 的严格单步 adapter：在无 observation 时，两者都返回了 `observe claude`；在仅含合成 `Chat` 控件的最小 observation 中，两者都返回了一个绑定 index `0` 的 click 和 `element_selected Chat` 后置条件。该记录只验证认证、实际 argv、Schema 与一步规划链路，不包含鼠标键盘动作，也不证明真实 Claude/Codex 界面的 UIA selector 或跨应用控制成功。

0.2 的 Codex Computer Use plugin/thread controller 仍以 `legacy_codex_cli` 名称显式保留。该路径由同一个 agent 观察、执行并输出 `VERIFIED_COMPLETION`，没有 0.3 的项目自有动作级 verifier；factory 不会自动选择或回退到它，新部署不应把它当成可信验收路径。

## 6. 反馈模式

0.3 保留四种模式，均可用语音切换：

- `overlay`：默认。屏幕顶层显示高对比大字，不抢输入焦点；适合看得到屏幕但不希望出声。
- `voice`：用 Windows 已安装的本机语音合成引擎读出短反馈；实际离线性和中文声音取决于本机已安装语音，需要安装后实测。
- `both`：同时显示并朗读。
- `silent`：只保留必要状态，不主动打扰；风险确认仍必须有可感知提示。

反馈可能显示或朗读识别内容、队列状态、错误和确认摘要，因此口述路径/项目名可能被旁观或旁听。TTS 是半双工的：播放期间暂停识别/命令处理，全部播完后丢弃同期麦克风缓冲；用户必须等提示结束再说下一句，否则可能被丢弃。0.3 会检测确认播报失败并强制显示可见错误；但本机是否有可用的中文 SAPI 声音仍需人工听测，默认 `overlay` 更稳妥。

持续会话中的普通任务按 FIFO 进入 `DesktopAgentLoopController`；通用文本输入，以及被本地已知词形/上下文识别为发送、删除等副作用的动作，会暂停队列等待与当前动作/界面绑定的随机四位一次性口令。词表不能穷举副作用语义，重要任务仍需人工看屏幕。用户必须说出提示中的完整“确认执行 + 四位码”；静态“确认执行”无效，runtime 也不会把确认作为新 prompt 交给模型重新解释。同一 `VoiceRuntime` 进程内，已签发码即使取消或超时也不回收，有界重抽耗尽时拒绝；该集合不跨重启持久化，所以随机码不是持久化防重放凭证或说话人认证，也无法阻止旁人、扬声器或实时转述/重放在本轮有效期内代说。急停可请求取消当前任务并清队列，但不能撤回已发生的点击、输入或外部副作用。

兼容的确定性听写/应用内语音路径来自早期版本：HandsFreePC 可进入已核验输入框，“电脑发送提示”由本地 parser 识别；`start_native_voice` 在一个计划中只能出现一次、必须位于最后，且不能和反馈模式切换组合。公开 Codex/Claude profile 没有经验证的原生语音 selector，因此这不是 0.3 通用 desktop agent 的成功证明，也不能替通用任务的 fresh-observation/LocalVerifier 验收背书。

HandsFreePC 的 blocked surface、动作 Schema、typed confirmation 和本地 verifier 只约束 HandsFreePC 自己的控制边界。一旦用户确认提交 composer 中已有 prompt，下游 Codex/Claude agent 的能力仍由它自己的 sandbox、approval 和 permissions 决定；部署时需要单独最小化。

## 7. 精确性验收边界

公开版不能用“能打开一次”、planner `done`、driver `accepted` 或静态 `doctor` 代替可靠性。0.3 把证据分成四层，上一层不能替下一层：

1. 默认自动化：会话/`over`/FIFO、NativeSkillRouter、单步 Schema、generation binding、本地 safety、typed confirmation、fresh observation、verifier、MCP 协议和 fail-closed 分支；
2. 静态 `doctor --strict`：只检查依赖、模型、音频设备、CLI 和配置线索，必须始终保持 `live_control_verified: false` 与 `ready_for_live_control: false`；
3. opt-in `computer-doctor --live`：只打开项目自有无害 fixture，执行含中文随机 token 的 UIA round-trip，并由同一个 `DesktopVerifier` 验收；
4. 目标应用人工受控验收：在非敏感测试账户和可回滚数据上逐版本、逐语言布局验证观察、导航、输入、多步任务、typed confirmation、急停和失败恢复。

持续验收目标还应包括：

- 精确路径样例全部打开正确目标；目录记录自动 exact-path 证据，文件另做人工 exact-target 核对并明确标注 title-based best-effort；同名候选全部触发消歧；
- observation/action generation 不匹配和任何前台窗口变化都不得把文字输入错误窗口；
- UAC、高完整性窗口、密码控件、付款/凭据界面和非 `Default` 输入桌面全部失败关闭；
- `type_text`/`set_value` 必须在 fresh UIA state 中读回精确 Unicode；发送、删除等外部副作用不能只靠按键调度声明成功；
- Codex/Claude 或其他目标应用升级后重新检查 UIA tree、窗口 profile 和本地 completion expectation；
- 不同 DPI、主题、单/双屏、最小化、遮挡、睡眠恢复和麦克风拔插有独立 smoke test；
- 使用真实家庭噪声样本统计误唤醒、漏唤醒和 `over` 漏识别，而不是只在安静房间测试；
- live 测试必须显式启用，因为它可能抢焦点、打开窗口、输入文字或使用麦克风；实验 `open_computer_use` driver 还需要独立的固定版本、Unicode 和断管验收，不能复用默认 UIA fixture 结论。

当前项目是 Windows 11 alpha，不宣称已经覆盖任意第三方应用、任意自绘控件、安全桌面或高价值无人值守任务。正式验收必须逐动作采用与上文相符的证据边界；UIA tree 可能缺少业务状态，应用也可能在验收后继续变化，因此 `LOCAL_VERIFIED_COMPLETION` 是受限的本地证据，不是形式化证明。

### 历史验证记录（2026-08-30，0.3 之前）

早期发布机曾通过短 SAPI、遮罩可见且不抢焦点、公开 `examples` 目录 Explorer dispatch、`Default` 输入桌面和 Codex 订阅 planner；Claude 订阅认证在该历史时点不可用，后来重新登录也不能追溯改变这项旧结果。当时尚未验证 Codex/Claude 项目/对话/Design/原生语音 selector、多 DPI 和真实家庭噪声。这组记录只能解释早期选型，不能证明 0.3 的持续会话、默认 Claude 单步 planner、项目自有 Windows UIA driver、fresh observe、typed confirmation 或 `computer-doctor --live` 已在当前机器通过。
