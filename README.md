# HandsFreePC

抱着孩子、双手腾不出来的时候，用嘴控制 Windows：本地离线听懂你说的话，把每一条指令交给 [Kimi Code CLI](https://moonshotai.github.io/kimi-code/) 在桌面上做完，屏幕大字或语音告诉你结果。

```text
麦克风
  -> Silero VAD + 中文 Vosk 控制词检测（开始 / 结束 / 停止 / 恢复）+ 英文 Vosk「over」检测
  -> SenseVoice 或 faster-whisper 转写指令正文（全部本地）
  -> 「开始语音操作」进入会话；每条指令以 over 结束，进入有界 FIFO 队列
  -> kimi -p "<前言 + 指令>"：Kimi 加载你的 gui-control 技能，截图 -> 看图 -> pyautogui 点击/粘贴 -> 再截图核对
  -> 屏幕大字 / 语音播报反馈；「结束语音操作」排空队列，「电脑停止」立即取消
```

项目里没有 UI Automation 驱动、没有规则解析器、没有确认口令：怎么找窗口、怎么点、怎么核对，全部由 Kimi 用它的技能决定。这个仓库只负责三件事：把话听准、按顺序交给 Kimi、把结果告诉你。

> [!WARNING]
> 这是给自己电脑用的个人工具。Kimi 是云端模型：指令文本会离开本机，Kimi 自己截的图和工具输出也会发给模型；Kimi 在 `-p` 模式下不经确认就执行工具调用。只在你自己的账号、自己的电脑上使用，并在 [PRIVACY.md](PRIVACY.md) 里了解边界。

## 需要什么

- Windows 11，64 位 Python 3.11 或 3.12，一个麦克风。NVIDIA GPU 可选，用于 faster-whisper。
- 已登录的 Kimi Code CLI（`kimi.exe`），模型要能看图（默认的 `kimi-code/k3` 可以）。
- Kimi 的用户技能目录里有一个 `gui-control` 技能（`%USERPROFILE%\.kimi-code\skills\gui-control\SKILL.md`），教 Kimi 怎么用截图和 pyautogui 操作桌面。见下面「gui-control 技能」。

## 安装

```powershell
pwsh scripts/install.ps1 -WithWhisper -DownloadModels
```

脚本会创建 `.venv`、安装依赖、把 `config.example.yaml` 复制成 `config.local.yaml`（不会提交）、下载本地语音模型，最后跑一次 `doctor`。手动等价步骤：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[audio,windows,whisper,dev]"
.venv\Scripts\python.exe -m handsfree_pc init
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml download-models --directory models
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml doctor --strict --check-kimi
```

`doctor --strict` 只有在模型齐全、有输入设备、找得到 `kimi.exe`、找得到 `gui-control` 技能时才返回 0。

## 配置

只有四个段，完整默认值见 [config.example.yaml](config.example.yaml)，缺省键沿用默认，旧版本多出来的段会被忽略。

```yaml
app:
  wake_phrases: ["开始语音操作"]
  end_session_phrases: ["结束语音操作"]
  stop_phrases: ["立即停止所有操作", "取消所有操作", "停止所有操作", "电脑停止"]
  resume_phrases: ["恢复语音操作", "恢复监听", "继续队列", "恢复队列"]
  prompt_delimiters: ["over"]
  strict_wake_phrase: true      # 唤醒词必须被正文转写确认、且在句首；聊天不会误触发
  feedback_mode: overlay        # overlay | voice | both | silent
  failure_policy: continue      # 一条失败后，后面的继续（continue）还是等你说「继续队列」（pause）
  max_queue_size: 8
  auto_pause_when_microphone_busy: true

privacy:
  save_transcripts: false       # true 时把本地识别原文写到 %LOCALAPPDATA%\HandsFreePC\transcripts

speech:
  command:
    backend: faster-whisper     # 或 sensevoice（更快，默认）
    model: large-v3-turbo
    device: cuda                # 没有 NVIDIA 就保留 auto
    compute_type: float16

kimi:
  executable: kimi              # 或 kimi.exe 的完整路径
  working_directory: null       # Kimi 的工作目录，null 是你的主目录
  model: null                   # null 用 CLI 默认模型
  timeout_seconds: 600          # 一条指令的上限；发文件这类任务实测 2 到 4 分钟
  preamble_file: null           # 想改 Kimi 收到的前言就指向一个文本文件
```

控制词改了之后，要把新词按「字 词 之间 加 空格」的形式加进 `speech.wake.grammar`，离线检测器只认词表里的话。

## 运行

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml run
```

或者 `pwsh scripts/run.ps1`（先跑 `doctor --strict` 再启动）。同一时间只会有一个监听进程，第二个会直接退出。

**开机自启**：

```powershell
pwsh scripts/install_autostart.ps1 -StartNow
```

注册一个登录触发的计划任务 `HandsFreePC`（延迟 20 秒，运行在桌面会话里，退出后自动重启），动作是 `.venv\Scripts\pythonw.exe -m handsfree_pc.cli --config config.local.yaml run`，没有窗口，输出追加到 `%LOCALAPPDATA%\HandsFreePC\logs\run.log`。`Stop-ScheduledTask HandsFreePC` / `Start-ScheduledTask HandsFreePC` 随时停止和重新开始；`pwsh scripts/uninstall_autostart.ps1` 注销任务并结束监听进程。

## 怎么说

| 说什么 | 发生什么 |
|---|---|
| 开始语音操作 | 进入会话，开始接收指令。也可以一口气说「开始语音操作 打开微信 over」。唤醒分两级：离线词表先听到，再由正文转写模型确认这句话确实以「开始语音操作」开头；聊天里带到这几个字（"我们开始语音操作吧"）、否定（"不要开始语音操作"）、转述和引用都不算 |
| …… over | 一条指令结束，进入队列。over 由独立的英文离线模型检测，不依赖正文转写 |
| 结束语音操作 | 不再接收新指令，已入队的继续做完；没说 over 的半条丢弃 |
| 立即停止所有操作 / 电脑停止 | 立刻终止正在执行的 Kimi、清空队列 |
| 继续队列 / 恢复语音操作 | 队列因失败暂停时继续；或从停止状态重新开始 |
| 切换到语音反馈 / 切换到屏幕反馈 / 大字和语音两种都开 / 切换到静默模式 | 换反馈方式，不经过 Kimi |

指令本身怎么说都行，Kimi 会拿转写文本当线索去对真实清单（窗口标题、侧栏会话名、目录里的文件名、你的项目地图），比如：

- 「切换到微信，找到文件传输助手 over」
- 「把下载文件夹里最新的那个 html 发给微信文件传输助手 over」
- 「去 Chrome 打开 ChatGPT，开一个新对话，输入一个测试问题，但是不要发送 over」
- 「打开 Claude，在 Code 页签里找到那个写论文的会话 over」

说了「不要发送」的内容 Kimi 绝不会按 Enter；Kimi 不会反问，拿不准就选最像的一个做到发送前。执行期间麦克风继续开着，可以接着说下一条，也可以随时说「电脑停止」。

## 屏幕与语音反馈

`app.feedback_mode`：`overlay` 在屏幕上方显示大字，不抢焦点也不挡点击；`voice` 用 Windows 语音朗读；`both` 两者都开；`silent` 只显示错误。语音播报会等你这句话说完再放，不会打断你。

提示框的大小随文字自适应，一行状态是一个紧凑的小框，最宽不超过屏幕的 72%。启动提示只显示 6 秒；「第 N 条已交给 Kimi 执行」会随着 Kimi 每一步工具调用更新，完成或失败后换成结果，桌面上不会留下常驻横幅。

## 麦克风避让

监听常开时，一旦别的程序（Zoom、Teams、腾讯会议、微信通话、浏览器里的会议……）开始采集麦克风，运行时会在约 3 秒内释放麦克风、暂停转写和语音播报，屏幕提示「X 正在使用麦克风，已暂停监听」；对方释放后 3 秒内自动恢复。判断依据是 Windows 自己的麦克风使用记录（`CapabilityAccessManager\ConsentStore\microphone` 注册表），不探测也不占用别的设备。`app.microphone_guard_ignore` 里可以列出不算抢占的程序名，例如 `["obs64.exe"]`。

## 不用麦克风试一条

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml exec "打开下载文件夹"
```

和语音路径完全相同的前言、超时和结果解析；stderr 上能看到 Kimi 每一步调用的工具，stdout 是 JSON 结果（成功与否、Kimi 的一句话说明、最终截图路径）。

## gui-control 技能

运行时交给 Kimi 的前言只说「用 gui-control 技能的方法做」，具体怎么做写在你的技能里。一个能用的技能至少包含：

- 环境：一个装了 `pyautogui`、`pyperclip` 的 venv，和几个小脚本（枚举窗口、用 AttachThreadInput 把窗口置顶后截图、粘贴文字）。
- 套路：截图 → 用 ReadMediaFile 看图算坐标（注意模型看到的是降采样图，要按比例乘回去）→ 点击/按键 → 每步截图核对。
- 中文必须走剪贴板粘贴；文件用 `Set-Clipboard -Path` 再 Ctrl+V；`pyperclip.copy` 会覆盖剪贴板里的文件，顺序要对。
- 各应用的约定：微信 4.x 的搜索框和「文件传输助手」；Codex 在 ChatGPT 桌面端里、搜索用 Ctrl+K、输入框紧挨模型选择器；Claude 桌面端有 Chat and Cowork / Code 两个页签，没特别说明用 Code。
- 意图定位：转写文本只是线索，去对真实清单做模糊匹配；「最新的」按修改时间取第一；后面补充的条件（「不要发送」）推翻前面的。
- 结尾两行固定格式，运行时靠它判断成败：`RESULT: <成功|失败> - <一句话>` 和 `SCREENSHOT: <路径>`。

技能里可以引用你自己的项目地图（例如一份按项目整理的目录索引），Kimi 会先查索引再按修改时间找文件，所以「打开 g 盘那个数据库文件夹」这种缩写也能定位。

## 日志与排查

- 事件日志：`%LOCALAPPDATA%\HandsFreePC\logs\handsfreepc.jsonl`，固定字段，不含指令原文、路径或屏幕内容。`handsfreepc logs --tail 50`、`handsfreepc diagnose-last`。
- 自启动的标准输出：`%LOCALAPPDATA%\HandsFreePC\logs\run.log`。
- 识别原文（需 `privacy.save_transcripts: true`）：`handsfreepc transcripts --tail 20`。
- Kimi 每次运行的工具调用序列保存在进程内的 `KimiRun.tool_log`，`exec` 会把工具名打印出来。

常见问题见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)，模块划分见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，测试方法见 [docs/TESTING.md](docs/TESTING.md)。

## 开发

```powershell
.venv\Scripts\python.exe -m pytest -q -m "not live"
.venv\Scripts\python.exe -m ruff check handsfree_pc tests
```

## 许可

MIT，见 [LICENSE](LICENSE)。语音模型的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
