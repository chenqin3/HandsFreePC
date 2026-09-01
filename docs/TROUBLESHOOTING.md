# HandsFreePC 0.4 故障排查

先区分问题发生在哪一层：控制词、正文 ASR、`over` 拼装、FIFO、planner、driver、fresh observation、LocalVerifier 或反馈。不要只看“我在听”或“操作成功”遮罩推断整条链路工作。

## 最小诊断顺序

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml list-audio-devices
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "我在听"
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml logs --tail 50
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml transcripts --tail 50
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml diagnose-last
```

`logs --tail 50` 与 `diagnose-last` 读取隐私受限的本地 JSONL 事件。默认路径是 `%LOCALAPPDATA%\HandsFreePC\logs\handsfreepc.jsonl`；日志会轮转，且不含原始 prompt、UIA 正文/值、截图、provider stderr、绝对路径或凭据。优先看 `stage` 与 `error_code`，再定位下文对应层，不要只根据遮罩里的短句猜测原因。

`transcripts --tail 50` 读取的是另一份、显式 opt-in 的 ASR 原文 journal；仅当 `privacy.save_transcripts: true` 时写入，默认路径为 `%LOCALAPPDATA%\HandsFreePC\transcripts\asr-transcripts.jsonl`。它显示送入会话层的 wake、普通 command 和 sample-bound marker segment 文本，保留内容、标点和大小写，但模型 adapter 会去掉首尾空白。若 marker segment 因静音门控未调用 ASR，会显示 `transcribed: false` 和 `skip_reason: silence_energy_gate`；真正调用 ASR 后返回空则为 `transcribed: true`。原文可能含敏感口述内容，不会保存 PCM。`transcripts` 输出给出原文文件的绝对路径；`run` 启动时会同时打印诊断路径、原文路径和启用状态。

需要验证自有 UIA driver 时，再按 [TESTING.md](TESTING.md) 配置 `local_agent/windows_uia`、`planner_backend: none`，显式运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml computer-doctor --live
```

普通 `doctor` 是静态预检，`ready_for_live_control` 和 `live_control_verified` 应为 `false`。只有 `computer-doctor --live` 对项目自有 fixture 的实际 Unicode round-trip 可以令后者为 `true`。

## “开始语音操作”说慢了不识别，说快反而成功

控制词由本地 Vosk 小词表和 `phrase_window_seconds` 判断，不是正文 ASR。检查：

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
- planner miss/失败后是否明确显示 `FAILURE`；0.4 不会静默换成另一套 controller；
- 目标应用是否已运行并匹配 `apps.*.process_names/title_patterns`。

`enabled: false` 时可以测试麦克风和兼容 parser，但不会启动连续桌面 agent。

## `Claude` 被听成 `cloud`，或中英混说无法绑定应用

先开启本地原文日志并复现一次：

```yaml
privacy:
  save_transcripts: true
```

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml transcripts --tail 50
```

当前确定性应用槽位会在明确的应用控制上下文中兼容已实测的 `cloud` / `cloloud`，也兼容 `chat in cowork`；它不会把普通 `cloud computing` 或要输入的 `cloud` 全局替换成 Claude。若项目名、对话名和英文 mode 仍频繁错字，安装并启用高精度正文 ASR：

```powershell
./scripts/install.ps1 -WithWhisper
```

```yaml
speech:
  command:
    backend: faster-whisper
    model: large-v3-turbo
    language: zh
    device: auto
    compute_type: auto
    beam_size: 5
    initial_prompt: "语音控制命令可能包含 Claude、Codex、Chat and Cowork、Design 和 over。"
    hotwords: "Claude Codex Chat and Cowork Design over"
```

模型第一次使用会下载较大的权重；有已验证的 NVIDIA CUDA 环境可把 `device`/`compute_type` 改成 `cuda`/`float16`。先用授权的 16 kHz 单声道 WAV 跑 `test-asr`，再做真人麦克风验收。不要仅因为输出“操作成功”就认定 ASR 或 UI 已完成，仍要结合 `COMMAND_ENQUEUED`、`CONTROL_STARTED` 和本地 verifier 的最终事件。

## `over` 经常漏掉

当前版本用独立英文 Vosk 模型检测 `over`，同时保留所选正文 ASR 识别作为后备。先检查：

- 升级代码后重新执行 `./scripts/download-models.ps1`；
- `doctor --strict` 的 `models.delimiter.path` 指向 `vosk-model-small-en-us-0.15` 且 `ready: true`；
- 把 `over` 作为一个清晰、独立的英文词说出；很短的自然停顿有助于识别和样本边界稳定，但不是协议强制要求；
- 用 `overlay` 查看队列数；没有显示“已入队”时不要继续堆很多正文；
- 用 `logs --tail 50` 查 `PROMPT_DELIMITER_DETECTED`。有该事件但无 `COMMAND_ENQUEUED`，说明 marker 前没有形成非空正文；两者都没有则是 KWS/麦克风层；
- 若已显式启用 `save_transcripts`，再用 `transcripts --tail 50` 看对应 `marker_segment` 是否为空或错字；`transcribed: false` 表示静音门控跳过，`transcribed: true` 且文本为空才表示 ASR 返回空。这能判断正文 ASR 路径，但不能证明 VAD 样本边界本身正确；
- 不要在配置中增加过多常见中文短词作为 delimiter，容易在正文误切；
- `mouseover`、`voiceover` 不会切分，这是预期行为。

检测器使用同一麦克风 block，不会另开音频设备；命中后继续录到 VAD 终点。运行时优先用 Vosk 词级/partial 词级时间形成 marker 样本区间，没有可用词时间时退回到命中 block；随后按 marker 区间切分本轮内存音频，marker 本身不进入正文 ASR，前后片段先经过静音门控，真实有声段再转写。它支持同一个 VAD 话语内的多个 `over` 和紧随其后的下一条正文，但边界只是识别结果，不保证在所有口音和噪声下精确；异常时先放慢并短暂停顿，再结合 `PROMPT_DELIMITER_DETECTED`、`COMMAND_ENQUEUED` 与 overlay 判断发生在哪一层。

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

## `strict`、`personal_trusted` 与 `local_unrestricted` 表现不同

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

若要让 planner 自行选择任意当前可见顶层窗口、跨 app/多 Chrome 窗口并推断中间导航，只能在同一份本机配置中显式使用：

```yaml
computer_control:
  driver: windows_uia
  safety_profile: local_unrestricted
```

这个模式不要求每条口述点名/预配置唯一应用，也不再套用 `strict`/`personal_trusted` 的 app-scope 与普通低风险导航确认；窗口/选项卡切换、菜单、Toggle 和未命中风险分类的通用 OK/Continue 对话框可由 planner 推断。若口述明确指定 app/window/field，最终用户步骤仍必须绑定所说窗口和字段；看到动作落在别的应用或相似编辑框应视为失败，不要删掉 binding 检查。UIA 可寻址的“搜索 X”必须精确设置字段、按 Enter/Return，并在 fresh observation 中看到结果语义变化。渲染搜索则先要求截图点击后的 Win32 focus/caret 证明，只输入用户本句中的精确目标；输入后的同一绑定仍有效且画面没有结果时，才允许一次 Enter/Return。该回车不能用于消息、prompt、回复、Send 或任意 Submit。识别到的发送/提交、删除、安装、上传/分享和关闭仍需本轮确认。每个允许动作仍要求 fresh bind 和 false-before/true-after 本地验收；`done` 前会重新观察同一窗口，多段明确动作必须按顺序完成。终端/shell、Windows Run、UAC/安全桌面、认证、密码/凭据、付款、隐私/账户设置、未绑定/可复用坐标和任意 shell 继续被阻断。

## 已设置 `local_unrestricted`，仍看到 `APP_SCOPE_REQUIRED`

`DesktopAgentLoopController` 在真正加载 `local_unrestricted` 后不会走 `APP_SCOPE_REQUIRED` 分支。仍出现该错误通常说明当前进程没有加载你修改的配置，而不是 Windows 权限不足。检查：

1. 启动命令是否明确使用预期的 `--config ./config.local.yaml`，或 `scripts/run.ps1` 是否指向同一文件；
2. YAML 中键是否位于 `computer_control.safety_profile`，值是否精确为 `local_unrestricted`；
3. 当前 backend/driver 是否为 `local_agent/windows_uia`，并已重启旧进程；
4. 用配置加载器直接核对实际值：

```powershell
./.venv/Scripts/python.exe -c "from handsfree_pc.config import load_settings; print(load_settings('config.local.yaml').computer_control.safety_profile)"
```

若错误变成 `NO_VISIBLE_WINDOWS`、`OBSERVE_DRIVER_FAILED` 或 stale-window 类错误，说明 app-scope 已经移除，问题转到可交互桌面、窗口枚举/激活或 UIA 层。不要通过改成 legacy controller、管理员运行或关闭前台 HWND 复核来绕过。

## 静态 doctor 通过，但 `computer-doctor --live` 失败

静态检查只证明文件和命令存在。live failure 常见原因：

- 当前不是可交互 `Default` desktop（锁屏、UAC、安全桌面、切换用户）；
- pywinauto/pywin32 安装到了另一个 Python；
- 杀毒/企业策略阻止 UIA 或 `SendInput`；
- HandsFreePC 被以不同完整性级别运行；
- fixture 启动慢、窗口被系统隐藏或 foreground activation 被拒；
- 中文 token 写入后无法从 UIA value 读回。

`ForegroundIntegrityBoundary` 不是可重试的 selector 错误，也不能靠重复点击、Alt 技巧或跳过前台断言修复。先由用户正常退出/降权造成高完整性前台的应用，或在组织明确评估后让目标应用与 HandsFreePC 处于相同完整性级别；项目不会自动提权、关闭进程或绕过 UIPI。一般仍以普通用户运行，不要仅为了让测试变绿改成管理员。记录 JSON 中的 `error_type`，再在同一 `.venv` 检查模块。

数位板、输入法和屏幕叠加层偶尔会留下一个不可见、零尺寸但仍占用 foreground input queue 的高完整性 helper。0.4 会在 `AttachThreadInput` 前跳过**已由 Win32 明确证明不可见**的 foreground HWND，避免把这种幽灵窗口误报成用户正在操作的高权限界面；但如果 Windows 之后仍拒绝把目标变成精确前台，driver 继续以 `WindowActivationError` 失败关闭，不会用软件伪造点击绕过 UIPI。此时先用一次真实鼠标/触控点击任意普通窗口，或正常退出造成该 helper 的设备/叠加软件，再重跑 `computer-doctor --live`。如果每次登录后都会复现，应修正该软件的启动/权限配置，而不是让 HandsFreePC 自动结束它。

## 找不到或无法唯一选择应用

`strict`/`personal_trusted` 的 `windows_uia` 只观察 `apps` 中配置的 profile。检查进程名和窗口标题：

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

在 `strict`/`personal_trusted` 中，先关闭重复测试窗口或把正确窗口放前台，再做低风险观察。通用任务的口述还必须肯定且只明确指定一个应用；“不要操作 Claude”“比较 Codex 和 Claude”“我在 Claude 看到了错误，帮我处理”这类零授权、多个或顺带提及不会由 planner 猜目标。把命令改成如“在 Claude 点击 Chat”并保持只授权一个应用。

`local_unrestricted` 不使用这一静态 profile 选择规则；它在任务开始及后续规划步骤间刷新全部可见普通顶层 HWND，并把多个 Chrome 窗口分别交给 planner。observe 会把 planner 选中的精确窗口激活到前台；若标题/PID/process 与 inventory binding 不一致、窗口已消失或 HWND 被复用，返回 stale observation。若一次已验收的 UI 动作新开了窗口，下一规划步骤可把它纳入 inventory；完全未启动且无法从当前 UI 打开的应用，仍需先由已配置的确定性 native skill 启动。`local_unrestricted` 允许用户完全不说 app，但不会无视明确定位词：例如“在 Claude 的 Message 输入……”必须在 Claude 窗口的 exact `Message` 字段完成，“在 Chrome 搜索……”不能由其他窗口的搜索框代替。

## 打开路径后仍显示失败，或打开了同名文件

确定性 `OPEN_PATH` 不是只检查 Shell dispatch 返回值：执行前目标后置条件必须为 false；打开后必须成为 true，而且前台 HWND 必须与 before 不同。

- 目录：前台 Explorer 的规范化路径必须与目标精确一致，证据较强；
- 文件：当前只能检查新前台窗口标题是否包含精确文件名，这是 best-effort；复用同一 HWND 的查看器会保守失败。

因此同名文件、查看器复用旧窗口、标题不显示文件名、启动后仍有选择器/登录页时都可能无法证明或存在误判。增加完整父目录只会改善解析，不会把文件标题验证升级为内容验证；重要文件请人工看屏幕核对，不要放宽 verifier。

## WorkMap 别名没有命中

当前 WorkMap 只做本地精确路由，不做模糊 planner 搜索。确认本机配置（不要提交）满足：

```yaml
workmap:
  enabled: true
  out_directory: "<local-workmap-out-directory>"
  aliases:
    资料库:
      project: "<unique-project-title-or-id>"
      relative_path: "<relative-folder>"
```

- `out_directory` 必须含可读的 `WORKMAP.md` 和 `projects/`，项目索引段必须完整；
- `project` 必须唯一匹配项目标题或 id，`relative_path` 只能位于该项目根目录之下且目标必须存在；
- 口述必须是完整、肯定、单一的精确请求，如“打开资料库”。否定、引号、多分句、未知/歧义 alias 都会 miss；
- WorkMap 正在重建或读取失败时不会阻止麦克风启动，而是回退到后续路由；
- `planner_hints` 当前没有接入云 planner，所以不要期待 Codex/Claude 根据 WorkMap 摘要做模糊猜测。

## UIA 看不到按钮、输入框或选项卡

Electron/canvas/远程桌面应用可能只暴露部分 accessibility tree。表现包括：元素缺失、多个同名元素、焦点状态不见、输入后 value 不可读。

可做：

- 开启应用自身的辅助功能支持；
- 在目标版本、语言和账号布局上用 UIA 检查器确认实际 tree；
- 为稳定动作增加确定性 native skill，而不是猜坐标；
- 若无法在 fresh state 中验证后置条件，保守失败并人工完成。

不要把 `allow_coordinate_actions` 当通用修复。默认安全层会阻断无 semantic target 的坐标 click/drag。

## 已有截图但没有 OCR 框，或视觉点击后不继续

视觉 fallback 是截图优先，不是 OCR 优先。先确认 `local_unrestricted/windows_uia` 使用 `codex_cli_best_effort`，目标应用在 `visual_ocr.apps` 中，并在本机配置显式启用：

```yaml
visual_ocr:
  enabled: true
  ocr_regions_enabled: false
```

`ocr_regions_enabled: false` 不会调用 PaddleOCR，但完整目标窗口截图和 frame-bound `VisualViewport` 仍应交给 Codex；没有编号文字框并不是失败。只有确实需要文字区域时，才把它改为 `true`、启动 [VISUAL_OCR.md](VISUAL_OCR.md) 中的 PaddleOCR loopback 服务并检查 `/health`。OCR 服务超时或报错时，系统会保留截图 viewport，不应把 OCR 当成截图规划的前置条件。若窗口已经暴露 rich actionable UIA，driver 会优先用 UIA，也不会额外合成视觉区域。

Codex 看到的图片可能不是原始像素尺寸：全窗 PNG 最大边超过 2048 px 时会等比缩到 planner canvas，返回的 `x/y` 再映射回原始截图坐标。若点击位置看起来成比例偏移，检查图片宽高读取、canvas 边界、横纵比例映射和 Windows DPI；不要手工写死缩放倍数或直接复用 planner 坐标。

每个视觉动作前都会重新截取当前绑定 HWND：窗口移动/缩放、OCR 文本区域不再唯一、区域 crop 变化、viewport 点击点附近 patch 变化都会使旧计划失效。一次 click、单页纵向 scroll、受限搜索输入或一次搜索 Enter/Return 后，都必须取得 fresh exact-window 完整截图并用它重新规划/验证；`visual action produced no observable exact-window change` 表示本地没有观察到状态转换，不应通过复用旧坐标、重复点击或跳过 fresh observe 绕过。

如果只是窗口其他区域的 loading/animation 改变，非视觉 UIA action 可以继续，但条件非常窄：fresh state 必须仍是同一 app/exact window、同一唯一 element index，目标的 `local_identity`、control type、enabled 与 addressable 全部不变，driver 还会在 dispatch 前重验。任一项变化都应视为 stale。截图 viewport point 不走这条例外；即使全窗动画无关，点击附近的 local patch 也必须稳定。

## 视觉搜索框点中了，但不能输入或不能按回车

渲染搜索不是 OCR 文本输入。viewport 点一次之后，下一次 fresh observe 必须由 Win32 `GetGUIThreadInfo` 同时证明：目标 PID/TID 未变，active/focus/caret HWND 属于 exact target window，system caret 可见且非空，并且 caret rectangle 与刚才的原始截图点击点足够接近。任一证据缺失时不会在 viewport 上声明 `type_text`。常见原因包括：

- 应用只绘制了自有光标，没有可见 Win32 system caret；
- 焦点落在整个 renderer 或另一个 HWND，而不是目标窗口的可证明输入上下文；
- 窗口/DPI 坐标转换失败，caret 与点击点不在同一坐标系；
- 点击后窗口、目标 patch、foreground、PID/TID 或 caret identity 已变化；
- 点击并不位于受限的搜索区域。

这是 focus/caret 证据不足，不一定是麦克风或管理员权限问题。不要用 OCR 命中、截图看起来像文本框或无条件 `SendInput` 绕过。证据成立时也只会输入用户指令中的精确目标/搜索文字；消息正文、prompt、凭据、付款内容、换行或屏幕上抄来的文字都会拒绝。

输入后会 fresh screenshot。若截图已经出现可点击结果，下一步应点击结果；只有画面没有结果且同一 focus/caret binding 仍有效时，才允许一次 Enter/Return 触发搜索。此时 `VisualViewport` 已 armed；只有 planner 再次点击该 viewport、单次左键点仍位于受限顶部搜索区域时，parser 才会确定性把它推进为这一次 Enter，并以 `LAST_ACTION_VERIFIED` 验收。当前帧中的语义结果按钮及其他截图区域仍保持 click。该能力用后即失效；失焦、换窗、第二次回车、其他 key、Send/Submit 或消息/回复语境都会拒绝。即使默认列表包含 `wechat`，这仍不表示任意微信输入或发送已支持。

若搜索 helper 中出现唯一 semantic result `Button`，只有其 exact full label 包含用户精确目标并以“前往”或 `Go to` 结束时，系统才允许用该**完整标签消失**确认一次 navigation bridge。按钮消失不等于目标已打开，更不等于任务完成；下一窗口仍要 fresh screenshot。看到相似按钮、多条结果或只有部分标签时，保守失败是预期行为。

若微信搜索后前台变成 `WeChatAppEx` 等新窗口，系统只接受与原微信窗口同 PID 或可验证父子进程关系的 transition，并把动态 app 精确重绑到新 HWND/PID/process/title 后继续观察。helper 只有在 immediate parent 唯一匹配已配置 profile 时才继承 profile；重绑后的 active app alias 会保留在子窗口，即使原父窗口仍可见，inventory 也不应把 alias 抢回。出现 stale/related-window 错误时，确认新窗口确实为前台、直接父进程识别合理且进程树相关；无关窗口、同名伪装窗口或无法证明关系的窗口不会被接管。

## planner 已返回视觉 DONE，但任务仍继续观察

这是双帧完成协议，不是卡住。发给云端 planner 的 observation 不含原始 HWND/`local_window_id`；第一条视觉 `DONE` 返回后，controller 才用本地完整 observation 生成绑定当前 app、exact window、generation 与截图 bytes 的 token，然后强制重新截取同一窗口，让 planner 在更新 generation 上再次判断。只有两张独立 fresh screenshot 都支持完成且窗口绑定未变，才进入本地 completion verifier。

若随后报 `VISUAL_COMPLETION_NOT_FRESH`、`VISUAL_COMPLETION_WINDOW_CHANGED` 或 `VISUAL_COMPLETION_BINDING_CHANGED`，检查截图 generation/capture time 是否增长、helper 是否已精确重绑、第二次观察是否仍是同一 task alias。不要把本地 HWND 塞进 planner prompt、复用第一张图或关闭第二次复核。

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

若从 Codex App 内置终端可以运行、但普通 PowerShell 的 `run.ps1` 报 `HandsFreePC is not ready to run (1)`，先查看 doctor JSON 中 `commands.codex_computer_control.found`。Codex Desktop 可能只把当前版本的 CLI 目录临时加入自身进程的 `PATH`，没有写入用户或系统 `PATH`。HandsFreePC 会先尊重正常 `PATH`；仅当配置仍使用裸 `codex`/`codex.exe` 且运行在 Windows 时，才从 `%LOCALAPPDATA%\OpenAI\Codex\bin` 自动选择最新的版本目录，doctor、桌面 step planner、旧 planner 和 legacy controller 共用同一解析器。显式路径或自定义命令名不会被替换。不要手工固定根目录的 `bin\codex.exe`：它可能落后于当前 Codex Desktop 版本。

若明确要排查 Codex 备选，配置名必须是 `codex_cli_best_effort`，并单独同意主机读取边界：

```yaml
computer_control:
  planner_backend: codex_cli_best_effort
  allow_codex_cli_host_read: true
```

0.4 planner 只能返回一个严格 JSON step。以下都会失败：多动作、未知字段、任意命令、没有 current observation 的 action、不同 app 的 action、不可本地检查的 done，以及除当前唯一 `VisualViewport` 一次左键外的任何 `x`/`y`。视觉 `x/y` 属于 planner canvas，必须由 parser 映射并由 driver 绑定到原始截图像素；失败是安全预期，不应切换到 legacy controller。

顶层 `planner.enabled` 是另一条仅兼容旧 `VoiceRuntime` 的 one-shot fallback。它只能提出用户原句肯定、非引号/数据引用且精确授权的应用 UI 导航；feedback/pause/resume/wait/path/text/send 必须由本地确定性 parser 完整命中。若旧云 planner 提出这些动作或从“输入‘打开 Claude’”这类数据文本推导导航，看到阻断是预期行为，不要放宽 safety。

Codex adapter 使用 ephemeral 临时目录、known-tool deny list 和 read-only sandbox，但订阅 CLI 没有完整 no-tools 保证，也不是主机级秘密隔离；Claude adapter 使用空工具列表、safe/restricted 模式与严格 MCP 配置。`local_unrestricted/windows_uia` 下只有 Codex 通过临时 `--image` 接收选中窗口 PNG；原图过大时发送的是最大边 2048 px 的等比 planner canvas。Claude CLI adapter 是 text-only，只接收 inventory/title/UIA context。两者启动/超时错误不会回显原始 prompt/provider stderr，以免泄漏窗口内容。

## 屏幕上下文许可错误

使用云 desktop planner 时必须同时设置：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  allow_screen_context_to_cloud: true
```

许可不是形式开关。`strict`/`personal_trusted` 会发送当前 task、唯一授权 app 摘要、裁剪后的 UIA 控件和本地验收历史，但不会发送真实窗口标题或截图。`local_unrestricted` 会发送全部 fresh 可见顶层窗口的标题/进程摘要；observe 后还发送真实窗口标题和经凭据过滤的可寻址 UIA context。若 planner 是 Codex，选中窗口 PNG 还会作为临时 `--image` 输入；视觉 fallback 以完整 exact-window capture 为主规划信号，图片过大时发送等比缩小的 planner canvas，`ocr_regions_enabled` 只决定是否另调用 PaddleOCR 增加文本框。关联微信窗口精确重绑后，新的窗口截图也可能进入后续调用。Claude 当前只接收文本 context。结构化 `CONTENT` 节点、automation ID、element value、原始 Win32 focus/caret handles 和 PCM 不发送，但截图像素仍可能显示正文或通知。CLI/provider 还可能处理账户/组织、认证、网络、CLI/OS/runtime、临时 cwd、用量、错误和诊断/遥测等自身元数据；项目开关不能证明这些数据为零。若不接受任何屏幕信息离机，使用 `planner_backend: none`，此时只能执行命中的本地 deterministic skills。

## 等待确认但“确认执行”无效

在 `strict` 下，通用文本输入会进入 confirmation；在 `personal_trusted` 下，只有满足唯一聚焦非密码输入框、文本等于本句完整口述且不构成发送等副作用的草稿输入才可免确认。`local_unrestricted` 的普通低风险导航/切换/Toggle/通用无风险对话框不确认，但本地识别出的发送/提交、删除、安装、上传/分享和关闭仍会确认。所有实际需要确认的动作都会绑定一个 runtime 保存的 ID，并为每次 pending action 生成随机四位挑战码。提示会类似“确认执行 4 8 2 7”；只说静态“确认执行”永远无效，也不靠模型理解“确认”。无效原因包括：

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

旧路径需要 Codex plugin/thread 并由同一 agent 自报结果，没有 0.4 LocalVerifier。若只是历史配置遗留，请迁移为：

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
