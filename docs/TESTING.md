# HandsFreePC 0.4 测试指南

测试分成四层：纯自动化、静态预检、项目自有 UIA fixture live test、目标应用人工验收。上一层通过不能替代下一层；尤其不能把 planner 输出、驱动返回或 `doctor` 静态结果当作真实屏幕成功。

本文给出命令和验收边界，不声称某台机器已经执行或通过 live 测试。发布说明只能引用实际运行产物。

## 1. 自动化套件

在 Windows PowerShell 中：

```powershell
./.venv/Scripts/python.exe -m pytest -q -m "not live" --basetemp ./.pytest-tmp/unit
./.venv/Scripts/python.exe -m ruff check handsfree_pc tests
```

若系统临时目录出现 `WinError 5`，继续使用项目内一个新的显式 `--basetemp`，不要递归删除整个用户 Temp：

```powershell
$testTemp = Join-Path $PWD ('.pytest-tmp\run-' + [guid]::NewGuid().ToString('N'))
./.venv/Scripts/python.exe -m pytest -q -m "not live" --basetemp $testTemp
```

自动化应覆盖：

- “开始语音操作”/“结束语音操作”、`over`、多 prompt 和未完成半条；
- FIFO、队列上限、失败暂停、恢复、drain、急停和 cooperative cancellation；
- `PromptAssembler.finalize()` out-of-band seam；
- NativeSkillRouter 完整请求判定、风险准备和确定性 miss；
- observation/action generation binding 与 stale index 拒绝；
- StepPlanner 单步 JSON Schema、默认 Claude 严格 argv、Codex best-effort 显式门禁和输出拒绝；
- 旧 `planner.enabled` one-shot fallback 只接受原句肯定、非引号/数据引用、精确授权的应用 UI 导航；云输出不能决定 feedback/pause/resume/wait/path/text/send；
- `strict`/`personal_trusted` 在零/多/否定/顺带提及应用且无可信继承窗口时拒绝；`local_unrestricted` 则 fresh 枚举全部可见顶层 HWND、区分多 Chrome 窗口、允许跨 app 且不返回 `APP_SCOPE_REQUIRED`；
- 已识别的 terminal/Run/UAC/认证/密码/凭据/支付/隐私 surface 在所有 profile 中 fail closed；`strict`/`personal_trusted` 另覆盖通用文本和已知副作用确认，`local_unrestricted` 另覆盖普通低风险导航/切换/Toggle/通用无风险对话框直通、显式 app/window/field exact binding、UIA 与渲染搜索各自的完整提交链，以及已知高影响副作用仍确认；未绑定/可复用坐标与任意 shell 始终阻断，视觉 viewport 的一次性 screenshot-local 点、Win32 focus/caret 绑定、单次搜索文字与搜索回车必须分别覆盖；
- confirmation ID、随机四位挑战码、静态前缀拒绝、超时、重放、界面变化与再次分类；同一 `VoiceRuntime` 进程内已签发码在成功、取消或超时后都不回收，重复抽样有界耗尽时拒绝；
- 通用 UI 确认摘要只原文显示用户原句中已验证的 exact target label；未授权 sibling/window label 的原文/语义只影响本地分类，不进入摘要，短 digest 仅是不可逆绑定元数据；
- 每个通用 planner 动作的 expectation false-before/true-after、fresh observation、fingerprint change、精确 Unicode 输入和本地 completion expectation；
- `native-...` 确认绑定完整 plan/source、规范路径、stat 身份及普通文件 SHA-256；确认时 re-prepare/re-safety/rebind，变更、替换、失败或重放均不执行；
- 持久 MCP client 的一次初始化、串行 call、超时、取消与进程树关闭；
- Qwen adapter 的 Unicode damage、edge-whitespace、tool 缺失、unknown mutation outcome；
- 静态 `doctor` 永不声称 live-ready；
- live doctor 使用自有 fixture，而不是用户应用。
- WorkMap 精确 alias、否定/引号/多分句/歧义拒绝、相对路径 containment，以及 `planner_hints` 未进入云 context。

涉及真实窗口、前台或麦克风的测试必须标注 `@pytest.mark.live`，默认命令排除它们。

## 2. 无副作用模拟

保持公开默认：

```yaml
computer_control:
  enabled: false
  backend: local_agent
  driver: windows_uia

execution:
  dry_run: true
```

运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml parse "打开桌面上的资料文件夹"
```

`simulate` 验证兼容 parser/计划和状态机，不创建真实连续 desktop agent，也不证明 UIA、planner 或目标应用可用。

确定性路径的 live 验收要另记：执行前 `path_open_state` 必须为 false，打开后必须为 true，且前台 HWND 必须不同于 before。目录还要求前台 Explorer 的规范化路径精确一致；文件当前只有“新前台标题包含精确文件名”的 best-effort 证据。应分别测试复用同一 HWND、同名文件和标题不显示文件名的应用，并把它们记为无法自动证明，不要写成 exact-path/exact-content PASS。

## 3. 静态环境检查

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
```

检查 JSON 中：

- Windows 与 Python 版本；
- `yaml`、`psutil`、audio、Vosk、sherpa-onnx、pywin32 和 pywinauto；
- 四个本地模型的运行文件（中文控制词、英文 delimiter、正文 ASR、VAD）；
- 至少一个音频输入；
- 当前 backend、driver、planner 与云许可；
- 相应 CLI 是否存在；
- `static_control_preflight_passed`。

静态 `doctor` 必须始终报告：

```json
{
  "live_control_verified": false,
  "ready_for_live_control": false
}
```

即使 `static_control_preflight_passed: true`，也没有打开目标应用、输入文本或验证 planner 登录。`--check-planner-auth` 会显式运行 Codex/Claude 的认证状态命令，可能联网；不加该 flag 时只检查命令存在。

## 4. 自有 UIA fixture live test

这是 0.4 的第一个可信桌面动作验收，但作用域刻意很小。它会打开 HandsFreePC 自己的 fixture、占用前台、把随机 token（包含“中文验收”）写入唯一 UIA 文本字段，然后 fresh observe 并调用 `DesktopVerifier`。

使用不需要云 planner 的本地测试配置：

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

显式运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml computer-doctor --live
```

或只运行对应 live pytest：

```powershell
./.venv/Scripts/python.exe -m pytest -q -m live tests/test_live_computer_doctor.py --basetemp ./.pytest-tmp/live
```

PASS 必须同时有：

- `fixture_started: true`；
- `fresh_observation: true`；
- `text_round_trip_verified: true`；
- `unicode_round_trip_verified: true`；
- `live_control_verified: true`；
- 进程退出后 fixture 已关闭。

ASCII 文本出现不算 Unicode PASS。退出码非 0、字段缺失或 verifier 原因失败均记 FAIL，不要手工改成通过。

若返回 `ForegroundIntegrityBoundary`，说明更高完整性前台阻止了精确 HWND 激活；这是 Windows UIPI 的安全失败，不是 PASS，也不得通过跳过前台验证、自动提权或结束用户进程来改绿。

该测试**不覆盖**：麦克风、`over`、队列、Codex/Claude、Qwen MCP、真实应用 selector、截图、点击、多步规划、外部副作用或高风险确认。

## 5. 本地语音链路

先保持电脑控制关闭和 dry-run：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml list-audio-devices
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml test-asr ./path/to/authorized-test.wav
./scripts/run.ps1
```

只用无敏感内容检查：

1. 正常语速说“开始语音操作”，确认进入“我在听”；
2. 说一条命令后清晰说英文 `over`，确认只入队一次；分别测试短暂停顿和同一 VAD 话语内紧接下一条正文；
3. 第一条执行反馈期间说第二条，确认 FIFO；再在同一 VAD 话语内说两个 `over`，确认两条依次入队且末尾未完成正文保留为 pending；
4. 说 `mouseover`、`voiceover`，确认不切分；
5. 不说 `over`，然后“结束语音操作”，确认半条被丢弃；
6. 测试急停、失败暂停、恢复和队列满反馈；
7. 分别测试 `overlay`、`voice`、`both`、`silent`。

当前 `over` 同时有独立英文 Vosk KWS 主路径和所选正文 ASR 后备路径。KWS 验收还要确认词时间与无词时间 block fallback：marker 音频不送入正文 ASR，marker 前后有声片段分别转写，纯静音 padding 不得调用 ASR 或幻听成新 prompt；每个 marker 只完成它前面的 prompt，末段进入下一条 pending。同一 VAD 内单个/多个 marker 都不得丢前缀、吞后缀、重复入队或把 `over` 混入正文。分别记录 `PROMPT_DELIMITER_DETECTED` 与 `COMMAND_ENQUEUED`。

TTS 为半双工：播放期间说话可能被丢弃，也不能靠语音急停打断正在播放的 SAPI。确认测试必须等确认提示实际显示或完整播报后，再说提示中的完整“确认执行 + 随机四位码”。还要断言静态“确认执行”、错误码、旧码、超时码和已使用码全部无效；同一进程内取消/超时码也不得重新签发，并测试有界生成空间耗尽会 fail closed。去重集合不跨进程持久化，重启后不保证绝对不复用。随机码不是持久化防重放凭证或说话人认证；另记录旁人/扬声器实时听到并转述本轮码的残余风险，不要把该测试写成防声学重放证明。

## 6. Codex/Claude 单步 planner 测试

先只做认证和结构化 dry checks，不打开真实 agent：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth
```

启用 full stack 前在本地配置中明确：

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: claude
  safety_profile: strict
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: false
  allow_legacy_codex_computer_use: false

execution:
  dry_run: false
```

对每个 provider 分开记录：CLI 版本、认证状态、模型（若显式配置）、首步延迟、结构化输出错误和超时。不要把 Claude 已登录推断为 Codex 可用，反之亦然。

测试 Codex 时单独改为：

```yaml
computer_control:
  planner_backend: codex_cli_best_effort
  allow_codex_cli_host_read: true
```

该 opt-in 只表示接受订阅版 Codex CLI 可能读取当前账户可见主机文件；deny list、read-only sandbox 和临时目录不能写成完整 no-tools PASS。测试记录还应把 HandsFreePC 主动发送的屏幕 context，与 CLI/provider 可能另行处理的账户、网络、CLI/OS/runtime、临时 cwd、用量和诊断/遥测元数据分开。

planner 测试的最低断言：

- 只返回 `observe/action/done/fail` 之一；
- 一个 response 最多一个 action；
- action 不含 shell 或未知字段；`x`/`y` 只可出现在当前 observation 唯一 `VisualViewport` 的一次左键动作中，且必须先验证 planner canvas 边界、再按比例映射到原始 capture 像素；其他坐标全部拒绝；
- UIA 文本里的指令没有被当作用户任务；
- `strict`/`personal_trusted` 下，用户原句未唯一肯定指定 app 且没有可继承的 fresh-verified window 时，planner 根本不被调用；`local_unrestricted` 则必须调用 planner，且候选范围恰为本轮 fresh 枚举的可见顶层窗口；
- 每个 action 的任务后置条件在 fresh before 为 false，在 fresh after 为 true；
- planner 的 `done` 会再由 LocalVerifier 检查；`local_unrestricted` 还必须在 `done` 前 fresh observe 同一 HWND；
- planner 退出或不合 Schema 时停止，不 fallback 到 legacy controller。

### `local_unrestricted` 专项

只在 Git 忽略的本机测试配置中设置：

```yaml
computer_control:
  backend: local_agent
  driver: windows_uia
  safety_profile: local_unrestricted
```

自动化和受控 live test 至少覆盖：

1. 未点名应用的自然任务不返回 `APP_SCOPE_REQUIRED`，而是把本轮 `list_apps()` 的所有可见顶层窗口作为 `allowed_apps`；
2. 同一 Chrome 进程的两个可见顶层 HWND 具有不同动态 app ID，planner 可选择其中任意一个；inventory 包含 display name、foreground、process name 和 window title；
3. `observe` 激活并复核确切 HWND/PID/process/title；窗口消失、HWND 复用、标题在非本项目动作后变化或 foreground 激活失败都拒绝；
4. planner 可从一个窗口 `observe` 到另一个窗口，推断未逐字口述的菜单/选项卡/搜索框中间导航；普通切换、菜单、Toggle 和未命中风险分类的通用 OK/Continue 对话框不触发 confirmation，每个实际 action 仍绑定当前 app/generation/element；
5. 未点名 app 的任务可由 planner 自选窗口；一旦用户明确说出 app/window/field，完成该口述步骤的 action 必须绑定 exact window/field。至少回归“在 Claude 的 Message 输入……”不能写入其他 app/editor，以及“In Chrome, search …”不能在非 Chrome 搜索框完成；
6. “搜索 X”只允许把 UIA 识别的搜索/地址输入字段精确设为用户原文 `X`，再按 Enter/Return，并要求 fresh `SEARCH_SUBMITTED` 结果语义 transition；写入聊天/普通编辑框、补写、改写、字段只包含 `X`、只填文字不按回车，或从 UI 内容发明文本都必须拒绝；
7. planner view 保留真实窗口标题、重复名称控件、未聚焦但可寻址的输入框，并移除 element value、automation ID、凭据样式标签和结构化 `CONTENT` 节点；
8. `windows_uia` 只捕获选中窗口而不是全桌面。Codex argv 包含临时 `--image`；原始窗口图超过最大边 2048 px 时临时文件是保持宽高比的 planner canvas，调用结束后清理。Claude CLI adapter 是 text-only，argv 不包含图片，但两者都收到文本 title/UIA context；
9. 识别到的发送/提交、删除、安装、上传/分享和关闭仍触发本轮 typed confirmation；终端/shell、Windows Run、UAC/安全桌面、认证、密码/凭据、付款、隐私/账户设置、未绑定/可复用坐标和任意 shell 仍阻断；key 仍受固定 allowlist；
10. 每个允许动作都需要 fresh bind、receipt、generation 增长、fingerprint 变化和同一后置条件 true-after；只切换窗口的零动作完成也只能由已 observe 的同一 app/HWND 的 `APP_VISIBLE` 验证。

视觉 fallback 另做独立自动化和受控 live 验收：

1. `visual_ocr.enabled: true`、`ocr_regions_enabled: false` 时不构造/调用 PaddleOCR client，完整目标窗口截图仍产生唯一 `VisualViewport` 并作为 Codex 视觉输入；PaddleOCR 不是截图规划前置条件；
2. `ocr_regions_enabled: true` 时才允许 PaddleOCR 增加文本区域；OCR 超时/异常只移除文本区域，截图 viewport 仍保留；
3. 原始截图最大边超过 2048 px 时，planner 图片必须保持宽高比缩小；等于/小于上限时字节不必改写。planner canvas 的边界、横纵比例映射、四舍五入和原图右/下边界 clamp 都要有测试，执行器最终只接收映射后的原始 capture 坐标；
4. 每个视觉 click/scroll 前重新截取当前 exact HWND 并复核窗口矩形。OCR click 还要唯一重绑同文同类近邻区域和 crop；viewport point 还要核对原图目标 patch，伪造/越界/旧 frame 全部拒绝；
5. viewport point click 后不能立刻获得文字输入。下一次 fresh observe 只有在 Win32 `GetGUIThreadInfo` 证明 exact target PID/TID、active/focus/caret HWND、可见非空 caret rectangle、caret 属于同一窗口/进程且几何位置与点击相符时，才临时声明一次 `type_text`；API 缺失/失败、foreign HWND/PID、foreground race、零高 caret、坐标转换失败或 caret 远离点击点都必须拒绝；
6. 渲染 `type_text` 只接受用户指令中的精确连续目标/搜索文字，拒绝截图文字、发明/改写/子串、消息正文、prompt、认证、凭据、付款和换行；执行前再验证同一 focus/caret identity 与未变化的点击点 patch，执行后立即消费文字能力；
7. 输入后的 fresh screenshot 已出现结果时必须正常点击结果，不能按键。只有画面没有结果、点击位于受限搜索区域且同一 focus/caret binding 仍有效时，下一步才可声明一次 Enter/Return，并以 `LAST_ACTION_VERIFIED` 绑定；视觉路径不使用 UIA 专属的 `SEARCH_SUBMITTED` expectation。armed viewport 上只有明确指向该 viewport、单次左键且仍在顶部搜索区域的重复 click 才可确定性改写为恰好一次 Enter 并消费能力；唯一语义结果 `Button` 及搜索区之外的视觉 click 必须保留原 action。其他 key、第二次 Enter、Send/Submit、消息/回复语境、失焦或换窗全部拒绝；
8. 每次只执行一个 click、单页 scroll、受限 `type_text` 或一次搜索 Enter/Return；每个动作后都要取得 fresh exact-window 截图并交给 planner 重规划/验证。旧截图、旧坐标、旧 caret、已消费 capability 或不重新规划都算 FAIL；
9. 动作导致新 foreground HWND 时，同 PID 或可验证父子进程关系才可更新原动态 app binding；helper executable 只从 immediate parent 的唯一 profile match 继承 profile。覆盖微信主窗到 `WeChatAppEx` 搜一搜、父窗口继续可见时 active task alias 仍保留在子窗口的成功用例，以及无关进程、同名伪装窗、身份变化、alias 被父窗抢回和非前台窗的拒绝用例；
10. WeChat 受控 live 用例只验搜索导航，不发送消息、不点击 Send/Submit；不得把受限搜索文字和一次搜索回车宣称为任意微信输入或发送。
11. 断言发给云端 planner 的 observation 没有原始 HWND/`local_window_id`。第一条视觉 `DONE` 只能在返回后由 controller 用本地完整 observation 绑定截图 token；随后必须取得同一 app/window、更新 generation/capture time 的第二张 fresh screenshot 并再次得到视觉 `DONE`。模型自造 token、复用 generation、换窗或只判断一帧都应失败；
12. 渲染搜索 helper 的 `TEXT_ABSENT` 特例只接受唯一 semantic `Button`：exact full label 必须包含用户精确 destination 并以“前往”或 `Go to` 结束，expectation 必须等于该完整标签。按钮消失只能验 navigation bridge，后续关联窗口仍须 fresh screenshot；部分标签、通用按钮、多个候选和以消失直接完成任务都应拒绝；
13. 构造全窗无关区域持续动画的 before/fresh frame：非视觉 UIA target 仅在 app/window、唯一 index、`local_identity`、control type、enabled 与 addressable 全部不变时可以继续，并由 driver dispatch-time rebind 再验；任一 identity/state 改变应拒绝。对应视觉 point 即使只有无关区域变化可容忍，也必须保持点击附近 local patch 稳定，target patch 改变时必须拒绝。

受控 live 记录必须注明屏幕上下文实际离机范围。若使用 Codex，记录选中窗口 PNG 已进入 provider context；不要用包含真实聊天、通知、病历、学生/客户资料或凭据的桌面做截图测试。

### WorkMap 精确路由专项

WorkMap 测试使用临时、无私人信息的假索引，不把真实导出路径或 alias 写入仓库。最低断言：

- 公开默认 `enabled: false`；启用时 `out_directory` 必填；
- 唯一项目标题、字符串 alias 和 `{project, relative_path}` alias 只对完整、肯定、单一的精确“打开/进入/查看”请求命中；
- 否定、引号、多分句、未知/歧义 alias、缺失目标、绝对/上跳相对路径都 miss 或配置失败；
- 命中生成 `source: workmap` 的确定性 `OPEN_PATH`，并继续走路径 binding、安全策略、执行和本地验证；
- WorkMap 缺失/重建中不会阻止 runtime 启动，任务可 fall through；
- 生产 agent loop 没有把 `planner_hints` 或本机绝对路径附加到 Codex/Claude context。

## 7. 第三方应用受控验收

fixture PASS 后，才在非敏感测试账户和可回滚数据上验证 Codex、Claude 或其他 app。`strict`/`personal_trusted` 的应用 profile 必须显式配置并人工检查唯一窗口；`local_unrestricted` 不要求静态 profile，而要检查 fresh inventory 中的 exact HWND/PID/process/title binding：

```yaml
apps:
  claude:
    process_names: ["claude.exe"]
    title_patterns: ["Claude"]
    mode_names:
      chat: ["Chat and Cowork", "Chat"]
      code: ["Code"]
```

先运行只读 profile/裁剪检查；输出只能有统计、digest、控件类型和经过安全筛选的标签，不能包含聊天正文、字段 value、automation ID 或截图：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --observe-only
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --observe-only
```

只读观察成功后，才可显式运行未发送草稿 smoke。它必须写入唯一、聚焦、非密码 composer，fresh observe 后 exact read-back，并报告 `sent: false`；随后只清理仍与本轮固定格式 token 精确相同的内容，并要求 `cleanup_verified: true`：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app claude --draft-smoke
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml app-doctor --app codex --draft-smoke
```

建议顺序：

1. **最小观察**：`strict` 下用户原句只肯定命名一个 app 和目标控件；验证未命名、两个 app、否定提及和顺带提及都会拒绝。`personal_trusted` 另验同一控制器可继承上一条 fresh-verified app/window，而新控制器、窗口变化和 strict 不继承。断言 `CONTENT` 永不作为结构化元素进入 planner；strict 只含被点名控件，personal_trusted 最多再含安全导航控件与当前输入框；两者都不含原始窗口标题、进程 ID、value/automation ID、截图 bytes 或真实截图可用性。`local_unrestricted` 则按上一节单独验全部 fresh 顶层窗口、真实标题/UIA context 与 Codex 选中窗口截图，并断言未点名 app 不返回 `APP_SCOPE_REQUIRED`、明确点名的 app/window/field 仍 exact bind；
2. **无副作用导航**：切换一个已知 tab，要求 after UIA 中出现选中状态或特定文本；
3. **本地输入**：在测试草稿框请求写入独特中英混合 token、不发送；`strict` 必须等待随机四位码，静态“确认执行”无效；`personal_trusted` 只有本句完整口述、唯一聚焦非密码输入框可免确认。这两种模式都要求 exact UIA round-trip，且不能点击发送。`local_unrestricted` 的 UIA 搜索另验精确替换查询字段、按 Enter/Return，并以 fresh result transition 而非字段回显验收；渲染搜索按上一节单独验 focus/caret、exact target、fresh screenshot 和至多一次搜索回车，不能用 UIA round-trip 替它背书；
4. **多步任务**：每步后核对 generation 增加、fingerprint 变化，并记录同一任务 expectation 的 false-before 和 true-after；另断言推断的中间导航不冒充口述步骤，只完成第一段后返回 `done` 必须失败；
5. **typed confirmation（所有 profile 的已识别高影响动作）**：用测试草稿的“发送”按钮触发确认，但先取消；验证错 ID、错误/旧四位码、过期、重放和确认前界面变化都拒绝。`local_unrestricted` 的普通低风险切换/导航/Toggle/通用无风险对话框应不确认，但发送/提交、删除、安装、上传/分享和关闭仍必须确认；
6. **一次确认执行（所有 profile 的已识别高影响动作）**：只在测试账户发送无害内容，说出本轮随机四位码，确认仅执行原动作一次；不得因 `local_unrestricted` 跳过或伪造高影响 confirmation PASS；
7. **阻断表面**：验证真实密码、聚焦 secret/API-key 输入、认证、付款和 UAC 不会进入 planner/action；同时验证聊天正文提到 password/terminal/payment、已知凭据示例或普通长 ID 不会阻断无关安全标签，正文与凭据原文也不会进入 planner；
8. **急停**：在可重复任务中急停，记录当前动作是否已发生和后续队列是否清空。

每一步 PASS 都必须同时有：用户意图、before state、实际 action、fresh after state、LocalVerifier 原因和人工屏幕核对。仅看到“操作成功”遮罩、planner prose 或 driver receipt 一律不算。

应用升级、语言切换、A/B 功能或 UIA tree 改变后，应重新执行这一层。不要把一台机器的 PASS 外推到所有用户。

## 8. Qwen open-computer-use 实验测试

只在阅读 [OPEN_COMPUTER_USE.md](OPEN_COMPUTER_USE.md) 后测试。要求固定上游 0.2.3，不能用未记录版本代替，也不要自动安装到其他用户机器。

本地配置必须显式：

```yaml
computer_control:
  backend: local_agent
  driver: open_computer_use
  allow_experimental_driver: true
  allow_coordinate_actions: false
```

最低测试：

- MCP initialize 只发生一次，required tools 全部存在；
- list/observe/action/observe 使用同一持久进程；
- 每个动作后旧 generation 被拒绝；
- 含中文的 app title、UIA text 和输入 round-trip；
- replacement character 立即 fail closed；
- 开头/结尾空白输入被拒绝，避免 0.2.3 静默 trim；
- mutating call timeout/断管后 outcome 标为 unknown，不重试动作；
- 坐标动作保持关闭。
- `get_app_state` 缺少结构化 elements 时，planner 不得捏造 element index；点击/导航明确受限，`type_text`/`set_value` 因没有可核验焦点元素而失败关闭。

上游 Windows 中文编码 [Issue #5](https://github.com/QwenLM/open-computer-use/issues/5) 与 [PR #6](https://github.com/QwenLM/open-computer-use/pull/6) 未解决时，ASCII smoke 不能变成“中文可用”的发布结论。`computer-doctor --live` 当前不支持这个 driver。

## 9. legacy_codex_cli 回归

若已有用户必须保留 0.2 配置，可单独运行 fake subprocess/unit 回归，但结果只能标为 compatibility：

```yaml
computer_control:
  backend: legacy_codex_cli
  allow_codex_cli_host_read: true
  allow_legacy_codex_computer_use: true
```

- argv/thread/resume/JSONL 正确；
- cancellation 和进程树关闭正确；
- 状态行协议拒绝畸形输出；
- 不会被 factory 自动选择。

真实屏幕中 `VERIFIED_COMPLETION` 仍由执行动作的同一 agent 自报，没有 LocalVerifier。不得把这层标成 0.4 trusted acceptance，也不得用它证明目标应用已操作成功。

## 10. 发布验收记录模板

```text
HandsFreePC version/commit:
Windows build / Python:
Config backend/driver/planner（无密钥、无路径）：
Unit tests command + result:
Ruff command + result:
Static doctor result:
Owned fixture live command + result: NOT RUN / PASS / FAIL
Unicode round-trip evidence: NOT RUN / PASS / FAIL
Voice start/end/over/FIFO: NOT RUN / PASS / FAIL
Codex planner: NOT RUN / PASS / FAIL
Claude planner: NOT RUN / PASS / FAIL
Target app + version + language:
Target-app navigation/input: NOT RUN / PASS / FAIL
Typed confirmation cases: NOT RUN / PASS / FAIL
Blocked-surface cases: NOT RUN / PASS / FAIL
Qwen experimental driver: NOT RUN / PASS / FAIL
Known skips and reasons:
```

只记录已执行结果；没有运行就写 `NOT RUN`。测试证据不得包含真实音频、转写、绝对路径、窗口私人内容、token、登录缓存或 provider stderr。
