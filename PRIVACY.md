# HandsFreePC 隐私说明

> **最重要的一句话：默认语音识别在本机完成，原始音频和转写不落盘；但如果你显式开启 Codex 或 Claude 云规划器，命令的转写文本和最小必要上下文会离开电脑。Codex 适配器还保留只读 shell，本机可见文件可能被工具读取；敏感主机应保持云规划关闭。**

HandsFreePC 面向“持续开着麦克风”的使用场景，因此把本地优先和可见状态作为产品边界，而不是只写在宣传语里。本文描述开源 alpha 的默认行为，不替代你所使用的 Windows、Codex、Claude、模型下载站或其他依赖的隐私条款。

## 默认数据流

| 数据 | 默认处理位置 | 默认是否保存 | 默认是否发送网络 |
|---|---|---:|---:|
| 麦克风原始 PCM | 当前 Windows 用户进程内存 | 否 | 否 |
| 唤醒/停止词识别 | 本机 Vosk | 否 | 否 |
| 命令语音 | 本机 Silero VAD + SenseVoice；faster-whisper 异常后备默认未安装且关闭 | 否 | 默认否；显式预下载/首次后备可能访问模型站 |
| 命令转写 | 当前进程内存 | 否 | 否；除非显式开启云规划 |
| 路径、窗口和 UIA 候选 | 本机解析与核验 | 默认不记录完整值 | 否；云规划时只发送必要上下文 |
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

speech:
  fallback:
    backend: none

execution:
  dry_run: true
```

代码要求 `planner.enabled: true` 与 `privacy.allow_cloud_planner: true` **同时**出现才允许云规划。只打开 `planner.enabled` 而没有隐私许可会拒绝启动；只打开 `allow_cloud_planner` 仍不会创建规划器或发送请求。`dry_run` 只阻止真实 Windows 桌面动作：直接 `run` 仍打开麦克风并产生反馈，显式双重开启的 planner 仍可能联网。

## 常开麦克风实际上做什么

休眠状态下，音频只进入短时内存预卷缓冲并交给本地 Vosk 小词表识别器。命中“现在开始语音操作”等唤醒词后，系统才把一段完整话语交给本地命令 ASR。一次性命令会在超时、取消或完成后回到休眠状态；自有听写成功后保持 `DICTATION`。应用内原生语音一旦进入执行尝试，无论成功或失败都保守进入 `PAUSED`；非法组合若在策略阶段阻断则尚未开麦并回 `ARMED`。

“不保存”是指 HandsFreePC 默认不会创建录音文件或转写历史文件。它不代表声音从未进入内存，也不代表操作系统音频驱动、杀毒软件、调试器或你安装的其他录音软件不会访问麦克风。

在 `overlay` 或 `both` 模式下，遮罩会显示完整的“识别：{转写}”；在 `voice` 或 `both` 模式下，SAPI 还会朗读这段完整转写，包括你口述的路径、项目/对话名或其他敏感词。路径动作后续的执行/失败摘要虽会隐藏完整路径，但不能撤回前面的识别反馈。`silent` 会隐藏普通反馈，但确认和错误仍强制显示遮罩；同时 `silent` 下暂停成功可能没有可见确认。请按旁观/旁听风险选择模式，不要口述不应暴露的信息。

TTS 是半双工的：PortAudio callback 仍将声音写入有界内存缓冲，但识别/命令处理暂停；整个待播队列结束后，输入队列和预卷一起丢弃。使用 `voice` / `both` 时必须等“我在听”或确认提示播完再说下一句，否则提前说的话可能被丢弃。若你无法接受旁人的声音短暂进入本机内存，请暂停或退出程序，并使用系统麦克风隐私开关。

## 开启 Codex/Claude 规划后会发送什么

确定性 parser 无法理解命令时，且你已经双重 opt-in，HandsFreePC 会构造一个规划 prompt。当前运行时发送：

- 这一次命令的转写文本；
- 当前状态、已配置的 app 名称和反馈模式；
- HandsFreePC 的受限动作 Schema 和安全规则。

`parse --use-planner` 只另外提供已配置 app 名称，不提供运行时状态/反馈模式。0.1.0 不把路径/UIA 消歧候选列表发送给 planner。

HandsFreePC 自己构造的 planner prompt 不主动包含：

- 原始音频；
- 连续背景谈话；
- 全桌面截图；
- 剪贴板；
- 全量目录清单；
- 无关日志和配置；
- 名称包含常见 `API_KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL` 标记的环境变量。

环境变量名称过滤只是纵深防御，不是秘密扫描器。如果你在语音命令里直接说出密码、病历、学生信息、客户数据或令牌，那段文本仍可能进入 planner。并且对 Codex 而言，“不放进 prompt”不等于 CLI 工具无法读取：见下一节。处理敏感场景时请保持云规划关闭。

另一个独立网络边界是用户主动提交：即使 HandsFreePC 的 planner 关闭，只要你在 Codex/Claude composer 中明确说完整控制命令“电脑发送提示”，已经听写的 prompt 就会由对应应用发送到其提供商。HandsFreePC 的 `blocked_keywords` 不约束下游 agent；下游能做什么和如何保留数据由它自己的 sandbox、approval、permissions、账户和提供商政策决定。请为下游使用最小权限，也不要在待提交文本中放入不应外发的信息。

## Codex 与 Claude 的本地/云边界

### Codex

适配器使用 `codex exec --ephemeral`，在临时目录中运行，忽略用户配置/规则并要求只读沙箱与结构化输出。`--ephemeral` 的含义是这次 CLI 运行不在本机持久化 rollout；它**不等于** OpenAI 服务端零保留，也不覆盖你的账户、订阅、组织或 API 数据设置。

Codex 的 `--sandbox read-only` 不是无工具模式：CLI 仍可执行模型生成的只读 shell 命令，并可能读取当前用户可见的本机文件。空临时工作目录和 prompt 中的“不要使用工具”只是减小暴露面，不能证明 Codex 只看见上文列出的最小 context。0.1.0 没有额外的 AppContainer/低权限账户隔离；如果机器上有敏感文件，请不要开启 Codex planner。

OpenAI API key 用户可查阅 [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)。使用 ChatGPT 订阅登录 Codex 的用户应在自己的 ChatGPT/Codex 账户中核对当前数据控制；不同账户类型的规则可能不同。

### Claude

适配器使用 `--no-session-persistence`、`--safe-mode` 和空工具集，避免为这次 planner 调用保存可恢复的本地 Claude 会话，并提供比 Codex read-only shell 更窄的本机工具面。它仍需要有效的 Claude OAuth/登录。Anthropic 官方明确说明：本地 Claude Code 与模型交互时会通过网络发送用户 prompt 和模型输出，消费者与商业账户有不同训练、保留和 ZDR 规则。[Claude Code data usage](https://code.claude.com/docs/en/data-usage)

### 共同说明

- Codex/Claude CLI 自己仍需认证并与提供商通信；它们可能有独立的更新检查、错误报告或遥测设置。
- 两个 planner adapter 都使用空临时 cwd，减少自动带入项目级上下文；这不是 Codex 主机文件隔离。启动/超时错误会泛化，不回显原始 prompt 或 provider stderr。
- 普通 `doctor` 只检查 CLI 是否存在，不执行提供商认证命令；显式使用 `doctor --check-planner-auth` 才会运行 Codex/Claude 的认证状态检查，这可能与提供商通信。
- HandsFreePC 不代理、修改或保证提供商的数据保留政策。
- 使用“订阅额度”不意味着数据只在本机，也不意味着零保留。
- 提供商规则会变化；使用前请重新阅读对应官方文档和账户设置。

## 本地持久化

项目默认可能保存的非内容数据包括：

- 你自己创建的 `config.local.yaml`，或 `%LOCALAPPDATA%\HandsFreePC\config.yaml`；
- 下载的 ASR/VAD 模型权重及其 LICENSE/README；
- 0.1.0 本身不创建持久化的运行内容日志；配置中的 `redact_paths_in_logs` 是为未来日志实现保留的策略位，不能自动清理外部工具输出；
- 由 Python、Windows 或上游 CLI 自己维护的安装缓存和诊断数据。

普通安装不包含 faster-whisper，默认 `backend: none`。`-WithWhisper` 只安装代码依赖；显式构造 `large-v3-turbo` 会从模型托管站下载并缓存 GB 级权重。当前异常后备只在已构造 SenseVoice 的 `transcribe()` 抛错时触发；如果未预下载，首次触发时可能联网。它不处理空/低置信度结果，也不能补救 SenseVoice 启动/模型加载失败。这组可选权重不属于三个默认固定 SHA 模型，应另行阅读其托管仓库/模型条款。

`doctor`、`test-asr` 等 CLI 的 stdout/JSON 可能包含配置、模型或输入文件的绝对路径。它们不是 HandsFreePC 自动保存的日志，但如果你重定向、复制或上传输出，就会形成新的持久化副本；分享前必须人工脱敏。

0.1.0 没有实现录音/转写持久化器；`save_audio` 和 `save_transcripts` 目前是默认关闭的策略保留位，即使改成 `true` 也不构成一个受支持的录音功能。未来若实现诊断保存，仍必须增加明确的单次 opt-in、输出位置和删除提示，不能只依赖这两个布尔值悄悄开始记录。

不得把以下内容提交到 Git：录音、转写、本机绝对路径、模型权重、令牌、登录缓存、日志或 `config.local.yaml`。

如需诊断真实音频，必须由用户针对一次测试明确开启、指定本地输出位置、了解其中可能录到旁人，并在问题解决后自行安全删除。公开 Issue 和安全报告中只能提供脱敏后的最小复现。

## 删除与停用

- 暂停桌面操作：使用配置中的暂停/停止短语。该状态仍保持低成本的本地唤醒/停止短语检测，麦克风并未关闭；0.1.0 没有持久状态页/托盘图标，`silent` 下成功反馈也可能不可见。需要可靠确认完全停止采集时，应退出进程或关闭 Windows 麦克风权限，并参考 Windows 自己的麦克风使用指示。
- 删除本项目配置：检查项目目录中的 `config.local.yaml` 和 `%LOCALAPPDATA%\HandsFreePC\config.yaml`。
- 删除模型：删除你在配置中指定的 `models` 目录；下次使用相应 ASR 前需重新下载。
- Codex/Claude 自己的本地会话、缓存和云端数据不由 HandsFreePC 管理；请使用各自 CLI/账户提供的删除和数据控制功能。

删除前先核对绝对路径，不要对用户目录或磁盘根目录运行递归删除命令。

## 旁人与家庭场景

常开麦克风会短暂处理房间中所有人的声音，包括孩子、来访者和远程通话中的声音。即使默认不保存，这仍可能涉及告知、同意和当地法律要求。

建议：

- 默认使用遮罩并留意 Windows 麦克风指示；0.1.0 的遮罩是短暂反馈、没有托盘图标，持续可见的监听指示仍是待补能力；
- 在访客、医疗、教育、会议或保密谈话时暂停；
- 不把唤醒词设成日常高频句子；
- 扬声器播放含控制词的视频时暂停，或把高风险动作保持阻断；
- 不用本项目记录、推断或识别儿童/旁人的身份。

## 本项目不做什么

- 不出售音频或转写；
- 不提供广告追踪；
- 不在默认模式下把音频上传到 OpenAI、Anthropic 或其他 ASR 服务；
- 不用本项目采集的数据训练模型；
- 不在后台自动打开云规划；
- 不承诺第三方依赖或提供商具有相同的数据政策。

隐私问题可通过公开 Issue 提交脱敏后的普通问题；若问题可能构成漏洞或会暴露个人数据，请按 `SECURITY.md` 使用私密报告渠道。

---

**English summary:** Audio recognition is local by default, and HandsFreePC does not save raw audio or transcripts by default. If you explicitly enable the Codex or Claude planner, the recognized command text and minimal context are sent to that provider; raw audio is not sent. The Codex adapter retains a read-only shell, so host-readable files may also be exposed to tool use; keep cloud planning disabled on sensitive machines. Provider-side retention and training depend on your account and settings.
