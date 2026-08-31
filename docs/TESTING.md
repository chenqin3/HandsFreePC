# HandsFreePC 0.3 测试指南

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
- 零/多/否定/顺带提及应用时拒绝，只接受用户肯定且唯一明确指定的应用；
- 已识别的 terminal/UAC/认证/密码/支付/隐私/公开链接 surface fail closed；全部通用文本输入，以及命中本地已知词形/上下文的发送/删除/安装/上传/分享/关闭动作确认；同时记录未知词形不能由该词表证明安全；
- confirmation ID、随机四位挑战码、静态前缀拒绝、超时、重放、界面变化与再次分类；同一 `VoiceRuntime` 进程内已签发码在成功、取消或超时后都不回收，重复抽样有界耗尽时拒绝；
- 通用 UI 确认摘要只原文显示用户原句中已验证的 exact target label；未授权 sibling/window label 的原文/语义只影响本地分类，不进入摘要，短 digest 仅是不可逆绑定元数据；
- 每个通用 planner 动作的 expectation false-before/true-after、fresh observation、fingerprint change、精确 Unicode 输入和本地 completion expectation；
- `native-...` 确认绑定完整 plan/source、规范路径、stat 身份及普通文件 SHA-256；确认时 re-prepare/re-safety/rebind，变更、替换、失败或重放均不执行；
- 持久 MCP client 的一次初始化、串行 call、超时、取消与进程树关闭；
- Qwen adapter 的 Unicode damage、edge-whitespace、tool 缺失、unknown mutation outcome；
- 静态 `doctor` 永不声称 live-ready；
- live doctor 使用自有 fixture，而不是用户应用。

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

这是 0.3 的第一个可信桌面动作验收，但作用域刻意很小。它会打开 HandsFreePC 自己的 fixture、占用前台、把随机 token（包含“中文验收”）写入唯一 UIA 文本字段，然后 fresh observe 并调用 `DesktopVerifier`。

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

当前 `over` 同时有独立英文 Vosk KWS 主路径和正文 SenseVoice 后备路径。KWS 验收还要确认词时间与无词时间 block fallback：marker 音频不送入正文 ASR，marker 前后非空片段分别转写，每个 marker 只完成它前面的 prompt，末段进入下一条 pending；同一 VAD 内单个/多个 marker 都不得丢前缀、吞后缀、重复入队或把 `over` 混入正文。分别记录 `PROMPT_DELIMITER_DETECTED` 与 `COMMAND_ENQUEUED`。

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
- action 不含坐标、shell 或未知字段；
- UIA 文本里的指令没有被当作用户任务；
- 用户原句未唯一肯定指定 app 时，planner 根本不被调用；
- 每个 action 的任务后置条件在 fresh before 为 false，在 fresh after 为 true；
- planner 的 `done` 会再由 LocalVerifier 检查；
- planner 退出或不合 Schema 时停止，不 fallback 到 legacy controller。

## 7. 第三方应用受控验收

fixture PASS 后，才在非敏感测试账户和可回滚数据上验证 Codex、Claude 或其他 app。应用 profile 必须显式配置并人工检查唯一窗口：

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

1. **最小观察**：`strict` 下用户原句只肯定命名一个 app 和目标控件；验证未命名、两个 app、否定提及和顺带提及都会拒绝。`personal_trusted` 另验同一控制器可继承上一条 fresh-verified app/window，而新控制器、窗口变化和 strict 不继承。断言 `CONTENT` 永不进入 planner；strict 只含被点名控件，personal_trusted 最多再含安全导航控件与当前输入框；两者都不含原始窗口标题、进程 ID、value/automation ID、截图 bytes 或真实截图可用性；
2. **无副作用导航**：切换一个已知 tab，要求 after UIA 中出现选中状态或特定文本；
3. **本地输入**：在测试草稿框请求写入独特中英混合 token、不发送；`strict` 必须等待随机四位码，静态“确认执行”无效；`personal_trusted` 只有本句完整口述、唯一聚焦非密码输入框可免确认。两种模式都要求 exact round-trip，且不能点击发送；
4. **多步任务**：每步后核对 generation 增加、fingerprint 变化，并记录同一任务 expectation 的 false-before 和 true-after；
5. **typed confirmation**：用测试草稿的“发送”按钮触发确认，但先取消；验证错 ID、错误/旧四位码、过期、重放和确认前界面变化都拒绝；
6. **一次确认执行**：只在测试账户发送无害内容，说出本轮随机四位码，确认仅执行原动作一次；
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

真实屏幕中 `VERIFIED_COMPLETION` 仍由执行动作的同一 agent 自报，没有 LocalVerifier。不得把这层标成 0.3 trusted acceptance，也不得用它证明目标应用已操作成功。

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
