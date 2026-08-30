# Qwen Open Computer Use 实验适配

HandsFreePC 0.3 默认使用项目自有 `windows_uia` driver。`open_computer_use` 是对 [Qwen open-computer-use](https://github.com/QwenLM/open-computer-use) MCP server 的**实验、显式 opt-in** 适配，不是必需依赖、不会由安装脚本自动下载，也不是中文 Windows 的推荐默认值。

## 支持范围

当前 adapter 只针对 `@qwen-code/open-computer-use` **0.2.3**：

- 通过 stdio JSON-RPC/MCP 启动一个持久子进程；
- initialize 和 `tools/list` 只做一次；
- 要求 `list_apps`、`get_app_state`、`click`、`perform_secondary_action`、`scroll`、`drag`、`type_text`、`press_key`、`set_value` 九个工具存在；
- 所有 call 串行化；
- observation 有本地 generation，动作必须绑定最新 generation；
- 当前 `get_app_state` 只能在本地 adapter 中形成 accessibility text/截图状态，没有可安全绑定的结构化 elements；safety 重建的云 planner view 会移除这些原始内容和真实截图可用性；
- 每个动作后必须重新 `get_app_state`；
- MCP 的“accepted/success”不作为任务证据，仍经过 HandsFreePC `DesktopSafetyPolicy` 和 `DesktopVerifier`；
- 超时、取消和关闭会停止整个 MCP 进程树；
- mutating call 断管/超时后的屏幕结果视为 unknown，不自动重试。

未来上游版本即使名称相同，也不能未经回归就视为兼容。

## 为什么不默认启用

上游 0.2.3 的 Windows 路径经过 PowerShell/UIA。中文 Windows 存在 GBK/UTF-8 边界导致 UIA 文本或输入损坏的问题：

- [Issue #5: Chinese input/output is garbled on Windows](https://github.com/QwenLM/open-computer-use/issues/5)
- [PR #6: proposed UTF-8 handling fix](https://github.com/QwenLM/open-computer-use/pull/6)

截至 0.3.0 文档冻结时，该 PR 尚未合并到受支持的 0.2.3。ASCII 操作成功不能证明中文 app 名、窗口文字、元素 index 或输入内容可靠。

HandsFreePC 的补充防护：

- accessibility text 出现 Unicode replacement character (`U+FFFD`) 就拒绝；
- 0.2.3 会 trim `type_text/set_value` 的前后空白，因此 adapter 对这类可能有损输入直接拒绝；
- 默认 `allow_coordinate_actions: false`；
- stale observation、missing tool、invalid content 和 unknown mutation outcome 全部 fail closed。

这些措施只能发现部分损坏，不能修复上游编码桥。需要中文可靠性时使用默认 `windows_uia`。

实验 opt-in 不放宽 0.3 的通用契约：用户仍须肯定且只明确指定一个目标应用；每个动作仍须有任务相关的 false-before/true-after 后置条件；任何能执行的通用 `type_text`/`set_value` 仍须等待本轮随机四位一次性确认，静态“确认执行”无效。四位码只在当前 `VoiceRuntime` 进程内保证不再次签发，去重不跨重启持久化。

## 手工、固定版本安装

只有明确接受实验风险后，才由用户手工安装精确版本：

```powershell
npm install --global @qwen-code/open-computer-use@0.2.3
open-computer-use --version
```

不要在 HandsFreePC 启动时执行 `npx ...@latest`，也不要把自动安装、自动升级或未固定 Git main 写入常驻监听流程。包和源码适用上游 MIT License；查看 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

若由组织分发，应锁定包管理器记录并复核实际包来源/完整性。HandsFreePC 不安装 Node.js、不保存 npm 凭据，也不管理全局包卸载。

## 配置

在被 Git 忽略的 `config.local.yaml` 中显式配置：

```yaml
computer_control:
  enabled: true
  backend: local_agent
  driver: open_computer_use
  planner_backend: claude      # 或 none
  open_computer_use_executable: open-computer-use
  open_computer_use_args: [mcp]
  allow_experimental_driver: true
  allow_coordinate_actions: false
```

缺少 `allow_experimental_driver: true` 时配置加载会拒绝启动。这不是可以由公开默认配置、安装脚本或 planner 自动改掉的“安全值”。

若 planner 为 Codex/Claude，还必须显式允许 transcript 与 task-authorized 控件子集离机；这项屏幕上下文门禁保持保守，即使本 driver 当前因没有结构化 elements 而通常只能生成空 UI 子集。若为 `none`，只有 NativeSkillRouter 命中的任务可完成，generic miss 会明确失败。

Claude 是默认 planner。若明确改用 Codex，必须写 `planner_backend: codex_cli_best_effort` 并另设 `allow_codex_cli_host_read: true`；订阅 Codex CLI 没有完整 no-tools 保证。无论换哪个 planner，都不会补出本 driver 缺失的结构化 element index。

## 当前功能限制：没有结构化 elements

严格桌面动作必须引用当前 observation 中真实存在的 `element_index`，文本输入还必须绑定已观察且聚焦的元素。open-computer-use 0.2.3 当前适配不能从 `get_app_state` 建立这份结构化元素表，因此：

- planner 驱动的元素点击、secondary action、scroll 和 tab/navigation 受限；
- `type_text`、`set_value` 和面向焦点元素的 `press_key` 会保守失败；
- 不得用 planner 猜 index、打开 `allow_coordinate_actions` 或信任 screenshot prose 绕过；
- NativeSkillRouter 完整命中的本地能力仍可按其自身 executor/verifier 合同运行。

这意味着实验 driver 不是默认 `windows_uia` 的等价替换。只有未来上游/adapter 提供可校验的结构化元素身份，并重新完成 generation、focus、Unicode 与 false-before/true-after 验收后，才可扩大声明。

## 验收要求

`computer-doctor --live` 当前只支持 `local_agent/windows_uia`，不能替本 driver 背书。实验验收必须单独记录：

1. 固定上游版本确为 0.2.3；
2. MCP 初始化一次，required tools 无缺失；
3. 同一进程完成 list/observe/action/observe；
4. 中文 app 名、窗口标题和 accessibility tree 无损；
5. 在项目自有、无敏感、可回滚 fixture 中做中英混合 token round-trip；
6. exact text 可从 fresh observation 读回；
7. 缺少结构化 elements 时，planner 不捏造 index，点击/导航受限且文本/焦点动作 fail closed；
8. replacement character 和前后空白 case 确实 fail closed；
9. 旧 generation、重复 action 和 coordinate action 被拒绝；
10. 断管/timeout 后不重试 mutating action；
11. 手工核对屏幕后置条件。

Issue #5/PR #6 未解决时，即使某台机器通过，也只能记录该机器/PowerShell/locale/上游版本的实验结果，不得写成一般中文支持承诺。

## 数据边界

MCP server 在当前用户上下文运行，能观察所选窗口的 accessibility tree，并可能取得 screenshot PNG。0.3 safety 会在调用单步 planner 前重建 task-authorized view，不发送原始 accessibility text、PNG bytes 或真实截图可用性；由于该 driver 没有结构化 elements，可发送的 UI 控件子集通常为空。MCP 进程本身仍在本机接触屏幕内容。

上游日志、PowerShell 子进程、npm 缓存和未来版本行为不由 HandsFreePC 控制。不要在密码、付款、UAC、终端或敏感数据窗口上测试。HandsFreePC 本地 safety 会对被已知词形/元素属性识别出的这些 surface fail closed，但词表不能覆盖未知语言、自绘控件或伪装界面，不是完整 DLP。

## 回退

出现乱码、缺失元素、unknown outcome 或无法做 Unicode round-trip 时，停止常驻进程并改回：

```yaml
computer_control:
  backend: local_agent
  driver: windows_uia
  allow_experimental_driver: false
```

不要改为 `legacy_codex_cli` 来绕过错误；旧 controller 没有 0.3 的本地动作验收。
