# HandsFreePC 0.3 隐私说明

HandsFreePC 是本地优先、常开麦克风的 Windows 辅助工具。公开配置默认不保存录音或转写，不启用云 planner，不启用电脑控制，不允许屏幕上下文离机，并保持 `execution.dry_run: true`。

“默认不保存”不等于声音从未被处理：运行期间音频、预卷、转写 fragment、pending prompt 和 FIFO 任务会短暂存在内存中。Windows 音频驱动、杀毒软件、调试器、你安装的其他程序以及 Codex/Claude CLI 有各自的数据边界。

## 默认本地数据流

```text
麦克风 PCM（内存）
  +-> 本地 Vosk：开始/结束/急停/确认/恢复等控制词
  +-> 本地 Silero VAD：切句
      -> 本地 SenseVoice：正文转写
      -> PromptAssembler：正文中的 over
      -> 本地有界 FIFO
```

默认：

- `privacy.save_audio: false`；
- `privacy.save_transcripts: false`；
- `privacy.allow_cloud_planner: false`；
- `computer_control.enabled: false`；
- `computer_control.allow_screen_context_to_cloud: false`；
- `execution.dry_run: true`；
- `speech.fallback.backend: none`。

0.3 没有受支持的音频/转写持久化器；把前两个布尔值改为 `true` 也不应被理解为已经启用录音功能。未来若增加诊断保存，必须有独立的单次 opt-in、明确目录和删除提示。

## 连续语音会话

说“开始语音操作”后，SenseVoice 的正文转写被拼到内存。只有识别到独立 `over` 后，完整 prompt 才进入队列。说“结束语音操作”会丢弃未完成半条并排空已接受任务；它不会关闭麦克风。急停会请求取消当前任务并清队列，也不会删除已经送往 provider 的数据或撤回外部副作用。

当前 `over` 仍依赖正文 ASR。`PromptAssembler.finalize()` 只是未来 KWS seam，没有第二个关键词模型在后台运行或下载。未来 KWS 应复用同一个音频采集流并携带 timestamp；候选模型许可待澄清。

## NativeSkillRouter

确定性本地命令在完整命中时不会调用 Codex/Claude。它会把转写留在当前进程内，使用本地路径/应用配置、WindowsExecutor 和安全策略。

但“本地”不等于“无旁观风险”：overlay 可能显示识别文本，SAPI 可能朗读路径、项目名、错误和确认摘要。使用前按旁观/旁听环境选择 `overlay`、`voice`、`both` 或 `silent`。

### 旧单句 cloud fallback 的单独边界

顶层 `planner.enabled` 属于兼容 `VoiceRuntime` 的 one-shot fallback，不是下面的 0.3 desktop step planner。确定性 parser miss 时，它接收用户完成的原句，以及只含 runtime state、已配置应用名和当前 feedback mode 的 `current_non_sensitive_context`；HandsFreePC 不为这条路径附加窗口标题、UIA tree、截图或路径目录清单。它仍会把原句发送给所选 CLI/provider，且下文所述账户、网络、runtime 与诊断元数据边界同样适用。

该云输出只可提出用户原句中肯定、非引号/数据引用且精确授权的应用 UI 导航（激活应用、打开明确项目/对话/tab/mode、进入明确听写或应用内语音）。反馈切换、暂停、恢复、等待、路径打开、文本输入和发送 prompt 不能由它决定；这些动作只接受本地确定性 parser 的完整命中。

## 启用 0.3 云单步 planner 后发送什么

只有同时设置以下许可，`local_agent` 的 Codex/Claude planner 才可用于真实电脑控制：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  enabled: true
  backend: local_agent
  planner_backend: claude
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: false
  allow_legacy_codex_computer_use: false

execution:
  dry_run: false
```

NativeSkillRouter miss 后，每个 planner step 当前可能收到：

- 用户完成的一条语音 prompt；
- 用户原句中唯一明确授权、且当前可见的 app 摘要；
- 当前 task-authorized observation generation；
- 只包含用户原句中肯定、精确点名控件的任务授权 UIA 子集；
- 这些控件的 index、名称、control type、selected/focused/enabled 状态；
- 最近最多 8 条本地验收历史；
- 合成的应用级窗口标识。

通用任务还必须在用户原句中肯定、明确且只指定一个已配置应用。应用清单会先被本地解析为严格 JSON；零个、多个、仅否定提及或顺带提及应用时，不会把整段自由文本清单继续交给 planner 猜测目标。

当前 0.3 planner prompt **不发送**：

- 原始 PCM 音频；
- 未由 `over` 完成的 pending 半条；
- screenshot PNG bytes；
- 真实 screenshot 是否存在/可用；
- 原始窗口标题和进程 ID；
- 未由当前任务明确点名的 UIA 元素、聊天正文和侧栏标题；
- automation ID 和元素 value；
- 剪贴板内容；
- 全桌面截图；
- 全量目录清单；
- HandsFreePC 主动收集的密码字段值；
- 名称含常见 API key/token/secret/password/credential 标记的环境变量。

完整 UIA 快照可能包含病历、学生信息、客户数据、聊天内容、文件名或页面里的无关私人信息，因此只留在本地做 fingerprint、目标重绑定和 after-state 验收。云 planner 只接收任务授权子集；凭据样式长串还会被本地替换为 redaction marker。该最小化不是完整 DLP：用户亲口点名的控件标签仍会离机，也可能包含敏感词，所以仍要求独立屏幕上下文许可，并应先在非敏感测试账户验收。

密码元素值不会进入 observation；完整本地快照若被已知词形/元素属性识别为认证、password、UAC/Windows Security、凭据、付款、terminal/shell、隐私设置或公开链接/链接共享 surface，就在发送前 fail closed。未授权的敏感旁支原文不会发送，但仍参与本地分类。规则无法识别所有语言、同义词、自绘控件或伪装界面，因此这不是完整 DLP。

通用 UI confirmation 摘要若需要原文显示一个目标标签，只回显用户原句中已经验证的 exact target label。未授权 sibling/window label 的原文和语义只在本地分类，不进入摘要；摘要中的短 digest 只是不可逆绑定元数据。文本 payload 只取用户亲口给出的 exact span；确定性 native path 摘要可能另行显示已解析路径，因此 overlay/SAPI 的路径旁观风险仍适用。

以上列表只描述 **HandsFreePC 主动构造的 planner 输入**，不是 CLI/provider 的完整数据清单。即使项目过滤环境变量并使用临时工作目录，Codex/Claude CLI 及其提供商仍可能处理账户/组织、认证、网络地址、CLI 与 OS/runtime 版本、调用时间、用量、错误和诊断/遥测等自身元数据，也可能构造自己的系统级 runtime context。HandsFreePC 不能查看、删除或承诺这些数据为零；应按所用 CLI 版本、账户类型和提供商当前政策单独审查。

## Claude planner 边界

Claude 是 0.3 的默认 desktop planner。adapter 使用独立 system policy、safe/restricted 模式、空工具列表、严格 MCP 配置、非交互权限模式、JSON Schema 和无会话持久化。它仍会把上述 prompt/context 发送给 Anthropic，并需要本机登录。

`--no-session-persistence` 只描述本次 CLI 的本地会话行为，不等于 provider 零保留。消费者、商业账户和组织设置可能不同；使用前阅读 Anthropic 当前数据使用说明并核对账户设置。

## Codex planner 边界

Codex 不是默认 planner。只有显式设置 `planner_backend: codex_cli_best_effort` 和 `allow_codex_cli_host_read: true` 后，0.3 单步 adapter 才可使用 ephemeral 临时目录、忽略用户配置/规则、结构化输出、environment filtering 与 read-only sandbox。它没有 HandsFreePC 的 DesktopDriver，也不复用旧 Computer Use thread。

项目会尽量禁用当前已知工具，但订阅版 Codex CLI 没有可由本项目证明的完整 no-tools 模式。read-only sandbox 也不是主机保密容器：CLI 仍以当前用户身份运行；空目录、deny list 和 prompt 禁令不证明当前用户可读文件绝对不可访问。不要在包含高敏感文件的主机上仅凭这些参数开启 planner。

Codex 的认证、传输、服务端留存、训练/数据控制、错误报告和缓存由当前账户与 OpenAI 产品政策决定。HandsFreePC 不代理或删除这些数据。参考 [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)；使用订阅登录的用户还应检查当前 ChatGPT/Codex 账户设置。

## 项目自有 Windows UIA driver

默认 `windows_uia` 在本机读取已配置目标应用的窗口标题、进程元数据和 accessibility tree；执行 UIA semantic action 或 Unicode `SendInput`，并重新读取状态做本地验证。

启用云 planner 时，仅前述 task-authorized 子集进入 planner；完整 UIA 元数据仍只在本地 verifier 使用。使用 `planner_backend: none` 时不发送给 Codex/Claude，但 generic miss 无法规划，只能运行确定性 native skill。

driver 不使用剪贴板，不读取 password value，不保存 screenshot/audio/transcript 文件。操作会占用前台窗口，并可能让旁人看到输入或使目标应用自己产生历史、草稿、审计日志和云同步；这些外部持久化不受 `save_transcripts` 控制。

通用 agent 的 `type_text`/`set_value` 即使只写草稿，也必须先显示/播报本轮随机四位一次性确认口令；单独说静态“确认执行”无效。同一 `VoiceRuntime` 进程内已签发码在确认、取消或超时后都不回收，有界重抽耗尽时拒绝；去重集合不持久化，重启后不保证绝对不复用。挑战码不是持久化防重放凭证或说话人认证，也不能阻止同一房间的人、扬声器或实时转述/重放获取本轮口令后代说。

## Qwen open-computer-use 实验 driver

[Qwen open-computer-use](https://github.com/QwenLM/open-computer-use) 0.2.3 是可选、本机 MCP stdio 进程，不随默认安装下载。它可能在本地取得目标窗口 accessibility text 和 screenshot PNG，但上游 0.2.3 当前没有向适配器提供可安全绑定的结构化 elements。HandsFreePC 的 safety 会重建 task-authorized planner view，移除原始 accessibility text、截图 bytes 和真实截图可用性；由于没有结构化元素，这个 driver 的云 UI 子集通常为空。上游进程本身仍能在本机访问屏幕内容。

中文 Windows 有未解决的编码边界：[Issue #5](https://github.com/QwenLM/open-computer-use/issues/5)、[PR #6](https://github.com/QwenLM/open-computer-use/pull/6)。只有显式 `allow_experimental_driver: true` 才可启用。不要把它部署到包含敏感中文内容的目标窗口，除非已在同版本上验证 Unicode round-trip 并接受残余风险。

上游进程的日志、缓存和未来版本网络行为不由 HandsFreePC 0.3 保证；安装或升级前应检查锁定版本和上游源码。详见 [docs/OPEN_COMPUTER_USE.md](docs/OPEN_COMPUTER_USE.md)。

## legacy_codex_cli 边界

旧 `legacy_codex_cli` 会让 Codex Computer Use plugin/thread 直接观察和操作目标应用，屏幕内容、截图、可见文字、thread context 和 agent 输出可能由 OpenAI 处理。该路径保留用户 Codex 配置/plugin，并可产生 Codex 自身的历史或 app approval。

它没有 0.3 LocalVerifier；`VERIFIED_COMPLETION` 是同一 agent 的自报状态。正常 drain、急停、退出 HandsFreePC 或删除 `config.local.yaml` 都不会自动删除 Codex/提供商历史、缓存或持久 app approval。该 backend 只用于显式兼容，不建议新部署。

启用它还必须同时设置 `allow_codex_cli_host_read: true` 与 `allow_legacy_codex_computer_use: true`；这两项分别表示接受 Codex CLI 主机读取边界和旧 controller 更宽的 Computer Use/无本地 verifier 边界。

## 反馈模式与旁观者

- `overlay`/`both` 可能显示完整识别文本、队列状态、错误和确认摘要；
- `voice`/`both` 可能朗读同样内容；
- `silent` 隐藏普通反馈，但确认/错误仍可能强制显示；
- SAPI 播放期间采用半双工，输入缓冲可能被清除，且播放中的声音不能用语音急停；
- 反馈不是审计日志，也不能证明操作成功。

敏感环境优先使用不含具体文本的短反馈，并保持屏幕不被旁人看到。不要口述密码、验证码、token、病历、学生/客户标识或不应外发的草稿。

## 常开麦克风、儿童与旁人

即使默认不保存，麦克风仍会短暂处理房间中所有声音，包括儿童、访客、会议和扬声器回放。HandsFreePC 没有说话人识别，也不区分用户与旁人。

使用者应：

- 遵守告知、同意和当地录音/隐私法律；
- 在访客、医疗、教育、保密会议或远程通话时退出；
- 不用本项目识别、推断或记录儿童/旁人身份；
- 留意 Windows 麦克风指示；
- 扬声器播放可能含控制词时暂停；
- 需要彻底停止时退出进程或关闭 Windows 麦克风权限。

结束语音会话、队列 drain、`silent` 和急停都不是关麦。

## 本地持久化

HandsFreePC 可能在本地保留：

- 用户自己创建的 `config.local.yaml` 或本地应用配置；
- 下载的 ASR/VAD 模型、来源与许可文件；
- Python/Windows/CLI 自己的安装缓存和诊断数据；
- 用户主动重定向保存的 doctor/test 输出。

HandsFreePC 0.3 自身不建立音频/转写历史库。prompt assembler、FIFO、confirmation ID 和 local agent task state 在进程内存中；退出后不会由本项目恢复。目标应用、Codex/Claude CLI、provider 和 optional MCP server 可能各自持久化数据。

`doctor`、`test-asr`、pytest failure 或手工 UIA 检查输出可能包含路径、设备名或窗口内容。重定向、复制或上传后就形成新的持久副本；分享前人工脱敏。

不得提交到 Git：音频、转写、本机绝对路径、UIA dump、截图、模型权重、token、登录缓存、日志或 `config.local.yaml`。

## 可选 faster-whisper

默认 `speech.fallback.backend: none`，普通安装不包含 faster-whisper。显式安装和预载 `large-v3-turbo` 会访问模型托管站并产生 GB 级缓存。当前 fallback 只在已成功构造的 SenseVoice 某次 `transcribe()` 抛异常时触发，不处理空/低置信度结果，也不能补救 SenseVoice 启动/模型加载失败。

若不接受首次触发联网、模型缓存或额外资源占用，保持 `none`。模型权重有独立许可与数据边界，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 删除与停用

- 停止采集：退出 HandsFreePC 或关闭 Windows 麦克风权限；
- 删除本地配置：核对后删除项目中的 `config.local.yaml` 和你明确创建的本地配置；
- 删除模型：仅删除你明确配置的 `models` 目录；下次使用需重新下载；
- 撤销 Codex/Claude 登录、历史、缓存、app approval 和云端数据：使用各自产品的设置/命令；
- 清理 optional Qwen 安装和缓存：按上游安装方式处理；HandsFreePC 不自动安装也不自动卸载。

删除前核对绝对路径；不要对用户目录、磁盘根或未知变量运行递归删除。

## 本项目不做什么

- 不出售音频或转写；
- 不提供广告追踪；
- 不默认上传原始音频；
- 不在后台自动启用云 planner、电脑控制或实验 driver；
- 不用本项目采集的数据训练模型；
- 不承诺第三方 CLI、MCP server、模型或 provider 具有相同隐私政策；
- 不把本地 `doctor` 或 planner prose 当成 live 屏幕证明。

普通问题可在公开 issue 中提供脱敏的最小复现。涉及漏洞或个人数据时，请按 [SECURITY.md](SECURITY.md) 使用私密渠道。

---

**English summary:** Audio recognition is local by default; HandsFreePC does not save audio or transcripts and disables cloud planning and live control. With the 0.3 local agent enabled, the default Claude planner—or an explicitly consented Codex CLI best-effort planner—may receive a completed transcript, visible-app summary, only the UI controls affirmatively and exactly named in that task, and recent local verification history. Raw window titles, process IDs, unrelated UI/chat content, automation IDs, element values, PCM, screenshot bytes, and actual screenshot availability are excluded from the project-built step prompt; the full snapshot stays local for verification. CLI/provider account, host, connection, runtime, and diagnostic metadata remain separate boundaries.
