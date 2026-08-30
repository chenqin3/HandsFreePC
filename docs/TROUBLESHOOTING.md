# HandsFreePC 故障排查

先运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
```

普通 `doctor` 便于查看各项状态；`doctor --strict` 用 `ready_for_run` 汇总本地依赖/模型/输入设备，启用 Computer Use 时还用 `ready_for_live_control` 静态检查四项配置门禁、controller 配置的 Codex executable、Computer Use skill 和 `node_repl` 线索。这不是 live 证明：它不实例化 ASR/VAD、不打开指定麦克风，也不验证 Codex 登录、Computer Use server、per-app approval、active desktop、真实点击或应用后置条件。`scripts/run.ps1` 和 `scripts/smoke-test.ps1` 会先经过 strict；直接执行 `handsfreepc run` 或 Startup 快捷方式不会，但配置加载器仍会拒绝 Computer Use 缺许可或与 `dry_run: true` 同开。输出可能包含本机路径、音频设备名和命令位置，公开前必须脱敏。

## PowerShell 阻止运行脚本

只为当前 PowerShell 进程放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

不需要也不建议永久关闭系统执行策略。仍失败时确认是在仓库根目录运行 `./scripts/install.ps1`，并且文件不是从不可信来源取得。

## 找不到 Python 或版本太旧

HandsFreePC 要求 64 位 Python 3.11 或 3.12；`pyproject.toml` 与安装器都拒绝 3.13。检查：

```powershell
python --version
py -0p
```

安装合适版本后删除并重新建立有问题的项目 `.venv`，或在新克隆中重装。不要把全局 site-packages 和项目虚拟环境混用。

## `pytest` 报临时目录 Access denied

某些 Windows 临时目录 ACL 会让 pytest 无法创建 fixture。把基准临时目录显式放在仓库的忽略目录：

```powershell
./.venv/Scripts/python.exe -m pytest -q --basetemp ./.pytest-tmp
```

`.pytest-tmp/` 已在 `.gitignore` 中。仍失败时检查该目录是否被另一个 pytest 进程占用。

## 模型不存在或下载不完整

重新运行：

```powershell
./scripts/download-models.ps1
```

默认配置期望：

```text
models/vosk-model-small-cn-0.22/am/final.mdl
models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx
models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt
models/silero-vad-v6.2.1/silero_vad.onnx
```

模型相对路径以所用配置文件的目录为基准，而不是永远以当前命令行目录为基准。对于全新下载，下载器在 staging 目录中核对项目固定的归档 SHA-256、解压、检查预期权重、下载许可文本并写入 `HANDSFREEPC_MODEL_SOURCE.txt`；这些全部完成后才替换目标目录，替换失败会尝试恢复旧目录。已有目录只有在预期权重、所有要求的许可文件和来源说明都存在时才跳过；跳过不重新校验历史归档哈希。若只有一个模型目录异常，删除该单个目录后运行不带 `-Force` 的 `./scripts/download-models.ps1`；`-Force` 会重新下载全部三个默认模型，只应在确实需要全量刷新时使用。不要从不明网盘复制模型。

## faster-whisper 后备未安装或首次运行突然下载

这是默认行为边界：普通 `install.ps1` 只安装 `audio` 与 `windows` extras，公开配置的 `speech.fallback.backend` 为 `none`。确实需要 Whisper 后备时，应在维护窗口先安装并显式预下载：

```powershell
./scripts/install.ps1 -WithWhisper
./.venv/Scripts/python.exe -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
```

该步骤会联网并产生 GB 级模型下载/缓存，也会增加磁盘、内存和推理负担。确认加载成功后，才把本地配置改为：

```yaml
speech:
  fallback:
    backend: faster-whisper
    model: large-v3-turbo
```

当前后备只在已经成功构造 SenseVoice 后、某次 `transcribe()` 调用抛出异常时触发；空文本、低置信度和 SenseVoice 启动/模型加载失败都不会触发。如果先改配置却未预下载，第一次命中这个异常分支时可能临时联网并长时间等待。0.2.0 自动化套件尚未覆盖该分支；不接受这条网络、资源或未覆盖测试边界时保持 `backend: none`。

## 没有音频输入设备

列出设备：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml list-audio-devices
```

在 Windows“设置 → 隐私和安全性 → 麦克风”中同时开启麦克风访问和桌面应用访问。然后在 `config.local.yaml` 中把 `speech.input_device` 设为返回列表中的输入设备编号；`null` 表示系统默认设备。

蓝牙耳机切换 profile、远程桌面重连、休眠恢复或 USB 麦克风拔插后，设备编号可能变化。停止并重新运行 HandsFreePC，再执行 `doctor`。

## 唤醒词总是听不到

逐项检查：

1. Vosk 模型目录存在；
2. 音频设备有输入电平，没有被应用独占；
3. `app.wake_phrases` 包含自然中文 `开始语音操作`；
4. `speech.wake.grammar` 有对应的分词形式 `开始 语音 操作`；
5. 说话时关键词之间不要刻意停顿太久；
6. 先在 0.5 m 安静环境测试，再逐步增加距离和噪声。

Vosk 小词表是低资源控制层，不是声纹认证，也不能保证电视或其他人不会误唤醒。运行时用 `speech.wake.phrase_window_seconds`（公开默认 5 秒）在有限 rolling window 内合并多个 final，因此慢速口令不必刻意说得飞快；但单词间停顿超过窗口仍会失败。高风险动作还必须在即将执行时确认。

## 一句话被截断或迟迟不结束

默认使用 Silero VAD。可调整：

- `speech.vad.threshold`：提高会减少噪声触发，但可能漏掉轻声；
- `min_silence_duration`：提高可容忍更长的句中停顿；
- `min_speech_duration`：过滤过短噪声；
- `max_speech_duration`：限制单句最长时间；
- `speech.trailing_silence_seconds`：仅在 energy 后端使用。

若 Silero 运行时加载或切句行为异常，可以暂时把 `speech.vad.backend` **准确写成** `energy` 做诊断。energy 是自适应能量门限后备，噪声环境下通常不如 Silero 稳；不要依赖拼写错误落入后备。另一个边界是：`doctor --strict` 仍无条件要求 Silero 模型文件和 sherpa 模块存在，因而 `scripts/run.ps1` 会在这些结构缺失时先拒绝；energy 只能在 strict 结构门禁通过时由脚本运行，或用直接 `handsfreepc ... run` 隔离诊断。不要一次改多个参数；每次用固定口令和相同距离记录结果。

## 说了 `over` 仍未入队

`over` 必须被 SenseVoice 转写成独立英文 token，匹配不区分大小写；`mouseover`、`voiceover` 不会切分。看遮罩区分“已记录，等待 over”和“已入队 N 条”：前者表示 delimiter 尚未识别，后者才表示进入普通 FIFO。一次 fragment 可含多个 `over` 并从左到右产生多条任务。

队列满时已完成 prompt 会被明确拒绝，不会静默丢弃；等队列有空间后必须重说。`simulate` 只走兼容 parser，没有连续 `over` 模拟器；无屏幕副作用的 delimiter/FIFO 检查应运行自动化测试，真实麦克风连续链路只能在四门禁全开的受控 Computer Use live test 中验证。

## SenseVoice 无法加载或转写为空

确认虚拟环境内安装的是项目锁定的 `sherpa-onnx==1.13.6`，模型目录同时有 `model.int8.onnx` 和 `tokens.txt`。用上游样例 WAV 隔离“模型问题”和“麦克风问题”：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml test-asr ./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/test_wavs/zh.wav
```

该命令只接受单声道、16-bit PCM WAV。其他格式需先用可信的本地工具转换。样例可用而实时为空，优先排查输入设备、VAD 和麦克风权限。

## 遮罩不显示、抢焦点或位置不对

先单独运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --duration 10 --mode overlay
```

检查 Windows 缩放、多显示器、远程桌面和全屏独占应用。遮罩使用 Tk 顶层窗口并尝试设置 Windows no-activate / click-through 样式；某些全屏应用或远程环境可能仍把它挡住。不要用不断 `Alt+Tab` 的方式强行让反馈置顶，因为那会破坏目标输入焦点。

## 语音反馈没有中文或系统识别了自己的声音

语音反馈使用 Windows 已安装的 SAPI voice；项目不附带中文 TTS 模型。安装合适的 Windows 中文语音，或改用默认 `overlay`：

```yaml
app:
  feedback_mode: overlay
```

连续会话支持 `overlay`、`voice`、`both`、`silent`，固定的反馈切换句由本地处理，带或不带 `over` 都不会进入 Computer Use FIFO。遮罩立即显示；`voice` / `both` 的待播反馈延迟到麦克风线程到达 utterance 边界，在该边界把多条按优先级合并，只播最高优先级中的最新一条，其余不会逐条补播。播完再 drain 输入并重置控制检测器。兼容路径也在完整 TTS 队列期间暂停识别并在结束后丢弃输入。**0.2.0 仍不能用停止词打断正在播放的 SAPI**，所以播报应短小；希望持续快速口述或保留逐条可见状态时使用 `overlay`。

纯 `voice` 模式下，高风险确认必须先完整成功播报；抢在提示结束前说“确认执行”不会授权。若 SAPI 拒绝、报错或提示被截断，程序会强制显示“切换到屏幕反馈后再确认”，pending action 继续锁定；先说“切换到屏幕反馈”或切 `both`，看清不超过 160 字的动作描述，再确认。连续路径会显式暴露这类语音错误；兼容路径的 SAPI worker/COM 错误仍可能只在进程内部，纯 `voice` 因而可能静默。

`doctor --strict` 不实例化 SAPI，也不枚举/试听声音。`overlay-demo` 与兼容路径的某些 SAPI worker、COM 或 voice dispatch 错误仍可能只保存在进程内部，不会让 `overlay-demo --mode voice` 返回失败；连续确认路径才有上一段的显式失败门禁。每台机器都必须人工听测 `voice`/`both`；默认 `overlay` 更稳妥，`both` 至少保留可见遮罩。

## 急停没有立刻生效

连续路径的“立即停止所有操作”“取消所有操作”等急停会设置当前任务的取消事件、清空普通/控制队列、终止或杀死 Codex 进程树，并关闭/丢弃当前 controller/thread 引用。这是 best effort：已经到达 Windows 或外部服务的点击、输入、发送等副作用不能撤回；下一轮会创建新的 controller/thread。

仍有三个不能承诺“立刻”的边界：

- Computer Use 底层 UI 调用可能在子进程收到终止前已经生效；
- SAPI 播放期间识别暂停，语音急停只能在播报返回后处理；
- 兼容 `EXECUTING` 中的同步 Windows/UIA 调用不能被语音抢占。

需要更强的带外停止时在启动终端按 `Ctrl+C`；但进程终止同样不能回滚已经发生的外部副作用。不要把 alpha 用于要求实时物理急停的设备或高风险流程。

## 说“结束语音操作”后仍在听，或一直未结束

这是连续协议的设计：“结束语音操作”只拒绝新的普通 prompt、丢弃未说 `over` 的半条，并进入 `DRAINING`；当前和已入队工作默认继续 FIFO 排空。麦克风仍用本地 Vosk 接收急停、确认和继续/恢复队列，退出进程或关闭系统麦克风权限才停止采集。

若排空中某条失败且后续仍有未完成任务，worker 会暂停，需说“继续队列”或急停；若失败的是最后一条，本轮会显示错误并直接回 `ARMED`，没有剩余队列可恢复。若等待高风险确认，必须在提示实际显示/完整播报后的 `execution.confirmation_timeout_seconds` 内说“确认执行”，或说急停/取消；“继续队列”会被拒绝。超时不是后台 Timer：无人说话时界面可能仍显示等待，下一段本地语音到来才检查；若已过期，该段被拒绝，并取消本轮、当前 controller 与全部队列。处理完当前与队列后才回 `ARMED`；已发生操作仍不可撤回。

## 兼容路径：文件或路径找不到

优先说出完整盘符和逐层名称。路径解析只会：

1. 使用显式别名；
2. 接受已存在的完整路径；
3. 对盘符路径逐层匹配；
4. 在配置的 `search_roots` 内有限搜索。

没有配置搜索根目录时，程序不会扫描整台电脑。两个候选得分过近会报歧义。请增加更具体的父目录或扩展名，不要降低 `ambiguity_threshold` 来掩盖同名问题。

0.2.0 的兼容执行器只接受本地路径合同。UNC、`//server`、URI（如 `file:` / `https:`）和 Win32 device namespace 会在任何文件系统访问前直接阻断，不能通过增加 `search_roots` 或确认短语放行；需要远程资源时先用受信任工具在项目外完成显式同步。这些 parser 白名单不约束连续 Computer Use。

## 兼容路径显示已打开，但文件内容不对

`open_path` 的结构化成功证据表示：路径唯一解析成功，并且 Windows Shell 启动调用返回成功。它不证明关联应用已经渲染了正确内容。检查：

- Windows 默认文件关联是否正常；
- 查看器是否在另一个桌面、显示器或已有窗口中复用；
- 文件是否被锁定、损坏或需要额外登录；
- 窗口标题和测试夹具内容是否一致。

这两层应在测试记录中分别标注。

风险判级并非“只拦一个危险后缀列表”：已存在目录和窄安全文件后缀可直接打开；未知后缀、无后缀普通文件以及主动/间接执行类型都要求完整确认短语。即使文件后缀看似安全，Windows 文件关联仍可能被错误配置或篡改，所以最终查看器状态始终需要人工或应用级 verifier 验收。

## Computer Use 报告完成，但屏幕没有变化

controller 只接受完整 JSONL turn，且最终消息必须是单行 `VERIFIED_COMPLETION:`、`NEEDS_CONFIRMATION:` 或 `FAILURE:`。这能拒绝模糊/无效协议输出，却不是第二个视觉 verifier：`VERIFIED_COMPLETION` 仍由执行动作的同一个 agent 根据自己的新观察报告。0.2 尚未完成真实屏幕验收。

保持目标应用在当前 active desktop 可见，人工检查/录制前台、指针、输入和应用级后置条件；确认是否出现未处理的 per-app approval，是否选错窗口，或 UIA/截图是否陈旧。屏幕与状态行不一致时按失败处理，不要因为 adapter 返回 success 就重试高风险动作。

## 兼容路径：找不到 Codex / Claude 窗口

HandsFreePC 先用 `process_names` 和 `title_patterns` 查找已有窗口；只有配置了 `apps.<name>.executable` 且文件存在时，才会尝试启动应用。

在本地配置中核对真实进程名、窗口标题和可执行文件绝对路径。标题模式不要写得过宽，否则可能误匹配浏览器标签或其他工具。若有多个匹配窗口且预期窗口不在前台，程序会故意报歧义；关闭多余窗口或先手动把目标置前。

## 兼容路径：项目、对话、Design 或语音按钮找不到

这些动作依赖 UI Automation 的可访问名称和控件类型，不依赖坐标。Electron / 自绘应用升级、界面语言变化、A/B 布局和折叠侧栏都可能改变 UIA 树。

建议：

1. 展开目标侧栏，并确保目标控件在 UIA 中可见且启用；
2. 只保留一个同名项目/对话；
3. 注意公开 Codex/Claude 档案的 `voice_button_names: []`、`native_voice_hotkey: null` 和 `search_hotkey: null` 是有意的安全默认值，不代表缺失安装步骤；
4. 用外部 UIA/辅助功能检查器确认唯一的实际可访问名称后，再更新 `voice_button_names`；0.2.0 没有内置 `inspect` 命令。若应用有稳定搜索/语音热键，也只在人工验证后配置；
5. 优先使用 HandsFreePC 自有听写，不要把原生语音按钮当作默认路径。

当前没有任意坐标点击或全屏视觉兜底。找不到稳定语义控件时应该失败，而不是“差不多点一下”。

## 兼容路径：文字没有输入，或因为窗口切换而失败

这是预期的安全检查。每次输入前程序都会再次核验：

- 目标窗口仍是前台；
- 焦点仍在唯一的 Edit / Document 控件；
- 控件不是密码框；
- 焦点控件仍与进入听写时固定的 RuntimeId/AutomationId 身份一致。

代码不会主动读取并比较目标进程完整性级别；不要以管理员身份运行目标应用而让普通权限 HandsFreePC 给它注入输入，Windows UIPI 通常会阻止跨完整性级别操作。也不要删除前台/控件身份复核来“修复”失败，否则文本可能进入错误窗口。即使输入调用报告成功，也只证明 `SendInput` 接受了 UTF-16 单元，不证明控件值已改变；提交只证明发送了 Enter，不证明消息出现或服务端接受。

## 兼容路径：应用内原生语音开启后像是暂停了

这是设计行为。`start_native_voice` 必须是计划最后一步且不能与反馈模式切换组合；非法组合在执行前阻断并回 `ARMED`。合法计划经确认、开始执行尝试后，成功或失败都会保守地留在 `PAUSED` 且只显示遮罩，因为按钮/热键失败不能证明第三方麦克风绝对未被部分触发。先在屏幕上核实并结束 Codex / Claude 自己的语音会话，再说 HandsFreePC 唤醒词恢复。第三方应用未释放麦克风时，恢复可能仍然失败。

## 连续 Computer Use 未启用、未就绪或不操作屏幕

公开 `config.example.yaml` 有意安全关闭。只在被 Git 忽略的 `config.local.yaml` 同时设置：

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

配置加载会拒绝缺任一云许可或仍标作 dry-run 的 Computer Use。再确认 controller 配置的 Codex CLI 已安装/登录，Computer Use plugin/server/skill 和 `node_repl` 已启用。`doctor --strict` 的 `ready_for_live_control` 只是静态线索，不验证登录、server、per-app approval、窗口可见性或点击。

Windows 目标应用必须在当前 active desktop 可见；Computer Use 会占用 foreground 并移动鼠标/键盘，不能在后台静默操作。首次控制某 app 时处理 Codex 的 per-app approval；`Always allow` 会成为 Codex 自己的持久决定，不受 HandsFreePC YAML 或删除本地配置自动撤销。不要批准密码管理器、支付或敏感通信应用。控制提示还禁止终端/Run、ChatGPT/Codex UI、管理员认证、UAC 和安全/隐私提示；这类目标被拒绝是预期行为。

若显示 `VERIFIED_COMPLETION` 但屏幕没变，按上一节作为假成功排查；仓库没有声称真实屏幕链路已通过。

## 兼容文本 planner 被禁用或提示需要云授权

云规划需要两个明确开关同时为真：

```yaml
privacy:
  allow_cloud_planner: true
planner:
  enabled: true
  backend: codex  # 或 claude
```

然后确认对应 CLI 已安装并完成官方账号登录。推荐让 HandsFreePC 在与 planner 相同的清洗环境中显式检查：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth
```

普通 `doctor` 只检查命令是否存在；`--check-planner-auth` 才执行认证状态命令，因此可能联网。Codex 的非交互命令是 `codex exec`；`codex -p` 是 profile。Claude 才用 `claude -p`。HandsFreePC 会移除规划器子进程环境中名称含 API key、token、secret、password 或 credential 的变量，因此应使用 CLI 自身的订阅登录会话，不应依赖环境变量密钥。planner 启动或超时错误会泛化反馈，不回显原始 prompt 或 provider stderr。

超时可在 `planner.timeout_seconds` 中调整，但先确认 CLI 单独可用。不要把含有真实路径、病历、学生信息、账号或秘密的句子用于云规划测试。

## 开机自启没有生效

HandsFreePC 使用当前用户的 Startup 快捷方式，不是 Windows Service：

```powershell
./scripts/install-autostart.ps1
```

确认 `.venv/Scripts/pythonw.exe` 和 `config.local.yaml` 仍在原位置。移动仓库后应先卸载再重装快捷方式：

```powershell
./scripts/uninstall-autostart.ps1
./scripts/install-autostart.ps1
```

Startup 快捷方式用 `pythonw.exe ... run` 直接启动，没有 `doctor --strict` 门禁、console、持久日志、托盘图标、自动重启或失败通知；模型、麦克风或配置异常可能看起来像“什么也没发生”。先在交互式 PowerShell 中运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./scripts/run.ps1
```

修复这里显示的错误并确认 live 运行后，再重新启用 Startup。

不要把它改成 Session 0 服务；服务不能可靠地操作当前登录用户桌面。

## 提交问题前

请提供：

- Windows、Python 和 HandsFreePC 版本；
- 目标应用名称和版本；
- 最小可复现口令；
- 期望/实际状态和脱敏后的 error type；
- 是否为 dry-run、模型 smoke 或真实 live test；
- 麦克风距离、噪声和 DPI 等必要环境信息。

不要公开提交音频、完整转写、本机绝对路径、窗口标题、项目/对话真实名称、日志、`config.local.yaml`、token 或其他凭据。安全漏洞请按根目录 [SECURITY.md](../SECURITY.md) 私下报告。
