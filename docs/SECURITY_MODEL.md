# HandsFreePC 安全模型

## 安全目标

HandsFreePC 会持续获得麦克风输入，并在用户会话中打开文件、激活窗口和输入文字，因此应按“本机辅助技术执行器”而不是普通语音记事工具来审视。

首版安全目标是：

- 电视、旁人、孩子或识别错误不能轻易触发桌面动作；
- 一个被误解的命令最多落入窄动作白名单，不能演化为任意代码执行；
- 任何 planner 输出都不被直接信任；
- 文字只会输入经过二次核验的目标窗口和控件；
- 高风险、歧义和系统安全边界一律失败关闭；
- 原始音频、完整转写、秘密和私密路径默认不持久化；
- 云规划只有显式开启，且用户清楚文本会离开机器。

公开模板还以 `execution.dry_run: true` 启动，只禁止真实 Windows 桌面动作。它不是总的隐私/副作用沙箱：直接 `run` 仍打开麦克风并产生反馈，显式双重开启的 planner 仍可能联网。

本模型不承诺防御已经控制当前 Windows 用户账户的攻击者，也不承诺跨越 Windows 本身的权限边界。

## 保护资产

- 麦克风中用户和旁人的谈话；
- 转写文本、项目/对话名称和本机路径；
- 当前窗口、剪贴板、输入框内容与屏幕可见信息；
- Codex/Claude 登录态及其本地会话；
- 用户文件、应用状态和外部消息/Prompt；
- 配置文件、日志以及 planner 子进程环境；
- 用户对“系统当前是否在听、准备做什么、是否已完成”的正确认知。

## 信任边界

```mermaid
flowchart TB
    World[房间声音：不可信] --> LocalASR[本地唤醒与 ASR]
    LocalASR --> Text[转写文本：仍不可信]
    Text --> Deterministic[确定性解析]
    Text -. 显式开启 .-> Cloud[Codex/Claude：外部处理方]
    Cloud --> Plan[JSON 计划：不可信]
    Deterministic --> Gate[本地 Schema + 风险策略]
    Plan --> Gate
    Gate --> Executor[白名单 Windows 执行器]
    Executor --> UI[目标应用/UIA：可能不完整或被伪装]
    UI --> Verify[独立后置条件核验]
```

特别注意：

- **语音转写不是授权证明。** 扬声器播放出来的录音也可能被识别；SAPI 播放期间 PortAudio callback 仍写入有界内存缓冲，但运行时暂停识别/命令处理，整个队列结束后丢弃输入队列和预卷。全局停止短语采用高优先级子串匹配，因此听写中说出完整停止短语也可能被当作控制。
- **Schema 合法不等于语义安全。** `type_text` 的文本仍可能敏感，`open_path` 仍可能指向可执行文件。
- **UIA 名称不是身份。** 恶意或错误窗口可以显示相同标题/按钮名称；首版结合允许的进程名、标题、窗口句柄、控件类型和唯一性，但尚未校验运行中进程签名/完整镜像路径。
- **planner 是网络边界。** 开启后，命令文本和上下文受对应提供商的传输、保留和账户策略约束。

## 威胁与控制

| 威胁 | 典型场景 | 主要控制 | 仍需接受的残余风险 |
|---|---|---|---|
| 环境误唤醒 | 电视说出相似短语、孩子模仿 | 有限 grammar、完整唤醒句、唤醒超时、控制前缀、状态遮罩、停止短语 | 任何仅靠声学短语的系统都可能被重放；高风险动作不能只靠一次唤醒 |
| ASR 误识别 | 盘符、文件名、项目名被听错 | 路径逐层解析、相似度阈值、同分候选消歧、动作摘要 | 唯一但错误的近似候选仍可能存在；关键路径应配置显式别名 |
| 语音提示注入 | 音频说“忽略规则并执行 PowerShell” | 确定性 parser 优先；计划 Schema 无 shell；未知字段拒绝；本地策略重判 | 合法动作组合仍可能不符合用户真实意图，需依靠摘要、确认和后置条件 |
| planner 越权/幻觉 | 模型编造路径、项目或动作 | 空临时目录、Schema、最多 8 步、路径解析、只允许已配置 app、风险重判；云来源 `TYPE_TEXT` / `SEND_PROMPT` 直接阻断；Claude 使用空工具集；Codex 使用 read-only sandbox | “不要发明 UI 名称”只是 prompt 指令，不是 provenance 强制校验；模型编出的名称若恰好唯一存在，仍可能被点击；Codex 还可能读取主机可见文件 |
| 环境秘密泄漏 | planner 子进程继承 API key 或读取本机文件 | 删除名称包含常见秘密标记的环境变量；HandsFreePC prompt 只构造最小 context；Claude 工具集为空 | 变量名过滤不是秘密检测；Codex 临时 cwd 不能阻止只读 shell 访问其他可见位置；命令文本本身也可能含秘密 |
| 错窗输入 | 焦点被通知、弹窗或用户移动 | 输入/提交前检查前台 HWND，并复核同一个已固定的非密码 Edit/Document；发送后再次检查前台 | `TYPE_TEXT` 只证明 SendInput 接受 UTF-16 单元，不证明控件值改变；提交也不证明消息出现或服务端接受；极短竞态仍存在 |
| UI 欺骗 | 假窗口复用“Claude”标题 | 当前按允许的进程名、标题、窗口句柄和 UIA 层级联合验证；多个匹配不猜 | 运行中进程的完整镜像路径与代码签名尚未核验；被当前用户运行的恶意进程仍可能伪装 |
| UIA 树漂移 | 应用升级、语言切换、Electron 自绘控件 | 可见/启用后代 + 控件类型 + 可访问名称唯一匹配、歧义即停、本机外部 UIA inspector + smoke test；公开 Codex/Claude 搜索/语音热键为 `null`、语音按钮名为空 | 0.1.0 没有内置 inspect 或版本化 selector profile；某些控件根本不暴露 UIA，未校准时相关动作应失败 |
| 路径/文件执行 | “打开安装程序”实际模糊命中主动类型，或未知文件被危险关联 | 确认前解析最终路径并重新判级；只有目录和窄安全后缀直接打开，其他未知/主动类型全部确认；不经 shell 拼接 | 文件关联本身可能被篡改；0.1.0 只证明系统接受打开调度，不证明最终应用状态 |
| 自动外发 | 听写内容因换行/快捷键意外发送 | 听写与提交分离；所有 action 字符串字段/plan summary 拒绝 Unicode C 类控制字符，`TYPE_TEXT` 因而不能带回车/换行；只有带控制前缀的完整提交命令可发送，否定句不提交；云 planner 的发送/输入动作直接阻断 | 第三方应用的快捷键/自动提交行为可能变化；用户提交后的下游 agent 权限另受其自身配置控制 |
| 误导/敏感反馈 | planner 生成诱导性摘要，或识别反馈显示/朗读私有名称 | 确认文案完全从已校验动作本地派生；路径执行/失败用通用摘要；planner summary 最长 200 字 | 非确认执行和部分 blocked 流程仍可能显示/朗读未受信任的 `plan.summary`；完整“识别：{转写}”也会在 overlay/both 显示并在 voice/both 朗读，仍有社会工程与旁观/旁听风险 |
| TTS 自触发或静默失败 | 系统朗读控制词被麦克风听到，或 SAPI 初始化失败 | 整个 TTS 队列期间暂停识别/命令处理，队列结束后丢弃 callback 填入的输入队列和预卷；默认 overlay；反馈保持短小 | 队列不能被停止词打断；SAPI worker/COM 错误当前不向 UI/退出码传播，纯 `voice` 模式可能静默，必须逐机感知测试，`both` 至少保留遮罩 |
| 同步动作不可语音中断 | UIA 等待或窗口激活期间用户说“停止” | 动作集合最多 8 步；已实现的激活/UIA 等待有边界；失败即停 | 并非所有同步 OS/UI 调用都有统一超时或可取消能力；停止词不能抢占已经开始的同步调用 |
| 双语音链竞争 | 明确开启 Codex/Claude 应用内语音后，两套 ASR 同时处理整句命令 | 应用内语音需确认；先等此前 TTS 队列清空；一旦进入执行尝试，中/成功/失败反馈 overlay-only，成功或失败均保守进入 `PAUSED` | 热键/按钮证据不证明第三方麦克风真正 active 或何时结束；过早重新唤醒仍会竞争 |
| 日志/诊断泄漏 | stdout、诊断 JSON 或崩溃信息含路径 | 0.1.0 不持久化内容日志、音频或转写；路径动作的普通遮罩使用通用提示；真实音频诊断必须显式 opt-in | `doctor`、其他 CLI JSON、Python 或上游 CLI 的输出仍可能含本机路径；分享前必须人工脱敏 |
| 锁屏/UAC/高权限窗口 | 无人可见时继续输入，或尝试控制管理员应用 | 每个真实动作先以 `OpenInputDesktop`/`UOI_NAME` 要求 `Default`；普通权限、不用 UIAccess、不自动同意 UAC；输入前再核验目标/前台 | 0.1.0 没有会话事件监听器，麦克风不会因锁屏自动暂停；输入桌面在检查后变化仍存在极短竞态 |
| 依赖/模型供应链 | 下载的 wheel 或权重被替换 | 有界依赖版本、固定 Vosk/sherpa 版本、官方模型入口；三个默认模型先在 staging 完成固定 SHA-256、预期权重、许可和来源说明，再替换目标；skip 也要求完整元数据 | 当前 alpha 没有完整锁文件、签名更新或可复现构建链；模型首次下载仍是网络事件 |

## 动作与风险分级

首版采用三个执行结果：

- `safe`：本地立即执行并收集该动作实际支持的证据，例如切换反馈、暂停、打开已存在目录或窄安全后缀文件、激活唯一目标窗口。
- `confirm`：先展示/朗读本地派生摘要并等待确认，例如打开任何未知/主动/间接执行类型文件、开启第三方应用的原生语音、非显式的发送动作。原生语音还必须是唯一一次且位于计划最后一步，不能与反馈模式切换组合；非法组合在执行前阻断并回 `ARMED`。只有已开始、可能触发第三方麦克风的执行尝试才在成功或失败后保持 `PAUSED` 并 overlay-only。
- `blocked`：首版完全不执行，例如删除、格式化、付款、转账、输入密码、任意 shell 或 Schema 外动作。

风险判级以本地策略为准，从 planner 返回的 `risk` 起步并且只能保持或升高，不能降低。来源为 Codex/Claude/LLM 的 `TYPE_TEXT` 或 `SEND_PROMPT` 直接 `blocked`；模型只能规划 `enter_dictation` 等聚焦前置动作，不能生成或提交文本。计划超过 8 步、字段未知、必填参数缺失、等待超过 10 秒、`text` 超过 2000 字或字段类型错误均拒绝；所有 action 字符串字段和 plan `summary` 都拒绝 Unicode C 类控制字符，包括 NUL、回车/换行。

### 确认不是万能开关

确认只能把策略明确标为 `confirm` 的动作推进一次，不能：

- 解锁 `blocked` 动作；
- 修改后续动作内容；
- 复用到下一条命令；
- 在超时后继续有效；
- 授予管理员权限或绕过 UAC；
- 让 planner 自己声称“用户已确认”。

确认短语必须是完整的标准化整句；包含该短语的否定句或长句不会授权。确认文案不信任 planner 提供的 `summary`，而是从已校验动作本地派生，明确提示第三方麦克风、提交提示或最终文件 basename。确认状态录音期间会逐 block 运行本地 Vosk，配置的停止短语可在 SenseVoice 前中断并进入 `PAUSED`；“取消操作”等取消短语仍需完整话语经 SenseVoice 返回，之后 `handle_text` 会在确认短语之前处理取消。两者都不能抢占已经开始的同步执行或 SAPI。语音重放无法被普通麦克风可靠区分，因此未来涉及外部写入时，建议升级为遮罩显示随机短码、用户复述短码的单次确认；首版的固定确认短语只适用于有限 MVP 风险面。

## planner 隔离

### Codex adapter

调用 `codex exec` 时采用临时目录、`--ephemeral`、`--ignore-user-config`、`--ignore-rules`、`--sandbox read-only` 和 JSON Schema 输出。目的在于避免加载用户配置/规则，减少本地会话持久化，并让最终 JSON 无法通过 HandsFreePC 动作 Schema 获得任意执行能力。

`read-only` 明确不是“无 shell”：Codex 仍可运行受沙箱约束的模型生成命令，并可能读取当前用户可见的本机文件。`-C` 指向空临时目录、忽略配置以及 prompt 中禁止工具，都不是主机文件保密边界；`--ephemeral` 也不等于提供商端零保留。0.1.0 把这一点列为残余风险，所以 planner 默认关闭，敏感主机不应开启 Codex planner。

### Claude adapter

调用 `claude -p` 时使用 `--safe-mode`、空工具集、`dontAsk`、JSON Schema 和 `--no-session-persistence`。空工具集提供比 Codex read-only shell 更窄的本机工具面，但需要有效的 Claude OAuth/登录；这也不改变“请求文本会通过网络进入 Anthropic 服务”的事实。

Claude 官方说明本地 Claude Code 与模型交互时会发送用户 prompt 和模型输出，并按账户类型/设置采用不同保留和训练策略。[Claude Code data usage](https://code.claude.com/docs/en/data-usage)

### 共同约束

- HandsFreePC 构造的 planner prompt 不主动包含截图、原始音频、剪贴板、全量目录清单或环境变量；
- prompt 上下文只提供完成当前计划所需的非敏感候选信息；但 Codex 自身的只读工具可见性是上述保证之外的独立风险；
- 两个 adapter 都把 cwd 设为空临时目录，避免自动带入当前项目上下文；启动/超时错误返回泛化信息，不回显原始 prompt 或 provider stderr；
- Schema/策略不强制验证 planner 给出的项目、对话、tab、mode 名称确实来自原始话语；“不要发明名称”是 prompt 指令。一个幻觉名称若恰好唯一匹配现有 UIA 项，仍可能被执行，这是残余风险；
- planner 超时、输出无效、可执行文件不存在或网络失败时直接失败，不回退到任意执行；
- 不允许 planner 自主重试外部发送、覆盖或其他有副作用动作；
- CLI/模型升级必须跑恶意 prompt、Schema 逃逸、超长输出和超时测试。

## Windows 权限边界

受支持的部署方式是在当前用户会话中以普通权限运行；自启动脚本不请求提升，但 0.1.0 尚未主动拒绝用户手工“以管理员身份运行”。Windows 的 `SendInput` 受 UIPI 限制，普通权限进程不能可靠向更高完整性级别注入输入。[Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

项目不启用 `UIAccess`。微软要求 UIAccess 应用经过 Authenticode 签名并安装到安全目录，而且它仍不等于控制 SYSTEM 安全桌面。[Microsoft UI Automation security overview](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview)

以下状态按设计不可操作：

- Windows 锁屏和登录界面；
- UAC 安全桌面；
- 其他用户会话；
- SYSTEM 完整性级别界面；
- UIA 标记为密码的字段；
- 无法唯一验证身份的窗口或控件。

0.1.0 在所有真实 OS/UI 动作入口调用 [`OpenInputDesktop`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop) 并读取 `UOI_NAME`，只有名称严格为 `Default` 才继续；`open_path` 也经过同一门禁。它仍不订阅锁屏/切换用户事件，所以麦克风不会自动暂停，且检查与动作之间存在极短竞态；用户在锁屏前主动暂停或退出仍是推荐做法。

## 文件与路径边界

- 不把语音文本拼入 `cmd.exe`、PowerShell 或 `shell=True`；
- UNC / `//server`、任意 URI scheme 和 Win32 device namespace 在任何文件系统访问前阻断，展开后的文本再检查一次；
- 只在显式路径、配置别名和 `search_roots` 中解析；
- 搜索有最大深度和最大条目数，避免遍历整个磁盘；
- 路径存在且候选唯一后才打开；
- 已存在目录与窄安全文件后缀白名单可以直接打开。当前白名单是 `.bmp`、`.csv`、`.gif`、`.jpeg`、`.jpg`、`.json`、`.m4a`、`.md`、`.mkv`、`.mov`、`.mp3`、`.mp4`、`.pdf`、`.png`、`.svg`、`.tsv`、`.txt`、`.wav`、`.webp`、`.xlsx`、`.yaml`、`.yml`；未知后缀、无后缀普通文件和所有主动/间接执行类型进入确认；
- Windows 文件关联仍可能把安全后缀交给错误或被篡改的 handler，当前 Shell dispatch 证据不证明最终内容正确；
- 未来加入写入/移动能力前，必须额外防御 NTFS junction、符号链接、重解析点、TOCTOU 和网络共享身份变化。当前“只打开”能力不能被当成可安全复用的写入校验。

## 失败和恢复

- 任何执行异常都停止当前计划，不继续剩余动作；
- 失败反馈不包含秘密或完整私密路径；
- 失败后不自动重试当前计划；外部提交或可执行文件尤其不会自动重试，用户须重新发出明确命令；
- planner/UI 动作失败会给出可见错误并回到安全状态；原生语音计划若在策略阶段被阻断，尚未开麦并回 `ARMED`。合法计划一旦进入执行尝试，其失败例外地保持 `PAUSED` 且强制 overlay-only，避免在不确定的第三方麦克风状态下继续或播报；
- 运行循环内的 `AudioError` 会发出错误反馈，但除非同时触发状态超时，通常保留当时的 `AWAKE` / `DICTATION` / `CONFIRMING` 状态；模型/音频 session 在启动阶段构造失败会逃逸到 CLI stderr，Startup 的 `pythonw` 路径可能因此静默退出；
- 崩溃恢复不重放尚未完成的计划。

## 安全测试最低集合

每次发布至少覆盖：

1. 唤醒/停止词的噪声、重放和 TTS 回声样本；
2. Schema 未知字段、额外动作、超长文本、NUL、超时和 planner 注入；
3. 路径同名、危险扩展名、junction/符号链接和文件在核验后被替换；
4. 前台窗口在“定位后、输入前”被切换；
5. 同标题伪窗口、重复 UIA 名称、控件消失/移动；
6. 密码框、管理员 Notepad、UAC、锁屏和切换用户；
7. 提交控制必须是带控制前缀的完整整句，否定句不提交；另行验证全局停止短语按设计可从听写中高优先级截断并进入 `PAUSED`；
8. planner 开关关闭时无网络规划调用，开启时不发送原始音频；
9. 错误日志和测试产物中不存在音频、完整转写、令牌或本机绝对路径；
10. 目标 Codex/Claude 版本的 opt-in live smoke test。

发布机的完整自动化收集项已通过，但有一项需要 Windows 创建符号链接权限的真实 symlink 测试因主机缺少该权限而跳过；`_resolve_within` 和 fake reparse 属性仍由单元测试覆盖。这不能替代有相应权限主机上的 live reparse-point 验收。

## 已知残余风险

- 没有声纹或物理按键的纯语音唤醒无法彻底抵御录音重放；
- Electron 应用的可访问性树可能随版本变化；
- Windows 前台焦点存在不可完全消除的竞态；
- 本地 ASR 可能把唯一目标识别错，用户应为常用路径配置明确别名；
- Codex/Claude CLI 和提供商策略会变化，使用订阅不等于零数据保留；
- HandsFreePC 的禁词只约束本地动作计划；用户明确提交后的下游 Codex/Claude agent 由其自身 sandbox、approval、permissions 控制，必须另行最小化；
- alpha 版依赖 PyPI/GitHub/模型站点下载，尚未实现端到端签名更新和完整 SBOM；
- 若当前用户账户已经被恶意软件控制，普通用户级 UIA/输入防护不能建立新的安全边界。

发现漏洞请按根目录 `SECURITY.md` 私下报告，不要在公开 Issue 中粘贴音频、转写、路径、令牌或可直接利用的细节。
