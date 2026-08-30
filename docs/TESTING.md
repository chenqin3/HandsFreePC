# HandsFreePC 测试指南

HandsFreePC 会持续使用麦克风，并可通过 Codex Computer Use 或兼容执行器改变前台窗口、鼠标和键盘状态，因此测试分成三层：纯自动化/fake、模型与反馈 smoke、显式授权的真实桌面 live test。前一层通过不能替代后一层；0.2 文档**不声称已完成真实屏幕 Computer Use 测试**。

## 1. 建立开发环境

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./scripts/install.ps1 -WithDevTools
```

若尚未下载语音模型：

```powershell
./scripts/download-models.ps1
```

公开仓库的 `config.example.yaml` 是模板；测试应使用被 Git 忽略的 `config.local.yaml`。先检查差异，确认没有真实路径、设备名或账号信息将被提交。

## 2. 无桌面副作用的自动化测试

```powershell
./.venv/Scripts/python.exe -m compileall -q handsfree_pc tests
./.venv/Scripts/python.exe -m pytest -q --basetemp ./.pytest-tmp
./.venv/Scripts/python.exe -m ruff check .
```

当前发布工作树的完整自动化收集项已通过。文档不固定记录用例数量，以免新增测试后产生陈旧数字；其中一项需要 Windows 创建符号链接权限的真实 symlink 测试因发布机缺少该权限而跳过，`_resolve_within` 和 fake reparse 属性仍有单元覆盖。应在具备相应权限的主机补做该 live case。这些测试覆盖：

- 配置合并、兼容 planner 双重云授权、Computer Use 默认关闭和启用时的云许可/`dry_run: false` 门禁；
- 中文文本归一化、“开始语音操作”“结束语音操作”、急停/确认/恢复短语和确定性意图；
- `PromptAssembler` 的独立英文 `over`、大小写、多分隔符、跨片段累积，以及 `mouseover`/`voiceover` 不误切；
- 连续会话的有界普通 FIFO、执行中继续收音、半条丢弃、结束排空、失败暂停、确认 continuation、从实际提示送达起算的确认超时和整轮/队列取消；
- Codex controller 的首次 thread、`resume`、完整 JSONL、单行/长度/控制字符状态协议、超时、进程树取消、环境变量清洗和临时文件清理；
- 连续反馈本地切换、utterance-boundary 按优先级合并且只播一条、纯 `voice` 确认播报门禁与 SAPI 失败回退；
- 盘符、别名、模糊匹配、同分候选拒绝；
- UNC / `//server`、URI 和 Win32 device namespace 在文件系统访问前阻断；
- `ARMED` / `AWAKE` / `DICTATION` / `CONFIRMING` / `PAUSED` 状态转换和超时；
- blocked / confirm / safe 风险重判，以及 planner 风险只能保持或升高；
- 原生语音必须为最后一步、不能和反馈模式切换组合；非法组合在执行前阻断并回 `ARMED`，已进入执行尝试的成功/失败保持 `PAUSED` 且只显示遮罩；
- Windows 热键白名单、Unicode `SendInput`、前台窗口漂移拒绝；
- UIA 精确/模糊匹配、歧义拒绝、密码输入框拒绝；
- 所有 action 文本字段和 plan summary 拒绝 Unicode C 类控制字符；`TYPE_TEXT` 因而不能包含回车/换行；
- 确认必须完整标准化整句，否定确认不授权；听写提交必须是带控制前缀的完整控制命令，否定句不提交；
- dry-run 不构造真实 Windows 后端。

Windows / UIA 测试使用注入式 fake backend；Computer Use adapter 测试使用 fake subprocess。两者都不会移动真实焦点，也不能证明 Codex 登录、Computer Use server、app approval、截图、点击或目标应用后置条件。`dry_run` 禁止兼容 Windows 后端/动作，不会自动禁止麦克风、反馈或已经双重开启的云 planner；配置加载器会另行拒绝 Computer Use 与 `dry_run: true` 同时开启。

默认模型安装器已有“许可文件下载失败时不留下半安装目标”和正常 staged install 的自动化覆盖；当前套件尚未逐项覆盖完整元数据 skip、缺元数据重下、`--force`、SHA mismatch、归档 traversal/link 拒绝以及已有目录在最终替换失败时的 rollback。代码实现了这些门禁，但发布验收仍应把“已实现”与“每条故障路径均有回归测试”分开。

## 3. 兼容解析和 dry-run

单条解析：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml parse "打开 D 盘的项目文件夹里的说明.txt"
```

批量模拟：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
```

测试兼容一次唤醒状态机时加 `--require-wake`，输入必须包含唤醒词：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --require-wake "开始语音操作，切换到屏幕反馈"
```

`--independent` 会让每一行从独立状态开始，避免前一条“进入听写”把后一条命令当作听写文本。检查每条 JSON 的 `actions`、`risk`、最终 `state`、`message` 和 `success`；任一条模拟失败时命令应返回非零。已存在目录和窄安全文件后缀可以保持 `safe`；未知后缀、无后缀普通文件、主动/间接执行类型和应用内原生语音应进入 `confirming`。planner 给出的风险不得被本地重判降低。

`simulate` 强制兼容路径 dry-run 并使用 no-op feedback，但仍会读取/解析检查本机路径并验证配置热键。它不运行连续 `over` FIFO，也不启动 Computer Use，所以 `simulate` 成功绝不是屏幕控制成功证据。若本地配置已经双重开启 planner，无法确定解析的输入仍可能调用 Codex/Claude 和网络；做纯离线解析验收时保持 planner 两个开关关闭，并保持 `computer_control.enabled: false`。

## 4. 环境和音频设备检查

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --strict
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml list-audio-devices
```

验收点：

- 平台为 Windows，Python 必须为 3.11 或 3.12；
- `yaml`、`psutil`、`numpy`、`sounddevice`、`vosk`、`sherpa_onnx`、`win32api`、`pywinauto` 可导入；
- Vosk wake、SenseVoice command 和 Silero VAD 三个模型都报告 ready；
- 目标输入设备出现在 `audio_inputs`；
- `ready_for_run=true`；`doctor --strict` 在上述依赖、三个模型预期文件或任意输入设备缺失时返回非零；
- 默认隐私报告中 `save_audio=false`、`save_transcripts=false`、`cloud_transcript_permission=false`、`planner_transcripts_to_cloud=false`、`computer_control_transcripts_to_cloud=false`，并且 `computer_control.enabled=false`、`screen_context_to_cloud=false`。

普通 `doctor` 只检查 planner/Computer Use 的静态 CLI 与配置线索，不执行真实 UI 命令；`doctor --check-planner-auth` 会调用相应 Codex/Claude 登录状态检查并可能联网。`doctor --strict` 不实例化 ASR/VAD、不打开默认/配置麦克风、不验证 `speech.input_device` 指定设备，也不验证 Computer Use server、app approval、目标窗口、截图或点击。因此它只是预检，不是 Computer Use 验收。`scripts/run.ps1` 与 `scripts/smoke-test.ps1` 会先执行 strict；直接 `handsfreepc run` 和 Startup 快捷方式不会，但配置加载仍会强制 Computer Use 的许可与 dry-run 组合。`doctor` 可能显示本机路径和命令位置，只应保存在本地测试记录中。

## 5. SenseVoice 样例 WAV

默认 SenseVoice 包通常带有 `test_wavs/zh.wav`。确认它是单声道、16-bit PCM WAV 后运行：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml test-asr ./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/test_wavs/zh.wav
```

2026-08-30 发布机的官方样例实际转写为：`开饭时间早上9点至下午5点。`，该路径已通过。记录时仍应包含模型目录名、输入 WAV 的来源、采样率、实际转写、期望文本和是否可接受。上游样例通过只证明模型加载与单文件解码路径，不代表真实麦克风、家庭噪声、抱娃距离或方言已通过。

## 6. 遮罩和本机语音反馈

遮罩 smoke test：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml overlay-demo --text "HandsFreePC 正在听" --kind listening --duration 5 --mode overlay
```

人工确认：

- 大字置顶且清晰；
- 遮罩出现时当前文本输入框不丢失焦点；
- 鼠标点击能穿过遮罩；
- 多显示器和 100% / 125% / 150% 缩放下位置可接受；
- 结束后遮罩消失，没有残留窗口。

兼容路径可分别用 `--mode voice` 和 `--mode both` 检查本机中文 SAPI 声音。朗读期间 PortAudio callback 仍向有界内存队列/预卷写入，运行时暂停识别和命令处理；`speaking` 覆盖整个待播队列，全部播放后这两处缓冲会一起丢弃，避免把自己的声音识别成命令。0.2.0 的 SAPI 播放不能被停止词打断，所以测试文本应保持短小。`doctor --strict` 不测试 SAPI，当前 worker/COM/dispatch 错误也不会让 `overlay-demo --mode voice` 返回失败；必须人工确认真的听到声音。连续路径还要验证反馈切换句由本地处理（带或不带 `over` 都不进入 Computer Use FIFO），语音反馈只在 utterance 边界播放，播完后不会把 TTS 回声或旧缓冲当作下一条 prompt。

## 7. 默认安全配置下的真实麦克风基础 smoke

先保持 `computer_control.enabled: false` 和 `execution.dry_run: true`，运行 `./scripts/run.ps1`。此时只能验证麦克风、Vosk/SenseVoice/Silero 和 0.1 兼容状态机，不能进入连续 `ACTIVE`/`over`/FIFO。检查预卷、正常停顿、最大话语、麦克风拔插、睡眠恢复和默认设备变化；说“开始语音操作”后应仍走兼容一次命令路径，不应启动 Codex Computer Use。

当前没有“真实麦克风 + 连续 controller dry-run”模式。连续协议的无屏幕副作用验证使用自动化 fake；真实麦克风的连续协议只能在下一节四项配置门禁全开的受控 live test 中验证。

## 8. 连续 Codex Computer Use 受控 live test（尚未执行）

> [!CAUTION]
> 本节会把识别 prompt、目标窗口元数据、辅助功能树、截图、可见内容和剪贴板状态交给 OpenAI 处理，并允许 Computer Use 抢占前台、移动鼠标和发送键盘输入。0.2 仓库尚未执行或通过本节；以下是首次本机验收清单，不是既有测试结果。

先使用专门的低权限测试账户/桌面，关闭聊天、邮件、密码管理器、支付页和其他敏感窗口；只打开一个目标应用，并在可回滚的测试目录放一份无敏感内容的夹具。保存所有工作，保持屏幕可见，手边保留 `Ctrl+C`。不要用付款、删除真实文件、外发真实消息或修改账号权限来验证确认。

安装并登录 Codex CLI，确认 Computer Use skill/server 与 `node_repl` 可用。Windows 目标应用必须位于当前 active desktop 且可见；首次控制某个应用时可能出现 per-app approval。`Always allow` 是 Codex 自己的持久授权，不等于下面的 YAML 许可；只批准测试应用，不要给敏感应用长期授权。

仅在被 Git 忽略的 `config.local.yaml` 中设置：

```yaml
privacy:
  allow_cloud_planner: true
computer_control:
  enabled: true
  backend: codex
  allow_screen_context_to_cloud: true
execution:
  dry_run: false
```

运行 `doctor --strict`，但只把 `ready_for_live_control=true` 视为静态预检；它不验证登录、server、app approval、目标窗口、截图或点击。然后启动 `./scripts/run.ps1`，逐项记录实际屏幕证据：

1. 说“开始语音操作”，应进入持续监听；慢速和正常速度各测，`phrase_window_seconds` 应允许有限跨 final 聚合。
2. 先说“切换到屏幕反馈”，再分别测试“切换到语音反馈 over”“大字和语音两种都开”；这些应在本地生效且不出现 Codex job。语音反馈应等 utterance 边界才播放；在一个边界前制造多条不同优先级反馈时，只应朗读最高优先级中的最新一条，不能期待其余逐条补播。播放期间语音急停不可用。
3. 说“打开文件资源管理器并进入无敏感测试文件夹 over”。确认只有一个目标窗口被观察；记录指针/前台变化、每个动作后的新 UIA/截图，以及最终可见路径。不要只记 `VERIFIED_COMPLETION` 文本。
4. 第一条仍在执行时，说“打开测试说明文件 over”，随后再说一条只读导航命令加 `over`。确认遮罩分别报告入队，屏幕实际结果严格按普通 FIFO 顺序出现。
5. 用可丢弃的未保存测试文档要求一个明确需确认的动作，例如“关闭这个测试文档，并在点击不保存前请求确认 over”。确认 agent 在最后动作**执行前**返回 `NEEDS_CONFIRMATION`，屏幕尚未发生该动作，后续普通队列暂停；description 必须单行、无控制字符且不超过 160 字。
6. 待确认时说“继续队列”，应被拒绝并再次提示只能确认或取消；随后在有效期内说“确认执行”，confirmation continuation 应优先恢复同一 Codex thread，只做此前描述的动作，再回到普通 FIFO。另一次在纯 `voice` 模式重复：确认提示完整播完前抢说“确认执行”必须不授权；完整播完后才启动 `confirmation_timeout_seconds`。注入 SAPI 失败时应强制提示切换 `overlay`/`both`，切换并实际显示后才启动计时。再重复一次，提示送达后等待超过有效期且不说话，确认没有后台 Timer 主动改变界面；随后说“确认执行”，应被拒绝并取消本轮/controller/全部队列。最后用“取消所有操作”验证主动拒绝路径。
7. 对无副作用的多步导航任务，在中途说“立即停止所有操作”。确认当前 Codex 子进程收到取消、待处理队列清空；已经完成的焦点/点击仍留在屏幕上，证明急停不回滚。
8. 新开一轮，入队两条只读任务后说“结束语音操作”。它应只停止接受新普通 prompt，仍监听急停/确认等本地控制，并默认进入 `DRAINING` 排空两条；未说 `over` 的半条应丢弃。
9. 多行、超过 600 字、Unicode C 类控制字符、未知前缀、失败和超时应在 fake subprocess 自动化中注入，不要为制造异常而破坏真实 CLI。live 中若自然发生失败，确认 worker 暂停且不自动重试；普通失败才可用“继续队列”。
10. 结束后检查本地输出、Codex thread/history 和提供商账户可见记录，确认没有意外敏感信息；不要假设 `save_transcripts: false` 会清理 Codex 侧记录。

每条 `VERIFIED_COMPLETION:` 都必须与人工观察的任务后置条件对照。若报告成功而屏幕不符，记录为 **FAIL（假成功）**；本地 adapter 的状态协议不是独立视觉 verifier。测试表的初始状态应写 `NOT RUN`，只有具体机器、应用版本和证据齐全后才能改为 PASS。

真实房间还应对开始、结束、急停与相近否定样本做以下矩阵，每个条件至少重复 10 次：

| 条件 | 距离 | 背景 | 记录 |
|---|---:|---|---|
| 安静、正对麦克风 | 0.5 m | 无 | 命中、漏识别、误触发、延迟 |
| 抱娃正常姿势 | 1–2 m | 衣物摩擦 / 婴儿声音 | 同上 |
| 屏幕扬声器开启 | 1–2 m | 视频或会议 | 同上 |
| 远距离或侧向 | 2–3 m | 日常家庭噪声 | 同上 |

Vosk grammar 中中文词之间要保留空格，例如 `开始 语音 操作`；运行时控制短语写自然中文。修改开始、结束、急停、确认或恢复列表时，要同步维护 grammar。

## 9. 兼容 Windows 执行器 live test

先把 `computer_control.enabled` 恢复为 `false`；否则上一节的 Computer Use 配置与本节的 `dry_run: true` 组合会被配置加载器拒绝。兼容 live test 可能打开窗口、改变前台焦点和输入文本。开始前：

1. 保存所有正在编辑的文档；
2. 关闭密码框、付款页、管理员窗口和无关的同名 Codex / Claude 窗口；
3. 创建一个只含无敏感文本的测试目录和 `.txt` 文件；
4. 先保持 `dry_run: true` 核对计划，再改为 `false`；
5. 屏幕上始终观察目标，准备好用 `Ctrl+C` 终止进程。

文件打开验收分两层记录：

- executor 是否解析到唯一的绝对路径并成功调用 Windows Shell；
- 关联应用是否打开了正确文件，标题/内容是否与测试夹具一致。

当前代码只对第一层提供结构化 evidence；第二层必须人工检查，不能把 Shell dispatch 成功写成“文件内容已经正确渲染”。

发布机已验证：当前输入桌面名称为 `Default`，并成功把仓库的公开 `examples` 目录调度给 Explorer。它没有证明任意文件关联查看器的内容后置条件；非 `Default` secure desktop 的拒绝由自动化测试覆盖。

## 10. 兼容 Codex / Claude 自有听写 live test

公开配置中 Codex/Claude 的 `search_hotkey` 与 `native_voice_hotkey` 都是 `null`，`voice_button_names` 是空列表；项目没有声称这些应用的当前 UI 选择器可直接使用。0.2.0 没有内置 `inspect` 命令；先用外部 UIA/辅助功能检查器在本机目标版本、语言、账号布局和 UIA 树上完成校准，再做以下测试。

每个应用版本分别测试：

1. 只保留一个匹配窗口；
2. `apps.<name>.process_names` 与实际进程一致；
3. `title_patterns` 不会误命中别的应用；
4. UIA 能在可见/启用后代中按控件类型和可访问名称唯一找到项目/对话；composer 能由内置候选名或唯一 Edit/Document fallback 找到；
5. 进入听写后，短中文只写入目标输入框；
6. 人为切换前台窗口后，下一段听写应失败关闭，不能写入错误窗口；
7. “电脑发送提示”前内容不提交；
8. 密码输入框、多个输入框或多个同名对话应拒绝；
9. 目标应用以管理员权限运行时，普通权限 HandsFreePC 应报错而不是尝试绕过。

后置证据也要按实际能力记录：输入前会重验同一个已固定的非密码控件，`TYPE_TEXT` 成功只证明 SendInput 接受 UTF-16 单元且前台未变，不证明控件值改变；`SEND_PROMPT` 只证明发送 Enter 并保持前台，不证明消息出现或服务端接受。代码不主动比较目标进程完整性级别，第 9 项主要依赖 Windows UIPI 的实际阻止行为。

Electron 应用升级后必须重新做这组测试。不要为了“让它能点”把唯一性阈值调得很低，也不要把屏幕坐标写成静默兜底。

## 11. 兼容应用内原生语音 live test

原生语音属于确认动作。只有在配置并验证 `native_voice_hotkey`，或 `voice_button_names` 能唯一命中按钮时测试：

1. 明确说“打开 Codex 应用内语音”；
2. 确认状态出现，但按钮尚未点击；
3. 说完整的标准化确认短语“确认执行”；再用“不要确认执行”等否定句验证不会授权；
4. 验证程序先等待此前整个 TTS 队列清空，再只点击预期按钮；执行中和成功提示均为 overlay-only，且 HandsFreePC 随后进入 `PAUSED`；
5. 结束第三方语音会话，再说唤醒词返回 HandsFreePC；
6. 记录第三方应用是否真正释放麦克风。

再各自注入无按钮、非法热键和执行异常，确认合法原生语音计划一旦进入执行尝试，失败也保持 `PAUSED` 且只显示遮罩；同时验证 `start_native_voice` 后还有动作、或与 `set_feedback_mode` 组合的非法计划会在执行前被阻断、回 `ARMED`，此时尚未尝试开麦并按当前反馈模式报错。

这一测试不能用自有听写的通过结果替代。

## 12. 可选兼容文本 planner 测试

先用对应官方 CLI 完成登录，并显式检查认证：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml doctor --check-planner-auth
```

该检查在清洗常见 key/token/secret 环境变量后的子进程中运行，可能联网。然后在本地配置中显式设置：

```yaml
privacy:
  allow_cloud_planner: true
planner:
  enabled: true
  backend: codex  # 或 claude
```

用一个确定性解析器不认识、但不包含本机秘密的文本测试结构化计划：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml parse --use-planner "请把反馈调整为只在屏幕上显示"
```

检查输出只包含 Schema 允许的动作，不包含 shell、坐标、凭据或发明出来的路径；本地安全策略只能保持或提高 planner 的风险。分别记录 Codex 和 Claude 的 CLI 版本、账号认证方式、模型、延迟、退出码和计划 JSON；不要记录 token。普通命令运行时仍应优先使用确定性解析器。

本次发布机验收中，Codex 通过已有 ChatGPT 订阅完成结构化规划；Claude 在清洗环境后的订阅 OAuth 认证不可用，且没有回退使用环境 API key。两者都没有完成 Codex/Claude 桌面项目、对话、Design 或原生语音按钮的 live selector 验证。planner 的启动/超时错误应只返回泛化错误，不回显原始 prompt 或 provider stderr。

HandsFreePC 的 `execution.blocked_keywords` 只约束它自己的本地动作计划。一旦用户在听写中明确说完整控制命令“电脑发送提示”，已经写入 composer 的文本会交给下游 Codex/Claude agent；下游能执行什么由该 agent 自己的 sandbox、approval 和 permissions 决定。live test 应给下游采用最小权限，不能把 HandsFreePC 的禁词当作下游 agent 安全边界。

## 13. 可选 faster-whisper 后备测试

默认安装只包含 `audio` 与 `windows` 依赖，`speech.fallback.backend` 为 `none`。需要后备时先安装额外依赖，并在启用前显式预下载模型：

```powershell
./scripts/install.ps1 -WithWhisper
./.venv/Scripts/python.exe -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
```

这会访问模型托管站并产生 GB 级下载/缓存。预下载和加载成功后，才把本地配置中的 `speech.fallback.backend` 改为 `faster-whisper` 并保留 `model: large-v3-turbo`。当前只在已构造的 SenseVoice 某次 `transcribe()` 抛异常时触发后备；空文本、低置信度和 SenseVoice 启动/模型加载失败都不会触发。0.2.0 自动化套件尚未覆盖这条后备，因此启用者应在隔离样例上验证异常分支，并记录首次/缓存后延迟、内存、设备与 compute type；不接受运行期联网或额外资源占用时保持 `none`。

## 14. 结果记录模板

```text
日期：
Windows 版本：
Python / handsfree-pc 版本：
目标应用及版本：
Computer Use plugin/server/skill 与 node_repl 状态：
目标应用 approval（本次/Always allow）：
controller 模型与 thread 标识（脱敏）：
四项 Computer Use 配置门禁：
显示器 / DPI：
麦克风设备与距离：
配置差异（不含私有路径和凭据）：
测试命令或口令：
期望结果：
实际结果：
结构化 evidence（脱敏）：
foreground / 指针 / 键盘实际变化：
Codex 最终状态前缀：
人工观察的应用后置条件：
本地录屏位置（不要提交）：
PASS / FAIL / BLOCKED：
失败复现：
```

公开 issue 中只提交最小、脱敏的复现。录音、完整转写、窗口标题、真实项目名和本机路径默认都视为私有信息。
