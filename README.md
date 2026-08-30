# HandsFreePC

用一句中文唤醒 Windows，然后用声音打开文件、切换应用、进入 Codex / Claude 对话并继续听写。

> [!WARNING]
> HandsFreePC 0.1.0 是面向 **Windows 11、64 位 Python 3.11 或 3.12** 的 alpha。它已经具备可测试的本地语音链路、受限动作模型和 Windows 执行器，但还不是“任何电脑、任何应用都能直接用”的通用语音 RPA。尤其是 Codex / Claude 桌面界面的 UI Automation 名称会随版本、语言和账号布局变化，首次使用必须做本机校准和 live smoke test。

## 它解决什么问题

HandsFreePC 适合双手被占用、但仍能说话的场景：

- 平时只在本机监听少量唤醒词和停止词；
- 说“现在开始语音操作”后，本地转写下一条完整命令；
- 打开已存在的盘符路径、文件夹或文件；
- 激活 Codex / Claude，按项目名和对话名查找可访问控件；
- 把后续说话内容写入经过核验的输入框，并且只有明确说“电脑发送提示”才提交；
- 用不抢焦点的大字遮罩、Windows 本机语音、两者同时或静默反馈；
- 常用命令由确定性代码解析，只有明确双重授权后才把识别文本交给 Codex 或 Claude 规划。

默认配置不保存录音、不保存转写、不启用云规划，也不需要管理员权限。公开模板还把 `execution.dry_run` 设为 `true`、把 `speech.fallback.backend` 设为 `none`；首次启动不会执行真实 Windows 桌面动作（麦克风和反馈仍工作），SenseVoice 失败时也不会悄悄下载或启用 Whisper。

## 工作方式

```text
麦克风
  └─ Vosk 小词表：本地、常开，只识别唤醒/停止短语
       └─ Silero VAD：判断一句话的起止
            └─ SenseVoice：本地转写完整命令
                 ├─ 确定性中文解析器
                 └─ 可选 Codex / Claude 文本规划器（默认关闭）
                      └─ 本地 Schema + 风险策略
                           └─ Windows 原生 API / UI Automation
                                └─ 前台窗口与操作结果核验
```

规划器和执行器是刻意分开的。Codex / Claude 只能返回最多 8 步的受限 JSON 计划；真正能改变桌面状态的只有本地白名单执行器。模型不能生成 shell 命令、坐标点击、注册表操作、密码输入或 UAC 确认；云 planner 返回的 `TYPE_TEXT` / `SEND_PROMPT` 也会被本地策略直接阻断，只能规划聚焦输入框等前置动作。

更完整的设计见 [架构说明](docs/ARCHITECTURE.md)、[技术调研](docs/RESEARCH.md) 和 [安全模型](docs/SECURITY_MODEL.md)。

## 快速开始

准备：Windows 11、64 位 Python 3.11 或 3.12（不支持 3.13）、可用麦克风，以及首次下载模型时的网络连接。PowerShell 中运行：

```powershell
git clone https://github.com/chenqin3/HandsFreePC.git
Set-Location HandsFreePC
Set-ExecutionPolicy -Scope Process Bypass
./scripts/install.ps1 -DownloadModels
```

安装脚本会创建 `.venv`、安装 `audio` 与 `windows` 依赖、复制一份不会提交到 Git 的 `config.local.yaml`，然后运行环境检查；默认**不安装 faster-whisper**。三个默认模型约为数百 MB，下载时间取决于网络。全新下载先在 staging 目录完成归档 SHA-256、预期权重、许可文件和来源说明核验，再替换目标目录；已有目录只有在权重和完整许可/来源元数据都齐全时才跳过，跳过时不会重新计算历史归档哈希。

先做不改动桌面的检查：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听"
```

`doctor` 的 JSON 会给出 `ready_for_run`。`doctor --strict` 只有在运行依赖、Vosk/SenseVoice/Silero 三个模型的运行所需文件和至少一个输入设备都就绪时才返回成功；它不实例化模型、不打开/验证指定麦克风，也不检查许可/source metadata 或重算历史归档 SHA。下载器的完整元数据 skip 门禁是另一层。`scripts/run.ps1` 与 `scripts/smoke-test.ps1` 会先使用 strict，直接 `handsfreepc run` 和 Startup 快捷方式不会。`simulate` 会为每条输入输出 `success`，任一条失败时进程返回非零。

确认输出无误后启动常驻监听：

```powershell
./scripts/run.ps1
```

第一次运行前请在 Windows“隐私和安全性 → 麦克风”中允许桌面应用访问麦克风。公开配置的 `execution.dry_run: true` 是安全默认值；熟悉解析结果后，只有在准备做受控 live test 时才显式改为 `false`。详细步骤见 [测试指南](docs/TESTING.md)，安装问题见 [故障排查](docs/TROUBLESHOOTING.md)。

## 示例口令

以下示例与当前确定性解析器一致；盘符、目录、项目和对话名称应替换成你自己的真实名称。

```text
现在开始语音操作，打开 D 盘的项目文件夹里的说明.txt

现在开始语音操作，切换到 Codex app，打开演示项目下的语音设计对话，打开语音输入
请检查这个项目里的测试并给出修改建议
电脑发送提示

现在开始语音操作，切换到 Claude app，到 Chat 选项卡，开启一个 Design

现在开始语音操作，切换到屏幕反馈
现在开始语音操作，切换到语音反馈
现在开始语音操作，大字和语音两种都开

停止所有操作
恢复语音操作
```

盘符表达会逐层解析；如果两个文件或控件同样接近，程序会失败关闭，而不是猜第一个。可以在 `execution.path_aliases` 中增加自己的别名，在 `execution.search_roots` 中显式限制允许搜索的根目录。

## 两种“语音输入”不是一回事

| 模式 | 怎样触发 | 麦克风与文本流 | 推荐用途 |
|---|---|---|---|
| HandsFreePC 自有听写 | 明确指定已配置应用，例如“打开 Codex 语音输入”；裸说“开始听写/打开语音输入”会被阻断 | HandsFreePC 本地 ASR → 核验输入框 → Unicode `SendInput`；不使用剪贴板；只有带控制前缀且整句精确匹配的“电脑发送提示”才按 Enter | 默认，行为可控且容易核验 |
| 应用内原生语音 | 必须明确说“应用内语音”，并再次确认 | 等此前 TTS 队列完全播放并清空后，点击已校准的原生语音按钮或热键；一旦开始执行尝试，执行中、成功及失败反馈只显示遮罩，成功或失败都保守进入 `PAUSED` | 确实需要应用自身的实时语音会话时 |

公开 Codex/Claude 档案的 `native_voice_hotkey`、`search_hotkey` 都是 `null`，`voice_button_names` 是空列表；这不是可直接点击的默认选择器。必须先在自己的应用版本、语言和布局中检查 UIA，再把唯一且稳定的名称或热键显式写入本地配置。不建议让两套完整听写同时抢占麦克风。使用应用内语音后，应先结束对方的语音会话，再说 HandsFreePC 唤醒词返回控制。第三方应用是否真的释放麦克风无法由 0.1.0 保证。

## 屏幕大字、语音或两者同时

`app.feedback_mode` 支持：

- `overlay`：默认；高对比、置顶、不抢焦点、鼠标穿透的大字遮罩；
- `voice`：通过本机 Windows SAPI 声音朗读短反馈；
- `both`：同时显示和朗读；
- `silent`：不主动显示或朗读普通反馈。

可以直接说“切换到屏幕反馈”“切换到语音反馈”“大字和语音两种都开”或“切换到静默模式”。SAPI 是否有合适的中文声音取决于本机安装情况。`overlay` / `both` 会显示完整的“识别：{转写}”，`voice` / `both` 还会朗读它，包括口述的路径或项目名；敏感环境应选择合适模式。

`speaking` 状态覆盖整个待播队列，而不只是单条语音；期间麦克风 callback 仍写入有界内存缓冲，但识别/命令处理暂停，全部播放完成后输入队列和预卷缓冲一起丢弃。反馈是半双工的：在 `voice` / `both` 中必须等“我在听”或确认提示说完再开口，否则提前说出的命令可能被一并丢弃。0.1.0 播报期间不能用停止词打断，且 SAPI worker/COM 错误不会传播成可见失败；每台机器都要人工听测，默认 `overlay` 更稳妥，`both` 至少保留遮罩。

## Codex `exec` 还是 Claude `-p`

结论：**默认使用 `codex exec`，同时保留 Claude `-p` 作为可替换规划器；两者都不直接操控桌面。**

| | Codex | Claude |
|---|---|---|
| 正确的非交互入口 | `codex exec` | `claude -p` / `--print` |
| 本项目的限权方式 | 临时目录、ephemeral、忽略用户规则、`read-only` sandbox、JSON Schema | safe mode、空 tools、无会话持久化、JSON Schema |
| 选择建议 | 默认；非交互入口和只读沙箱适合受限规划 | 已有 Claude 订阅或在自己的命令上效果更好时切换 |

`codex -p` 表示 **profile**，不是 prompt；不要把它当作 Claude 的 `-p`。参见 [OpenAI 的 Codex 非交互模式](https://developers.openai.com/codex/noninteractive)、[Codex CLI 参数参考](https://developers.openai.com/codex/cli/reference) 和 [Claude Code CLI 参考](https://code.claude.com/docs/en/cli-reference)。

两种后端都依赖相应官方 CLI 已安装并登录；订阅是否覆盖调用、可用模型、额度和延迟由提供商与账号决定。HandsFreePC 会从规划器子进程环境中删除常见 API key / token / secret 变量，设计目标是复用 CLI 的登录会话，而不是读取密钥。

本次发布机实测中，Codex 使用已有 ChatGPT 订阅完成了结构化规划；Claude 在清洗环境后无法通过当前订阅 OAuth 认证，程序没有回退到环境变量 API key。这个结果只验证当时的认证与 planner 通路，不代表 Codex/Claude 桌面 UI 选择器已验证，也不是两种模型能力的对照结论。Codex 的 read-only sandbox 仍保留只读 shell/文件可见性；敏感机器应保持 planner 关闭。

云规划默认关闭。要启用，必须同时修改两个开关：

```yaml
privacy:
  allow_cloud_planner: true

planner:
  enabled: true
  backend: codex  # 或 claude
```

启用后，**识别文本和最小必要上下文会离开本机**；原始音频仍不发送。确定性解析成功的命令不会调用云规划器。隐私边界见 [PRIVACY.md](PRIVACY.md)。

## 配置要点

公开仓库只提供 `config.example.yaml`。本机设置位于被忽略的 `config.local.yaml`：

- `speech.input_device`：`null` 使用默认麦克风，也可填 `list-audio-devices` 返回的编号；
- `app.wake_phrases` / `app.stop_phrases`：运行时匹配文本；Vosk 的 `speech.wake.grammar` 需同步维护，中文词之间保留空格；
- `speech.fallback.backend`：公开默认值为 `none`；只有完成可选依赖安装和模型预下载后才改成 `faster-whisper`；
- `execution.dry_run`：公开默认值为 `true`，禁止构造/调用真实 Windows 桌面后端；它不禁止直接 `run` 打开麦克风和产生反馈，也不阻止已双重启用的 planner 联网；
- `execution.path_aliases` / `search_roots`：显式定义路径范围；
- `apps.*.executable`：目标未运行时允许启动的完整路径；未配置就只激活已有窗口；
- `apps.*.title_patterns` / `process_names`：用于唯一识别窗口；
- `apps.*.search_hotkey`、`native_voice_hotkey`、`voice_button_names`：Codex/Claude 公开默认分别为 `null`、`null`、`[]`，只有经过本机 UIA/热键验证后再显式配置。

把本机路径、设备名、窗口标题、转写内容和凭据留在本地配置或本地测试记录中，不要提交到公开 issue。

### 可选 faster-whisper 后备

默认安装和运行都不会启用 Whisper。当前后备只在已经成功构造 SenseVoice 后、某次 `transcribe()` 调用抛出异常时触发；空文本或低置信度不会触发，SenseVoice 启动/模型加载失败也不能由它补救。确实需要这条异常后备时，先安装额外依赖并在联网维护窗口**显式预下载** `large-v3-turbo`：

```powershell
./scripts/install.ps1 -WithWhisper
./.venv/Scripts/python.exe -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
```

确认下载与加载成功后，再在 `config.local.yaml` 中设置：

```yaml
speech:
  fallback:
    backend: faster-whisper
    model: large-v3-turbo
```

该模型会产生 GB 级网络下载与本地缓存，并明显增加磁盘、内存、启动延迟和 CPU/GPU 负担。如果不先预下载，首次进入后备路径时可能临时联网并长时间等待；不接受这条网络边界时保持 `backend: none`。

0.1.0 的自动化套件尚未覆盖这条 faster-whisper 异常后备，启用者必须单独做受控 smoke test。

## 安全边界与已知限制

- `删除`、`格式化`、`付款`、`转账`、`输入密码`等首版禁词会直接阻止计划；
- 已存在目录和窄文件白名单（常见文档、图片、音视频及数据文件）可直接打开；任何未知后缀、无后缀普通文件或主动/间接执行类型一律再次确认，而不只检查少数已知危险扩展名；
- 确认短语必须是完整的标准化整句；“不要确认执行”等包含确认词的否定句不会授权。planner 自报的风险只能被本地策略保持或升高，不能降低；
- 听写中的 `SEND_PROMPT` 必须是带控制前缀的完整控制命令，例如“电脑发送提示”；否定句只会作为文字处理，不会提交。所有 action 文本字段和 plan `summary` 都拒绝 Unicode C 类控制字符；因此 `TYPE_TEXT` 不能夹带回车、换行等提交控制；
- 确认反馈从已经校验的动作本地生成，不采用 planner 的 `summary`；它会明确提示第三方麦克风、提交动作或最终文件 basename；
- 应用内原生语音必须再次确认，只能作为计划最后一步，不能与反馈模式切换组合；非法组合会在执行前阻断并回 `ARMED`，只有已经开始、可能触发第三方麦克风的执行尝试才在成功或失败后保守留在 `PAUSED` 且只显示遮罩；
- 多个匹配路径、窗口或 UI 控件会报歧义，不会自动取第一个；
- UNC / `//server`、URI 与 Win32 device namespace 在任何文件系统访问前直接阻断；
- 输入前会复核前台窗口和非密码输入框；普通权限进程不会越过 UAC、高完整性窗口或安全桌面；
- `open_path` 能核验路径解析和 Windows Shell 启动调用，但 0.1.0 不能证明第三方查看器已经正确渲染文件内容；
- UI Automation 对 Electron / 自绘界面的可见性取决于应用版本，当前没有通用视觉坐标点击兜底；
- 配置的全局停止短语按高优先级子串匹配，并在 `AWAKE` / `DICTATION` / `CONFIRMING` 录音中由本地 Vosk 低延迟检测；因此听写内容只要说出完整停止短语，也可能被当成控制并进入 `PAUSED`。**0.1.0 的 `EXECUTING` 是同步的，已经开始的 OS/UI 调用不能被停止词抢占；SAPI 队列播放期间也不能用停止词打断。** 动作和播报都应保持短小；
- 锁屏、UAC、安全桌面、休眠恢复、麦克风拔插、多屏和不同 DPI 仍需逐机验证。

本项目不是安全隔离边界，也不应被用于无人看管的高风险操作。发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

`execution.blocked_keywords` 只约束 HandsFreePC 自己的本地动作计划。用户明确说“电脑发送提示”后，composer 中已有的 prompt 会交给下游 Codex/Claude agent；对方之后能执行什么由其自身的 sandbox、approval 和 permissions 决定。应给下游 agent 配置最小权限，不能把 HandsFreePC 的禁词当作下游安全边界。

## 当前验证状态

下表是 2026-08-30 当前工作树能诚实声明的范围；“模拟/单元测试通过”不等于目标电脑上的 live UI 已通过。

| 项目 | 状态 | 边界 |
|---|---|---|
| 完整自动化收集项：配置、解析、路径消歧、状态机、安全策略、Windows/UIA fake 后端 | 已通过 | 一项需 Windows symlink 权限的真实测试在发布机跳过；相关逻辑仍有单元覆盖，但需在有权限主机补验；fake 后端不能替代 live UI |
| SenseVoice 官方样例 WAV | 已通过 | 实际转写为“开饭时间早上9点至下午5点。”；不代表家庭噪声、远场或方言效果 |
| Vosk 合成唤醒/停止、Silero 官方样例、16 kHz 真实麦克风读取、完整本地运行时启动/停止 | 已通过发布机 smoke | 真实家庭噪声、抱娃距离、误唤醒/漏唤醒统计仍未验证 |
| 短 SAPI；遮罩显示、视觉效果与不抢焦点 | 已通过发布机 live smoke | 多显示器、不同 DPI、远程桌面和各种已安装声音仍未验证 |
| 打开仓库 `examples` 目录 | Explorer dispatch 已通过 | 只证明 Windows 接受并调度；最终关联应用/查看器内容仍是部分后置条件 |
| 当前输入桌面 | 发布机确认返回 `Default` | secure desktop 拒绝由自动化测试覆盖；仍存在检查与动作间的短竞态 |
| Codex 订阅 planner | 已通过结构化规划 | Codex read-only shell 的主机文件可见性仍是残余风险 |
| Claude 订阅 planner | 当前认证不可用 | 清洗环境后的 OAuth 失败，未回退使用环境 API key |
| Codex / Claude 项目、对话、Design、原生语音选择器 | 尚未验证 | 公开 `search_hotkey` / `native_voice_hotkey` / `voice_button_names` 均未提供可用默认值，必须逐机校准 |

复现实验、live checklist 和结果记录格式见 [docs/TESTING.md](docs/TESTING.md)。

## 模型与许可

模型不随 Git 仓库分发，由下载脚本从上游获取：

- [Vosk `vosk-model-small-cn-0.22`](https://alphacephei.com/vosk/models)：约 42 MB，Apache-2.0；用于有限词表唤醒；
- [sherpa-onnx SenseVoice INT8](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)：用于本地中文命令转写；权重受 [FunASR Model License](https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE) 约束，必须注明来源/作者并保留模型名称；
- [Silero VAD v6.2.1](https://github.com/snakers4/silero-vad/tree/v6.2.1)：MIT；用于语音起止点检测；
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：可选后备，不在默认常开路径中。

完整署名与再分发提醒见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。HandsFreePC 自身代码使用 [MIT License](LICENSE)。

## 参与开发

```powershell
./scripts/install.ps1 -WithDevTools
./.venv/Scripts/python.exe -m pytest -q --basetemp ./.pytest-tmp
./.venv/Scripts/python.exe -m ruff check .
```

提交新应用适配器时，请同时提供：稳定的语义选择器、歧义分支、前台核验、动作后置条件、fake 单元测试，以及明确标注的人工 live test。不要提交录音、转写、本机配置、日志或模型权重。
