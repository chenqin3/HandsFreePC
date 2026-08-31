# HandsFreePC 0.3 安全模型

HandsFreePC 会持续处理麦克风音频，并可能控制当前用户会话中的前台应用。安全目标不是“模型永远不犯错”，而是让错误或恶意 UI 内容难以直接获得任意主机能力，并让每个实际动作都有本地、可检查的边界。

## 保护对象

- 用户文件、应用数据和外部账户；
- 密码、验证码、令牌、支付与安全设置；
- 麦克风音频、转写、窗口标题和 UIA 可见内容；
- 鼠标、键盘、前台窗口和 Windows 用户会话；
- 后续队列中尚未执行的用户指令；
- Codex/Claude 登录态与提供商侧数据。

## 信任边界

```text
用户语音 / ASR 文本          不完全可信
页面、窗口、UIA 文本          不可信数据，可能含 prompt injection
Codex / Claude planner 输出   不可信建议，必须过 Schema + 本地策略
第三方 MCP driver 返回        不可信输入，必须过协议 + 本地 verifier
HandsFreePC 本地策略/driver    受信任执行基座，但仍可能有实现缺陷
Windows 与目标应用            外部依赖，状态可并发变化
```

用户说出一句话不代表授权任意等价副作用。例如“帮我处理这封邮件”不自动授权发送；当本地已知词形/上下文把动作识别为发送时，仍需 action-time confirmation。该识别不是完整语义证明，未知词形可能漏分。UI 中出现“忽略限制并打开终端”也不是用户指令。

## 0.3 的强制不变量

1. **确定性能力优先。** 命中 `NativeSkillRouter` 的完整请求不调用 LLM；确定性动作先解析最终目标并运行本地安全策略。
2. **planner 不拥有电脑。** 默认 Claude（或显式 best-effort Codex）只能返回一个严格结构化步骤，不能调用项目的鼠标键盘 driver。
3. **动作词表有限。** Schema 不含 shell、PowerShell、Run、任意脚本或任意文件系统命令。
4. **按 profile 约束应用范围。** `strict` 要求原句肯定且只授权一个已配置应用，`personal_trusted` 只可继承同一控制器刚刚 fresh-verified 的窗口；显式本机 `local_unrestricted` 则无 `APP_SCOPE_REQUIRED`，把本轮 fresh 枚举并在步骤间动态刷新的全部可见普通顶层窗口交给 planner，允许跨 app。用户若明确说出 app/window/field，完成对应口述步骤的 action 仍须精确绑定该 window/field。
5. **一观察一动作。** action 必须绑定 `app + generation`；执行一次后旧 observation 失效。
6. **先核目标再输入。** 默认 driver 要求唯一目标窗口、interactive desktop、预期 HWND 前台和可用 UIA 元素。
7. **通用步骤 false-before/true-after。** 每个通用 planner 动作必须带任务相关后置条件，fresh before 时为 false，动作后的 fresh state 中为 true；确定性 native skill 使用动作特定证据，精确目标状态已成立时可幂等成功。
8. **动作后重新观察。** driver “accepted” 不等于成功；必须得到更高 generation 的 fresh state。
9. **本地完成验收。** planner prose 和旧 `VERIFIED_COMPLETION` 都不是证据；只有 `DesktopVerifier` 通过才返回 `LOCAL_VERIFIED_COMPLETION`。
10. **精确确认。** 确认 ID 绑定动作参数、expectation 与 observation fingerprint；随机四位一次性口令还绑定本轮 runtime pending action，并只保证在当前 `VoiceRuntime` 进程运行期内不再次签发。
11. **失败不降级。** `local_agent` 失败不会自动回退到旧 Codex controller、坐标点击或 shell。
12. **按 profile 最小化界面数据。** 本地策略先在完整 observation 上按已知词形和元素属性分类敏感 surface。`strict`/`personal_trusted` 只为云 planner 重建已授权控件子集，不发送原始标题或截图；`local_unrestricted` 会发送全部 fresh 窗口的标题/进程摘要和选中窗口的真实标题/UIA context，Codex 还可接收选中窗口 PNG，Claude CLI adapter 仍为 text-only。结构化 `CONTENT`、element value/automation ID、PCM 与剪贴板不发送。
13. **参数与显式目标绑定。** `strict`/`personal_trusted` 中，动作类型、完整目标短语、完整口述输入 payload、按键、点击参数、secondary action、滚动方向和页数必须来自当前用户步骤；`type/input` 与 `fill/write` 分别只授权 `type_text` 与 `set_value`。`local_unrestricted` 可推断普通中间导航，但用户明确说出的 app/window/field 仍约束最终用户 action，输入文本仍必须来自口述 exact span。自然搜索还必须精确设置查询、按 Enter/Return 并验证 fresh result transition。较长用户目标不能缩成短标签，输入不能截取 payload 子串或借用后续 payload，结果文本不能反向提供参数；不支持的尾随动作仍计入总步骤，不能被提前完成掩盖。
14. **不猜条件语义。** 没有本地条件求值器的条件命令整体 fail closed；不会把 `if/when/unless` 的分支动作当成无条件授权。

精确标签必须在动作前成立；fuzzy 命中不能先点击再失败。对于 click/navigation，单纯 `focused_contains` 只说明焦点移动，不证明目标页面已经打开。同目标 `ELEMENT_SELECTED` 只适用于用户明确要求 select/choose/switch 的步骤；open/click/send/delete/close 等必须使用用户原句明确给出的独立目标状态。

## 本地阻断和确认

### 已识别时默认阻断

- Windows Terminal、PowerShell、Command Prompt、shell/Run 表面；
- UAC、Windows Security、管理员批准和系统安全提示；
- password 元素、密码、PIN、OTP、API key、access token 等凭据界面；
- 支付、购买、下单、转账、银行卡和提现界面；
- 登录/注册/身份验证，以及隐私、遥测、账户设置、公开链接/链接共享界面；
- clipboard paste；
- 没有 UIA semantic target 的坐标 click/drag；
- 不属于当前 app/generation、已经过期或找不到的元素；
- 含 Unicode replacement character 的损坏 observation。

### 已识别时需要 typed confirmation

- `strict` 的全部通用 `type_text` 与 `set_value`，包括只写入草稿而不发送；`personal_trusted` 仅豁免本句完整口述、唯一聚焦非密码输入框中的未发送草稿；
- 本地词形/上下文识别为发送、提交、发布、回复或评论；
- 本地词形/上下文识别为删除、移除、清空或永久删除；
- 本地词形/上下文识别为安装、卸载或升级软件；
- 本地词形/上下文识别为上传、附加或共享文件；
- 本地词形/上下文识别为关闭、退出、取消或舍弃应用状态；
- 已知会触发上述副作用的 Enter/Delete/快捷键。

`local_unrestricted` 的普通低风险窗口/选项卡切换、菜单导航、Toggle 和没有命中风险分类的通用 OK/Continue 对话框不需要确认；这项豁免不覆盖上列已识别的高影响动作。除 `type_text`/`set_value` 的 profile 门禁外，以上副作用确认和 surface 阻断依赖有限的中英文词形、控件属性与上下文规则。它们只是纵深防御，不是完整语义证明；未知语言/同义词、应用文案变化、自绘控件或伪装界面可能漏检。因此公开默认关闭真实执行，重要外发、删除、安装、分享或不可逆任务不应无人监督。

## typed confirmation 协议

本地安全层对 `action type + app + arguments + observation fingerprint + expectation` 计算 digest，产生 `desktop-...` confirmation ID。fingerprint 含只在本地使用的窗口 HWND identity 与完整 UIA 状态；generation 不直接进入 digest，因为确认执行前必须 fresh observe，再把原动作重绑定到新 generation。确定性/旧单句计划使用绑定完整 plan/source 和目标文件快照的 `native-...` ID；待确认 `Plan` 以规范深快照保存，对外只返回副本，执行器得到的是确认后重新构造且不与调用方共享 `Action` 的独占快照。

流程：

1. agent loop 保存待确认的精确动作、原 observation、摘要和过期时间；
2. runtime 从当前进程尚未签发的四位码中有界抽样；确认、取消或超时都不回收已签发码，找不到新码时 fail closed；只有显示/完整播报“确认执行 4 8 2 7”这类提示后才开放确认；
3. 用户准确说出本轮前缀加四位码时，runtime 把保存的 ID 传给 controller，而不是向 planner 发送一条“确认” prompt；只说静态“确认执行”无效；
4. controller 检查 ID 唯一、未使用且未过期；
5. 对通用桌面动作重新 observe，并要求 fingerprint 未变化；
6. 把同一动作 rebind 到新 generation，重新做本地风险分类；
7. 只执行这一个动作，再 fresh observe 和本地验收；
8. confirmation 一经取出就不能重放。

确认只能覆盖已描述动作，不能授权后续队列或新的页面状态。等待确认时“继续队列”不能绕过确认。

`native-...` 绑定包含 `Plan.to_dict()`、`plan.source`，以及每个已解析 `OPEN_PATH` 的规范绝对路径、mode/size/mtime/ctime/device/inode 与目录标志；普通文件还绑定在稳定文件身份下读取的 SHA-256。确认时先重新 `prepare_plan`，保持风险不得降低，再用保存的原用户文本重新做 safety、构造独占执行快照并计算新 binding。无需确认的安全 `OPEN_PATH` 也必须在 runtime 和 deterministic native router 内做 safety 前后双 binding；二者都不能成为旁路。Windows 上从最后 binding 到执行和后置检查结束，目标读取句柄只允许其他读取者共享，拒绝并发写入、删除或替换。plan/source、规范路径、stat 身份或文件 hash 任一变化，或无法重新验证，都会取消而不是执行。目录只保护/绑定自身，不递归冻结或哈希目录内容。

通用 UI confirmation 摘要若原文回显 UI 标签，唯一允许的是用户原句中已验证、且 fresh safety 再次确认的 exact target label。未授权 sibling/window label 的原文和语义只参与本地完整 surface 分类与 fingerprint，不进入摘要；摘要中的短 digest 只是不可逆绑定元数据。文本输入 payload 另标为用户亲口给出的 exact span，不从 UI 抄取。

四位码去重集合不持久化；进程重启后不保证绝对不复用，所以它不是持久化防重放凭证或说话人认证。旁人、扬声器或实时转述/重放若在有效期内听到本轮码，仍可能代说；遮罩也可能被旁观。高风险动作需要用户看屏幕监督，不能把随机码当作无人值守授权。

## UI prompt injection

网页、聊天消息、文档、项目名和 accessibility label 都可能包含恶意指令。防护：

- planner prompt 明确把 task-authorized 控件子集标为 data；
- `strict`/`personal_trusted` 的 planner 只收到唯一授权 app 摘要、相应裁剪控件和最近本地验收摘要；`local_unrestricted` 则会收到全部 fresh 可见顶层窗口的标题/进程摘要、选中窗口真实标题和可寻址 UIA 控件，Codex 还会收到选中窗口截图；
- Schema 只允许单个语义动作；
- 本地策略不采信页面关于安全/确认的声明；
- `strict`/`personal_trusted` 在发送 planner view 前阻断已识别的终端、认证、凭据、付款、隐私/公开链接和 OS 安全界面；`local_unrestricted` 在 observe 前阻断终端/Run/UAC/认证身份、聚焦 secret 和高置信凭据，付款/隐私目标则在 action 风险评估时硬阻断；
- 动作必须引用当前 observation 的 element index；历史 index 不可复用；
- 本地 verifier 只比较状态，不采信页面里的“操作已成功”作为唯一证据。

残余风险：阻断/确认词表无法穷举语义，UIA 名称可用未知词形伪装正常按钮，文本 expectation 也可能只证明一段文字出现而不是业务副作用完成。涉及外发、删除、资金、认证、软件安装、链接公开或不可逆后果时仍需人工监督或保持阻断。

## planner 隔离边界

### 旧单句 cloud fallback

顶层 `planner.enabled` 的兼容 one-shot planner 只能返回原句明确授权的应用 UI 导航：`ACTIVATE_APP`、`OPEN_CONVERSATION`、`OPEN_MODE`、`ENTER_DICTATION` 或 `START_NATIVE_VOICE`。应用及 project/conversation/tab/mode 必须由用户原句肯定、非引号/数据引用地精确授权；听写或应用内语音也必须有对应原句授权词。`SET_FEEDBACK_MODE`、`PAUSE`、`RESUME`、`WAIT`、`OPEN_PATH`、`TYPE_TEXT` 和 `SEND_PROMPT` 即使由云 Schema 合法解析，也会被本地 safety 阻断，只能来自本地 deterministic parser。

### Claude（默认）

Claude adapter 显式传入独立 system policy、空工具列表、safe/restricted 模式、严格 MCP 配置、非交互拒绝权限升级和无会话持久化。它仍是当前用户启动的联网 CLI，其认证、遥测和服务端保留不由 HandsFreePC 管理。

### Codex（显式 best-effort）

单步 adapter 只有在 `planner_backend: codex_cli_best_effort` 与 `allow_codex_cli_host_read: true` 同时设置后才启用。它使用 ephemeral 临时目录、忽略用户配置/规则、结构化输出、环境变量过滤、read-only sandbox，并尽量禁用当前已知工具；它不获得 DesktopDriver。

订阅版 Codex CLI 没有可由本项目证明的完整 no-tools 模式。read-only sandbox 也不是主机隔离容器，不能保证当前用户可读文件无法被 CLI 访问。不要在敏感主机上仅凭这些 flag 开启云 planner。若后续迁移到支持 `tools: []`/`tool_choice: none` 的 API harness，仍必须保留本地执行和 verifier 边界。参考 [OpenAI computer-use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)、[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) 与 [Responses API tool choice](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)。

### 共同云数据边界

启用 `local_agent` 且 `planner_backend != none` 时，配置必须同时允许转写和屏幕上下文离开本机。当前 0.3 planner prompt 可包含：

- 完整的当前任务转写；
- task-authorized observation generation；
- 最近最多 8 条本地验收历史。

`strict`/`personal_trusted` 另包含唯一授权 app 摘要和相应的 task-authorized UIA element 名称/index/control type/selected/focused/enabled 状态；不发送原始窗口标题、截图 bytes 或真实截图可用性。`local_unrestricted` 另包含本轮所有 fresh 可见普通顶层窗口的动态 app ID、display name、foreground、process name、真实窗口标题，以及 observe 后经凭据过滤的全部可寻址 control/input 元素。Codex adapter 还可把选中窗口 PNG 作为临时 `--image` 输入；Claude CLI adapter 是 text-only，只接收文本 inventory/title/UIA context，不接收这份 PNG。

所有 profile 都不把 automation ID、element value、PCM、剪贴板或 `CONTENT` plane 节点作为结构化字段发送；完整原始 observation 留在本地 verifier，凭据样式长串在 planner payload 中再做 redaction。但 `local_unrestricted` 截图像素可能视觉包含聊天正文、文件名或通知。这不是未来版本保证，升级前应重审 [PRIVACY.md](../PRIVACY.md)。

这只限定 HandsFreePC 主动构造的 prompt。CLI/provider 仍可能处理账户/组织、认证、网络、CLI/OS/runtime、临时工作目录、调用时间/用量、错误和诊断/遥测等自身元数据，或添加自己的系统级 runtime context；项目开关不能证明这些数据不存在。

## 默认 Windows UIA driver 边界

受支持方式是普通权限、当前交互用户、`Default` input desktop。项目不安装 Windows Service、不要求管理员权限、不使用 UIAccess，也不自动同意 UAC。

Windows UIA driver 的本地证据包括窗口 HWND、进程名/标题、当前 generation、可见/启用元素、值/选中/焦点状态。password value 不进入 observation。动作前后复核前台窗口；Windows UIPI 仍可能阻止普通进程向更高完整性应用输入。

公开默认 `strict`/`personal_trusted` 只解析已配置 app profile。显式本机 `local_unrestricted` 会 fresh 枚举全部可见普通顶层 HWND，将多 Chrome 窗口分别绑定，并在后续步骤刷新 inventory；observe 时激活/复核确切 HWND/PID/process/title，消失、复用或身份变化即停止。它取消 `APP_SCOPE_REQUIRED`、普通中间导航目标点名与普通低风险导航确认，允许跨 app 推断窗口/选项卡、菜单、Toggle 和未命中风险分类的通用 OK/Continue 对话框；明确口述的 app/window/field 仍 exact bind。自然搜索必须精确设置 query、按 Enter/Return 并看到 fresh result transition。识别到的发送/提交、删除、安装、上传/分享和关闭等高影响动作仍要求本轮确认；终端/shell、Windows Run、UAC/安全桌面、认证、密码/凭据、付款、隐私/账户设置、纯坐标和任意 shell 仍是硬边界。每个允许动作仍需 fresh bind 和本地 false-before/true-after 验证。

残余风险：

- process name/title 不是代码签名；同名窗口可能伪装；
- Electron 应用更新、语言、A/B 功能或自绘控件会改变 UIA；
- 应用可在 observation 与 action 之间并发变化；generation/fingerprint 缩小但不能消除 TOCTOU；
- UIA 驱动在输入前重新读取 exact element identity、可见/启用/password 状态；这一复核与实际 OS 输入之间仍存在不可完全消除的瞬时 TOCTOU；
- 同一 HWND 的界面若变化后又完全回到相同 fingerprint（ABA），确认前比较无法发现中间过程；高价值任务仍需人工监督；
- `SendInput` 成功返回不证明目标应用接受文本，所以 verifier 还必须在 fresh UIA 中看到精确值；
- UIA 不一定暴露 canvas、远程桌面或浏览器内部所有状态。

确定性 `OPEN_PATH` 也有分层证据：Explorer 目录用规范化精确路径和前台 HWND 验证；文件要求出现新的前台 HWND，且标题包含精确文件名，仍只是 best-effort。同名文件、复用旧窗口和不显示文件名的查看器会失败或仍可能歧义，不能用于高价值文件的无人监督确认。

## Qwen open-computer-use 实验驱动

[Qwen open-computer-use](https://github.com/QwenLM/open-computer-use) 0.2.3 为 MIT 软件，但不随默认安装分发。它通过 MCP stdio 运行，Windows 实现会调用 PowerShell/UIA。中文 Windows 上游仍有编码问题：[Issue #5](https://github.com/QwenLM/open-computer-use/issues/5)、[PR #6](https://github.com/QwenLM/open-computer-use/pull/6)。

项目要求 `allow_experimental_driver: true`，并在 Unicode 损坏、edge whitespace 会被截断、tool 集缺失、过期 generation 或 mutating call 后连接结果未知时 fail closed。即使开启实验 driver，也仍需本地 safety、fresh observe 和 verifier；MCP 返回“成功”不是证据。

当前 0.2.3 observation 没有可安全绑定的结构化元素列表，因此通用 planner 的元素点击/导航能力受限，`type_text`/`set_value` 也会因缺少可验证的焦点元素而失败关闭。该 driver 不是默认 UIA driver 的能力等价物。

该 driver 暂不进入 `computer-doctor --live` 的受支持组合，不能用默认 UIA fixture 结果替它背书。

## legacy_codex_cli

旧 controller 通过 Codex Computer Use plugin/thread 直接读取并操作桌面，然后由同一 agent 输出 `VERIFIED_COMPLETION`、`NEEDS_CONFIRMATION` 或 `FAILURE`。协议解析可以拒绝畸形输出，但不能独立证明屏幕变化。

因此：

- 只可显式配置 `backend: legacy_codex_cli`，并同时设置 `allow_codex_cli_host_read: true` 和 `allow_legacy_codex_computer_use: true`；
- factory 永不自动 fallback；
- `VERIFIED_COMPLETION` 不得写入测试证据的 PASS 条件；
- 它不拥有 0.3 typed action binding、fresh local UIA verifier 或自有 live doctor 保证；
- 新部署应使用 `local_agent/windows_uia`。

## 语音与队列威胁

| 威胁 | 当前缓解 | 残余风险 |
|---|---|---|
| 旁人/扬声器说出唤醒词 | 小词表、本地状态机、动作级安全与确认 | 没有说话人识别；环境音仍可能触发 |
| 旁人/扬声器重放确认 | 随机四位码、短有效期、动作/界面绑定、当前进程内已签发码不回收 | 实时听到并转述/重放本轮码仍可能授权；重启后可能复用，不是持久凭证或说话人认证 |
| `over` 漏识别 | 独立英文 Vosk 小词表、正文 ASR 后备、overlay 队列反馈 | 口音、噪声或设备距离仍可能漏检 |
| 一条失败后后续继续误操作 | worker 默认失败暂停 | 已发生副作用不能撤回 |
| 队列淹没 | 有界 FIFO、满时显式拒绝 | 用户可能没看到/听到拒绝反馈 |
| 急停延迟 | 本地控制词、取消当前与清队列 | cooperative cancellation 不能中断已到 OS 的一次输入；TTS 播放中不能语音急停 |
| 待确认界面变化 | 确认前 fresh observe + fingerprint | 应用仍可能在最终动作瞬间变化 |
| KWS 双开麦克风或 delimiter 错切 | 两个 Vosk detector 复用同一 `MicrophoneSource` block；delimiter 请求词级/partial 词级时间并按样本区间切分 | 词时间或 block fallback 仍可能受口音、噪声和语速影响而产生近似边界；短暂停顿可提高稳健性，但仍需核对入队反馈 |

英文 small-en-us 模型由安装器从 Vosk 官方地址下载并校验固定 SHA-256；模型不进 Git，安装目录保留来源、哈希和 Apache-2.0 COPYING。PCM 音频不落盘。公开默认也不保存转写；只有显式设置 `privacy.save_transcripts: true` 才把送入会话层的 ASR 文本写入独立的 per-user 轮转 JSONL，保留内容、标点和大小写但去掉模型输出首尾空白，并标记被静音门控跳过的 segment；它不混入隐私受限诊断日志。

## 静态与 live 证据

`doctor --strict` 是静态门禁，只检查依赖、模型、音频设备、CLI 与配置。它不会设置 `live_control_verified=true`。

`computer-doctor --live` 只对项目自有 fixture 做一次 Windows UIA Unicode round-trip，并由 `DesktopVerifier` 验收。通过仅能说明此机器的基础 driver 链路当时工作；不说明：

- 麦克风、Vosk、VAD 或所选正文 ASR 准确；
- Codex/Claude 认证、规划质量或服务可用；
- Codex、Claude 或其他目标应用的 UIA selector 可用；
- 发送、删除、上传、认证或其他高风险动作可安全执行；
- Qwen 实验驱动可用。

真实目标应用必须逐版本、逐语言布局做人工监督的低风险验收。见 [TESTING.md](TESTING.md)。

## 非目标

HandsFreePC 0.3 不是：

- 管理员提权、UAC 自动化或 Windows Security 控制工具；
- 无人值守的支付、认证、删除或外发机器人；
- 任意 shell/RPA 脚本入口；
- speaker identification 或儿童/旁人身份识别系统；
- provider 数据保留或本机绝对隔离保证；
- 对所有 Electron/canvas/远程桌面 UI 的兼容承诺。

发现绕过本地 safety、typed confirmation、generation binding 或 verifier 的漏洞时，请按根目录 [SECURITY.md](../SECURITY.md) 私密报告，不要在公开 issue 中附真实转写、窗口内容、路径或凭据。
