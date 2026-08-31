# HandsFreePC

HandsFreePC 让 Windows 11 在双手被占用时继续接受语音操作：说“开始语音操作”进入连续会话，每条指令以英文 `over` 结束并按 FIFO 排队，说“结束语音操作”停止接收新指令并排空已接受队列。

0.3.0 的关键变化是：**Codex 或 Claude 只负责规划下一步，本项目自己持有鼠标键盘权限，并在每个通用 planner 动作后重新观察、在本地验收。** 不再把 `codex exec` 的“已完成”文本当成屏幕操作成功的证据。

> [!WARNING]
> 这是面向 **Windows 11、64 位 Python 3.11/3.12** 的 alpha。公开配置默认关闭电脑控制、云规划和真实执行。即使通过自有 fixture 的 live test，也只证明本机 UIA 基础链路可用，不证明任意第三方应用或高风险任务安全可控。

## 现在的底层

```text
麦克风
  -> 本地 Vosk 控制词 + Silero VAD + SenseVoice 正文转写
  -> “开始语音操作”会话 / over 分段 / 有界 FIFO
  -> NativeSkillRouter（确定性命令优先）
  -> StepPlanner（默认 Claude CLI；Codex CLI 仅为显式 best-effort 备选）
  -> DesktopDriver（默认项目自有 Windows UIA/Win32 驱动）
  -> fresh observe
  -> LocalVerifier（本地比较动作前后状态和任务完成条件）
```

核心约束：

- 持续监听、`over` 分段、执行期间继续收音、FIFO、失败暂停、结束后 drain 和急停均保留；
- 确定性解析命中时，`NativeSkillRouter` 先解析目标并走本地白名单执行器，不调用模型；
- 普通桌面任务必须在口述中明确且肯定地只指定一个目标应用，才进入单步 agent loop；零个、多个、仅否定提及或顺带提及应用都会拒绝；
- 通用 agent 的每个 planner 动作都执行 `fresh before -> 任务后置条件此时必须为 false -> 一个动作 -> fresh after -> 同一后置条件必须为 true`，再交给 LocalVerifier；确定性 native skill 使用各动作自己的本地证据，精确目标状态已成立时可幂等成功；
- planner 没有鼠标键盘能力，也不能把 shell 命令放入动作 Schema；UI 文本一律按不可信数据处理；
- 默认 `windows_uia` 驱动把元素索引绑定到应用、窗口和本次 observation generation；动作后旧索引立即失效；
- 本地有限语法把动作类型、目标完整短语、完整口述输入 payload、按键、左右键/点击次数、secondary action、滚动方向/页数逐项绑定到当前用户步骤；`type/input` 只授权键入，`fill/write` 才授权直接设置字段值；不能把“Open settings”缩成当前唯一可见的“Open”，不能只输入口述文本的子串，也不能借用同句后续输入动作的 payload；若文本动作后还有独立、非空且肯定的 clause，payload 出现不能自证完成，必须验证用户给出的真实结果；条件句在没有本地条件求值器时整体拒绝，尚不支持的尾随桌面动作也会计入步骤数，不能被提前 `DONE` 掩盖；
- 只有本地 verifier 通过才返回 `LOCAL_VERIFIED_COMPLETION`。驱动“已接受动作”或 planner “done”都不是证据；
- 公开配置默认使用 `strict`：通用 agent 的 `type_text`/`set_value` 文本输入使用绑定到**确切动作与确切界面快照**的确认。仅在本机忽略提交的配置里显式使用 `personal_trusted` 时，才可免确认执行安全导航，以及把用户本句完整口述的草稿写入唯一、已聚焦、非密码输入框；它不会自动点击发送。两种模式下，被本地已知词形识别为发送/提交、删除、安装/卸载、上传/共享、关闭等副作用仍要求确认；认证、密码、付款、UAC 和 Windows Security 界面仍直接阻断。每次确认提示会生成新的随机四位口令；只说静态“确认执行”、口令不匹配、已使用、超时或界面变化都会拒绝；
- 风险词表和上下文规则不是完整语义证明：未知语言、同义词、自绘控件或伪装文案仍可能漏分。重要外发、删除、安装、分享或不可逆任务必须有人看屏幕监督；
- 完整本地快照一旦被已知词形/元素属性识别为密码、凭据、付款/转账、认证、隐私/公开链接设置、终端/shell、UAC 或 Windows Security surface 就 fail closed；纯坐标点击默认阻断。未识别 surface 仍是残余风险；
- 通用 UI 确认摘要若原文显示目标标签，只显示已由用户原句精确授权并再次验证的 exact target label；未授权 sibling/window 标签的原文和语义只在本地参与分类，不进入摘要，摘要中的不可逆短 digest 仅是绑定元数据；

完整设计见 [架构说明](docs/ARCHITECTURE.md) 和 [安全模型](docs/SECURITY_MODEL.md)。

## 快速开始

```powershell
git clone https://github.com/chenqin3/HandsFreePC.git
Set-Location HandsFreePC
Set-ExecutionPolicy -Scope Process Bypass
./scripts/install.ps1 -DownloadModels
```

安装脚本会创建 `.venv`、安装本地语音和 Windows 依赖，并复制一份不会提交的 `config.local.yaml`。默认不会安装 faster-whisper，也不会启用电脑控制。

先运行无桌面副作用检查：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听"
```

`doctor` 是**静态预检**。它会报告依赖、模型、音频输入、CLI 和配置是否看起来齐全，但始终令：

```text
live_control_verified = false
ready_for_live_control = false
```

它不会因为发现某个 CLI、plugin 或 UIA 包就声称真实控制已经验收。

## 先验证项目自有的桌面驱动

`computer-doctor --live` 是 opt-in 测试。它只打开 HandsFreePC 自己的无害 fixture，把含中文的随机 token 写入唯一文本框，重新读取 UIA，并通过本地 verifier 检查 Unicode round-trip。它会抢占一次前台焦点，但不会操作 Codex、Claude 或用户文件。

若当前前台属于更高完整性进程，Windows 可能返回 `ForegroundIntegrityBoundary`。项目会失败关闭，不会自动提权、结束该进程或跳过前台 HWND 验证；处理方法见 [故障排查](docs/TROUBLESHOOTING.md)。

在本地配置中临时使用不联网的测试组合：

```yaml
privacy:
  allow_cloud_planner: false

computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
  allow_screen_context_to_cloud: false

execution:
  dry_run: false
```

然后运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml computer-doctor --live
```

只有输出中 `live_control_verified: true` 才表示这次 fixture 验收通过。该结果仍不覆盖麦克风、云 planner、应用选择器、第三方窗口或业务任务；详见 [测试指南](docs/TESTING.md)。

### 观察 Claude / Codex 的真实界面

在 fixture 通过后，可用 `app-doctor` 对已经打开的目标应用做受控检查。先把目标 Claude 或 Codex 窗口置于当前桌面，再运行只读观察：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --observe-only
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --observe-only
```

`--observe-only` 不点击、不输入，只输出元素数量、控件类型、截断/省略统计和不可逆摘要等脱敏信息；不会输出聊天正文、输入值、窗口完整文本或截图。它用于确认应用 profile、窗口选择和 UIA tree 是否可用，不代表业务流程已经完成。

确认观察结果正常后，可显式运行草稿 smoke：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --draft-smoke
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --draft-smoke
```

`--draft-smoke` 只会在可唯一、安全绑定的非密码编辑框中写入一个随机测试 token，再通过 fresh observe 和 LocalVerifier 读回；**不会点击发送，也不会替你提交 prompt**。它会在界面里留下未发送草稿，测试后可人工清除。若编辑框不唯一、没有可靠焦点、目标是敏感字段或界面无法读回，命令会失败关闭。

## 启用连续桌面 agent

任意 UI 任务通常需要单步 planner。Codex 和 Claude 都可以规划；真正执行动作的仍是本地 `DesktopDriver`。只在被 Git 忽略的 `config.local.yaml` 中显式授权：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: claude
  safety_profile: personal_trusted
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: false
  allow_legacy_codex_computer_use: false
  allow_experimental_driver: false
  allow_coordinate_actions: false

execution:
  dry_run: false
```

公开仓库的 `config.example.yaml` 始终使用 `safety_profile: strict`。上面的 `personal_trusted` 只适合写入不会提交的 `config.local.yaml`，用于本人看着屏幕、目标应用和 Windows 会话都受信任的电脑：它允许 planner 逐步完成安全导航，并把**本句完整口述、未发送的草稿**写入唯一聚焦编辑框，减少抱娃场景下反复念确认码。它不是“关闭全部安全”；发送/提交、删除、上传/分享、安装/卸载、关闭等副作用仍要求本轮随机码，密码/令牌/认证/付款/UAC/Windows Security 仍阻断，纯坐标和 shell 仍不可用。

若要复现公开项目的最保守行为，改回：

```yaml
computer_control:
  safety_profile: strict
```

这会把完成的语音 prompt、唯一明确授权的可见应用摘要、observation generation 和最近的本地验收摘要发送给选定的 planner。`strict` 只暴露本句肯定且精确点名的可寻址控件；`personal_trusted` 还可暴露已授权应用内的安全导航控件和当前输入框，但内容层节点始终不进入 planner。原始窗口标题、进程 ID、聊天正文、automation ID、元素 value、原始音频、截图字节和真实截图可用性不进入项目构造的单步 prompt；本地仍保留完整快照用于 freshness 与动作后验收。`allow_screen_context_to_cloud` 仍是必需的，因为可见控件标签和状态也属于屏幕上下文。先登录相应 CLI，再运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth --strict
./scripts/run.ps1
```

Claude CLI 是默认规划器，使用独立 system policy、safe/restricted 模式、空工具列表、严格 MCP 配置和非持久会话。它仍是联网 CLI，不是本地模型。

若明确要用订阅版 Codex CLI，只能写 `planner_backend: codex_cli_best_effort`，并额外设置 `allow_codex_cli_host_read: true`。项目会尽量禁用已知工具并使用临时目录、结构化单步输出和 read-only sandbox，但 Codex CLI 没有可由本项目证明的完整 no-tools 模式；这个开关表示你接受它仍可能读取当前 Windows 账户可见的其他主机文件。两条路径都不获得 HandsFreePC 的桌面驱动。参考 [OpenAI Computer Use 自定义 harness 指南](https://developers.openai.com/api/docs/guides/tools-computer-use)、[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 和 [Responses API tool choice](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。

不要把 `computer_control.planner_backend` 与顶层 `planner.enabled` 混淆。后者只是兼容旧单句 `VoiceRuntime` 的云 fallback：模型只能提出 `ACTIVATE_APP`、`OPEN_CONVERSATION`、`OPEN_MODE`、`ENTER_DICTATION` 或 `START_NATIVE_VOICE`，并且应用及项目/对话/tab/mode 必须由用户原句肯定、非引号/数据引用地精确授权。它不能决定反馈模式、暂停/恢复/等待，也不能打开路径、输入文本或发送 prompt；这些能力只能由完整命中的本地确定性 parser 决定。

旧单句运行时若需要确认，`native-...` ID 会绑定完整 plan（含 source）及每个已解析路径的规范绝对路径与 stat 身份；普通文件还绑定 SHA-256。说出本轮随机码后会重新 prepare 目标、重新运行 safety 并重新计算 binding；计划、来源、路径身份或文件内容任一变化都会取消，不会执行替换后的目标。即使 `OPEN_PATH` 被判定为安全目录而无需口令，runtime 和 deterministic native router 也都会在 safety 前后重新绑定目标，并从最后绑定一直持有防替换句柄到本地执行返回。

Electron 应用的可访问标签会随版本变化。`apps.*.mode_names` 同时是 native 模式 allowlist：只有显式 key 可执行，并映射到当前版本的精确 UIA 标签（如 Claude 的 `Chat and Cowork`）；缺少映射会在任何点击前拒绝。执行器只接受精确匹配，并在动作后验证 `selected`。单纯获得键盘焦点只证明点击到控件，不证明页面已经切换。若 Codex/Claude 应用只暴露一个空 Pane、没有可操作子元素，或模式控件不暴露可验证的选中状态，UIA 路径会失败关闭，不能靠模糊匹配或坐标猜测。

## 语音协议

```text
开始语音操作
打开 Claude 并切换到 Chat 选项卡 over
在 Claude 点击新对话，并在输入框写入测试内容 over
结束语音操作
```

- 唤醒词现在是“开始语音操作”；结束词是“结束语音操作”；
- 每个独立英文单词 `over` 完成一条 prompt；`mouseover` 和 `voiceover` 不会切分；
- 一条正在执行时可以继续说下一条，普通任务严格 FIFO；队列满会明确拒绝，不会静默丢弃；
- “结束语音操作”丢弃尚未由 `over` 完成的半条，并默认排空已接受任务；
- “立即停止所有操作”“取消所有操作”等急停词请求取消当前任务并清空队列，但不能撤回已经发生的点击、输入或外部副作用；
- 失败或待确认会暂停队列。提示会给出随机四位挑战码，例如“确认执行 4 8 2 7”；只有本轮准确口令会携带运行时保存的 confirmation ID。单独说“确认执行”无效，也不会作为新 prompt 交回模型猜测。
- 同一 `VoiceRuntime` 进程运行期内，已经签发的四位码持续保持占用：确认、取消或超时都不回收到抽样池；有界重抽仍找不到新码时 fail closed。该集合不持久化，进程重启后不保证绝对不复用，所以四位码不是持久化防重放凭证。

### `over` 的当前限制

0.3 只增加了 `PromptAssembler.finalize()` 这个未来 KWS 可调用的 out-of-band seam；**当前运行时的 `over` 仍由正文 SenseVoice ASR 识别**，并不是独立关键词检测器。短英文词在中文语流中可能漏识别。

未来接入独立 KWS 不能简单再开一次麦克风：需要单一音频采集后的 block fan-out、统一时间戳、命中前音频前缀回灌和去重，否则会吞掉 `over` 前的正文。候选 sherpa-onnx KWS 模型的许可归属仍待上游澄清，因此 0.3 不自动下载或宣称已经启用。当前建议清晰说出 `over`，并以屏幕队列反馈确认是否入队。

## 屏幕与语音反馈

`app.feedback_mode` 支持：

- `overlay`：默认，高对比置顶大字，不抢焦点；
- `voice`：本机 Windows SAPI 朗读短反馈；
- `both`：两者同时；
- `silent`：隐藏普通反馈，但安全确认/错误仍可能强制显示。

可以说“切换到屏幕反馈”“切换到语音反馈”“大字和语音两种都开”或“切换到静默模式”。SAPI 播放期间采用半双工处理，播报结束前说的话可能被丢弃，且语音急停不能打断正在播放的 SAPI；需要连续快速输入时优先用 `overlay`。

## 本地诊断日志

运行时会写入有界轮转的 JSONL 诊断事件。任务失败后先看最近事件，再看最近一次失败：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml logs --tail 50
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml diagnose-last
```

默认文件位于 `%LOCALAPPDATA%\HandsFreePC\logs\handsfreepc.jsonl`，单文件最大 2 MiB，保留 5 个备份。事件只保留 `stage`、`error_code`、异常类型、应用代号、observation generation 和短安全说明等白名单字段；不记录原始语音 prompt、完整转写、UIA 正文/值、截图、provider stderr、绝对路径或凭据。它是定位 `plan`、`observe_driver`、`action_safety`、`execute`、`reobserve`、`verify_action`、`verify_completion` 等阶段的诊断线索，不是完整操作审计。

## 可选 Qwen Open Computer Use 驱动

[Qwen open-computer-use](https://github.com/QwenLM/open-computer-use) 0.2.3 的 MCP server 可作为持久连接的实验驱动，但**不是默认依赖，也不会自动安装**。0.3 固定适配 0.2.3，并要求同时设置：

```yaml
computer_control:
  driver: open_computer_use
  allow_experimental_driver: true
```

Windows 中文环境存在尚未合并修复的 UTF-8/PowerShell 边界问题：[Issue #5](https://github.com/QwenLM/open-computer-use/issues/5)、[PR #6](https://github.com/QwenLM/open-computer-use/pull/6)。适配器发现 Unicode replacement character、前后空白会被 0.2.3 截断、过期 observation 或变更请求结果未知时会 fail closed。不要把 ASCII smoke 当作中文可用证明。完整说明见 [Open Computer Use 适配说明](docs/OPEN_COMPUTER_USE.md)。

此外，0.2.3 的 `get_app_state` 在当前适配中没有提供可安全绑定的结构化元素列表。因而通用 planner 不能可靠地产生元素索引：点击/导航能力受限，`type_text`/`set_value` 也会因缺少可核验的焦点元素而保守失败。它不是默认 UIA driver 的等价替换。

## 旧 `legacy_codex_cli`

0.2 的 `CodexComputerController` 只保留为显式兼容后端：

```yaml
computer_control:
  backend: legacy_codex_cli
  allow_codex_cli_host_read: true
  allow_legacy_codex_computer_use: true
```

它依赖 Codex thread 和 Computer Use plugin，并把同一 agent 的 `VERIFIED_COMPLETION` 状态作为协议结果；没有 0.3 的本地动作级 verifier，**不能作为可信验收路径，也不会自动回退到它**。新安装不要使用。`codex -p` 是 profile 参数，不是 Claude 式 prompt 参数；Claude 的非交互参数才是 `claude -p`。

## 默认隐私与安全边界

- 原始音频和转写默认不落盘；
- 云 planner、电脑控制、屏幕上下文许可和真实执行默认关闭；
- 不允许任意 shell、PowerShell、Run 对话框或自定义脚本进入桌面动作 Schema；
- 完整 UIA 名称、标题和页面文本只在本地观察/验收，但仍可能包含敏感信息；云 planner 只接收本句肯定且精确点名控件的最小子集。为降低本地旁观和规则漏检风险，首次验收仍应关闭无关窗口；
- 上述“当前 prompt 不含音频/截图字节”等范围只描述 HandsFreePC 主动组装的输入；Codex/Claude CLI 及其提供商仍可能处理账户、网络、CLI/OS/runtime、临时工作目录和诊断/遥测等自身元数据，项目开关不能把这层变成零元数据；
- 确定性 `OPEN_PATH` 会要求后置条件先为 false、打开后为 true，且前台 HWND 必须发生变化。Explorer 目录再通过规范化路径精确验证；普通文件目前只能检查新前台窗口标题包含精确文件名，仍是 best-effort。同名文件、不显示文件名或复用同一窗口 HWND 的应用仍需人工检查；
- 常开麦克风仍会在内存中处理房间声音，包括儿童、访客和远程通话；请遵守告知、同意和当地法律；
- 随机四位口令在本次进程运行期内不复用，降低固定录音重放风险；但它不是持久凭证，没有说话人识别，重启后也不保证绝对不复用，更不能抵御旁人、扬声器或实时转述/重放听到本轮口令后代说；高风险动作必须有人看屏幕监督；
- `silent`、结束会话或急停都不等于关闭麦克风；完全停止采集需退出进程或关闭 Windows 麦克风权限；
- 本项目不管理 Codex/Claude 的登录缓存、提供商留存或账户数据控制。

详见 [PRIVACY.md](PRIVACY.md)、[SECURITY.md](SECURITY.md)、[故障排查](docs/TROUBLESHOOTING.md) 和 [第三方许可说明](THIRD_PARTY_NOTICES.md)。

## 开发测试

```powershell
./.venv/Scripts/python.exe -m pytest -q -m "not live" --basetemp ./.pytest-tmp/unit
./.venv/Scripts/python.exe -m ruff check handsfree_pc tests
```

带 `@pytest.mark.live` 的测试会打开窗口或移动焦点，必须显式运行。自动化通过不能替代目标应用的人工受控验收。

---

**English summary:** HandsFreePC 0.3.0 keeps continuous local speech input, `over` segmentation, and FIFO execution, but replaces model-owned mouse/keyboard control with an owned Windows UIA driver. Claude CLI is the default strict step planner. Codex CLI is an explicit best-effort alternative that requires host-read consent and is not a guaranteed no-tools mode. Every generic planner action requires a false-before/true-after local postcondition; deterministic native skills use action-specific local evidence and may succeed idempotently when the exact target state already holds. The public `strict` profile requires a fresh random four-digit confirmation for generic text entry; an explicitly local `personal_trusted` profile may enter the user's exact spoken draft into one focused non-password field, but never auto-sends it and retains confirmation/blocking for side effects and sensitive surfaces. Cloud planning and live control remain disabled by default. The legacy Codex Computer Use controller and Qwen open-computer-use 0.2.3 adapter are explicit, non-default compatibility/experimental options.
