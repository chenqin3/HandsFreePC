# HandsFreePC 安全政策

这个项目会常开麦克风，并把你的指令交给一个云端模型（Kimi）在你的桌面上执行。请不要在公开 Issue 中粘贴录音、转写、用户名、本机路径、窗口截图、令牌或可直接利用的漏洞细节。

## 支持范围

| 版本 | 安全修复 |
|---|---|
| 最新 `main` | 支持 |
| 较旧提交、个人 fork、修改后的模型或非官方安装包 | 不保证；请先在最新版复现 |
| Windows 10、WSL/Linux/macOS、管理员权限运行 | 不在支持边界 |

## 私密报告漏洞

首选 GitHub 的 [Private Vulnerability Reporting](https://github.com/chenqin3/HandsFreePC/security/advisories/new)。如果该页面不可用，请只创建一个**不含漏洞细节和个人数据**的公开 Issue，标题写 "Request private security contact"，等维护者建立私密渠道后再发送细节。

报告中请尽量包含：受影响 commit 与 Windows 版本、安装方式与 Python 版本、预期与实际行为、最小且脱敏的复现步骤、是否需要真实麦克风或 Kimi 账号、影响范围和你建议的缓解方式。请用占位符代替真实路径和名称，不要上传真实的家人或旁人声音。

维护目标（不是服务等级承诺）：7 天内确认收到，14 天内给出初步判断，与报告者协调修复和披露时间。项目不提供漏洞奖金，也不授权测试不属于你的电脑、账户或数据。

## 我们重点关注的安全问题

- 没有说唤醒词、或说了「电脑停止」之后仍有指令被交给 Kimi 执行；
- 控制词（结束、停止、恢复、切换反馈）被当成普通指令发给了 Kimi；
- 指令原文、路径、窗口标题、截图或令牌被写进事件日志或提交到 Git；
- 「不要发送」的内容仍被发送（前言或结果解析的缺陷，而非模型判断失误）；
- 队列顺序错乱、停止后旧会话的结果污染新会话、同一时间跑起两个监听进程；
- 麦克风避让失效：别的程序在采集时本项目仍占用或抢回麦克风；
- 模型下载或依赖存在供应链替换风险；
- 提示框抢焦点导致输入落进错误控件，或 TTS 自触发循环。

Kimi 本身对目标的判断错误（点错窗口、选错文件）属于普通 bug；除非它稳定地绕过上面的边界，否则不按安全问题处理。

## 部署者清单

- 用普通 Windows 用户权限运行，不要「以管理员身份运行」。
- 只在你自己的 Kimi 账号和自己的电脑上使用；执行期间屏幕上有什么，Kimi 就可能看到什么。
- `config.local.yaml` 不进 Git；`privacy.save_transcripts` 默认关闭。
- 从官方入口下载模型并保留上游许可；`download-models` 会核对固定的 SHA-256。
- 每次 Kimi CLI 或 Windows 大更新后先用 `handsfreepc exec` 跑一条无副作用的指令再恢复语音使用。

## 依赖与模型

报告第三方问题时请同时给出上游项目和版本。模型许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

**English summary:** Report vulnerabilities through the repository's private vulnerability reporting page; never post recordings, transcripts, local paths, tokens, screenshots, or exploit details publicly. HandsFreePC is a personal tool: local offline speech recognition plus one executor, Kimi Code CLI, which runs tool calls without confirmation on the user's own desktop. Only the latest `main` on Windows 11 is supported.
