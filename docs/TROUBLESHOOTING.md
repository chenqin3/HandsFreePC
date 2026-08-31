# HandsFreePC 0.3 故障排查

先区分问题发生在哪一层：控制词、正文 ASR、`over` 拼装、FIFO、planner、driver、fresh observation、LocalVerifier 或反馈。不要只看“我在听”或“操作成功”遮罩推断整条链路工作。

## 最小诊断顺序

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml list-audio-devices
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听"
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml logs --tail 50
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml diagnose-last
```

最后两条读取的是隐私受限的本地 JSONL 事件：`logs --tail 50` 展示最近 50 条，`diagnose-last` 只找最近一次失败。默认路径是 `%LOCALAPPDATA%\HandsFreePC\logs\handsfreepc.jsonl`；日志会轮转，且不含原始 prompt、UIA 正文/值、截图、provider stderr、绝对路径或凭据。优先看 `stage` 与 `error_code`，再定位下文对应层，不要只根据遮罩里的短句猜测原因。

需要验证自有 UIA driver 时，再按 [TESTING.md](TESTING.md) 配置 `local_agent/windows_uia`、`planner_backend: none`，显式运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml computer-doctor --live
```

普通 `doctor` 是静态预检，`ready_for_live_control` 和 `live_control_verified` 应为 `false`。只有 `computer-doctor --live` 对项目自有 fixture 的实际 Unicode round-trip 可以令后者为 `true`。

## “开始语音操作”说慢了不识别，说快反而成功

控制词由本地 Vosk 小词表和 `phrase_window_seconds` 判断，不是正文 SenseVoice。检查：

1. `app.wake_phrases` 中是完整“开始语音操作”；
2. `speech.wake.grammar` 有带空格的 `"开始 语音 操作"`；
3. 使用 `list-audio-devices` 确认选中的麦克风；
4. 避免在四个字之间停很久；控制词需要落在同一短窗口；
5. 调整 Windows 输入增益，避免第一个字被噪声门截掉；
6. 不要同时让另一个应用独占麦克风；
7. 用 overlay 观察是否真的进入连续 `ACTIVE`，而不是兼容状态机的一次命令。

不要为解决这一问题随意降低所有安全阈值或扩充大量相似唤醒词；那会增加旁人和扬声器误触发。先用受控录音对 Vosk grammar 做离线测试。

## 已显示“我在听”，但后续指令不执行

按顺序检查：

- `computer_control.enabled` 是否为 `true`；
- `execution.dry_run` 是否为 `false`；
- backend 是否为 `local_agent`，不是旧 `legacy_codex_cli`；
- `doctor` 中 `models.delimiter.ready` 是否为 `true`，日志中是否出现 `PROMPT_DELIMITER_DETECTED`；
- 队列是否已满、暂停或等待确认；
- 云 planner 开启时，两项许可和 CLI 登录是否齐全；
- planner miss/失败后是否明确显示 `FAILURE`；0.3 不会静默换成另一套 controller；
- 目标应用是否已运行并匹配 `apps.*.process_names/title_patterns`。

`enabled: false` 时可以测试麦克风和兼容 parser，但不会启动连续桌面 agent。

## `over` 经常漏掉

当前版本用独立英文 Vosk 模型检测 `over`，同时保留 SenseVoice 正文识别作为后备。先检查：

- 升级代码后重新执行 `./scripts/download-models.ps1`；
- `doctor --strict` 的 `models.delimiter.path` 指向 `vosk-model-small-en-us-0.15` 且 `ready: true`；
- 把 `over` 作为一个清晰、独立的英文词说出；很短的自然停顿有助于识别和样本边界稳定，但不是协议强制要求；
- 用 `overlay` 查看队列数；没有显示“已入队”时不要继续堆很多正文；
- 用 `logs --tail 50` 查 `PROMPT_DELIMITER_DETECTED`。有该事件但无 `COMMAND_ENQUEUED`，说明 marker 前没有形成非空正文；两者都没有则是 KWS/麦克风层；
- 不要在配置中增加过多常见中文短词作为 delimiter，容易在正文误切；
- `mouseover`、`voiceover` 不会切分，这是预期行为。

检测器使用同一麦克风 block，不会另开音频设备；命中后继续录到 VAD 终点。运行时优先用 Vosk 词级/partial 词级时间形成 marker 样本区间，没有可用词时间时退回到命中 block；随后按 marker 区间切分本轮内存音频，marker 本身不进入 SenseVoice，前后非空段分别转写。它支持同一个 VAD 话语内的多个 `over` 和紧随其后的下一条正文，但边界只是识别结果，不保证在所有口音和噪声下精确；异常时先放慢并短暂停顿，再结合 `PROMPT_DELIMITER_DETECTED`、`COMMAND_ENQUEUED` 与 overlay 判断发生在哪一层。

## 任务显示成功，但屏幕没有变化

先检查 backend：

```yaml
computer_control:
  backend: local_agent
  driver: windows_uia
```

`local_agent` 只有在 fresh observation 和 LocalVerifier 通过后才返回 `LOCAL_VERIFIED_COMPLETION`。若看到单纯 `VERIFIED_COMPLETION:`，通常仍在旧 `legacy_codex_cli`；该状态由同一个 Codex agent 自报，不是可信验收。

对通用 agent loop：

1. 运行 `computer-doctor --live`，确认自有 fixture 的 Unicode round-trip；
2. 查看失败是 window resolution、action、fresh observe 还是 completion expectation；
3. 确认本动作任务后置条件在 fresh before 为 false；若动作前已经成立，系统会拒绝把无变化操作算成功；
4. 确认 after generation 严格增加、fingerprint 变化且同一后置条件为 true；
5. 输入任务要求 exact text 出现在新 UIA 状态；应用若不暴露 value，LocalVerifier 会保守失败；
5. 对“打开/切换”任务要求具体可观察后置条件，不要只要求 planner 报 done；
6. 人工核对实际屏幕。UIA 文字出现仍不一定证明远端发送、保存或同步成功。

不要用延迟、重复点击、坐标 fallback 或 shell 来“掩盖”失败；这会破坏动作绑定和防重放。

## `computer-doctor --live` 拒绝运行

它只支持：

```yaml
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none

execution:
  dry_run: false
```

常见 `error_type`：

- `UnsupportedPlatform`：不是 Windows；
- `LiveDoctorBackendUnsupported`：不是 `local_agent/windows_uia`；
- `ComputerControlDisabled`：总开关关闭；
- `DryRunEnabled`：仍为 dry-run；
- `ForegroundIntegrityBoundary`：当前前台窗口处于更高完整性级别，Windows 拒绝连接输入队列；
- fixture/driver 异常：UIA 依赖、前台桌面、唯一文本框或 Unicode 验收失败。

该命令不会替 Qwen driver、Codex/Claude planner 或真实应用做验收。失败时不要把 `planner_backend` 改成云端来绕过 fixture。

## fixture 通过后，怎样检查 Claude / Codex

`computer-doctor --live` 只证明项目自有 fixture。要确认真实应用能否被当前 UIA profile 观察，先打开并置前 Claude 或 Codex，再运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --observe-only
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --observe-only
```

`--observe-only` 不执行鼠标键盘动作，只报告脱敏控件统计、截断/省略情况和摘要；它不会打印聊天正文、字段值或截图。若这里失败，先修正 `apps.*.process_names/title_patterns`、重复窗口、目标窗口前台状态或应用辅助功能，不要直接尝试真实语音任务。

只读观察通过后，才能显式做未发送草稿 smoke：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --draft-smoke
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --draft-smoke
```

该命令只向唯一、安全绑定的非密码编辑框写入随机 token，并 fresh observe 后本地读回；不会点击发送。成功路径仅在字段仍精确等于本轮固定格式 token 时自动清空，并报告 `cleanup_verified: true`。出现 `ComposerNotUnique`、没有可靠焦点、敏感输入框、读回或清理失败时，应调整窗口/焦点或应用 profile 后重试，不要打开坐标 fallback。

## `strict` 与 `personal_trusted` 表现不同

公开示例默认：

```yaml
computer_control:
  safety_profile: strict
```

`strict` 会让通用文本输入走绑定当前界面的随机码确认。本人的受监督电脑若希望先验证连续“听—导航—写草稿”，可以只在 Git 忽略的 `config.local.yaml` 中显式改为：

```yaml
computer_control:
  safety_profile: personal_trusted
```

`personal_trusted` 可免确认完成安全导航，并把用户本句完整口述的文字写入唯一聚焦、非密码编辑框；它不自动发送。发送/提交、删除、上传/分享、安装/卸载、关闭等副作用仍需本轮随机码，密码/令牌/认证/付款/UAC/Windows Security 仍阻断，纯坐标、shell 和未可靠绑定的控件仍不可用。若草稿输入仍要求确认，检查当前配置实际加载的是哪个文件、`safety_profile` 拼写，以及目标输入框是否唯一、聚焦并由 UIA 标为非密码字段。

## 静态 doctor 通过，但 `computer-doctor --live` 失败

静态检查只证明文件和命令存在。live failure 常见原因：

- 当前不是可交互 `Default` desktop（锁屏、UAC、安全桌面、切换用户）；
- pywinauto/pywin32 安装到了另一个 Python；
- 杀毒/企业策略阻止 UIA 或 `SendInput`；
- HandsFreePC 被以不同完整性级别运行；
- fixture 启动慢、窗口被系统隐藏或 foreground activation 被拒；
- 中文 token 写入后无法从 UIA value 读回。

`ForegroundIntegrityBoundary` 不是可重试的 selector 错误，也不能靠重复点击、Alt 技巧或跳过前台断言修复。先由用户正常退出/降权造成高完整性前台的应用，或在组织明确评估后让目标应用与 HandsFreePC 处于相同完整性级别；项目不会自动提权、关闭进程或绕过 UIPI。一般仍以普通用户运行，不要仅为了让测试变绿改成管理员。记录 JSON 中的 `error_type`，再在同一 `.venv` 检查模块。

## 找不到或无法唯一选择应用

默认 `windows_uia` 只观察 `apps` 中配置的 profile。检查进程名和窗口标题：

```yaml
apps:
  claude:
    process_names: ["claude.exe"]
    title_patterns: ["Claude"]
    mode_names:
      chat: ["Chat and Cowork", "Chat"]
      code: ["Code"]
      design: ["Design"]
```

- 未运行的应用不会由 generic driver 自动猜路径启动；可由确定性 native skill 在显式 `apps.*.executable` 配置后启动；
- 零个匹配返回 not found；
- 多个匹配只在其中唯一一个是 foreground 时接受，否则返回 ambiguous；
- title pattern 太宽会命中错误窗口，太窄会因语言/版本变化失效；
- process name/title 不是签名验证，不要为高价值应用信任同名陌生窗口。
- `mode_names` 同时是 native mode allowlist：只有显式 key 可执行，labels 按优先顺序映射到该版本的精确 accessible label。缺少映射会在输入前拒绝；只接受 normalized exact match，并要求最终 mode 变为 selected，focus-only 不算完成。应用升级后若 `Chat` 变成 `Chat and Cowork`，应更新映射并重测，不要降低模糊阈值。

先关闭重复测试窗口或把正确窗口放前台，再做低风险观察。通用任务的口述还必须肯定且只明确指定一个应用；“不要操作 Claude”“比较 Codex 和 Claude”“我在 Claude 看到了错误，帮我处理”这类零授权、多个或顺带提及不会由 planner 猜目标。把命令改成如“在 Claude 点击 Chat”并保持只授权一个应用。

## 打开路径后仍显示失败，或打开了同名文件

确定性 `OPEN_PATH` 不是只检查 Shell dispatch 返回值：执行前目标后置条件必须为 false；打开后必须成为 true，而且前台 HWND 必须与 before 不同。

- 目录：前台 Explorer 的规范化路径必须与目标精确一致，证据较强；
- 文件：当前只能检查新前台窗口标题是否包含精确文件名，这是 best-effort；复用同一 HWND 的查看器会保守失败。

因此同名文件、查看器复用旧窗口、标题不显示文件名、启动后仍有选择器/登录页时都可能无法证明或存在误判。增加完整父目录只会改善解析，不会把文件标题验证升级为内容验证；重要文件请人工看屏幕核对，不要放宽 verifier。

## UIA 看不到按钮、输入框或选项卡

Electron/canvas/远程桌面应用可能只暴露部分 accessibility tree。表现包括：元素缺失、多个同名元素、焦点状态不见、输入后 value 不可读。

可做：

- 开启应用自身的辅助功能支持；
- 在目标版本、语言和账号布局上用 UIA 检查器确认实际 tree；
- 为稳定动作增加确定性 native skill，而不是猜坐标；
- 若无法在 fresh state 中验证后置条件，保守失败并人工完成。

不要把 `allow_coordinate_actions` 当通用修复。默认安全层会阻断无 semantic target 的坐标 click/drag。

## planner 不可用或无法解析

配置示例：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  planner_backend: claude
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: false
```

检查认证：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth
```

Claude 是默认 planner。Codex 与 Claude 登录互不替代。`codex -p` 是 profile；Codex 非交互入口是 `codex exec`。Claude 的 prompt 模式才是 `claude -p`。

若明确要排查 Codex 备选，配置名必须是 `codex_cli_best_effort`，并单独同意主机读取边界：

```yaml
computer_control:
  planner_backend: codex_cli_best_effort
  allow_codex_cli_host_read: true
```

0.3 planner 只能返回一个严格 JSON step。以下都会失败：多动作、未知字段、坐标、任意命令、没有 current observation 的 action、不同 app 的 action、不可本地检查的 done。失败是安全预期，不应切换到 legacy controller。

顶层 `planner.enabled` 是另一条仅兼容旧 `VoiceRuntime` 的 one-shot fallback。它只能提出用户原句肯定、非引号/数据引用且精确授权的应用 UI 导航；feedback/pause/resume/wait/path/text/send 必须由本地确定性 parser 完整命中。若旧云 planner 提出这些动作或从“输入‘打开 Claude’”这类数据文本推导导航，看到阻断是预期行为，不要放宽 safety。

Codex adapter 使用 ephemeral 临时目录、known-tool deny list 和 read-only sandbox，但订阅 CLI 没有完整 no-tools 保证，也不是主机级秘密隔离；Claude adapter 使用空工具列表、safe/restricted 模式与严格 MCP 配置。两者启动/超时错误不会回显原始 prompt/provider stderr，以免泄漏窗口内容。

## 屏幕上下文许可错误

使用云 desktop planner 时必须同时设置：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  allow_screen_context_to_cloud: true
```

许可不是形式开关：当前 task、唯一明确授权的可见 app 摘要、本句肯定且精确点名的 UIA 控件子集和本地验收历史会进入 provider context。原始窗口标题、进程 ID、未点名 UI 内容、automation ID、element value、PCM、screenshot bytes 和真实截图可用性当前不进入 HandsFreePC 组装的单步 prompt；完整快照只在本地验收。CLI/provider 仍可能另行处理账户/组织、认证、网络、CLI/OS/runtime、临时 cwd、用量、错误和诊断/遥测等自身元数据；项目开关不能证明这些数据为零。若不接受任何控件标签离机，使用 `planner_backend: none`，此时只能执行命中的本地 deterministic skills。

## 等待确认但“确认执行”无效

在 `strict` 下，通用文本输入会进入 confirmation；在 `personal_trusted` 下，只有满足唯一聚焦非密码输入框、文本等于本句完整口述且不构成发送等副作用的草稿输入才可免确认。其余需要确认的动作会绑定一个 runtime 保存的 ID，并为每次 pending action 生成随机四位挑战码。提示会类似“确认执行 4 8 2 7”；只说静态“确认执行”永远无效，也不靠模型理解“确认”。无效原因包括：

- 确认提示尚未实际显示/完整播报；
- 已超过 `execution.confirmation_timeout_seconds`；
- 任务被急停/取消，pending ID 已清除；
- 同一个 ID 已经使用，重放被拒绝；
- 四位码错误、属于上一轮或只说了静态前缀；
- 确认前界面 fingerprint 改变；
- fresh safety 分类不再是同一 confirmation；
- 对 `native-...` 计划，重新 prepare 后完整 plan/source、规范路径、stat 身份或普通文件 SHA-256 与确认时不同；
- worker 正在普通失败暂停，而不是等待确认。

同一 `VoiceRuntime` 进程运行期内，已签发码在成功、取消或超时后都不会再次签发；有界重抽耗尽时会拒绝创建新确认。去重集合不跨重启持久化，因此四位码不是持久化防重放凭证。

纯 `voice` 模式要等 SAPI 完整播完再说；需要快速确认时用 `overlay` 或 `both`。准确说出屏幕/语音提示中的本轮完整口令，不要连续重复静态“确认执行”。随机码不做说话人识别：旁人、扬声器或实时转述/重放若听到本轮码仍可能代说，高风险动作必须有人看屏幕。

## 说“继续队列”不能继续

- 等待 typed confirmation 时只能确认或取消，`继续队列` 不得绕过；
- 普通任务失败导致的 `PAUSED` 才允许恢复；
- 队列在 `DRAINING`/停止中时不会重新接受普通任务；
- 先前急停已经清空的任务无法恢复。

## 急停后仍发生了一次点击/输入

急停是 cooperative cancellation。它会设置当前 controller cancel event 并清空待处理队列，但已经到达 Win32/UIA 的一次动作不能撤回。MCP mutating call 若断管/超时，结果会标为 unknown，系统不会自动重试，以免重复副作用。

需要硬停止采集和后续处理时，退出 HandsFreePC 进程或关闭 Windows 麦克风权限。对不可逆动作继续依赖 typed confirmation 和人工监督，不要只依赖急停。

## Qwen Open Computer Use 中文乱码或输入不完整

确认它只是实验配置：

```yaml
computer_control:
  driver: open_computer_use
  allow_experimental_driver: true
```

固定测试上游 0.2.3。其 Windows 中文编码问题仍未合并修复：

- [Qwen open-computer-use Issue #5](https://github.com/QwenLM/open-computer-use/issues/5)
- [PR #6](https://github.com/QwenLM/open-computer-use/pull/6)

HandsFreePC 会在 replacement character 或前后空白会被截断时拒绝操作，这是预期 fail closed。不要通过 `errors=ignore`、删掉 Unicode 检查或只跑 ASCII token 来宣称修好。优先改回默认 `windows_uia`；完整边界见 [OPEN_COMPUTER_USE.md](OPEN_COMPUTER_USE.md)。

即使没有乱码，0.2.3 的当前适配也没有可安全绑定的结构化 elements。planner 不能可靠生成元素 index，所以点击/导航受限，`type_text`/`set_value` 会因缺少可验证的焦点元素而失败。这不是提高 timeout、打开坐标或换 planner 能安全修复的问题。

## 旧 `VERIFIED_COMPLETION` 或 Computer Use plugin 问题

这些属于显式 `legacy_codex_cli`：

```yaml
computer_control:
  backend: legacy_codex_cli
  allow_codex_cli_host_read: true
  allow_legacy_codex_computer_use: true
```

旧路径需要 Codex plugin/thread 并由同一 agent 自报结果，没有 0.3 LocalVerifier。若只是历史配置遗留，请迁移为：

```yaml
computer_control:
  backend: local_agent
  driver: windows_uia
```

factory 不会自动 fallback。除回归兼容外，不要继续排查 `node_repl`/Computer Use skill 来建立新安装。

## 反馈遮罩不显示或 SAPI 没声音

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听" --mode overlay
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听" --mode voice
```

- overlay 是置顶、鼠标穿透且不抢焦点的短反馈，不是持久托盘状态；
- `voice` 依赖本机 Windows SAPI 中文声音；命令返回不代表人工真的听到；
- TTS 播放期间识别暂停/清缓冲，过早说话可能丢失；
- 播放中的 SAPI 不能被语音急停打断；
- `silent` 会隐藏普通状态，但确认和错误仍可能显示。

## pytest 大量错误但代码似乎没坏

若首个错误是用户 Temp 下的 `PermissionError [WinError 5]`，这是 pytest base temp 权限，不要按错误数量判断为代码回归：

```powershell
$testTemp = Join-Path $PWD ('.pytest-tmp\run-' + [guid]::NewGuid().ToString('N'))
./.venv/Scripts/python.exe -m pytest -x -vv -m "not live" --basetemp $testTemp
```

只清理明确的项目内测试目录；不要递归删除用户目录、系统 Temp 根或未知路径。

## 报告问题

请提供：版本/commit、Windows/Python、backend/driver/planner/safety profile、`diagnose-last` 的脱敏 `stage` 与 `error_code`、是否为 static/fixture/target-app 哪一层，以及最小复现。不要上传音频、完整转写、绝对路径、窗口截图、UIA 私人内容、token、登录缓存或 provider stderr。

涉及安全绕过时按根目录 [SECURITY.md](../SECURITY.md) 私密报告。
