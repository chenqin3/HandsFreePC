# 测试指南

## 单元测试

```powershell
.venv\Scripts\python.exe -m pytest -q -m "not live"
.venv\Scripts\python.exe -m ruff check handsfree_pc tests
```

覆盖：配置加载与校验、`over` 分段与 FIFO、会话状态机、Kimi stream-json 解析与超时/取消、提示框尺寸、麦克风避让的注册表判断、CLI 命令。没有任何测试会打开麦克风、弹窗或启动 Kimi。

如果 `%TEMP%\pytest-of-<用户>` 目录拒绝访问，加 `--basetemp .pytest-tmp`。

## 静态检查

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml doctor --strict --check-kimi
```

返回 0 表示：模型齐全、有输入设备、`kimi.exe` 找得到、`gui-control` 技能找得到。

## 不开麦克风的端到端

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml exec "打开下载文件夹"
```

走的是和语音一模一样的路径（前言、超时、结果解析）。stderr 是 Kimi 的逐步工具调用，stdout 是 JSON。建议的验收指令（用你自己电脑上真实存在的目标替换）：

- 打开某个项目目录：「打开 D 盘那个数据库文件夹」（缩写、同音字也应能定位）
- 微信：「切换到微信，打开文件传输助手」「把下载文件夹里最新的 html 发给文件传输助手」
- Codex / Claude：「在 Claude 的 Code 页签打开某个会话，输入一句话但不要发送」
- Chrome：「打开 ChatGPT 网页，新建对话，输入测试问题，不要发送」

每条都看最后的截图，确认「不要发送」的内容只在输入框里、没进聊天记录。

## 语音链路

```powershell
.venv\Scripts\python.exe -m handsfree_pc --config config.local.yaml run
```

依次说：「开始语音操作」→ 看到「持续语音操作已开始」→ 「打开记事本 over」→ 看到「已入队 1 条」→ 等结果 → 「结束语音操作」。听不到 `over` 时先看 `doctor` 里 `models.delimiter.ready`。

## 麦克风避让

用本机 python 模拟别的程序开麦测不出来（Windows 把本程序自己的采集也记在同一个解释器名下）。用 Chrome 模拟浏览器会议：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=$env:TEMP\hfpc-mic --use-fake-ui-for-media-stream file:///path/to/mic.html
```

`mic.html` 里调用 `navigator.mediaDevices.getUserMedia({audio:true})` 并持有 25 秒后 `stop()`。事件日志里应在几秒内出现 `MICROPHONE_GUARD_PAUSED`，释放后出现 `MICROPHONE_GUARD_RESUMED`。

## 开机自启

```powershell
pwsh scripts/install_autostart.ps1 -StartNow
Get-ScheduledTask HandsFreePC | Select State
```

状态应为 Running，事件日志出现 `MICROPHONE_READY`；`Stop-ScheduledTask HandsFreePC` 后 python 进程应立即消失。
