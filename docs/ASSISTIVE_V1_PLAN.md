# assistive_v1 改造计划（2026-09-02 定稿）

依据：2026-09-01 实测日志（13 条指令 2 成功、复合任务 0 成功）的六根因分析 + GPT-Pro 复核意见 + 用户决策"安全向执行优先靠拢"。

核心原则：**危险动作守住；普通动作尽快执行；单步失败允许等待、重试和换路；最终目标达到才算成功。**

三个 PR 串行推进：PR1（assistive 骨架 + P0）→ PR2（ASR 卫生与交互）→ PR3（高频技能与速度）。前一个 PR 的 live 场景验收不达标，不开始下一个。

## 安全决策表（assistive profile）

| 自动执行 | 口头确认（动作短语，15 秒） | 硬阻断 |
|---|---|---|
| 切换/激活应用；打开文件夹、普通文档 | 发给人类的消息（微信/邮件；per-app 可配） | 密码框、登录/认证 |
| 导航点击、标签页、滚动、搜索、打开网页 | 删除/覆盖文件 | 支付、转账 |
| 键入用户口述草稿 | 安装/卸载、上传/分享 | UAC、Windows Security |
| 发送到 AI 助手应用（send_policy: auto） | 关闭含未保存内容的窗口 | 系统安全/隐私设置 |
| 关闭无脏状态窗口/标签 | 首次执行未知程序 | 终端/Shell（v1） |

铁律：①`forbid_submit=True` 时本任务一切提交动作拒绝；②确认短语按动作定制（确认发送/确认删除），绑定确切动作+窗口+15 秒，assistive 不用随机口令；③动作前精确绑定（HWND、前台、密码框、目标控件复核）保留。

---

## PR1 · Codex 交付指令（可直接粘贴）

```text
请基于 chenqin3/HandsFreePC 当前 main：
acc1465b1feefe85967fe068dcc37fcc6ba38576
创建分支 refactor/assistive-v1。

产品目标只有三个：快、容错、能完成。
本轮仅实现 P0。不实现 ASR 质量门、持久 planner、LLM-NLU 或新的视觉策略。

硬性禁令：
- 不得向 handsfree_pc/desktop/safety.py 和 agent_loop.py 增加任何
  新规则、新正则、新例外；这两个文件只允许为引擎分流增加最小接缝。
- 不得新增证明型状态机、用户动词计数器或 VisualViewport 完成例外。
- 现有 proof 引擎及其测试必须原样保留并继续通过。

一、引擎分流
1. 配置增加 computer_control.engine: assistive_v1 | proof_v1（默认 proof_v1，
   config.example.yaml 保持 proof_v1；本机 config.local.yaml 用 assistive_v1）。
2. proof_v1 走现有 DesktopAgentLoopController；assistive_v1 走新建的
   handsfree_pc/desktop/assistive/controller.py::AssistiveController。
3. 新目录结构：
   handsfree_pc/desktop/assistive/
       models.py task_parser.py controller.py policy.py
       verifier.py retry.py skills/（explorer.py 等）

二、数据模型（models.py）
GoalKind: app_foreground / path_open / url_loaded / conversation_selected /
          input_contains / text_visible / element_state / message_sent / free_form
TaskSpec: goals 元组 + forbid_submit + side_effect + raw_text
SkillStatus: completed / progress / retryable_failure / miss /
             needs_confirmation / blocked
ActionOutcome: completed / progress / no_effect / unobservable

三、确定性 task_parser（task_parser.py）
整条指令只解析一次。必须覆盖并有单测：
1. 切换/激活应用（切换到X、打开X、激活X、切到X）；
2. 打开资源管理器；
3. 打开X盘 → path_open("X:\\")，X∈A-Z 中文语境（"D盘""d 盘"）；
4. 打开路径/已配置别名（桌面/文档/下载）；
5. 无标点复合句"切换到X 打开Y"必须拆成两个 Goal（不得依赖逗号）；
6. "在X里找到/打开会话Y" → app_foreground + conversation_selected；
7. "输入……"含"不要发送/别发送/先不发" → input_contains + forbid_submit=True。
解析不了的整句 → 单 Goal free_form(raw_text)，交通用 assistive agent。

四、任务级完成（verifier.py::GoalVerifier）
1. assistive 路径不得调用 user_action_step_count、
   window_activation_matches_next_user_step 等步骤台账函数，
   不存在 ACTION_AFTER_USER_STEPS_COMPLETE / OBSERVE_AFTER_USER_STEPS_COMPLETE /
   NO_VERIFIED_ACTIONS 这些终止条件。
2. 任务开始前先跑 GoalVerifier，全部 Goal 已成立 → 直接成功（零动作合法）。
3. 每个动作后同样先验 Goal。
4. planner 的 done 只是建议，最终由 GoalVerifier 判定。
5. assistive planner 的输出不含 expectation 字段；解析器不得改写模型的
   done 语义（不做 DONE→VISUAL_STATE_VERIFIED 一类的全局改写）。

五、纯观察
1. assistive 引擎构造 driver 时 activate_on_observe=False、
   capture_screenshots=False。
2. observe 不得激活窗口或以任何方式改变桌面状态；激活是显式技能/动作。
3. 截图仅按需：UIA 无可用控件 / 动作后无法判断进展 / planner 明确要求 /
   应用 profile 标记 visual_required。
4. WindowsUiaDriver.observe 在 assistive 模式下不得因"截图暂未变化"抛异常；
   无变化正常返回 observation，交 ProgressDetector 判断。

六、宽容验收（retry.py::wait_for_outcome）
动作后最多轮询 3 秒（0/0.1/0.25/0.5/1.0/1.75/3.0；技能可覆盖至 5 秒），每轮依次：
最终 Goal 成立 → completed；任务相关状态成立 → completed/progress；
UIA/窗口/焦点/标题/截图有意义变化 → progress；否则继续等待。
超时后：普通点击同目标重试一次 → 无效则换 Invoke/Select/视觉方式 →
仍无效重新规划一次 → 连续两轮零进展才失败。
文本输入仅当焦点仍绑定且文本确实不存在时才重试。

七、Native fallback
NativeRouteStatus 增加 RETRYABLE_FAILURE。
WindowNotFound / AmbiguousWindow / UIA 暂时不可见 / selector 失效 /
后置证据不足 / bare ACTIVATE_APP、OPEN_MODE、OPEN_CONVERSATION、
ENTER_DICTATION 失败 → retryable_failure → 自动回落通用 assistive agent。
BLOCKED、密码、认证、支付、UAC、安全桌面、路径身份危险变化 → 终止，不回落。

八、Explorer 技能（skills/explorer.py）
1. WindowInfo 增加 class_name（GetClassNameW）。
2. Explorer 识别：process_name==explorer.exe 且
   class_name∈{CabinetWClass, ExploreWClass}；不依赖窗口标题。
3. "打开资源管理器"：有窗口 → 前台优先，其次 Z-order 最前；
   无窗口 → 启动 %WINDIR%\explorer.exe 并等待窗口出现。
4. "打开D盘" → 直接 ShellExecuteW("D:\\")，等待 Explorer 显示该路径。
5. 多个 Explorer 窗口不得让纯激活任务失败；仅"打开特定同名文件夹"才消歧。
6. 新增共享 sanitize_windows_ui_text()，UIA 元素文本和 Win32 窗口标题
  （windows/native.py 的 GetWindowTextW 结果）都必须经过它，
   至少清除 \x00、U+200B、U+200E、U+200F。

九、安全策略（policy.py，目标 300–500 行）
按本文件顶部的三列决策表实现，另加：
1. computer_control.send_policy: {app: auto|confirm} 映射；
   claude/codex 默认 auto，wechat 默认 confirm，未列出的应用默认 confirm。
2. forbid_submit=True 时拒绝一切发送/提交动作，优先级高于 send_policy。
3. 确认为动作短语模式（确认发送/确认删除/确认安装/确认上传/确认关闭），
   绑定当前确切动作+窗口，15 秒过期，完整短语匹配；不用随机数字口令。
4. 动作前绑定保留：精确 HWND、前台校验、密码框检测、执行前目标控件复核。

十、队列与日志
1. assistive 的 failure_policy 默认 continue；普通失败不暂停队列；
   仅等待确认和硬阻断可暂停。
2. 保留现有脱敏 diagnostics；新增默认关闭、仅本机的 debug.log：
   exception type、str(exc)、traceback、stage、代码位置；
   不记录音频、截图、完整 UIA 树、完整用户口述。
   配置 diagnostics.debug_log_enabled / debug_log_local_only。

十一、规划器接缝（本轮仍用冷启动 CLI，只做四件事）
1. assistive 专用瘦政策（~1K 字符）：任务目标、当前观察、允许的动作清单、
   每次只选一个动作、目标已成立就 done、不碰标记 blocked 的元素。
   安全由本地 policy.py 执行，不写进提示词。
2. assistive planner 输出 schema 去掉 expectation。
3. 新配置键 computer_control.planner_step_timeout_seconds（本轮设 60），
   factory 不得再与 legacy planner.timeout_seconds 取 min。
4. computer_control.model 显式设置一个快模型并实际传给 CLI。

十二、AGENTS.md
替换"每个动作必须前验后验"条目为：
- 键鼠输入前，绑定到确切可见的非敏感窗口与目标；
- 动作后轮询任务进展或最终目标；
- 微步骤无法证明时允许有界重试、换执行方式或重新规划，
  不得自动判整条任务失败；
- 最终成功由任务级 GoalVerifier 决定；
- 导航与未发送草稿输入可自动执行；发送、提交、删除、覆盖、安装、
  上传、分享、丢弃式关闭需动作绑定的口头确认；
- 密码、认证、支付、UAC、安全与隐私界面保持阻断。

十三、scenarios 验收 CLI（必须实现，报告是完成的唯一凭据）
新增 handsfreepc scenarios 子命令：复用 simulate 的文本注入通道，
真实执行并计时，输出 JSON 报告（场景、结果、耗时、重试次数、失败阶段）。
必测场景与门槛：
1. Claude 已在前台，"切换到 Claude" → 零动作成功，<1 秒；
2. 无 Explorer 窗口，"打开资源管理器" → 窗口出现，<3 秒；
3. "打开D盘" → Explorer 显示 D:\，<4 秒；
4. "切换到 Chrome 打开 Google 网页"（无标点）→ 完成，<6 秒，
   全程不得出现 ACTION_AFTER_USER_STEPS_COMPLETE；
5. 微信内点击后 UI 延迟 1.5–3 秒 → 轮询后成功，不得单发 ACTION_NOT_VERIFIED；
6. 人为让一个 native 技能失败 → 通用 assistive agent 接管并完成；
7. 一条普通任务失败后 → 队列中下一条继续执行。
确定性技能各重复 20 次成功率 ≥95%。

完成后报告：修改文件清单；assistive 路径绕开了哪些旧硬门；
scenarios JSON 报告原文；未完成项。
未跑通真实 Windows 场景，不得声称 P0 完成。
```

---

## PR2 · 范围备忘（PR1 验收通过后展开成指令）

1. TranscriptionResult 结构化 + 组合式质量门（no_speech_prob>0.65 且 avg_logprob 低 / avg_logprob 极低 / compression_ratio>2.4）；SenseVoice 走文本卫生。
2. 幻觉黑名单（点赞订阅/字幕志愿者/明镜与点点/谢谢观看族，整段主体匹配才丢）+ 重复 n-gram + 非中英主体拒收 + 短音频长文本拒收 + 连续重复幻觉去重。
3. PromptAssembler 碎片 TTL 45 秒，过期丢弃并播报。
4. 唤醒交叉验证：唤醒命中后用同窗口正文 ASR 转写与唤醒词做模糊相似度复核，不过阈值不开会话；会话闲置 120 秒自动结束（awake_idle_timeout_seconds）。
5. SpokenEntityResolver：精确别名 → 领域混淆表（锯盘/地盘/滴盘/第盘→D盘；克劳德→Claude；文件传输小助手→文件传输助手）→ 拼音距离 → 编辑距离；仅限盘符/应用/项目/会话/文件/联系人；首选分高且明显领先才自动纠正并播报，接近则一句话反问。
6. feedback_mode both；只朗读纠正、失败原因、完成结果、待确认；完成语用任务语言（"已打开 D 盘""文字已输入，没有发送"）。

## PR3 · 范围备忘

1. 应用适配器（Claude / Codex(ChatGPT) / 微信 / Chrome）：composer 与搜索的 UIA AutomationId 档案，快捷键次之（微信 Ctrl+F、Chrome Ctrl+L），视觉兜底。
2. 九个参数化技能：SwitchApp / OpenExplorer / OpenPath / OpenBrowserUrl / BrowserSearch / OpenConversation / FocusComposer / TypeDraft / SendMessage；策略链 = 专用 UIA → 快捷键 → 通用 UIA → 视觉。
3. free_form 整句 → 单次 LLM-NLU 产 TaskSpec。
4. PlannerSession 常驻化：首选长驻 CLI worker（claude -p stream-json 或 codex proto，订阅计费），备选直连 API + prompt caching；planner_step_timeout_seconds 降到 15；政策缩到现在的十分之一。
5. 分级感知 L0（窗口清单）→ L1（任务相关 UIA）→ L2（目标窗口截图）→ L3（OCR/视觉坐标），按需升级。
6. send_policy: auto 在 AI 应用闭环生效（说→切→键入→自动发送）。

## PR1 状态（2026-09-02 真机验证）

`handsfreepc --config config.local.yaml scenarios` 五个确定性场景全部通过（`all_success: true`）：

| 场景 | 结果 | 耗时 |
|---|---|---|
| Claude 已在前台，"切换到 Claude" | 零动作成功 | 0.6 s |
| "打开D盘" | Explorer 显示 D:\ | 1.0 s |
| "切换到 Chrome 打开 <URL>"（无标点） | URL 验收通过，2 个动作 | 5.9 s（阈值 6 s，偏紧） |
| native 注入 retryable_failure 后回落 | 确定性技能接管完成 | 5.0 s |
| 缺失路径任务失败后队列继续 | 第二条正常完成 | 0.1 s |

其他真机验证：`在 Claude 里输入 这是测试 不要发送` 8 s、2 个动作（聚焦 composer + 键入）、零规划器调用、本地验证到输入框含文字且未发送。

PR1 复核时补上的关键修正（gpt-sol 版本之外）：
- 不可本地验证的目标（free_form、无 UIA 语义面的窗口）改为"规划器看过窗口后其 done 生效"，否则微信/Codex 这类窗口的任务必然失败；
- 未知应用名（"切换到周报生成聊天框"）按窗口清单解析，无匹配时整句降级为 free_form 交规划器；规划器 observe 另一窗口时由控制器显式激活；
- composer 草稿技能与"目标带 app 先激活"；
- 浏览器技能改为绑定前台 + 读回地址栏实时值（`read_element_state`），不再依赖驱动的"恰好一个焦点元素"守卫（Google 搜索页上 Ctrl+L 后 Chrome 同时报告页内搜索框和 Omnibox Popup 有焦点）；地址栏回车永远不算"发送"；
- `_is_browser_chrome_descendant` 只对 Edit/ComboBox 计算（原来占 Chrome 观察耗时的一半）；任务目标在别的应用时不再先观察当前前台窗口；
- 规划器：`planner_reasoning_effort: low`（每步约 10–14 s，原来 18–60 s）、截图坐标回映射、决策写入本地 debug.log；
- 不存在的绝对路径直接失败，不进规划器；scenarios 的唯一 URL 改用 Google 搜索页（首页会丢弃未知查询参数）。

会话切换（2026-09-02 下午补充，真机全部通过）：
- 微信：`切换到微信 打开文件传输助手` 13.8 s——Ctrl+F → 键入名称 → 截图 + 本地 OCR 行框 → 跳过"搜索网络结果"联想块、点击第一个真实分组（功能/联系人/群聊/最常使用）下的条目 → OCR 复核右侧标题。OCR 服务是仓库 `scripts/visual_ocr_server.py` 新增的 `--engine ppocr`（PP-OCRv5 行级），在 WSL `~/paddleenv` GPU 环境跑在 8767 端口（8766 被另一个项目占用），`scripts/start_visual_ocr_wsl.ps1` 一键启动；全窗截图约 1.1 s/张。
- Codex：`切换到 Codex 打开报表生成` 10.0 s——Ctrl+K 命令面板的条目是真实 UIA ListItem，键入后点击第一个匹配项。
- Claude：`在 Claude 里打开会话 项目周报` 4.5 s——同样是 Ctrl+K 面板。**教训**：面板前两项是 `New chat“项目周报”`/`New task“项目周报”`（把输入文本本身当新聊天），第一版技能点了它、新建了聊天并把名称当消息发了出去；现已排除以 New chat/新聊天/新建 等开头或用引号回显输入文本的条目，只点已有会话。
- 解析器："切换到<聊天应用> 打开X" → 会话目标；"切换到X聊天框/会话/对话" → 前台聊天应用中的会话目标。
- Claude 双面板：Code 会话与 Chat and Cowork 聊天在不同表面，侧边栏顶部有切换按钮（运行中显示为 "Code, working"）。口述里带 "Chat and Cowork / cowork / 聊天模式" 走 Chat 面板，带 "Claude Code / code 模式" 走 Code 面板，**不说就默认 Code**。技能先切面板，再优先点侧边栏行（"Idle X"/"Running X"/"Mark as unread X"，排除 "More options for"/"New session in"/分组折叠头），侧边栏没有再退回 Ctrl+K 面板。真机：`在 Claude 的 Chat and Cowork 里打开会话 写作指导` 7.0 s、`在 Claude 里打开会话 项目周报` 6.9 s（自动切回 Code），各 1 个动作。

打开文件/文件夹 + 缩写容错（2026-09-02 晚补充，真机全部通过）：
- `打开d盘研究数据库那个文件夹` → `D:\研究数据仓库` 1.2 s。新增 `spoken_paths.py`：先按"盘"提取盘符提示，再查 WorkMap 项目索引（`search_candidates` + `resolve_candidate_id`），用字符 bigram 包含度给"研究数据库"这类缩写打分，盘符提示与项目盘符不符则排除；也支持"下载/桌面/文档 文件夹里那个X"按目录条目模糊匹配（带"网页/表格/pdf"等类型提示）。只在有明显赢家时返回，平票或弱匹配返回 None 交规划器。控制器在 `_resolve_task_goals` 里把解析不出的 APP_FOREGROUND 目标先过这个解析器，命中则改成 PATH_OPEN，用 `ExplorerSkill.open_directory`（文件夹）或 `native.open_path`（文件）打开。
- 微信发文件：`把下载文件夹里面那个季度总结的网页发送到微信的文件传输助手` → 26.5 s，把 `%USERPROFILE%\Downloads\季度总结·离线版.html` 发给文件传输助手。解析成三目标（APP_FOREGROUND + CONVERSATION_SELECTED + FILE_SENT）；FILE_SENT 目标里的文件描述在运行时用 spoken_paths 解析成真实路径。发送经剪贴板 CF_HDROP（`native.copy_files_to_clipboard`，等价于资源管理器复制）→ 聚焦输入框（OCR 定位 发送 按钮上方）→ Ctrl+V → 回车；文件传输助手默认自动发送，发给人类的联系人受 send_policy 约束（confirm 则只附加不回车）。发送后 OCR 复核聊天区出现文件名。
- 网页更丰富动态：`去chrome打开chatgpt网页然后开一个新对话，问一下测试问题（但是不要发送）` → 5.1 s，2 个动作，把"测试问题"打进 ChatGPT 输入框且不发送。解析器把"去X打开Y然后Z"拆成多目标（app + url + input_contains），"开一个新对话"在加载 chatgpt.com 首页时自动满足；网站别名表新增 chatgpt/claude/gemini/deepseek/kimi/知乎/b站等。控制器改成技能链循环，一条复合指令里各技能各管一个目标依次完成。

复核后的修正（2026-09-02 深夜，全部有单元测试）：
- 发文件前用 OCR 核对当前聊天标题等于目标会话，不符则中止（`ASSISTIVE_WRONG_CONVERSATION`）；发送改点 OCR 定位的"发送"按钮（不依赖回车发送设置）；发送后聊天区看不到文件报 `ASSISTIVE_SEND_UNVERIFIED` 而不是成功；"但是不要发送"只附加不发送；发完恢复剪贴板里原有的文字。
- 可执行文件（exe/msi/bat/cmd/ps1/lnk 等）永远不会被口语解析器选中，显式路径指向可执行文件也拒绝自动运行（`ASSISTIVE_POLICY_REJECTED`）。
- 只有"打开/进入/查看/启动"开头的指令才把未知应用名解析成路径；"切换到 X"永远不会变成打开文件夹。没有位置线索（文件夹/盘/网页/表格…）时要求对称 bigram 相似度 ≥0.8 的无歧义匹配，"打开记事本"不会被"记事本工具"项目劫持。
- 新增 `skills/app_launch.py`：应用没有窗口时，从配置的 executable、常用附件别名表（记事本/计算器/画图…）或开始菜单快捷方式启动它，再绑定新出现的窗口；终端、注册表、设置、卸载类一律不启动。
- 解析器："去"只在后面跟已知应用名时算激活动词；"再/并"只在后面跟动词时切分子句（"并购数据"不再被切开）；只有明确的"新对话/新聊天"才被首页加载吸收；"把 X 发给文件传输助手"不说微信也默认微信；发给 Claude/Codex 的文件在动手前就明确拒绝（`ASSISTIVE_UNSUPPORTED_TARGET`）；找不到文件在碰桌面前就失败并列出最接近的候选。
- 微信搜索下拉改成轮询（最多 4 次 × 0.7 s）；微信/Claude/Codex 的会话与发文件技能优先选主窗口（微信的图片查看器、小程序窗口同属 Weixin.exe，标题不是"微信"）。
- 浏览器：URL 目标要求页面 Document 元素的地址也是目标 URL（地址栏一回车就变，页面还没加载）；草稿技能排在导航之后，最多等 10 秒让 ChatGPT 这类重页面渲染出输入框，每次轮询重新读窗口清单（未配置的 Chrome 窗口 ID 绑定标题，标题随页面变化）。
- 场景套件新增 `open_workmap_folder`（默认打开 Downloads，用 `HANDSFREEPC_SCENARIO_FOLDER_COMMAND` / `HANDSFREEPC_SCENARIO_FOLDER_PATH` 换成项目地图缩写和对应目录）、`chrome_chatgpt_draft`、`wechat_send_file_self`（发文件有副作用，只有 `HANDSFREEPC_SCENARIO_WECHAT_SEND_COMMAND` 给出指向本机真实文件的口语指令时才运行）。验收用例只放中性示例，本机文件名通过环境变量注入。

## 方向切换：`engine: kimi_agent`（2026-09-02 深夜）

用户实测 UIA 路线仍不可靠且"太笨、太多东西要自己弄"，而用 Kimi Code CLI 直接以键鼠（截图→看图算坐标→pyautogui→截图核对）操作桌面全部成功，于是把执行层整体换成 Kimi：

- `handsfree_pc/desktop/kimi_agent.py`：`KimiAgentController` 实现原 `Controller` 协议，每条语音指令 → `kimi -p "<前言>\n用户指令：<转写>" --output-format stream-json`，工作目录默认用户主目录（技能、WorkMap、`gui_control` 脚本都在那里）。`-p` 模式本身免审批执行（与 `--yolo/--auto` 互斥）。流式解析 assistant/tool/meta 事件：每次工具调用写 diagnostics（`KIMI_TOOL_CALL`），最终文本里的 `RESULT: 成功|失败 - 说明` 决定成败，`SCREENSHOT:` 给出核对截图；无 verdict → `KIMI_NO_VERDICT`；超时/取消会杀掉整个进程树。
- 前言要求代理按 `~/.kimi-code/skills/gui-control/SKILL.md`（用户那轮 Kimi 会话沉淀的技能）的"意图定位"原则对转写做模糊匹配、"不要发送"绝不回车、Claude 默认 Code 页签、找文件用 find 按 mtime（下载目录几千个文件，Glob 会超时）。
- 配置：`computer_control.engine: kimi_agent`，`kimi_executable / kimi_working_directory / kimi_model / kimi_skills_dir / kimi_preamble_file / kimi_resume_session`；要求 `privacy.allow_cloud_planner: true`；failure_policy 默认 continue。语音前端、队列、反馈、急停全部沿用。
- 非交互验收（6/6 通过，均由最终截图核对）：发文件到文件传输助手 223 s；Codex 找会话输入不发 94 s；Claude Code 找会话输入不发 92 s；Chrome 开 ChatGPT 新对话输入不发 78 s；打开 G 盘项目文件夹 66 s；微信找联系人打开聊天 66 s。
- 已知代价：每条指令 1–4 分钟（每步一次视觉模型调用），比 assistive 确定性技能慢一个数量级，但覆盖面和鲁棒性远好于 UIA。proof_v1/assistive_v1 保留可选。

已知缺口（进入 PR2/PR3）：
- 微信/Codex 视觉规划器路径仍然慢而不可靠（仅作为技能失败后的兜底）；后台窗口截图会抓到叠在上面的其他窗口内容，所以观察前必须激活。
- 会话/草稿技能踩过两个坑（已修）：① 微信搜索下拉是网络加载、布局不稳，会话已经打开时不该再搜——现在先截图看标题栏，已打开则零动作成功；② 导航后打草稿要重新观察前台窗口，否则读到的是地址栏那份旧观察（ChatGPT 输入框尚未渲染）。
- Electron 应用首次 UIA 查询可能只返回十几个元素（无障碍树懒加载），控制器对 `Chrome_WidgetWin_1` 窗口做一次 0.4 s 重试；Chrome/Claude 一次完整观察仍需 1.3–3.4 s，是复合指令延迟的主要来源。
- 规划器仍为每步冷启动 CLI；常驻会话（PR3）之前长尾任务每步 10 s 以上。

## 发布门槛（总）

- 确定性技能 20 次重复成功率 ≥95%；六类真实命令总体 ≥80%；
- 切换应用 p95 <3 秒；两步复合 p95 <10 秒；
- 普通失败不阻塞队列；单条命令占队列 ≤120 秒；
- CI 的 `not live` 全绿仅为必要条件，发布判断以 scenarios 报告为准。
