# HandsFreePC 隐私说明

> **最重要的一句话：默认语音识别在本机完成，原始音频和转写不落盘，Computer Use 与云 planner 均关闭。只有你在私有本地配置中显式满足四项门禁后，连续 `over` prompt、目标窗口元数据、辅助功能树、截图及可见内容等才可能由 OpenAI 处理；原始音频仍不发送。连续 controller 复用非 ephemeral Codex thread，`save_transcripts: false` 不控制 Codex/提供商记录。**

HandsFreePC 面向“持续开着麦克风”的使用场景，因此把本地优先和可见状态作为产品边界，而不是只写在宣传语里。本文描述开源 alpha 的默认行为，不替代你所使用的 Windows、Codex、Claude、模型下载站或其他依赖的隐私条款。

## 默认数据流

| 数据 | 默认处理位置 | 默认是否保存 | 默认是否发送网络 |
|---|---|---:|---:|
| 麦克风原始 PCM | 当前 Windows 用户进程内存 | 否 | 否 |
| 开始/结束/急停/确认等控制词 | 本机 Vosk | 否 | 否 |
| 命令语音 | 本机 Silero VAD + SenseVoice；faster-whisper 异常后备默认未安装且关闭 | 否 | 默认否；显式预下载/首次后备可能访问模型站 |
| 连续命令转写与未完成 prompt | 当前进程内存 | 否 | 默认否；显式开启 Computer Use 后，每条 `over` prompt 发给 OpenAI |
| 兼容路径、窗口和 UIA 候选 | 本机解析与核验 | 默认不记录完整值 | 默认否；兼容云 planner 只发送必要上下文 |
| Computer Use 屏幕上下文 | Codex/Computer Use；可能含窗口元数据、UIA、目标窗口截图、可见内容和剪贴板状态 | HandsFreePC 不主动保存；Codex/提供商边界另算 | 默认否；四项门禁满足并执行任务后可能发送 OpenAI |
| 连续队列与 Codex thread id | 当前进程内存；Codex CLI 另有自己的 thread/history | HandsFreePC 队列不持久化；Codex 侧不由本项目控制 | 默认否；启用后 Codex thread/输出经网络处理 |
| 屏幕反馈 | 本机大字遮罩 | 否 | 否 |
| 语音反馈 | Windows 已安装的本机 TTS | 否 | 项目本身不发送；具体语音包应在本机验证 |
| 模型/依赖下载 | 官方下载源 | 模型与软件会保存到本机 | 首次安装/更新需要网络 |

默认配置为：

```yaml
privacy:
  save_audio: false
  save_transcripts: false
  redact_paths_in_logs: true
  allow_cloud_planner: false

planner:
  enabled: false

computer_control:
  enabled: false
  allow_screen_context_to_cloud: false

speech:
  fallback:
    backend: none

execution:
  dry_run: true
```

兼容文本 planner 要求 `planner.enabled: true` 与 `privacy.allow_cloud_planner: true` 同时出现。连续 Computer Use 可保持 `planner.enabled: false`，但启用时必须同时设置 `computer_control.enabled: true`、`privacy.allow_cloud_planner: true`、`computer_control.allow_screen_context_to_cloud: true` 和 `execution.dry_run: false`。缺任一云许可或仍保留 dry-run 标签都会被配置加载器拒绝。只预设某个许可而不打开相应总开关不会发送请求；真实选择只应写入被 Git 忽略的 `config.local.yaml`。

`dry_run` 主要阻止兼容 Windows 动作，不是 Computer Use 的隐私或能力沙箱。直接 `run` 仍会打开麦克风和反馈；双重开启的兼容 planner 仍可能联网。

## 常开麦克风实际上做什么

公开默认关闭 Computer Use 时，仍运行 0.1 兼容的一次唤醒/命令状态机。显式启用 Computer Use 后，说“开始语音操作”进入 `ACTIVE`：SenseVoice 把语音切成多个本地 fragment，只有独立英文 `over` 前的完整 prompt 才进入有界 FIFO。说“结束语音操作”会丢弃未完成半条、拒绝新普通 prompt 并进入 `DRAINING`，默认继续排空当前与已接受队列；急停才请求取消当前并清空待处理。

结束、排空或 `PAUSED` 并不等于麦克风关闭：本地 Vosk 仍接收急停、确认和队列恢复等控制词。只有退出进程或关闭 Windows 麦克风权限，才可靠停止采集。

“不保存”是指 HandsFreePC 默认不会创建录音文件或转写历史文件。它不代表声音从未进入内存，也不代表操作系统音频驱动、杀毒软件、调试器或你安装的其他录音软件不会访问麦克风。

`overlay`、`voice`、`both`、`silent` 在连续与兼容路径都可用，连续模式的固定切换句由本地处理。遮罩或 SAPI 可能显示/朗读识别文本、队列状态、错误以及 Computer Use 返回的待确认动作描述；其中可能含路径、项目名、窗口内容摘要或其他敏感词。`silent` 会隐藏普通反馈，但确认和错误仍可能强制显示。请按旁观/旁听风险选择，不要口述或让目标窗口显示不应暴露的信息。

TTS 是半双工的。连续模式把语音反馈延迟到 utterance 边界，并把该边界前的待播项按优先级合并，只播最高优先级中的最新一条；其余不保证逐条播完。兼容模式也在完整待播队列期间暂停识别。播放结束后会丢弃期间进入的音频缓冲，连续路径还会重置控制 detector。使用 `voice` / `both` 时必须等播报结束再说，否则提前说的话可能被丢弃；播放期间不能靠语音急停。纯 `voice` 模式中的高风险确认只有在完整提示成功播完后才解锁并启动确认有效期；可见模式从提示实际显示起计时。过早确认不授权，SAPI 失败会强制提示先切 `overlay`/`both`。超时在下一段本地语音到来时检查并取消本轮/队列，而不是由后台 Timer 主动弹出。希望连续快速口述时选择 `overlay`；语音反馈也不能当作逐条审计记录。若无法接受旁人的声音短暂进入本机内存，请退出进程并使用系统麦克风隐私开关。

## 开启兼容 Codex/Claude 文本 planner 后会发送什么

确定性 parser 无法理解命令时，且你已经双重 opt-in，HandsFreePC 会构造一个规划 prompt。当前运行时发送：

- 这一次命令的转写文本；
- 当前状态、已配置的 app 名称和反馈模式；
- HandsFreePC 的受限动作 Schema 和安全规则。

`parse --use-planner` 只另外提供已配置 app 名称，不提供运行时状态/反馈模式。0.2.0 的兼容 planner 不把路径/UIA 消歧候选列表发送给 provider。

HandsFreePC 自己构造的**兼容 planner prompt**不主动包含：

- 原始音频；
- 连续背景谈话；
- 全桌面截图；
- 剪贴板；
- 全量目录清单；
- 无关日志和配置；
- 名称包含常见 `API_KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL` 标记的环境变量。

环境变量名称过滤只是纵深防御，不是秘密扫描器。如果你在语音命令里直接说出密码、病历、学生信息、客户数据或令牌，那段文本仍可能进入 planner。并且对 Codex 而言，“不放进 prompt”不等于 CLI 工具无法读取：见下一节。上述“不主动包含截图/剪贴板”只适用于兼容 planner，不能拿来描述 Computer Use。

另一个独立网络边界是用户主动提交：即使 HandsFreePC 的 planner 关闭，只要你在 Codex/Claude composer 中明确说完整控制命令“电脑发送提示”，已经听写的 prompt 就会由对应应用发送到其提供商。HandsFreePC 的 `blocked_keywords` 不约束下游 agent；下游能做什么和如何保留数据由它自己的 sandbox、approval、permissions、账户和提供商政策决定。请为下游使用最小权限，也不要在待提交文本中放入不应外发的信息。

## 开启连续 Codex Computer Use 后会发送什么

每条以 `over` 完成的 prompt 会进入同一语音轮次的 Codex thread。为执行和自检 GUI 动作，Computer Use 可能处理：

- 完整的已识别 prompt 与后续确认 continuation；
- 目标窗口标题/元数据、辅助功能树和新目标窗口截图；
- 目标窗口中的可见文字、图像、输入状态，以及 Computer Use 能访问的剪贴板状态；
- 每轮模型输出、严格状态行和 Codex thread 上下文。

HandsFreePC 不把原始 PCM 音频发送给 Codex；房间声音先在本机转写，只有完成的 prompt 和本地控制产生的 continuation 进入 controller。但屏幕许可不是“只发用户刚说的名词”：目标窗口内无关但可见的私人内容也可能进入 UIA/截图。Windows 目标应用必须在 active desktop 可见，Computer Use 操作时会占用 foreground 并移动鼠标/键盘；不应把敏感窗口留在目标旁边或依赖后台无感运行。

首次控制某个应用可能需要 per-app approval；`Always allow` 会成为 Codex 自己的持久权限决定，与 HandsFreePC YAML 的三项开关和 dry-run 值相互独立。关闭项目开关或删除 `config.local.yaml` 不会自动撤销 Codex 已保存的 app approval；请在 Codex/ChatGPT 设置中单独核对或撤销。详见 [OpenAI Computer Use 文档](https://learn.chatgpt.com/docs/computer-use)。

## Codex 与 Claude 的本地/云边界

### 兼容 Codex planner

适配器使用 `codex exec --ephemeral`，在临时目录中运行，忽略用户配置/规则并要求只读沙箱与结构化输出。`--ephemeral` 的含义是这次 CLI 运行不在本机持久化 rollout；它**不等于** OpenAI 服务端零保留，也不覆盖你的账户、订阅、组织或 API 数据设置。

Codex 的 `--sandbox read-only` 不是无工具模式：CLI 仍可执行模型生成的只读 shell 命令，并可能读取当前用户可见的本机文件。空临时工作目录和 prompt 中的“不要使用工具”只是减小暴露面，不能证明 Codex 只看见上文列出的最小 context。0.2.0 没有额外的 AppContainer/低权限账户隔离；如果机器上有敏感文件，请不要开启 Codex planner。

OpenAI API key 用户可查阅 [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)。使用 ChatGPT 订阅登录 Codex 的用户应在自己的 ChatGPT/Codex 账户中核对当前数据控制；不同账户类型的规则可能不同。

### 连续 Codex Computer Use controller

第一条任务使用 `codex exec --json` 建立 thread，后续任务和确认使用 `codex exec resume <thread-id> --json`。为了加载 Computer Use skill，controller 保留用户 Codex 配置和插件，不使用兼容 planner 的 `--ephemeral --ignore-user-config` 隔离。正常排空或急停会关闭 controller 并丢弃 HandsFreePC 内存中的 thread 引用；这不表示 Codex CLI 或 OpenAI 端历史被删除。

controller 使用 `--sandbox read-only` 限制 shell 文件写入，并通过 prompt 禁止用 shell/终端替代 UI 操作；这不会阻止鼠标键盘改变其他应用，也不会阻止 Codex/插件读取用户账户可见信息。环境中带常见 secret 名称的变量会被过滤，临时 last-message 文件会在读取后删除；用户 Codex 配置、插件、历史、缓存、app approvals 和提供商保留均不由这两项措施清除。

最终消息必须为单行、不超过 600 字、无 Unicode C 类控制字符，并以 `VERIFIED_COMPLETION:`、`NEEDS_CONFIRMATION:` 或 `FAILURE:` 开头；待确认 description 另限 160 字。confirmation continuation 使用 JSON string 引用该描述。这是协议与注入面收窄，不是独立视觉隐私或正确性验证：状态与屏幕后置条件仍由同一个 agent 产生。

### 兼容 Claude planner

适配器使用 `--no-session-persistence`、`--safe-mode` 和空工具集，避免为这次 planner 调用保存可恢复的本地 Claude 会话，并提供比 Codex read-only shell 更窄的本机工具面。它仍需要有效的 Claude OAuth/登录。Anthropic 官方明确说明：本地 Claude Code 与模型交互时会通过网络发送用户 prompt 和模型输出，消费者与商业账户有不同训练、保留和 ZDR 规则。[Claude Code data usage](https://code.claude.com/docs/en/data-usage)

### 共同说明

- Codex/Claude CLI 自己仍需认证并与提供商通信；它们可能有独立的更新检查、错误报告或遥测设置。
- 两个兼容 planner adapter 都使用空临时 cwd，减少自动带入项目级上下文；这不适用于连续 controller，也不是 Codex 主机文件隔离。启动/超时错误会泛化，不回显原始 prompt 或 provider stderr。
- 普通 `doctor` 只检查 CLI 是否存在，不执行提供商认证命令；显式使用 `doctor --check-planner-auth` 才会运行 Codex/Claude 的认证状态检查，这可能与提供商通信。
- HandsFreePC 不代理、修改或保证提供商的数据保留政策。
- 使用“订阅额度”不意味着数据只在本机，也不意味着零保留。
- 提供商规则会变化；使用前请重新阅读对应官方文档和账户设置。

## 本地持久化

项目默认可能保存的非内容数据包括：

- 你自己创建的 `config.local.yaml`，或 `%LOCALAPPDATA%\HandsFreePC\config.yaml`；
- 下载的 ASR/VAD 模型权重及其 LICENSE/README；
- 0.2.0 本身不创建持久化的音频/转写日志；连续 prompt 队列与 HandsFreePC 保存的 thread id 只在进程内存中。配置中的 `redact_paths_in_logs` 是为未来日志实现保留的策略位，不能自动清理外部工具输出；
- 由 Python、Windows 或上游 CLI 自己维护的安装缓存和诊断数据。

Codex CLI 的 thread/history、用户配置、插件缓存、app approval 和提供商端记录是另一套持久化边界。`save_transcripts: false`、正常 drain、急停或退出 HandsFreePC 都不会保证删除这些记录；请用 Codex/ChatGPT 自己的数据控制和清理功能管理。

普通安装不包含 faster-whisper，默认 `backend: none`。`-WithWhisper` 只安装代码依赖；显式构造 `large-v3-turbo` 会从模型托管站下载并缓存 GB 级权重。当前异常后备只在已构造 SenseVoice 的 `transcribe()` 抛错时触发；如果未预下载，首次触发时可能联网。它不处理空/低置信度结果，也不能补救 SenseVoice 启动/模型加载失败。这组可选权重不属于三个默认固定 SHA 模型，应另行阅读其托管仓库/模型条款。

`doctor`、`test-asr` 等 CLI 的 stdout/JSON 可能包含配置、模型或输入文件的绝对路径。它们不是 HandsFreePC 自动保存的日志，但如果你重定向、复制或上传输出，就会形成新的持久化副本；分享前必须人工脱敏。

0.2.0 没有实现录音/转写持久化器；`save_audio` 和 `save_transcripts` 目前是默认关闭的策略保留位，即使改成 `true` 也不构成一个受支持的录音功能。未来若实现诊断保存，仍必须增加明确的单次 opt-in、输出位置和删除提示，不能只依赖这两个布尔值悄悄开始记录。

不得把以下内容提交到 Git：录音、转写、本机绝对路径、模型权重、令牌、登录缓存、日志或 `config.local.yaml`。

如需诊断真实音频，必须由用户针对一次测试明确开启、指定本地输出位置、了解其中可能录到旁人，并在问题解决后自行安全删除。公开 Issue 和安全报告中只能提供脱敏后的最小复现。

## 删除与停用

- 结束输入：“结束语音操作”只拒绝新普通 prompt，并默认排空当前/队列；麦克风仍监听本地急停/确认/恢复。急停会请求取消并清队列，但不能撤回已发生副作用。两者都不会删除 Codex/提供商 thread。
- 完全停止采集：退出 HandsFreePC 进程或关闭 Windows 麦克风权限，并参考 Windows 自己的麦克风使用指示。0.2.0 没有持久状态页/托盘图标，`silent` 下普通反馈也可能不可见。
- 删除本项目配置：检查项目目录中的 `config.local.yaml` 和 `%LOCALAPPDATA%\HandsFreePC\config.yaml`。
- 删除模型：删除你在配置中指定的 `models` 目录；下次使用相应 ASR 前需重新下载。
- Codex/Claude 自己的本地会话、缓存、app approvals 和云端数据不由 HandsFreePC 管理；请使用各自 CLI/账户提供的删除和数据控制功能。

删除前先核对绝对路径，不要对用户目录或磁盘根目录运行递归删除命令。

## 旁人与家庭场景

常开麦克风会短暂处理房间中所有人的声音，包括孩子、来访者和远程通话中的声音。即使默认不保存，这仍可能涉及告知、同意和当地法律要求。

建议：

- 默认使用遮罩并留意 Windows 麦克风指示；0.2.0 的遮罩是短暂反馈、没有托盘图标，持续可见的监听指示仍是待补能力；
- 在访客、医疗、教育、会议或保密谈话时暂停；
- 不把唤醒词设成日常高频句子；
- 扬声器播放含控制词的视频时暂停，或把高风险动作保持阻断；
- 不用本项目记录、推断或识别儿童/旁人的身份。

## 本项目不做什么

- 不出售音频或转写；
- 不提供广告追踪；
- 不在默认模式下把音频上传到 OpenAI、Anthropic 或其他 ASR 服务；
- 不用本项目采集的数据训练模型；
- 不在后台自动打开云 planner 或 Computer Use；
- 不承诺第三方依赖或提供商具有相同的数据政策。

隐私问题可通过公开 Issue 提交脱敏后的普通问题；若问题可能构成漏洞或会暴露个人数据，请按 `SECURITY.md` 使用私密报告渠道。

---

**English summary:** Audio recognition is local by default, raw audio and transcripts are not saved by HandsFreePC, and both cloud planning and Computer Use are disabled. Enabling continuous Codex Computer Use requires four explicit local settings; completed voice prompts, target-window metadata, accessibility trees, screenshots, visible content, clipboard state, and agent/thread output may then be processed by OpenAI, while raw audio remains local. The controller resumes a non-ephemeral Codex thread and keeps user config/plugins; `save_transcripts: false`, drain, emergency stop, or deleting local YAML does not erase Codex/provider history or app approvals. Provider retention depends on the account and settings. Real-screen Computer Use has not been validated by this release.
