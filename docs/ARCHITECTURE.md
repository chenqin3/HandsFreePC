# HandsFreePC 0.3 架构

HandsFreePC 是运行在当前 Windows 11 用户会话中的本地优先语音控制器。0.3 保留持续监听、`over` 分段、有界 FIFO、失败暂停、急停和双反馈，把底层桌面控制改成项目自有闭环：模型只规划一个步骤，本地代码掌握 UI 权限并独立验收。

设计与 OpenAI 官方建议的自定义 computer-use harness 一致：应用负责观察、执行和安全策略，模型只返回动作建议。参考 [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)。未来若把 CLI adapter 换成持久 API，可参考 [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)；0.3 当前仍用已登录的 CLI。

## 总览

```text
AudioCapture（单麦克风）
  +-> Vosk control detector：开始/结束/急停/确认/恢复
  +-> Vosk delimiter detector（small-en-us）：over
  +-> Silero VAD + delimiter 样本区间切分
                    -> SenseVoice 分段 command ASR
                    -> PromptAssembler（逐 marker finalize；正文 ASR delimiter 后备）
                    -> bounded FIFO CommandWorker
                    -> DesktopAgentLoopController
                         1. NativeSkillRouter
                         2. DesktopStepPlanner（默认 Claude；Codex 仅显式 best-effort）
                         3. DesktopDriver（默认 WindowsUiaDriver）
                         4. fresh observe
                         5. DesktopVerifier
```

语音采集和任务执行在不同线程。执行第一条时，采集线程可以继续拼装并入队第二条；普通任务只由一个 worker 串行取出，因此不会同时争夺前台窗口。

## 会话和队列

状态语义：

1. 平时本地监听控制词；
2. “开始语音操作”进入连续 `ACTIVE` 会话；
3. SenseVoice fragment 进入 `PromptAssembler.feed()`；
4. 只有独立 delimiter `over` 前的非空文本成为一条任务；
5. 任务进入有界普通队列，严格 FIFO；
6. 安全确认进入独立控制队列并先于后续普通任务执行，但控制项之间仍 FIFO；
7. 普通失败令 worker `PAUSED`，需要恢复或急停；
8. “结束语音操作”拒绝新任务、丢弃未完成半条并进入 `DRAINING`，默认排空已接受任务；
9. 急停设置当前任务的 cooperative cancellation event 并清空待处理任务。

取消无法撤回已经到达 Windows 或外部服务的副作用。退出进程或关闭系统麦克风权限才会停止采集；结束语音会话不是关麦。

## `over` 的独立 KWS

delimiter 有两条本地路径。主路径由 Vosk small-en-us 0.15 小词表检测器请求词级及 partial 词级时间，把命中的 `over` 绑定到单调递增的麦克风样本区间；VAD 结束后，本轮内存音频按 marker 区间切成 n+1 段，marker 音频不进入 SenseVoice，各非空段分别转写，每个 marker 依次 `finalize()` 它前面的 prompt，最后一段保留为下一条 pending prompt。若某次 Vosk 结果没有可用词时间，则区间退化为命中所在 audio block，不能把这个近似当成精确词边界。若 KWS 没有命中而正文 SenseVoice 转写出独立单词 `over`，`PromptAssembler.feed()` 仍会按 ASCII 单词边界切分。中文 Vosk 不再加载它词表中不存在的英文 `over`。

两套 Vosk 检测器与 VAD/ASR 都消费同一个 `MicrophoneSource` 的 block，不会各自打开麦克风：

```text
Audio block + monotonic sample counter
  +-> control/KWS stream
  +-> VAD stream + in-memory capture

delimiter KWS hit
  -> 继续录到本话语的 VAD 终点
  -> 用词时间或 block fallback 得到 marker 样本区间
  -> 按一个或多个 marker 区间切成 n+1 段
  -> marker 音频不进 ASR；其他非空段分别由 SenseVoice 转写
  -> 每个 marker finalize 前段；末段保留为下一条 pending prompt
```

KWS 命中不会抛异常中断 recorder，因此不会仅因听到 `over` 就吞掉前面的正文。当前实现支持同一 VAD 话语内的多个 marker，也会把 marker 后正文留给下一条 prompt；但 Vosk 词时间或 block fallback 都不保证在所有口音、噪声和语速下形成精确边界。短暂自然停顿是提高识别和切分稳健性的建议，不是协议强制要求，用户仍应以入队反馈确认结果。

## NativeSkillRouter：本地确定性优先

`handsfree_pc/desktop/native_skills.py` 复用确定性 intent parser、`WindowsExecutor.prepare_plan()` 和本地 `SafetyPolicy`：

- parser miss 是唯一允许进入模型 fallback 的分支；
- parser 只匹配请求前缀、但后面还有点击/填写/上传等未覆盖工作时，视为 miss，不能静默丢掉后半句；
- 一旦确定性命中，先解析全部目标并完成风险分类，再执行第一个动作；
- 受限动作直接完成，确认动作保存精确 `Plan`，被阻断或失败的确定性计划不会交给模型“再试一次”。

该层没有 LLM、shell 或任意脚本 hook。它适合路径打开、激活已配置应用、固定选项卡/听写和反馈切换等可严格描述的操作。

`OPEN_PATH` 也遵守 false-before/true-after：执行前目标不能已经满足“当前前台已打开”，Shell dispatch 后要求前台 HWND 与 before 不同。目录再通过 Shell.Application 返回的 Explorer 路径做规范化精确比较，证据较强；文件目前只能验证新前台窗口标题包含精确文件名，因此是 best-effort，不能区分不同目录中的同名文件，也不能覆盖复用同一 HWND 或不显示文件名的查看器。

`OPEN_MODE` 把 parser 产生的 canonical `tab`/`mode` 名称交给应用 profile。只有 `apps.*.mode_names` 显式列出的 canonical key 才属于 native allowlist，labels 按顺序尝试；缺少映射会在激活或点击前拒绝。执行器在任何输入前拒绝 fuzzy-only 命中，并要求最终 mode 的精确标签可验证为 selected；focus-only 不构成导航完成证据。

### 兼容 `VoiceRuntime` 的旧单句云 fallback

顶层 `planner.enabled` 只服务旧 one-shot `VoiceRuntime`，与下文 `computer_control.planner_backend` 的逐步 planner 不同。它仅在本地 deterministic parser 不能完整覆盖原句时运行；`SafetyPolicy` 对 source 为 `claude`/`codex`/`llm` 的 plan 只允许 `ACTIVATE_APP`、`OPEN_CONVERSATION`、`OPEN_MODE`、`ENTER_DICTATION` 和 `START_NATIVE_VOICE`。应用以及 project/conversation/tab/mode 必须在用户原句中肯定、非引号/数据引用地精确出现；听写和应用内语音还需原句明确给出对应授权词。

因此旧云 fallback 不能决定 `SET_FEEDBACK_MODE`、`PAUSE`、`RESUME`、`WAIT`、`OPEN_PATH`、`TYPE_TEXT` 或 `SEND_PROMPT`。这些状态、文件和数据动作只接受本地 parser 的完整命中，云 plan 越界时在执行前阻断。

该运行时的 `native-...` confirmation binding 覆盖完整 `Plan.to_dict()` 与 `plan.source`。每个 `OPEN_PATH` 目标先 `resolve(strict=True)`，再绑定规范绝对路径、mode/size/mtime/ctime/device/inode 和目录标志；普通文件还在同一文件身份稳定时计算 SHA-256。待确认计划先变成不共享 `Action` 的规范深快照；对外读取 `pending_plan` 也只返回副本。确认口令匹配后不会直接执行保存或返回给调用方的对象，而是再次 `prepare_plan`、保持风险不降级、用原用户文本重新运行 safety、重建独占执行快照并重新计算 binding。无需确认的安全 `OPEN_PATH` 在 runtime 和 deterministic native router 中同样执行 bind → 二次 safety → deep clone → rebind。Windows 上最后一次绑定、执行和后置检查由拒绝写入/删除共享的读取句柄覆盖；plan/source、路径或文件身份/内容任一变化都取消。目录句柄只保护目录自身，不递归冻结其中内容。

## DesktopStepPlanner：模型只给一个步骤

未命中 NativeSkillRouter 的请求才使用 `handsfree_pc/desktop/step_planner.py`。planner 每次只能返回严格 JSON Schema 中的一种决定：

- `observe`：选择一个已配置且可见的应用；
- `action`：对当前 observation 给出一个 allow-listed 语义动作；
- `done`：给出一个可由本地状态检查的 expectation；
- `fail`：目标缺失、歧义、禁止或无法验证时停止。

动作集合只有 `click`、`perform_secondary_action`、`scroll`、`type_text`、`press_key` 和 `set_value`。0.3 planner Schema 不含任意 shell、PowerShell、脚本、文件系统 API 或坐标字段。

进入 planner 前，agent loop 会从用户原句中提取肯定、明确命名的已配置应用。`strict` 要求每条任务恰好命名一个应用；`personal_trusted` 可在同一控制器会话内沿用上一条已经本地验证成功的应用/窗口，但沿用前必须 fresh observe 并核对应用与窗口身份。新控制器、窗口变化、零个可验证上下文、多个应用、否定提及或说明性顺带提及时均失败关闭；planner 不能自行把任务扩展到第二个应用。

规划上下文包括用户任务、唯一明确授权的可见应用摘要、task-authorized observation generation、最多 8 条本地已验收历史，以及由本地策略重建的 UI 子集。`strict` 只包含本句肯定且精确点名的可寻址控件；`personal_trusted` 还可包含已授权应用内的安全导航控件与当前输入框。两者都只发送 index/control type/selected/focused/enabled 等最小状态；`CONTENT` plane 永不进入 planner。原始窗口标题、进程 ID、automation ID、value、聊天正文、截图字节和真实截图可用性不进入 planner；完整 observation 只在本地做 freshness、动作重绑定与 after-state 验收。UI 子集仍被标记为 data，不能成为新指令。

### Claude adapter（默认）

Claude 使用独立 `--system-prompt`、`--safe-mode`、`--restricted`、`--strict-mcp-config`、`--tools ""`、`--disallowedTools mcp__*`、`--permission-mode dontAsk`、JSON Schema 与 `--no-session-persistence`。它只返回一步，不拥有 UI 驱动。

### Codex adapter（显式 best-effort）

Codex 每一步使用 ephemeral 临时目录、忽略用户配置和规则、结构化输出 Schema、`shell_environment_policy.inherit=none`、read-only sandbox，并尽量禁用当前已知工具。只有配置 `planner_backend: codex_cli_best_effort` 和 `allow_codex_cli_host_read: true` 才能启用。该 adapter 不持有 DesktopDriver，也不复用旧 Computer Use thread。

这不是完整 no-tools 保证。Codex CLI 仍是当前用户进程；临时空目录、deny list、环境变量过滤、prompt 禁令和 read-only sandbox 只是减小暴露面，不能证明当前用户可读文件绝对不可见。

两个 adapter 都使用登录后的 CLI 订阅，认证、额度、保留和可用模型由对应提供商决定。HandsFreePC 描述的最小 planner context 不涵盖 CLI/provider 自身可能添加的账户、网络、OS/runtime、临时工作目录、用量、错误与诊断/遥测元数据。启动失败、非零退出、超时、取消或不合 Schema 的输出全部 fail closed。

## DesktopDriver：项目持有 UI 权限

默认 `handsfree_pc/desktop/windows_uia.py` 组合 Win32 和 pywinauto UIA：

- 只接受 `config.local.yaml` 中声明的应用 profile；
- 通过进程名和标题查找可见窗口；多个窗口时只接受唯一前台匹配，否则报歧义；
- 每次动作前要求 interactive `Default` desktop，激活并复核目标 HWND 为前台；
- observation 为不可变快照，元素 index 绑定 `app + HWND + generation`；
- 一次动作后必须重新 observe，旧 index 失效；
- 密码元素值永不进入 observation，也不能被执行器操作；
- UIA 元素先分为 `CONTROL`、`INPUT`、`CONTENT`、`DIALOG`；Claude/Codex profile 优先保留可操作控件，长内容节点只保留本地有界摘要/digest 或省略；单个属性读取失败和元素超限不会拖垮整个 observation；
- planner-facing observation 按 safety profile 保留精确点名控件或安全导航控件，但永不包含 `CONTENT`；本地原始 observation 与其 fingerprint 不被该最小化替代；
- click 是 UIA `invoke/select/toggle` 或已绑定元素的一次左键 activation，不接受纯坐标；
- 输入优先 UIA value pattern，或对唯一焦点元素使用 Unicode `SendInput`；不使用剪贴板；
- secondary action 只允许固定集合；drag 和坐标 click 默认关闭。

驱动返回 `accepted` 只表示动作调用已发出，绝不表示任务完成。

## Agent loop

`DesktopAgentLoopController` 对每条任务执行：

```text
NativeSkillRouter
  hit -> 本地 prepare/safety/execute/verify -> terminal result
  miss
    -> driver.list_apps
    -> planner.decide
       observe -> driver.observe -> local surface inspection -> loop
       action  -> fresh before -> local safety
               -> expectation 必须为 false
               -> driver.execute
               -> driver.observe(fresh generation)
               -> verifier.verify_action + 同一 expectation 必须为 true -> loop
       done    -> verifier.verify_completion -> terminal result
       fail    -> terminal failure
```

循环有 `max_steps` 和总 timeout。任一步失败都会停止这条任务，不会静默切换到 `legacy_codex_cli`，也不会让模型通过另一工具绕过阻断。

上述 false-before/true-after 对每个通用动作都强制执行；如果后置条件在动作前已经成立，系统不会把无变化操作误记为成功，也不会继续盲点一次。

## LocalVerifier

`handsfree_pc/desktop/verifier.py` 不读取 planner/driver 的完成 prose 作为证据。动作级检查至少要求：

- receipt 对应同一动作和 before generation；
- after observation 属于同一应用且 generation 严格增加；
- after 不早于 before，Unicode 没有 replacement character；
- before/after fingerprint 不同；
- `type_text`/`set_value` 的**精确文本**出现在新 UIA 状态中。

动作还必须携带任务相关 expectation。agent loop 在执行前确认它为 false，执行和 fresh observe 后确认它为 true；动作级 receipt/fingerprint 变化与任务后置条件两项缺一不可。

任务级 `done` 只能使用有限 expectation：应用当前可观察、文本存在/不存在、焦点元素包含文本，或上一个动作已由同一 generation 的本地 verifier 验收。只有这一步通过才产生 `LOCAL_VERIFIED_COMPLETION`。

这是比 0.2 自报状态更强的证据，但仍不是形式化证明：UIA 树可能缺失业务状态，应用可能在验收后立即变化，外部网络副作用也可能延迟。高价值任务仍需人工监督。

## 本地安全策略与 typed confirmation

在把 task-authorized 子集发送给云 planner **之前**，本地策略把三类判断分开：planner 数据最小化、当前敏感 surface 分类、具体动作风险。聊天正文等 `CONTENT` plane 即使讨论 password、terminal、payment 或示例 token，也不会把整个窗口判成敏感 surface，并且不会进入 planner。私钥头、已知服务 key、结构有效 JWT、明确 Bearer 等高置信度凭据会被删除/脱敏；普通 40--512 字符不透明标识只记为低置信度并从 planner view 排除，绝不单独阻断整窗。真正的密码属性、当前聚焦的 secret/API-key 输入框、认证/UAC/Windows Security/付款顶层界面仍 fail closed。每个 planner action 在执行前都会对目标元素和 fresh snapshot 重新分类。

本地有限语法还要求 planner 动作与用户下一步逐字段一致：动作类型、完整目标边界、完整口述输入 payload、按键、单次左键、secondary `invoke`、滚动方向和显式页数都不能由 outcome 文本、后续文本动词的 payload、口述文本子串或较长标签的短前缀借出授权。`type/input/输入/键入` 只匹配 `type_text`，`fill/write/填写/写入` 只匹配 `set_value`；文本动作后若还有 payload 外的独立非空肯定 clause，payload-presence 特例整体关闭，必须验证真正的用户结果；明确否定的“不发送”等 side clause 不会被误当成结果。尚未实现本地条件求值的 `if/when/unless` 等条件命令整体 fail closed；不支持的 drag/hover/move/resize/double-click/right-click/download/copy/rename 等尾随动作仍计入用户步骤，不能被 planner 提前 `DONE` 隐去。同目标 `ELEMENT_SELECTED` 只可证明用户明确说出的 select/choose/switch；open/click/send/delete/close 等需要用户原句中独立、可观察的结果条件。

公开默认 `strict` 对通用 `type_text`/`set_value` 要求确认。只在本机忽略提交的配置中显式启用的 `personal_trusted`，可以免确认执行安全导航，以及把本句完整口述草稿写入唯一、聚焦、非密码输入框；它不会自动发送。点击/按键上下文命中本地已知的发送、提交、删除、安装、上传、共享、关闭等词形时，两种 profile 都要求确认。该词表不是完整语义证明，未知语言/同义词、自绘控件或伪装文案可能漏分，重要副作用必须人工监督。需要确认的动作会产生由动作类型、应用、参数、包含本地 HWND identity 的 observation fingerprint 和 expectation 绑定的 ID。generation 在确认前 fresh observe 后重新绑定，不直接进入 digest。runtime 另外生成随机四位挑战码并提示“确认执行 4 8 2 7”这类一次性口令；只说静态“确认执行”永远不授权，也不会重新让模型解释确认意图。

通用 UI confirmation 摘要若原文显示 UI 标签，只使用从用户原句验证出的 exact target label。未授权 sibling/window label 的原文和语义仍保留在本地完整快照中做风险分类，不进入摘要；摘要中的短 digest 只是不可逆绑定元数据。输入 payload 则只来自用户亲口给出的 exact span。

确认执行前还会：

1. 重新 observe；
2. 要求界面 fingerprint 与确认时一致；
3. 把同一动作 rebind 到新 generation；
4. 重新运行本地安全分类；
5. 只执行原动作一次，再进入 fresh observe/LocalVerifier。

ID/四位码不匹配、重复使用、超时、界面变化或风险分类变化全部拒绝。同一 `VoiceRuntime` 进程运行期内，已签发四位码在成功、取消或超时后都不回收；有界重抽耗尽时 fail closed。该集合不持久化，重启后不保证绝对不复用，因此随机码不是持久化防重放凭证或说话人认证，也无法阻止旁人、扬声器或实时转述/重放在本轮有效期内代说口令。

## 实验 Open Computer Use driver

`PersistentOpenComputerUseDriver` 是对 [Qwen open-computer-use](https://github.com/QwenLM/open-computer-use) **0.2.3** MCP server 的可选持久 stdio adapter：初始化一次、校验 9 个所需工具、串行复用同一子进程，并在每个动作后强制 fresh observe。

它不是默认依赖，只有 `driver: open_computer_use` 与 `allow_experimental_driver: true` 同时设置才可加载。0.2.3 在中文 Windows 有未解决的 PowerShell/UTF-8 边界问题：[Issue #5](https://github.com/QwenLM/open-computer-use/issues/5)、[PR #6](https://github.com/QwenLM/open-computer-use/pull/6)。因此它不作为中文默认驱动，也不自动安装。详见 [OPEN_COMPUTER_USE.md](OPEN_COMPUTER_USE.md)。

当前 0.2.3 adapter 从 `get_app_state` 得不到可安全绑定的结构化元素列表；原始 accessibility text 和截图只可能由本地 MCP/driver 接触，safety 重建的云 planner view 会移除它们及真实截图可用性。通用 planner 因此得不到可用 element index：基于元素的点击/导航受限，`type_text`/`set_value` 也会因没有可验证的焦点元素而失败关闭。它不等价于默认 `windows_uia`。

## 旧兼容 controller

`handsfree_pc/computer_control.py` 中基于 `codex exec/resume` 和 Computer Use plugin 的 controller 仍可通过 `backend: legacy_codex_cli` 显式选择，以免破坏已有私有配置。

该路径由同一个 agent 执行动作并输出 `VERIFIED_COMPLETION`/`NEEDS_CONFIRMATION`/`FAILURE`，没有 0.3 的项目自有 Driver 与 LocalVerifier。除 `backend: legacy_codex_cli` 外还必须同时设置 `allow_codex_cli_host_read: true` 和 `allow_legacy_codex_computer_use: true`。它的状态行只是协议，不是可信屏幕证据。factory 不会自动选择或回退到它；0.3 新安装应使用 `local_agent`。

## 静态 doctor 与 live doctor

普通 `doctor` 检查依赖、模型、音频设备、配置和 CLI 线索，只设置 `static_control_preflight_passed`；它总把 `live_control_verified` 与 `ready_for_live_control` 保持为 `false`。

`computer-doctor --live` 仅支持 `local_agent/windows_uia`。它打开本项目自有 fixture，对唯一文本字段执行含中文随机 token 的 `set_value`，重新 observe 并调用同一 `DesktopVerifier`。这证明一次 UIA + Unicode + fresh-observation round-trip，不覆盖 planner、声音、第三方应用或业务后置条件。

## 配置与模块边界

默认公开配置：

```yaml
computer_control:
  enabled: false
  backend: local_agent
  driver: windows_uia
  planner_backend: claude
  allow_codex_cli_host_read: false
  allow_legacy_codex_computer_use: false
  allow_experimental_driver: false
  allow_coordinate_actions: false

execution:
  dry_run: true
```

主要模块：

- `session.py`：delimiter、队列和 worker；
- `runtime.py`：语音状态机、反馈、队列与 controller 集成；
- `desktop/native_skills.py`：确定性本地路由；
- `desktop/step_planner.py`：Codex/Claude 单步规划；
- `desktop/protocol.py`：严格 observation/action/decision 类型；
- `desktop/windows_uia.py`：默认自有驱动；
- `desktop/safety.py`：界面/动作风险策略与 typed confirmation；
- `desktop/verifier.py`：动作和完成条件本地验收；
- `desktop/agent_loop.py`：持久 controller；
- `desktop/open_computer_use.py`、`desktop/mcp_client.py`：实验 MCP 驱动；
- `live_fixture.py`：自有 live doctor fixture。

任何新的 driver 都必须保持同样的 observation generation、单动作、fresh observe、local verification 和 typed confirmation 契约，不能通过“模型说成功”降低验收强度。
