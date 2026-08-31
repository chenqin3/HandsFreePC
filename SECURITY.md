# HandsFreePC 安全政策

感谢你帮助改进 HandsFreePC。这个项目会访问麦克风并操控当前 Windows 桌面，请不要在公开 Issue 中粘贴录音、转写、用户名、本机路径、窗口截图、令牌或可直接利用的漏洞细节。

## 支持范围

HandsFreePC 当前处于 alpha：

| 版本 | 安全修复 |
|---|---|
| 最新 `main` | 支持 |
| 较旧提交、个人 fork、修改后的模型或非官方安装包 | 不保证；请先在最新版复现 |
| Windows 10、WSL/Linux/macOS、管理员/UIAccess 模式 | 不在当前支持边界 |

安全更新可能包含不兼容的配置或应用档案变化。发布后请重新运行单元测试和目标应用的 opt-in live smoke test。

## 私密报告漏洞

首选使用 GitHub 的 [Private Vulnerability Reporting](https://github.com/chenqin3/HandsFreePC/security/advisories/new)。GitHub 对该功能的说明见 [Privately reporting a security vulnerability](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/privately-reporting-a-security-vulnerability)。

如果私密报告页面在新仓库上暂时不可用，请只创建一个**不含漏洞细节和个人数据**的公开 Issue，标题写“Request private security contact”。维护者建立私密渠道前，请不要发送 PoC、日志、音频或利用步骤。

报告中请尽量包含：

- 受影响版本、commit 和 Windows 版本；
- 安装方式、Python 版本、相关依赖和目标应用版本；
- 预期行为与实际行为；
- 最小、脱敏、可重复的步骤；
- 是否需要真实麦克风、云 planner 或 live UI 控制；
- 影响：错误窗口输入、未授权外发、音频/转写持久化、命令/路径注入、权限越界等；
- 你建议的缓解方式（如有）。

请用占位符代替真实路径和名称，例如 `C:\Users\USER\PRIVATE`、`PROJECT_A`、`TOKEN_REDACTED`。不要上传真实婴儿/旁人声音；可以用合成音频或书面描述复现。

## 响应与披露

维护目标（不是法律或服务等级承诺）：

- 7 天内确认收到；
- 14 天内给出初步影响判断或请求补充；
- 与报告者协调修复、测试和披露时间；
- 高影响问题优先发布修复，再公开足以帮助用户升级的说明。

在修复发布前，请保持细节私密。维护者会在致谢前征求报告者对署名方式的意见。

项目目前不提供漏洞奖金，也不授权测试不属于你的电脑、账户、订阅或数据。请避免持久化访问、破坏数据、向真实第三方发送内容、绕过提供商限制或收集旁人信息。

## 我们重点关注的安全问题

- 未唤醒或未确认仍执行动作；
- ASR/planner 文本导致 shell、PowerShell、脚本或任意坐标执行；
- planner 关闭时仍发送网络请求，或上传原始音频；
- Codex/Claude planner 继承秘密、加载不应加载的插件/MCP，或绕过 JSON Schema；
- 文本被输入到错误窗口、密码框或更高权限应用；
- 听写内容被意外提交到 Codex、Claude 或其他外部服务；
- 静态“确认执行”、旧录音或错误四位码仍可授权本轮动作；本次 `VoiceRuntime` 进程内已签发码被跨动作复用，或取消/超时后被回收再次签发；
- 路径遍历、符号链接/junction、文件替换或危险扩展名绕过确认；
- 音频、转写、截图、完整路径、令牌或登录缓存被写入日志/Git；
- 锁屏、UAC、安全桌面或切换用户后仍继续执行；
- 依赖、模型下载或更新流程存在供应链替换风险；
- 大字遮罩抢焦点，导致后续输入落入错误控件；
- TTS 自触发造成循环或自动执行。

普通识别精度问题、第三方应用 UI 更新导致选择器失效、需要本机安装模型，以及已明确记录的功能缺失，通常是普通 bug；如果它们会稳定造成未授权动作、外发或数据泄漏，则按安全问题报告。

## 部署者安全清单

- 从官方仓库拉取代码，核对 release/tag；不要运行来源不明的整合包。
- 使用普通 Windows 用户权限，不要“以管理员身份运行”，不要配置 UIAccess。
- 从项目文档列出的官方入口下载模型，保留上游 README/LICENSE；发布方应提供并核对哈希。
- 先复制 `config.example.yaml` 为不纳入 Git 的本地配置；不要在 YAML 中写令牌。
- 保持 `save_audio: false`、`save_transcripts: false` 和 `allow_cloud_planner: false`，除非你理解具体影响。
- 只把确实需要的目录放进 `search_roots`；常用目录优先设置明确别名。
- planner 如需启用，默认使用严格的 Claude CLI adapter，并用单独测试命令确认输出、超时、网络断开和提供商账户数据设置。
- Codex CLI 只可作为 `codex_cli_best_effort` 显式备选，并必须设置 `allow_codex_cli_host_read: true`。项目会尽量禁用已知工具，但订阅 CLI 没有完整 no-tools 保证；`read-only` sandbox 也并非主机文件保密边界。敏感电脑保持 planner 关闭。
- 顶层 `planner.enabled` 是旧单句 cloud fallback，不是 desktop step planner。其输出仅可包含用户原句肯定、非引号/数据引用且精确授权的应用内导航；反馈、暂停/恢复/等待、路径、文本和发送动作一律由本地 parser 决定，云输出提出即阻断。
- 旧 `legacy_codex_cli` 还必须另设 `allow_legacy_codex_computer_use: true`；它没有 0.3 本地动作 verifier，不得作为新部署的可信完成路径。
- 普通 `doctor` 不运行提供商认证检查；只有理解其可能联网并会显示诊断路径后，才使用 `doctor --check-planner-auth`，分享输出前先脱敏。
- 每次 Codex/Claude/Windows 更新后先运行 dry-run 和 live smoke test，再允许听写或发送。
- `strict` 要求每条通用任务在原句中肯定且唯一明确指定一个应用；`personal_trusted` 仅可在同一控制器会话内沿用上一条已 fresh-verified 的应用/窗口。每个通用 planner 动作都必须有可本地检查的后置条件，并验证 false-before/true-after；无法建立这一证据时停止。确定性 native skill 使用动作特定证据，精确状态已成立时可幂等成功。
- `strict` 的通用 `type_text`/`set_value` 要等待随机四位一次性口令；`personal_trusted` 只免确认写入本句完整口述的未发送草稿到唯一聚焦非密码输入框。发送及其他副作用仍需确认，静态“确认执行”不授权。随机码不能替代说话人识别或人工看屏幕，旁人、扬声器和实时转述/重放仍可能捕获本轮口令。
- 四位码只保证当前 `VoiceRuntime` 进程运行期内不再签发；取消、超时和成功使用都不回收，有界重抽耗尽时必须拒绝。去重集合不持久化，重启后不保证绝对不复用，四位码不是持久化防重放凭证。
- 点击/按键 surface 的发送、删除、安装、上传/分享、关闭等确认依赖已识别的本地词形和上下文，不是完整语义分类器；未知语言、同义词、自绘控件或伪装文案可能漏分，重要副作用必须人工监督。认证、密码属性、聚焦 secret/API-key 输入、付款、UAC 和 OS 安全 surface 仍 fail closed；聊天/文档内容里仅出现这些词或示例凭据不会阻断无关安全导航，且内容节点不会发送给 planner。
- 通用 UI confirmation 摘要只可原文回显用户原句中已验证的 exact target label；未授权 sibling/window label 的原文和语义只在本地完整快照中分类，不进入摘要，摘要里的短 digest 仅作不可逆绑定元数据。
- 旧单句确认绑定完整 plan/source 的规范深快照，不与返回给调用方的可变 `Action` 共享引用；已解析路径还绑定规范绝对路径和 stat 身份，普通文件再绑定 SHA-256。确认时必须 re-prepare、重新 safety、重建独占执行快照并重新 binding；安全目录无需确认时，runtime 和 deterministic native router 也必须执行 safety 前后双 binding。Windows 路径在最后绑定到执行/后置检查期间拒绝并发写入或删除共享，任一变化即取消。
- 确认遮罩不抢焦点；测试通知弹窗、窗口切换、锁屏、UAC、管理员 Notepad 和密码框。
- 不要关闭确认、扩大动作 Schema 或加入 `shell=True`、任意快捷键/坐标，只为“让一次 demo 跑通”。

## 依赖与模型漏洞

报告第三方问题时，请同时给出上游项目和版本。HandsFreePC 会在本项目可控范围内升级、钉住版本或增加缓解；上游修复仍应由相应维护者负责。

模型文件不提交到本仓库。Vosk 模型页将 `vosk-model-small-cn-0.22` 标为 Apache-2.0，但其 zip 可能只带 README；再分发时必须同时保留/引用 [Vosk v0.3.45 COPYING](https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING) 和模型来源。SenseVoice 权重受 [FunASR Model License](https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE) 约束，不能按 sherpa-onnx 运行时代码的 Apache-2.0 许可来替代说明。

## 安全设计文档

实现者和审计者应先阅读：

- `docs/SECURITY_MODEL.md`：资产、信任边界、威胁、控制和残余风险；
- `docs/ARCHITECTURE.md`：状态机、planner 隔离、动作 Schema 与执行核验；
- `PRIVACY.md`：本地音频与可选云文本边界；
- `docs/RESEARCH.md`：已核实事实和工程选型依据。

---

**English summary:** Please report vulnerabilities through the repository's private vulnerability reporting page. Do not post recordings, transcripts, local paths, tokens, screenshots, or exploit details publicly. HandsFreePC is an alpha project; only the latest `main` on Windows 11 is supported.
