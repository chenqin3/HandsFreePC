# HandsFreePC

说“开始语音操作”进入连续会话；每条指令用英文 `over` 收尾，按 FIFO 排队交给被要求逐动作刷新并自检的 Codex Computer Use 执行。

> [!WARNING]
> HandsFreePC 0.2.0 是面向 **Windows 11、64 位 Python 3.11 或 3.12** 的 alpha。连续会话、`over` 分段、FIFO worker、取消/排空以及 Codex Computer Use 适配器已有自动化测试，但本版本**尚未完成真实屏幕 Computer Use 验收**。公开配置默认关闭 Computer Use、云端屏幕上下文和真实执行；不要在未做本机受控 live test 前用于重要或高风险操作。

## 它解决什么问题

HandsFreePC 适合双手被占用、但仍能说话的场景：

- 平时只在本机用 Vosk 监听少量控制口令；
- 说“开始语音操作”后保持连续收音，不必为每条指令重新唤醒；
- 一段指令说完时说英文单词 `over`，完整 prompt 才进入有界 FIFO 队列；
- 第一条执行期间仍可继续说第二、第三条，普通指令严格按入队顺序处理；
- 可选的 Codex Computer Use worker 观察指定窗口，以鼠标/键盘完成一个原子动作；控制提示要求它刷新观察、自检任务相关后置条件后再继续；
- 说“结束语音操作”只停止接收新的普通 prompt，默认继续排空已接受队列；未说 `over` 的半条指令会丢弃，麦克风仍在本地接收急停/确认等控制词；
- 说“立即停止所有操作”“取消所有操作”等急停词，请求取消当前工作并清空待处理队列；已经发生的点击、输入或外部副作用无法撤回；
- 保留 0.1 的本地确定性解析器、白名单 Windows 执行器和 Codex / Claude 文本 planner，作为 Computer Use 未启用时的兼容模式。

默认配置不保存录音、不保存转写，不启用云 planner 或 Computer Use，也不允许屏幕上下文离开本机。公开模板同时保留 `execution.dry_run: true` 和 `speech.fallback.backend: none`；首次启动不会执行真实 Windows 桌面动作（麦克风和反馈仍工作），SenseVoice 失败时也不会悄悄下载或启用 Whisper。

## 工作方式

```text
麦克风
  └─ Vosk 小词表：本地、常开，识别开始/结束/急停等控制口令
       └─ Silero VAD + SenseVoice：本地切句与转写
            └─ PromptAssembler：只在英文 over 处完成一条 prompt
                 └─ 有界 FIFO worker：收音与执行解耦
                      └─ Codex exec 首轮 / resume 同一控制会话（显式 opt-in）
                           └─ Computer Use：观察目标窗口 → 一个动作 → 刷新 → 自检并报告

Computer Use 关闭时
  └─ 0.1 兼容路径：确定性解析 / 可选文本 planner → 本地白名单执行器
```

两条执行路径的权限边界不同。兼容路径中，Codex / Claude 文本 planner 只能返回最多 8 步的受限 JSON 计划，真正改变桌面状态的是本地白名单执行器。连续 Computer Use 路径则会让 Codex 通过已安装的 Computer Use skill 读取目标窗口的 UIA/截图并操作鼠标键盘；它被要求每次只做一个原子动作、立即刷新并自检后置条件，不得改用终端、Run 对话框、猜测坐标或操作密码/UAC/安全设置。高风险或需要确认的动作应在即将执行时返回 `NEEDS_CONFIRMATION` 并停下，待用户说完整的“确认执行”后才继续同一个控制会话。当前本地 adapter 不独立复核视觉后置条件或模型是否漏报风险，因此仍需受控 live test 和人工监督。

Computer Use 意味着识别后的 prompt、窗口元数据、辅助功能树、目标窗口截图、可见内容和剪贴板状态可能由 OpenAI 处理。必须显式启用总开关、识别文本云许可和屏幕上下文云许可，并关闭 dry-run 配置；原始音频仍留在本机。Windows 目标应用必须在 active desktop 可见，操作时 Computer Use 会占用前台并移动鼠标/键盘。per-app approval / `Always allow` 是 Codex 自身的另一层授权，不会由本项目 YAML 代替或自动撤销；请核对[官方 Computer Use 文档](https://learn.chatgpt.com/docs/computer-use)。

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

`doctor` 的 JSON 会给出 `ready_for_run` 和 `ready_for_live_control`。Computer Use 关闭时，`doctor --strict` 只要求运行依赖、Vosk/SenseVoice/Silero 三个模型的运行所需文件和至少一个输入设备就绪；启用 Computer Use 后，strict 还静态检查真实执行模式、两项云端许可、controller 配置的 Codex CLI、Computer Use skill 和 `node_repl` 线索。它不实例化模型、不打开/验证指定麦克风，也不验证 Codex 登录、Computer Use server、应用授权或具体目标应用。因此 `ready_for_live_control` 只是预检信号，不是 live-ready 证明。`scripts/run.ps1` 与 `scripts/smoke-test.ps1` 会先使用 strict，直接 `handsfreepc run` 和 Startup 快捷方式不会；不过配置加载本身仍会拒绝 Computer Use 缺许可或与 `dry_run: true` 同时开启。`simulate` 只覆盖兼容解析路径并强制 dry-run，不会证明 Computer Use 能真实操作屏幕。

确认输出无误后启动常驻监听：

```powershell
./scripts/run.ps1
```

第一次运行前请在 Windows“隐私和安全性 → 麦克风”中允许桌面应用访问麦克风。保持公开配置即可先测试本地唤醒、转写和反馈；这不会启用连续 Computer Use。详细步骤见 [测试指南](docs/TESTING.md)，安装问题见 [故障排查](docs/TROUBLESHOOTING.md)。

### 显式启用连续 Computer Use

先安装并登录 Codex CLI，在 Codex 中启用 Computer Use plugin/server/skill 和 `node_repl`，再确认你接受识别文本及屏幕上下文进入云端。只在被 Git 忽略的 `config.local.yaml` 中修改：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  enabled: true
  backend: codex
  allow_screen_context_to_cloud: true

execution:
  dry_run: false
```

上述四个值缺一都会使 Computer Use 配置无效或不满足 strict 预检。`execution.dry_run` 主要约束旧本地执行器；配置加载器要求启用 Computer Use 时把它设为 `false`，但这不使 `dry_run` 成为 Computer Use 的权限沙箱。真正的总开关仍是 `computer_control.enabled`，并且还必须有两项云端许可。修改后先运行 `doctor --strict`，再按照测试指南从无副作用目标逐步做本机 live test。当前仓库没有声称这条真实屏幕链路已经验收。

## 示例口令

启用 Computer Use 后，持续会话协议如下。`over` 必须说英文单词；它不作为 prompt 内容发送。可以在第一条执行期间继续说后续指令。

```text
开始语音操作
打开文件资源管理器并进入文档文件夹 over
打开说明文件，并确认内容页已经出现 over
切换到目标应用，把刚才看到的标题输入搜索框 over
结束语音操作
```

“结束语音操作”只停止接受新的普通 prompt，并默认等待上述已入队命令全部结束；没有说 `over` 的半条会丢弃。麦克风仍用本地 Vosk 接收急停、确认和队列恢复，只有退出进程或关闭系统麦克风权限才停止采集。若要取消当前与待处理工作，说完整的“立即停止所有操作”或“取消所有操作”。急停是协作式取消：它会终止当前 Codex 子进程并清空队列，但不能撤回已发生的点击、按键、发送或其他外部副作用。

普通队列严格 FIFO。若某条任务在真正执行高风险动作前返回 `NEEDS_CONFIRMATION`，队列会暂停；描述被限制为单行、无控制字符且不超过 160 字。说“确认执行”后，确认 continuation 会先于后面的普通命令恢复同一 Codex 会话。待确认期间说“继续队列”不会绕过确认，而会再次提示只能确认或取消；普通非确认失败暂停时，才用“继续队列”或急停词清空。纯 `voice` 模式下必须等完整确认提示成功播完再确认；过早说不会授权，SAPI 失败时应先切换到屏幕或双重反馈。确认提示实际显示或完整播报后才开始 `execution.confirmation_timeout_seconds`；它不是后台计时器，下一段本地语音到来时若已过期，程序会拒绝确认并取消本轮及全部队列。已经发生的操作仍不可撤回。

Computer Use 未启用时，程序仍走 0.1 兼容解析路径。该路径支持盘符、路径、Codex/Claude 听写和反馈模式等固定句式，并继续使用 `execution.path_aliases`、`search_roots`、应用档案和本地风险策略；它不是连续鼠标键盘 agent。

## 兼容模式中的两种“语音输入”

以下两种方式属于 Computer Use 关闭时的 0.1 兼容路径，不是 `over` 队列协议：

| 模式 | 怎样触发 | 麦克风与文本流 | 推荐用途 |
|---|---|---|---|
| HandsFreePC 自有听写 | 明确指定已配置应用，例如“打开 Codex 语音输入”；裸说“开始听写/打开语音输入”会被阻断 | HandsFreePC 本地 ASR → 核验输入框 → Unicode `SendInput`；不使用剪贴板；只有带控制前缀且整句精确匹配的“电脑发送提示”才按 Enter | 默认，行为可控且容易核验 |
| 应用内原生语音 | 必须明确说“应用内语音”，并再次确认 | 等此前 TTS 队列完全播放并清空后，点击已校准的原生语音按钮或热键；一旦开始执行尝试，执行中、成功及失败反馈只显示遮罩，成功或失败都保守进入 `PAUSED` | 确实需要应用自身的实时语音会话时 |

公开 Codex/Claude 档案的 `native_voice_hotkey`、`search_hotkey` 都是 `null`，`voice_button_names` 是空列表；这不是可直接点击的默认选择器。必须先在自己的应用版本、语言和布局中检查 UIA，再把唯一且稳定的名称或热键显式写入本地配置。不建议让两套完整听写同时抢占麦克风。使用应用内语音后，应先结束对方的语音会话，再说 HandsFreePC 唤醒词返回控制。第三方应用是否真的释放麦克风无法由 0.2.0 保证。

## 屏幕大字、语音或两者同时

`app.feedback_mode` 支持：

- `overlay`：默认；高对比、置顶、不抢焦点、鼠标穿透的大字遮罩；
- `voice`：通过本机 Windows SAPI 声音朗读短反馈；
- `both`：同时显示和朗读；
- `silent`：不主动显示或朗读普通反馈。

在 0.1 兼容路径中，可以直接说“切换到屏幕反馈”“切换到语音反馈”“大字和语音两种都开”或“切换到静默模式”。SAPI 是否有合适的中文声音取决于本机安装情况。`overlay` / `both` 会显示完整的“识别：{转写}”，`voice` / `both` 还会朗读它，包括口述的路径或项目名；敏感环境应选择合适模式。

连续 Computer Use 会话也支持四种模式，并在本地识别反馈切换句；可直接说，也可在句尾加 `over`，这些本地设置不会进入 Computer Use FIFO。遮罩反馈立即显示；`voice` / `both` 在每个 utterance 边界把待播反馈按优先级合并，只朗读最高优先级中的最新一条，其余该批不保证逐条播完。播完会清空输入缓冲和控制检测器，避免半句话中途插播或把 TTS 回声重新识别。播放期间仍不能用语音急停，所以反馈应保持短小；切换到 `overlay` 或 `silent` 会清除尚未播出的语音反馈。语音反馈是状态提示，不是逐条审计日志；兼容模式继续使用原有半双工 TTS 行为，每台机器都要人工听测。

## Codex Computer Use 与旧文本 planner

0.2 的连续桌面控制后端固定为 `codex`。第一条队列任务通过 `codex exec --json` 建立线程，后续任务用 `codex exec resume <thread-id> --json` 延续同一控制会话；“确认执行”也在该线程内恢复此前暂停的确切动作。正常 drain 或急停会关闭 controller 并丢弃本地 thread 引用；controller 不是 ephemeral，不能据此推断 CLI/提供商历史已删除。适配器使用 `--sandbox read-only` 约束 shell 文件写入，但保留用户 Codex 配置和插件，以便加载 Computer Use skill。`read-only` **不阻止鼠标键盘 Computer Use**，也不是屏幕数据保密边界。

worker prompt 要求使用 Computer Use skill，经 `node_repl` / `@oai/sky` 只选择一个目标窗口，优先 UIA，否则使用刚刷新的目标窗口截图；每次观察后只能做一个原子动作，随后必须刷新、自检任务相关后置条件并报告。它禁止用 shell、PowerShell、终端、Run 对话框或其他工具替代 UI 操作，也禁止控制 ChatGPT/Codex UI、认证、密码、UAC 和安全/隐私设置。最终消息必须严格为单行 `VERIFIED_COMPLETION:`、`NEEDS_CONFIRMATION:` 或 `FAILURE:` 状态，本地 adapter 同时校验 CLI 退出与 JSONL turn 完整性。模型或插件仍可能出错，而且没有第二个本地视觉 verifier；因此 `VERIFIED_COMPLETION` 仍是同一 agent 的自检报告，不是独立屏幕证据。

[OpenAI 的非交互模式文档](https://developers.openai.com/codex/noninteractive)说明 `codex exec` 是脚本入口；[Computer Use 文档](https://learn.chatgpt.com/docs/computer-use)说明这项能力能读取并操作图形界面，也可能改变项目工作区以外的应用或系统状态。订阅是否覆盖调用、可用模型、额度和延迟取决于当前账户与提供商设置，本项目不作保证。

旧文本 planner 仍可选择 Codex 或 Claude，但只为兼容白名单执行器产生受限 JSON 计划，不拥有 Computer Use。Claude `-p` 仅保留在这条旧 planner 路径；0.2 没有 Claude Computer Use controller。`codex -p` 表示 profile，不是 prompt；不要把它当作 Claude 的 `-p`。

旧云 planner 也默认关闭。若只想启用它而不启用 Computer Use，仍必须同时修改两个开关：

```yaml
privacy:
  allow_cloud_planner: true

planner:
  enabled: true
  backend: codex  # 或 claude
```

启用后，识别文本和最小必要上下文会离开本机；原始音频仍不发送。确定性解析成功的命令不会调用云 planner。Computer Use 是另一组更宽的显式授权，还会让窗口元数据、辅助功能树和截图可能离开本机。完整边界见 [PRIVACY.md](PRIVACY.md)。

## 配置要点

公开仓库只提供 `config.example.yaml`。本机设置位于被忽略的 `config.local.yaml`：

- `speech.input_device`：`null` 使用默认麦克风，也可填 `list-audio-devices` 返回的编号；
- `app.wake_phrases` / `end_session_phrases` / `stop_phrases`：分别配置“开始语音操作”、只结束输入并排空队列、以及急停取消；Vosk 的 `speech.wake.grammar` 需同步维护，中文词之间保留空格；
- `app.prompt_delimiters`：默认只有英文 `over`；ASCII 单词边界匹配不区分大小写，`mouseover`、`voiceover` 不会误切；
- `speech.fallback.backend`：公开默认值为 `none`；只有完成可选依赖安装和模型预下载后才改成 `faster-whisper`；
- `computer_control.enabled`：连续 Codex Computer Use 总开关，公开默认 `false`；仅支持 `backend: codex`；
- `computer_control.allow_screen_context_to_cloud`：允许窗口元数据、辅助功能树和截图进入 Codex 上下文，公开默认 `false`；
- `computer_control.max_queue_size` / `max_prompt_chars`：有界 FIFO 与单条 prompt 上限；队列满时明确拒绝，不静默丢弃；
- `computer_control.failure_policy: pause` / `end_policy: drain`：失败暂停，结束输入后排空；0.2 只接受这两个保守策略；
- `execution.confirmation_timeout_seconds`：兼容与连续确认的有效期；连续模式从确认反馈实际显示或在纯 `voice` 中完整成功播报后起算，并在下一段本地语音到来时惰性检查，过期会取消本轮、controller 与全部队列；
- `execution.dry_run`：公开默认 `true`，禁止旧本地 Windows 执行器做真实动作；它不禁止麦克风/反馈或双重启用的旧 planner 联网。配置加载器会拒绝 `computer_control.enabled: true` 与 `dry_run: true` 的组合，但 `dry_run` 本身不是 Computer Use 的能力沙箱；
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

0.2.0 的自动化套件尚未覆盖这条 faster-whisper 异常后备，启用者必须单独做受控 smoke test。

## 安全边界与已知限制

- 连续 Computer Use 与旧本地执行器是两条不同的授权路径；旧 `blocked_keywords` 和动作 Schema 不能替代 Computer Use skill 自身的确认政策；
- Computer Use 控制提示要求在需要确认的动作即将执行时停下并返回 `NEEDS_CONFIRMATION`，队列随即暂停。只有在提示实际送达后、`confirmation_timeout_seconds` 内完整说出“确认执行”，才会在同一 Codex 会话中继续此前描述的确切动作；过期在下一段本地语音到来时取消本轮和队列。确认不是对后续任务的长期授权，待确认时“继续队列”会被拒绝；当前仍没有独立本地 risk classifier 验证模型没有漏报；
- `删除`、`格式化`、`付款`、`转账`、`输入密码`等禁词仍会阻止旧本地动作计划；
- 已存在目录和窄文件白名单（常见文档、图片、音视频及数据文件）可直接打开；任何未知后缀、无后缀普通文件或主动/间接执行类型一律再次确认，而不只检查少数已知危险扩展名；
- 确认短语必须是完整的标准化整句；“不要确认执行”等包含确认词的否定句不会授权。planner 自报的风险只能被本地策略保持或升高，不能降低；
- 听写中的 `SEND_PROMPT` 必须是带控制前缀的完整控制命令，例如“电脑发送提示”；否定句只会作为文字处理，不会提交。所有 action 文本字段和 plan `summary` 都拒绝 Unicode C 类控制字符；因此 `TYPE_TEXT` 不能夹带回车、换行等提交控制；
- 确认反馈从已经校验的动作本地生成，不采用 planner 的 `summary`；它会明确提示第三方麦克风、提交动作或最终文件 basename；
- 应用内原生语音必须再次确认，只能作为计划最后一步，不能与反馈模式切换组合；非法组合会在执行前阻断并回 `ARMED`，只有已经开始、可能触发第三方麦克风的执行尝试才在成功或失败后保守留在 `PAUSED` 且只显示遮罩；
- 多个匹配路径、窗口或 UI 控件会报歧义，不会自动取第一个；
- UNC / `//server`、URI 与 Win32 device namespace 在任何文件系统访问前直接阻断；
- 输入前会复核前台窗口和非密码输入框；普通权限进程不会越过 UAC、高完整性窗口或安全桌面；
- `open_path` 能核验路径解析和 Windows Shell 启动调用，但 0.2.0 的旧本地执行器仍不能证明第三方查看器已经正确渲染文件内容；
- 旧 UI Automation 对 Electron / 自绘界面的可见性取决于应用版本；连续 Computer Use 增加目标窗口截图回退，但仍禁止复用陈旧截图或猜测全屏状态；
- “结束语音操作”不是急停：它丢弃未以 `over` 完成的半条输入，拒绝新普通 prompt，并等待当前与已入队工作结束；麦克风仍在本地检测急停/确认等控制词；
- 急停词请求取消当前 Codex 子进程并清空普通/确认队列。取消是协作式和有界的，不能撤回已经到达 Windows 或外部服务的点击、按键、提交、付款等副作用；
- 兼容路径中的同步 OS/UI 调用和完整 SAPI 队列仍不能被语音抢占；连续路径虽然可以终止 Codex 子进程，也不能保证正在执行的底层 UI 调用从未生效；
- 锁屏、UAC、安全桌面、休眠恢复、麦克风拔插、多屏和不同 DPI 仍需逐机验证。

本项目不是安全隔离边界，也不应被用于无人看管的高风险操作。发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

`execution.blocked_keywords` 只约束 HandsFreePC 自己的本地动作计划。用户明确说“电脑发送提示”后，composer 中已有的 prompt 会交给下游 Codex/Claude agent；对方之后能执行什么由其自身的 sandbox、approval 和 permissions 决定。应给下游 agent 配置最小权限，不能把 HandsFreePC 的禁词当作下游安全边界。

## 当前验证状态

下表是 2026-08-31 当前工作树能诚实声明的范围；“模拟/单元测试通过”不等于目标电脑上的 live UI 已通过。

| 项目 | 状态 | 边界 |
|---|---|---|
| 完整自动化收集项：配置、解析、路径消歧、状态机、安全策略、Windows/UIA fake 后端 | 已通过 | 一项需 Windows symlink 权限的真实测试在发布机跳过；相关逻辑仍有单元覆盖，但需在有权限主机补验；fake 后端不能替代 live UI |
| 连续会话协议：开始/结束、多个 `over`、半条丢弃、普通 FIFO、失败暂停、确认 continuation、排空和急停 | 自动化测试已覆盖 | 使用 fake controller；未证明真实麦克风分段或真实桌面行为 |
| Codex Computer Use 适配器：首次 thread、resume、超时、取消、JSONL 协议和环境变量清洗 | 自动化测试已覆盖 | 使用 fake subprocess；**尚未做真实屏幕 Computer Use 测试** |
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
