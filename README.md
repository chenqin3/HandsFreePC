# HandsFreePC

HandsFreePC 让 Windows 11 在双手被占用时继续接受语音操作：说“开始语音操作”进入连续会话，每条指令以英文 `over` 结束并按 FIFO 排队，说“结束语音操作”停止接收新指令并排空已接受队列。

0.3.1 的关键变化是：**Codex 或 Claude 只负责规划下一步，本项目自己持有鼠标键盘权限，并在每个通用 planner 动作后重新观察、在本地验收。** `over` 另由独立英文离线模型检测，不再依赖中文词表或正文转写碰巧识别成功。

> [!WARNING]
> 这是面向 **Windows 11、64 位 Python 3.11/3.12** 的 alpha。公开配置默认关闭电脑控制、云规划和真实执行。即使通过自有 fixture 的 live test，也只证明本机 UIA 基础链路可用，不证明任意第三方应用或高风险任务安全可控。

## 现在的底层

```text
麦克风
  -> 中文 Vosk 控制词 + 英文 Vosk `over` 检测 + Silero VAD
  -> SenseVoice（快速默认）或 faster-whisper（中英专名高精度）正文转写
  -> “开始语音操作”会话 / over 分段 / 有界 FIFO
  -> NativeSkillRouter（确定性命令优先）
  -> StepPlanner（默认 Claude CLI；Codex CLI 仅为显式 best-effort 备选）
  -> DesktopDriver（默认项目自有 Windows UIA/Win32 驱动）
  -> fresh observe
  -> LocalVerifier（本地比较动作前后状态和任务完成条件）
```

核心约束：

- 持续监听、独立 `over` 检测、执行期间继续收音、FIFO、结束后 drain 和急停均保留；公开配置在普通失败后暂停，本机 `failure_policy: continue` 可让后续已入队指令继续；
- 确定性解析命中时，`NativeSkillRouter` 先解析目标并走本地白名单执行器，不调用模型；
- 公开默认 `strict` 要求普通桌面任务在口述中明确且肯定地只指定一个目标应用；`personal_trusted` 只可沿用同一控制器中刚刚本地验收的窗口。只在本机忽略提交配置中显式启用的 `local_unrestricted` 会改为重新枚举全部可见顶层窗口，让 planner 自选窗口并跨应用导航，不再产生 `APP_SCOPE_REQUIRED`；
- 通用 agent 的每个 planner 动作都执行 `fresh before -> 任务后置条件此时必须为 false -> 一个动作 -> fresh after -> 同一后置条件必须为 true`，再交给 LocalVerifier；确定性 native skill 使用各动作自己的本地证据，精确目标状态已成立时可幂等成功；
- planner 没有鼠标键盘能力，也不能把 shell 命令放入动作 Schema；UI 文本一律按不可信数据处理；
- 默认 `windows_uia` 驱动把元素索引绑定到应用、窗口和本次 observation generation；动作后旧索引立即失效。`local_unrestricted` 还把每个可见顶层 HWND（包括同一进程的多个 Chrome 窗口）作为独立候选，observe 时激活并复核 planner 选中的确切 HWND；
- `strict`/`personal_trusted` 的本地有限语法把动作类型、目标完整短语、完整口述输入 payload、按键、左右键/点击次数、secondary action、滚动方向/页数逐项绑定到当前用户步骤；`type/input` 只授权键入，`fill/write` 才授权直接设置字段值；不能把“Open settings”缩成当前唯一可见的“Open”，不能只输入口述文本的子串，也不能借用同句后续输入动作的 payload；若文本动作后还有独立、非空且肯定的 clause，payload 出现不能自证完成，必须验证用户给出的真实结果；条件句在没有本地条件求值器时整体拒绝，尚不支持的尾随桌面动作也会计入步骤数，不能被提前 `DONE` 掩盖；
- 只有本地 verifier 通过才返回 `LOCAL_VERIFIED_COMPLETION`。驱动“已接受动作”或 planner “done”都不是证据；`local_unrestricted` 的 `done` 还会重新观察并复核同一 HWND，多段明确动作必须按口述顺序全部完成，不能用中间结果提前结束；
- 公开配置默认使用 `strict`：通用 agent 的 `type_text`/`set_value` 文本输入使用绑定到**确切动作与确切界面快照**的确认。仅在本机忽略提交的配置里显式使用 `personal_trusted` 时，才可免确认执行安全导航，以及把用户本句完整口述的草稿写入唯一、已聚焦、非密码输入框；它不会自动点击发送。两种模式下，被本地已知词形识别为发送/提交、删除、安装/卸载、上传/共享、关闭等副作用仍要求确认；认证、密码、付款、UAC 和 Windows Security 界面仍直接阻断。每次确认提示会生成新的随机四位口令；只说静态“确认执行”、口令不匹配、已使用、超时或界面变化都会拒绝；
- `local_unrestricted` 会取消上述 `APP_SCOPE_REQUIRED`、普通导航目标必须由用户预先点名以及普通低风险导航确认限制，允许 planner 从全部 fresh 可见普通顶层窗口中选择并动态跨应用推断中间导航；普通切换、菜单/选项卡导航、Toggle，以及没有命中风险分类的通用 OK/Continue 对话框动作可直接进入本地验收。用户若明确说出 app/window/field，真正完成该口述步骤的动作仍必须精确绑定到所说窗口和字段，不能在其他窗口或近似输入框“完成”。自然“搜索 X”必须把搜索/地址字段设为精确 `X`、按 Enter/Return，并在 fresh observation 中看到与该查询对应的新结果状态；只写入文字不算搜索完成。识别到的发送/提交、删除、安装、上传/分享和关闭等高影响动作仍要求本轮确认。它仍不是任意电脑权限：终端/shell、Windows Run、UAC/安全桌面、认证、密码/凭据、付款和隐私/账户设置继续硬阻断，动作 Schema 仍无任意 shell、文件系统 API 或纯坐标动作；
- 风险词表和上下文规则不是完整语义证明：未知语言、同义词、自绘控件或伪装文案仍可能漏分。重要外发、删除、安装、分享或不可逆任务必须有人看屏幕监督；
- 所有 profile 对被已知词形/元素属性识别为密码、凭据、付款/转账、认证、隐私/公开链接设置、终端/shell、Windows Run、UAC 或 Windows Security 的操作 fail closed；纯坐标点击默认阻断。`local_unrestricted` 的窗口 inventory/截图可能早于具体 action 风险分类离机，未识别 surface 也仍是残余风险；
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

如果命令经常夹杂 `Claude`、`Codex`、`Chat and Cowork` 等英文专名，推荐安装可选的高精度 ASR：

```powershell
./scripts/install.ps1 -WithWhisper -DownloadModels
```

然后在不会提交的 `config.local.yaml` 中把 `speech.command.backend` 改为 `faster-whisper`。`model: large-v3-turbo` 支持 `initial_prompt` 和 `hotwords`；有可用的 NVIDIA CUDA 环境可设 `device: cuda`、`compute_type: float16`，否则保留 `auto`。模型权重第一次使用时会下载约 GB 级缓存，之后启动无需重复下载。完整字段见 `config.example.yaml`。

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

`--draft-smoke` 只会在可唯一、安全绑定的非密码编辑框中写入一个随机测试 token，再通过 fresh observe 和 LocalVerifier 读回；**不会点击发送，也不会替你提交 prompt**。成功后只在当前字段仍精确等于本轮随机 token 时自动清空，并再次观察确认空白；若编辑框不唯一、没有可靠焦点、目标是敏感字段、界面无法读回或精确清理无法验收，命令会失败关闭。

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

若希望 planner 接受不点名应用的自然语言、在多个应用或多个 Chrome 顶层窗口之间自行选择并推断中间导航，可只在同一份本机配置中显式改为：

```yaml
computer_control:
  driver: windows_uia
  safety_profile: local_unrestricted
```

`local_unrestricted` 每条任务都会 fresh 枚举当前可见的全部普通顶层窗口；每个窗口都有独立动态 app ID，后续步骤还会刷新 inventory，planner 因此可以动态跨应用，也不会再因没有预先配置/点名应用而返回 `APP_SCOPE_REQUIRED`。planner 选择 `observe` 后，driver 会激活并复核确切 HWND/PID/process/title，再读取 UIA 和该窗口截图。窗口消失、HWND 被复用、身份变化或无法成为前台都会停止，而不是向近似窗口发送输入。没有点名 app 时 planner 可以自行选择；一旦用户明确说出 app、窗口或字段，完成相应口述步骤的 action 仍必须绑定该 exact window/field，中间跨 app bridge 不能把最终输入或搜索落到别处。

这个模式还允许 planner 推断必要的菜单、选项卡、搜索框等中间步骤。普通切换、菜单/选项卡、Toggle 和未命中风险分类的通用 OK/Continue 对话框不要求确认，但每一步仍必须绑定 fresh semantic target。“搜索 X”无需额外说“输入”，却不是单纯的文本写入：若 UIA 已确认搜索/地址字段为空，可用精确 `type_text`；字段非空或旧值不明时必须用 `set_value` 精确替换为用户原文 `X`。随后还要按 Enter/Return，并用 fresh result transition 验收 `SEARCH_SUBMITTED`；已有 `X` 的较长字符串、追加输入或只看到字段值都不能冒充完成。识别到的发送/提交、删除、安装、上传/分享和关闭等高影响动作仍要求本轮确认。终端/shell、Windows Run、UAC/安全桌面、认证、密码/凭据、付款、隐私/账户设置、纯坐标和任意 shell 仍是硬边界；每个实际动作仍必须经过 fresh bind、执行后重新观察和本地后置条件验收。

若要复现公开项目的最保守行为，改回：

```yaml
computer_control:
  safety_profile: strict
```

`strict`/`personal_trusted` 会把完成的语音 prompt、唯一授权的可见应用摘要、observation generation、经裁剪的可寻址 UIA 控件和最近的本地验收摘要发送给选定 planner；原始窗口标题、截图字节和未授权控件不进入这两个 profile 的 planner view。`local_unrestricted` 的边界更宽：planner 先收到全部 fresh 可见顶层窗口的 display name、process name 和窗口标题；观察某个窗口后还会收到真实窗口标题和经凭据过滤的可寻址 UIA 控件。`CONTENT` 节点、元素 value/automation ID、原始音频和 PCM 仍不作为结构化 planner 字段发送。

在 `local_unrestricted` 中使用 `codex_cli_best_effort` 时，当前窗口截图还会写入一次性临时目录并通过 `codex exec --image` 交给 Codex；它是选中窗口的截图，不是全桌面截图，但仍可能显示聊天、文件名或其他敏感画面。Claude adapter 当前只接收文本化窗口标题/UIA context，不接收这份 PNG。临时文件由本轮 planner 临时目录清理，但 Codex CLI/provider 已处理的数据受其自身政策约束。三种 profile 只要启用云 planner 都必须设置 `allow_screen_context_to_cloud: true`。先登录相应 CLI，再运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth --strict
./scripts/run.ps1
```

Claude CLI 是默认规划器，使用独立 system policy、safe/restricted 模式、空工具列表、严格 MCP 配置和非持久会话。它仍是联网 CLI，不是本地模型。

若明确要用订阅版 Codex CLI，只能写 `planner_backend: codex_cli_best_effort`，并额外设置 `allow_codex_cli_host_read: true`。项目会尽量禁用已知工具并使用临时目录、结构化单步输出和 read-only sandbox，但 Codex CLI 没有可由本项目证明的完整 no-tools 模式；这个开关表示你接受它仍可能读取当前 Windows 账户可见的其他主机文件。两条路径都不获得 HandsFreePC 的桌面驱动。参考 [OpenAI Computer Use 自定义 harness 指南](https://developers.openai.com/api/docs/guides/tools-computer-use)、[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 和 [Responses API tool choice](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。

不要把 `computer_control.planner_backend` 与顶层 `planner.enabled` 混淆。后者只是兼容旧单句 `VoiceRuntime` 的云 fallback：模型只能提出 `ACTIVATE_APP`、`OPEN_CONVERSATION`、`OPEN_MODE`、`ENTER_DICTATION` 或 `START_NATIVE_VOICE`，并且应用及项目/对话/tab/mode 必须由用户原句肯定、非引号/数据引用地精确授权。它不能决定反馈模式、暂停/恢复/等待，也不能打开路径、输入文本或发送 prompt；这些能力只能由完整命中的本地确定性 parser 决定。

旧单句运行时若需要确认，`native-...` ID 会绑定完整 plan（含 source）及每个已解析路径的规范绝对路径与 stat 身份；普通文件还绑定 SHA-256。说出本轮随机码后会重新 prepare 目标、重新运行 safety 并重新计算 binding；计划、来源、路径身份或文件内容任一变化都会取消，不会执行替换后的目标。即使 `OPEN_PATH` 被判定为安全目录而无需口令，runtime 和 deterministic native router 也都会在 safety 前后重新绑定目标，并从最后绑定一直持有防替换句柄到本地执行返回。

Electron 应用的可访问标签会随版本变化。`apps.*.mode_names` 同时是 native 模式 allowlist：只有显式 key 可执行，并映射到当前版本的精确 UIA 标签（如 Claude 的 `Chat and Cowork`）；缺少映射会在任何点击前拒绝。执行器只接受精确匹配，并在动作后验证 `selected`。单纯获得键盘焦点只证明点击到控件，不证明页面已经切换。若 Codex/Claude 应用只暴露一个空 Pane、没有可操作子元素，或模式控件不暴露可验证的选中状态，UIA 路径会失败关闭，不能靠模糊匹配或坐标猜测。

### 可选的本地 WorkMap 精确别名

如果本机已经有 WorkMap 导出的 `WORKMAP.md` 和 `projects/`，可以在不提交的 `config.local.yaml` 中为常用项目或项目内相对目录建立精确别名：

```yaml
workmap:
  enabled: true
  out_directory: "<local-workmap-out-directory>"
  aliases:
    资料库:
      project: "<unique-project-title-or-id>"
      relative_path: "<relative-folder>"
```

当前生产路由只接受完整、肯定、单一的精确打开请求，例如“打开资料库”；引号、否定、多分句、歧义、目标不存在或相对路径逃出项目根目录都会 miss，并继续走其他路由，而不会做模糊猜测。命中后生成确定性的本地 `OPEN_PATH`，仍走路径绑定、安全策略和本地后置验证。WorkMap 的搜索候选/`planner_hints` 目前**没有接入云 planner**；WorkMap 导出目录、别名和任何本机路径都应只留在 `config.local.yaml`，不要提交到公开仓库。

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
- 公开默认 `failure_policy: pause` 会在普通失败或待确认时暂停队列；本机设为 `continue` 后普通失败不再卡住后续指令，但待确认仍暂停。确认提示会给出随机四位挑战码，例如“确认执行 4 8 2 7”；只有本轮准确口令会携带运行时保存的 confirmation ID。单独说“确认执行”无效，也不会作为新 prompt 交回模型猜测。
- 同一 `VoiceRuntime` 进程运行期内，已经签发的四位码持续保持占用：确认、取消或超时都不回收到抽样池；有界重抽仍找不到新码时 fail closed。该集合不持久化，进程重启后不保证绝对不复用，所以四位码不是持久化防重放凭证。

### `over` 的独立本地检测

`over` 现在由独立的英文 Vosk small-en-us 0.15 小词表检测器识别，不再要求中文 Vosk 词表或正文 ASR 必须转写出这个短词。中文控制词检测、英文 delimiter 检测、Silero VAD 和正文 ASR 都消费同一次麦克风采集的音频 block；程序不会为 `over` 再打开一个麦克风，也不会保存原始音频。

英文 detector 会请求 Vosk 的词级和 partial 词级时间，把命中的 `over` 绑定到当前麦克风流的样本区间；若没有可用词时间，则保守退回到命中所在音频 block 的近似区间。命中不会用异常截断 VAD，而是在话语结束后按 marker 区间切分本轮内存音频：marker 本身不送入正文 ASR，前后片段先经过独立的分窗能量门控，明显静音不会再送给模型产生“嗯”等幻听；真实有声片段分别转写。每个 marker 完成它前面的 prompt，最后一段保留为下一条 pending prompt。因此同一个 VAD 话语中的多个 `over` 也可依次入队；若 KWS 漏掉，正文 ASR 自己识别出的独立单词 `over` 仍是后备路径。安装或升级后必须重新运行 `download-models`；`doctor` 的 `models.delimiter.ready` 应为 `true`。样本边界不等于所有口音和噪声下都精确的词边界，清晰说出 `over` 并留一个很短的自然停顿仍有助于识别；最终以“已入队”反馈为准。

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

### 可选的 ASR 原文日志

如果需要排查正文 ASR 是否听对，可在本机配置中显式设置 `privacy.save_transcripts: true`。运行时会把送入会话层的 ASR 文本写入独立的 UTF-8 JSONL 轮转文件，包括唤醒话语、普通命令话语，以及按 `over` 样本边界切开的每个 segment；内容、标点和大小写不再经过 prompt 归一化，模型 adapter 只会去掉首尾空白。空 segment 也记录；若它因静音能量门控而根本没有调用 ASR，会明确带 `transcribed: false` 和 `skip_reason: silence_energy_gate`，从而与“调用了 ASR 但返回空”区分。它不会保存 PCM 音频，也不会把原文混进上述隐私受限诊断日志。

启动时会打印诊断文件、原文文件的完整绝对路径及启用状态。也可直接查看最近原文：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml transcripts --tail 50
```

默认原文文件是 `%LOCALAPPDATA%\HandsFreePC\transcripts\asr-transcripts.jsonl`，单文件最大 5 MiB，保留 5 个备份。公开默认和 `config.example.yaml` 仍为 `false`；原文可能包含口述路径、姓名、聊天内容或其他敏感信息，启用者应自行控制该 Windows 用户账户和文件备份/同步范围。

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

- 原始音频始终不由该功能落盘；转写默认不落盘，只有显式设置 `privacy.save_transcripts: true` 才写入独立的本机原文日志；
- 云 planner、电脑控制、屏幕上下文许可和真实执行默认关闭；
- 不允许任意 shell、PowerShell、Run 对话框或自定义脚本进入桌面动作 Schema；
- `strict`/`personal_trusted` 仍只向云 planner 暴露裁剪后的控件子集；`local_unrestricted` 会发送所有 fresh 可见顶层窗口的标题/进程摘要，并在观察后发送真实窗口标题和可寻址 UIA 上下文，Codex 还可接收选中窗口截图。截图和窗口标题可能包含敏感信息；首次验收应关闭无关窗口并使用非敏感账户；
- 上述数据范围只描述 HandsFreePC 主动组装的输入；Codex/Claude CLI 及其提供商仍可能处理账户、网络、CLI/OS/runtime、临时工作目录和诊断/遥测等自身元数据，项目开关不能把这层变成零元数据；
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

**English summary:** HandsFreePC 0.3.1 keeps continuous local speech input, independent offline English-Vosk `over` detection, and FIFO execution, but replaces model-owned mouse/keyboard control with an owned Windows UIA driver. Claude CLI is the default strict, text-only step planner. Codex CLI is an explicit best-effort alternative that requires host-read consent, can receive the selected-window screenshot in `local_unrestricted`, and is not a guaranteed no-tools mode. Every generic planner action requires a fresh binding and a false-before/true-after local postcondition; deterministic native skills use action-specific local evidence. Public defaults remain disabled and `strict`. An explicitly local, uncommitted `local_unrestricted` profile removes `APP_SCOPE_REQUIRED`, dynamically enumerates every visible ordinary top-level window, and permits inferred cross-app low-risk navigation without confirmation. An explicitly spoken app/window/field still binds the exact user step; natural search requires the exact query, Enter/Return, and a fresh result transition. Recognized send/delete/install/upload/share/close actions still require confirmation. Terminal/Run/UAC/authentication/password/credential/payment/privacy surfaces, coordinate-only actions, and arbitrary shell remain hard boundaries. WorkMap execution currently uses exact local aliases only; planner hints are not attached to cloud prompts.
