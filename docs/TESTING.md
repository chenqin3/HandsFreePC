# HandsFreePC 测试指南

HandsFreePC 会使用麦克风、抢占前台窗口并注入键盘输入，因此测试分成三层：纯自动化、模型/反馈 smoke test、真实桌面 live test。前一层通过不能替代后一层。

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

- 配置合并、双重云授权和数据模型校验；
- 中文文本归一化、唤醒/停止短语和确定性意图；
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

Windows / UIA 测试使用注入式 fake backend，不会移动真实焦点，也不能证明目标应用当前版本的选择器有效。`dry_run` 只禁止真实 Windows 桌面后端/动作，不会自动禁止麦克风、反馈或已经双重开启的云 planner。

默认模型安装器已有“许可文件下载失败时不留下半安装目标”和正常 staged install 的自动化覆盖；当前套件尚未逐项覆盖完整元数据 skip、缺元数据重下、`--force`、SHA mismatch、归档 traversal/link 拒绝以及已有目录在最终替换失败时的 rollback。代码实现了这些门禁，但发布验收仍应把“已实现”与“每条故障路径均有回归测试”分开。

## 3. 解析和 dry-run

单条解析：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml parse "打开 D 盘的项目文件夹里的说明.txt"
```

批量模拟：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --independent --file ./examples/demo_commands.txt
```

测试完整唤醒状态机时加 `--require-wake`，输入必须包含唤醒词：

```powershell
./.venv/Scripts/handsfreepc.exe --config ./config.local.yaml simulate --require-wake "现在开始语音操作，切换到屏幕反馈"
```

`--independent` 会让每一行从独立状态开始，避免前一条“进入听写”把后一条命令当作听写文本。检查每条 JSON 的 `actions`、`risk`、最终 `state`、`message` 和 `success`；任一条模拟失败时命令应返回非零。已存在目录和窄安全文件后缀可以保持 `safe`；未知后缀、无后缀普通文件、主动/间接执行类型和应用内原生语音应进入 `confirming`。planner 给出的风险不得被本地重判降低。

`simulate` 强制 dry-run 并使用 no-op feedback，但仍会读取/解析检查本机路径并验证配置热键。若本地配置已经双重开启 planner，无法确定解析的输入仍可能调用 Codex/Claude 和网络；做纯离线解析验收时保持 planner 两个开关关闭。

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
- 默认隐私报告中 `save_audio=false`、`save_transcripts=false`、`cloud_planner=false`。

普通 `doctor` 只检查 planner CLI 是否存在，不执行提供商认证命令；需要显式验证订阅登录时使用 `doctor --check-planner-auth`，这一步可能联网。`doctor --strict` 只是运行结构检查：不实例化 ASR/VAD、不打开默认/配置麦克风、不验证 `speech.input_device` 指定设备，也不检查模型许可/source metadata 或重算历史归档 SHA；下载器的完整元数据 skip 门禁是另一层。`scripts/run.ps1` 与 `scripts/smoke-test.ps1` 都先执行严格 readiness 门禁；直接 `handsfreepc run` 和 Startup 快捷方式不会先执行它。`doctor` 会显示本机路径和命令位置，只应保存在本地测试记录中。

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

发布机已通过短 SAPI 播放，以及遮罩可见、不抢焦点的 live smoke。再分别用 `--mode voice` 和 `--mode both` 检查本机中文 SAPI 声音。朗读期间 PortAudio callback 仍向有界内存队列/预卷写入，运行时暂停识别和命令处理；`speaking` 覆盖整个待播队列，全部播放后这两处缓冲会一起丢弃，避免把自己的声音识别成命令。0.1.0 的 SAPI 队列播放不能被停止词打断，所以测试文本应保持短小。`doctor --strict` 不测试 SAPI，当前 worker/COM/dispatch 错误也不会让 `overlay-demo --mode voice` 返回失败；必须人工确认真的听到声音。默认 `overlay` 更安全，`both` 至少保留遮罩。多显示器、不同 DPI、远程桌面和不同中文声音仍应逐机测试。

## 7. 真实麦克风、Vosk 唤醒和 Silero VAD

先在 `config.local.yaml` 中设置：

```yaml
execution:
  dry_run: true
```

然后运行：

```powershell
./scripts/run.ps1
```

发布机已经通过 Vosk 合成唤醒/停止、Silero 官方样例、16 kHz 真实麦克风读取，以及完整本地运行时启动/停止；这些是链路 smoke，不是家庭环境准确率验收。仍应做以下矩阵，每个条件各说 10 次唤醒词和 10 次相近但不应唤醒的普通话：

| 条件 | 距离 | 背景 | 记录 |
|---|---:|---|---|
| 安静、正对麦克风 | 0.5 m | 无 | 命中、漏唤醒、误唤醒、延迟 |
| 抱娃正常姿势 | 1–2 m | 衣物摩擦 / 婴儿声音 | 同上 |
| 屏幕扬声器开启 | 1–2 m | 视频或会议 | 同上 |
| 远距离或侧向 | 2–3 m | 日常家庭噪声 | 同上 |

还要检查：

1. 只说“现在开始语音操作”时进入等待命令，超时后回到 `ARMED`；
2. 唤醒词和命令同一句说出时，预卷缓冲没有吞掉开头；
3. 正常停顿不提前截断，长时间不说话能结束一句；
4. “停止所有操作”在音频循环活动时进入 `PAUSED`；全局停止短语按高优先级子串匹配，并在 `AWAKE` / `DICTATION` / `CONFIRMING` 录音中逐 block 送入本地 Vosk，因此听写内容说出完整停止短语也可能被截断并暂停；
5. “恢复语音操作”恢复待唤醒；
6. 拔插麦克风、睡眠恢复和默认设备变化后的行为有记录。

Vosk grammar 中中文词之间要保留空格，例如 `现在 开始 语音 操作`；运行时唤醒词则写自然中文。修改一个列表时要同步修改另一个。

## 8. 受控 Windows live test

live test 可能打开窗口、改变前台焦点和输入文本。开始前：

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

## 9. Codex / Claude 自有听写 live test

公开配置中 Codex/Claude 的 `search_hotkey` 与 `native_voice_hotkey` 都是 `null`，`voice_button_names` 是空列表；项目没有声称这些应用的当前 UI 选择器可直接使用。0.1.0 没有内置 `inspect` 命令；先用外部 UIA/辅助功能检查器在本机目标版本、语言、账号布局和 UIA 树上完成校准，再做以下测试。

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

## 10. 应用内原生语音 live test

原生语音属于确认动作。只有在配置并验证 `native_voice_hotkey`，或 `voice_button_names` 能唯一命中按钮时测试：

1. 明确说“打开 Codex 应用内语音”；
2. 确认状态出现，但按钮尚未点击；
3. 说完整的标准化确认短语“确认执行”；再用“不要确认执行”等否定句验证不会授权；
4. 验证程序先等待此前整个 TTS 队列清空，再只点击预期按钮；执行中和成功提示均为 overlay-only，且 HandsFreePC 随后进入 `PAUSED`；
5. 结束第三方语音会话，再说唤醒词返回 HandsFreePC；
6. 记录第三方应用是否真正释放麦克风。

再各自注入无按钮、非法热键和执行异常，确认合法原生语音计划一旦进入执行尝试，失败也保持 `PAUSED` 且只显示遮罩；同时验证 `start_native_voice` 后还有动作、或与 `set_feedback_mode` 组合的非法计划会在执行前被阻断、回 `ARMED`，此时尚未尝试开麦并按当前反馈模式报错。

这一测试不能用自有听写的通过结果替代。

## 11. 可选云规划器测试

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

## 12. 可选 faster-whisper 后备测试

默认安装只包含 `audio` 与 `windows` 依赖，`speech.fallback.backend` 为 `none`。需要后备时先安装额外依赖，并在启用前显式预下载模型：

```powershell
./scripts/install.ps1 -WithWhisper
./.venv/Scripts/python.exe -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
```

这会访问模型托管站并产生 GB 级下载/缓存。预下载和加载成功后，才把本地配置中的 `speech.fallback.backend` 改为 `faster-whisper` 并保留 `model: large-v3-turbo`。当前只在已构造的 SenseVoice 某次 `transcribe()` 抛异常时触发后备；空文本、低置信度和 SenseVoice 启动/模型加载失败都不会触发。0.1.0 自动化套件尚未覆盖这条后备，因此启用者应在隔离样例上验证异常分支，并记录首次/缓存后延迟、内存、设备与 compute type；不接受运行期联网或额外资源占用时保持 `none`。

## 13. 结果记录模板

```text
日期：
Windows 版本：
Python / handsfree-pc 版本：
目标应用及版本：
显示器 / DPI：
麦克风设备与距离：
配置差异（不含私有路径和凭据）：
测试命令或口令：
期望结果：
实际结果：
结构化 evidence（脱敏）：
PASS / FAIL / BLOCKED：
失败复现：
```

公开 issue 中只提交最小、脱敏的复现。录音、完整转写、窗口标题、真实项目名和本机路径默认都视为私有信息。
