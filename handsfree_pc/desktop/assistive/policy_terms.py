"""Static vocabulary for assistive policy classification.

Keeping vocabulary separate leaves ``policy.py`` focused on executable policy,
binding, expiry, and precedence rather than a long list of localized labels.
"""

DEFAULT_SEND_POLICY = {
    "claude": "auto",
    "codex": "auto",
    "chatgpt": "auto",
    "wechat": "confirm",
    "weixin": "confirm",
    "微信": "confirm",
}

TERMINAL_APPS = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "windowsterminal",
        "windowsterminal.exe",
        "terminal",
        "wt",
        "wt.exe",
        "conhost",
        "conhost.exe",
        "bash",
        "wsl",
        "wsl.exe",
        "ubuntu",
        "ubuntu.exe",
    }
)

TERMINAL_CONTROL_NAMES = frozenset(
    {
        "terminal",
        "integrated terminal",
        "shell",
        "console",
        "command prompt",
        "powershell",
        "终端",
        "命令行",
        "控制台",
    }
)

TERMINAL_AUTOMATION_MARKERS = ("terminal", "console", "shell")
TERMINAL_TITLE_PREFIXES = (
    "powershell",
    "windows powershell",
    "windows terminal",
    "command prompt",
    "cmd",
    "cmd.exe",
    "ubuntu",
    "wsl",
    "命令提示符",
    "管理员: windows powershell",
    "administrator: windows powershell",
)

HARD_BLOCK_TERMS = (
    "password",
    "passcode",
    "credential",
    "sign in",
    "log in",
    "login",
    "authenticate",
    "authentication",
    "verification code",
    "two-factor",
    "2fa",
    "one-time password",
    "otp",
    "windows security",
    "windows defender",
    "user account control",
    "privacy settings",
    "security settings",
    "payment",
    "checkout",
    "bank transfer",
    "wire transfer",
    "密码",
    "口令",
    "验证码",
    "登录",
    "认证",
    "双重验证",
    "安全验证",
    "windows 安全",
    "用户帐户控制",
    "用户账户控制",
    "安全设置",
    "隐私设置",
    "支付",
    "付款",
    "转账",
    "收银台",
)
SEND_TERMS = ("send", "submit", "post", "publish", "reply", "发送", "提交", "发布", "回复")
DELETE_TERMS = (
    "delete",
    "remove",
    "erase",
    "overwrite",
    "replace existing",
    "discard changes",
    "删除",
    "移除",
    "清空",
    "覆盖",
    "替换已有",
    "放弃更改",
    "丢弃更改",
)
INSTALL_TERMS = ("install", "uninstall", "setup", "安装", "卸载")
UPLOAD_TERMS = ("upload", "share", "attach", "上传", "分享", "共享", "添加附件")
CLOSE_TERMS = ("close", "exit", "quit", "关闭", "退出")
UNSAVED_TERMS = (
    "unsaved",
    "not saved",
    "save changes",
    "don't save",
    "do not save",
    "未保存",
    "尚未保存",
    "保存更改",
    "不保存",
)
EXECUTABLE_TERMS = (
    ".exe",
    ".msi",
    ".bat",
    ".cmd",
    ".ps1",
    "run application",
    "launch program",
    "运行程序",
    "启动程序",
)
