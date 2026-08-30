from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from .protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    contains_credential_like_value,
    is_allowed_desktop_key,
)


class DesktopSafetyDisposition(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class DesktopConfirmation:
    """An exact, action-and-observation-bound confirmation request."""

    confirmation_id: str
    action_digest: str
    summary: str


@dataclass(frozen=True, slots=True)
class DesktopSafetyResult:
    disposition: DesktopSafetyDisposition
    reason: str
    confirmation: DesktopConfirmation | None = None

    @property
    def allowed(self) -> bool:
        return self.disposition == DesktopSafetyDisposition.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.disposition == DesktopSafetyDisposition.CONFIRM


_TERMINAL_TERMS = (
    "windows terminal",
    "command prompt",
    "powershell",
    "pwsh",
    "cmd.exe",
    "cmd",
    "terminal",
    "shell",
    "shell input",
    "console",
    "developer console",
    "command line",
    "commandline",
    "repl",
    "git bash",
    "bash",
    "zsh",
    "fish shell",
    "mingw64",
    "wsl",
    "windows subsystem for linux",
    "命令提示符",
    "终端",
    "控制台",
    "命令行",
    "交互解释器",
)
_RUN_IDENTITY_TERMS = ("run", "windows run", "run dialog", "运行", "运行对话框")
_UAC_TERMS = (
    "user account control",
    "do you want to allow this app",
    "administrator approval",
    "windows security",
    "microsoft defender smartscreen",
    "windows protected your pc",
    "open file security warning",
    "security warning",
    "unknown publisher",
    "windows defender firewall",
    "windows firewall",
    "blocked some features",
    "allow access",
    "run anyway",
    "protected view",
    "enable editing",
    "enable content",
    "macros have been disabled",
    "your connection is not private",
    "your connection isn't private",
    "net err cert authority invalid",
    "proceed to unsafe site",
    "deceptive site ahead",
    "malware ahead",
    "security risk",
    "blocked macros from running",
    "threat found",
    "proceed anyway",
    "this file may harm your computer",
    "keep anyway",
    "dangerous file",
    "virus & threat protection",
    "virus and threat protection",
    "allow on device",
    "certificate warning",
    "用户账户控制",
    "是否允许此应用",
    "管理员批准",
    "windows 安全",
    "windows 已保护你的电脑",
    "安全警告",
    "未知发布者",
    "windows defender 防火墙",
    "windows 防火墙",
    "已阻止此应用的部分功能",
    "允许访问",
    "仍要运行",
    "受保护的视图",
    "启用编辑",
    "启用内容",
    "宏已禁用",
    "连接不是私密连接",
    "仍然继续",
    "此文件可能会损害您的计算机",
    "仍然保留",
    "危险文件",
    "病毒和威胁防护",
    "允许在设备上",
    "证书警告",
)
_AUTH_SURFACE_TERMS = (
    "sign in",
    "sign up",
    "sign into",
    "sign into your account",
    "log in",
    "log on",
    "login",
    "register",
    "create account",
    "create your account",
    "verify your identity",
    "use another account",
    "choose an account",
    "select an account",
    "continue with google",
    "continue with apple",
    "continue with microsoft",
    "continue with github",
    "continue with email",
    "continue with phone",
    "single sign-on",
    "sso",
    "oauth",
    "oauth authorization",
    "authentication",
    "account verification",
    "use a passkey",
    "magic link",
    "send me a magic link",
    "authorization required",
    "authorize",
    "connect account",
    "verify account",
    "confirm your email",
    "grant permission",
    "continue as",
    "permissions requested",
    "consent required",
    "approve access",
    "登录",
    "注册",
    "创建账户",
    "创建账号",
    "使用其他账户",
    "使用其他账号",
    "选择账户",
    "选择账号",
    "身份验证",
    "验证身份",
    "账户验证",
    "需要授权",
    "授权",
    "连接账户",
    "验证账户",
    "确认邮箱",
    "授予权限",
)
_AUTH_WELCOME_TERMS = ("welcome back", "欢迎回来")
_AUTH_IDENTITY_FIELD_TERMS = ("email address", "user name", "username", "电子邮件地址", "用户名")
_SECRET_TERMS = (
    "password",
    "passcode",
    "credential",
    "one-time code",
    "security code",
    "verification code",
    "recovery code",
    "recovery codes",
    "backup code",
    "backup codes",
    "otp",
    "2fa",
    "mfa",
    "authenticator app",
    "pin",
    "passkey",
    "passphrase",
    "secret key",
    "private key",
    "ssh key",
    "client secret",
    "session cookie",
    "auth token",
    "seed phrase",
    "recovery phrase",
    "secret phrase",
    "mnemonic phrase",
    "api key",
    "access token",
    "totp code",
    "recovery key",
    "bearer token",
    "密码",
    "口令",
    "凭据",
    "验证码",
    "安全码",
    "恢复码",
    "备用码",
    "通行密钥",
    "密码短语",
    "身份验证器",
    "助记词",
    "恢复短语",
    "私钥",
    "客户端密钥",
    "会话 Cookie",
    "身份令牌",
    "SSH 密钥",
    "密钥",
)
_PAYMENT_TERMS = (
    "pay",
    "payment",
    "pay now",
    "buy now",
    "purchase",
    "confirm purchase",
    "order now",
    "confirm order",
    "checkout",
    "place order",
    "place your order",
    "buy",
    "subscribe",
    "subscription",
    "confirm subscription",
    "billing",
    "billing information",
    "payment method",
    "make payment",
    "complete payment",
    "confirm payment",
    "apple pay",
    "google pay",
    "card number",
    "card details",
    "cvc",
    "cvv",
    "expiry date",
    "expiration date",
    "withdraw",
    "add card",
    "debit card",
    "bank transfer",
    "wire transfer",
    "credit card",
    "paypal",
    "cash out",
    "add funds",
    "send money",
    "donate",
    "donation",
    "tip",
    "支付",
    "付款",
    "购买",
    "下单",
    "转账",
    "银行卡",
    "信用卡",
    "借记卡",
    "账单",
    "付款方式",
    "立即购买",
    "确认购买",
    "确认订单",
    "提交订单",
    "结算",
    "订阅",
    "捐赠",
    "小费",
    "汇款",
    "充值",
    "提现",
    "卡号",
    "卡片信息",
    "有效期",
)
_SEND_TERMS = (
    "send",
    "forward",
    "reshare",
    "retweet",
    "submit",
    "publish",
    "post",
    "reply",
    "comment",
    "unpublish",
    "repost",
    "发送",
    "转发",
    "提交",
    "发布",
    "回复",
    "评论",
)
_DELETE_TERMS = (
    "delete",
    "remove",
    "erase",
    "purge",
    "wipe",
    "destroy",
    "clear",
    "clear all",
    "trash",
    "move to bin",
    "move to recycle bin",
    "move to trash",
    "permanently delete",
    "删除",
    "移除",
    "清空",
    "移到回收站",
    "移动到回收站",
    "永久删除",
    "彻底清除",
    "销毁",
)
_INSTALL_TERMS = (
    "install",
    "uninstall",
    "upgrade",
    "update",
    "安装",
    "卸载",
    "升级",
)
_UPLOAD_TERMS = (
    "upload",
    "attach",
    "attachment",
    "add attachment",
    "add file",
    "add files",
    "choose file",
    "choose files",
    "browse files",
    "import file",
    "import files",
    "add from computer",
    "attach file",
    "share file",
    "share",
    "sharing",
    "invite people",
    "invite",
    "grant access",
    "上传",
    "附件",
    "附加",
    "添加附件",
    "添加文件",
    "浏览文件",
    "导入文件",
    "从电脑添加",
    "附加文件",
    "共享文件",
    "共享",
    "分享",
    "邀请",
)
_CLOSE_TERMS = (
    "close",
    "quit",
    "exit",
    "dismiss",
    "cancel",
    "discard",
    "end task",
    "terminate",
    "force quit",
    "kill process",
    "force stop",
    "shut down",
    "shutdown",
    "关闭",
    "退出",
    "取消",
    "放弃",
    "舍弃",
    "结束进程",
    "强制停止",
    "关机",
)
_PRIVACY_SETTING_TERMS = (
    "telemetry",
    "analytics",
    "privacy",
    "data controls",
    "data sharing",
    "chat history and training",
    "chat history & training",
    "improve the model",
    "model training",
    "training data",
    "usage data",
    "account settings",
    "public link",
    "public url",
    "public on the web",
    "publish to web",
    "anyone can view",
    "public visibility",
    "repository visibility",
    "link visibility",
    "anyone on the internet",
    "people with the link",
    "anyone in organization",
    "anyone in the organization",
    "unlisted",
    "discoverable on the web",
    "search engines can index",
    "website visibility",
    "anyone can access",
    "shared publicly",
    "shareable link",
    "link settings",
    "link access",
    "link sharing",
    "anyone with the link",
    "anyone who has the link",
    "make public",
    "public access",
    "general access",
    "diagnostic data",
    "use chats to improve ai",
    "help train our models",
    "help improve claude",
    "use chats for training",
    "use conversations to improve our models",
    "model improvement",
    "activity controls",
    "history settings",
    "temporary chat",
    "incognito mode",
    "retention settings",
    "conversation retention",
    "reference chat history",
    "saved memories",
    "conversation history",
    "opt out of training",
    "do not train on my data",
    "export data",
    "download your data",
    "遥测",
    "分析数据",
    "隐私",
    "数据控制",
    "数据共享",
    "聊天记录和训练",
    "数据管控",
    "改进模型",
    "帮助改进模型",
    "模型训练",
    "训练数据",
    "使用数据",
    "使用情况数据",
    "账户设置",
    "公开链接",
    "公开网址",
    "在网络上公开",
    "发布到网络",
    "任何人都可以查看",
    "任何人都可以访问",
    "仓库可见性",
    "链接可见性",
    "公开可见性",
    "互联网上的任何人",
    "可分享链接",
    "可共享链接",
    "链接设置",
    "链接访问权限",
    "链接共享",
    "拥有链接的任何人",
    "任何知道链接的人",
    "任何获得链接的人",
    "设为公开",
    "公开访问",
    "常规访问权限",
    "诊断数据",
    "使用聊天改进 AI",
    "帮助训练模型",
    "使用聊天进行训练",
    "模型改进",
    "活动控制",
    "历史记录设置",
    "临时聊天",
    "无痕模式",
    "隐身模式",
    "保留期设置",
    "对话保留",
    "引用聊天记录",
    "已保存的记忆",
    "对话历史记录",
    "退出训练",
    "不要使用我的数据训练",
    "导出数据",
    "下载您的数据",
)
_CONFIRM_GROUPS = (
    ("send or submit external content", _SEND_TERMS),
    ("delete or remove data", _DELETE_TERMS),
    ("install or uninstall software", _INSTALL_TERMS),
    ("upload or share a file", _UPLOAD_TERMS),
    ("close, dismiss, or discard application state", _CLOSE_TERMS),
)

_CLAUSE_BOUNDARY_RE = re.compile(
    r"[，。；,;.!！？?\n]|但是|不过|然后|并且|而且|以及|但|\b(?:but|then|and)\b",
    re.IGNORECASE,
)
_NEGATED_ACTION_RE = re.compile(
    r"(?:不要|不能|请勿|避免|切勿|不得|莫|别|不用|无需|禁止|不许|不想|忽略|不可|勿|"
    r"不应|不应该|不该|不会|没有|尚未|未|拒绝|"
    r"\bdo\s+not\b|\bdon't\b|\bcannot\b|\bcan't\b|\bshould\s+not\b|"
    r"\bshouldn't\b|\bmust\s+not\b|\bmustn't\b|\bnot\b|\bnever\b|"
    r"\b[a-z]+n't\b|\bwithout\b|\bignore\b|\bavoid\b|\brefuse\s+to\b)",
    re.IGNORECASE,
)
_PURE_NEGATED_TEXT_SIDE_CLAUSE_RE = re.compile(
    r"\s*(?:"
    r"(?:(?:please|kindly)\s+)?(?:(?:do\s+not|don't|never)\s+"
    r"(?:(?:send|submit)(?:\s+(?:it|this|that|the\s+(?:message|prompt|text)))?|"
    r"(?:click|press|use)\s+(?:send|submit|enter|return)(?:\s+(?:button|key))?))"
    r"|without\s+(?:sending|submitting)(?:\s+(?:it|this|that|the\s+(?:message|prompt|text)))?"
    r"|(?:(?:请|麻烦)?\s*(?:不要|别|不用|无需|切勿|请勿)\s*"
    r"(?:(?:发送|提交)(?:它|这个|这段(?:文字|内容))?|"
    r"(?:点击|点按|按下?)\s*(?:发送|提交|回车)(?:按钮|键)?)))\s*",
    re.IGNORECASE,
)
_POLITE_TAIL_RE = re.compile(r"\s*(?:please|kindly|请|谢谢)\s*", re.IGNORECASE)
_AFFIRMATIVE_ACTION_RE = re.compile(
    r"(?:点击|点开|打开|开启|选择|切换到|切换至|进入|查看|按下|滚动|"
    r"输入|填写|写入|激活|执行|操作|使用|删除|关闭|验证|核验|检查|看到|显示|"
    r"\b(?:click|open|choose|select|switch|enter|view|show|press|scroll|"
    r"type|input|fill|write|activate|execute|operate|use|delete|close|verify|check|focus)\b|"
    r"聚焦)",
    re.IGNORECASE,
)
_ACTIVATION_TARGET_RE = re.compile(
    r"(?:点击|点开|打开|开启|选择|切换到|切换至|进入|按下(?:tab|制表|回车|空格|退出|方向键)?|激活|执行|操作|使用|"
    r"删除|关闭|展开|折叠|"
    r"\b(?:click|open|choose|select|switch|enter|press(?:\s+(?:tab|shift\+tab|enter|"
    r"return|escape|space|pageup|pagedown|home|end|left|up|right|down))?|"
    r"activate|execute|operate|use|"
    r"delete|close|expand|collapse)\b)",
    re.IGNORECASE,
)
_APP_CONTROL_TARGET_RE = re.compile(
    rf"(?:{_ACTIVATION_TARGET_RE.pattern}|(?:查看|检查|核验|验证|聚焦|"
    r"\b(?:view|check|verify|focus|inspect)\b))",
    re.IGNORECASE,
)
_SCROLL_TARGET_RE = re.compile(r"(?:滚动|\bscroll\b)", re.IGNORECASE)
_TYPE_TEXT_ACTION_RE = re.compile(
    r"(?:输入|键入|转写|复述|\b(?:type|input|transcribe|repeat)\b)",
    re.IGNORECASE,
)
_SET_VALUE_ACTION_RE = re.compile(
    r"(?:填写|写入|设置值|设值|\b(?:fill|write|set(?:\s+the)?\s+value)\b)",
    re.IGNORECASE,
)
_TEXT_PAYLOAD_ACTION_RE = re.compile(
    rf"(?:{_TYPE_TEXT_ACTION_RE.pattern}|{_SET_VALUE_ACTION_RE.pattern})",
    re.IGNORECASE,
)
_EDITABLE_CONTROL_TYPES = frozenset({"edit", "document", "combobox", "spinner"})
_UNSUPPORTED_DESKTOP_ACTION_RE = re.compile(
    r"(?:拖动|拖拽|悬停|移动|调整大小|缩放|双击|右键(?:点击)?|下载|复制|重命名|"
    r"\b(?:drag|hover|move|resize|double[-\s]?click|right[-\s]?click|download|copy|rename)\b)",
    re.IGNORECASE,
)
_USER_ACTION_VERB_RE = re.compile(
    rf"(?:{_UNSUPPORTED_DESKTOP_ACTION_RE.pattern}|{_ACTIVATION_TARGET_RE.pattern}|"
    rf"{_TEXT_PAYLOAD_ACTION_RE.pattern}|"
    rf"{_SCROLL_TARGET_RE.pattern})",
    re.IGNORECASE,
)
_TEXT_PAYLOAD_FILLER_RE = re.compile(
    r"\s*(?:(?:the\s+)?(?:words?|phrase|text|content|following)|"
    r"(?:以下|这段|这些|几个)?(?:文字|字|话|内容|词句))\s*",
    re.IGNORECASE,
)
_TEXT_TARGET_SEPARATOR_RE = re.compile(r"\b(?:into|in|to)\b|(?:到|至|进)", re.IGNORECASE)
_REFERENCE_BRIDGE_RE = re.compile(
    r"(?:\b(?:next\s+to|beside|near|nearby|neighboring|under|below|above|left\s+of|right\s+of|"
    r"in\s+front\s+of|behind|around|adjacent\s+to|outside|away\s+from|where|whose)\b|"
    r"旁边|旁的|附近|下方|下面|上方|上面|左侧|左边|右侧|右边|左右|前面|后面|前后|周围|相邻|之外|外面|外部|远离)",
    re.IGNORECASE,
)
_EXCLUDED_REFERENCE_BRIDGE_RE = re.compile(
    r"(?:\b(?:instead\s+of|rather\s+than|other\s+than|except(?:\s+for)?|excluding|"
    r"as\s+opposed\s+to|in\s+place\s+of)\b|"
    r"而不是|而非|代替|替代|除外|以外)",
    re.IGNORECASE,
)
_OUTCOME_BRIDGE_RE = re.compile(
    r"(?:\b(?:until|unless|if|when|whenever|once|while|because|since|for|before|after|"
    r"provided(?:\s+that)?|providing(?:\s+that)?|assuming(?:\s+that)?|"
    r"so(?:\s+that|\s+as\s+to)?|such\s+that|in\s+order\s+(?:to|that)|"
    r"in\s+case|as\s+a\s+result|as\s+soon\s+as|by\s+the\s+time|"
    r"thereby|therefore|thus|hence|whereupon|resulting\s+in|leading\s+to|"
    r"to\s+(?:make|ensure|cause|get|have))\b|"
    r"如果|若|假如|倘若|因为|由于|为了|以便|以使|使得?|让|令|直到|直至|"
    r"从而|这样|之后|随后|之前|以前|当|一旦|只要|除非|同时|期间|导致|结果)",
    re.IGNORECASE,
)
_DIRECT_TARGET_BRIDGE_RE = re.compile(
    r"\s*(?:(?:\b(?:to|into|in|on)\b|(?:到|至|进))\s*)?"
    r"(?:"
    r"(?:(?:the|a|an|this|that|my|current|visible|exact|desktop)\s+)?"
    r"(?:(?:app|application|button|tab|menu|item|option|field|box|link|dialog|window|"
    r"project|conversation|chat|design|file|folder)\s+)?"
    r"(?:(?:named|labelled|labeled|called|titled)\s+)?"
    r"|(?:请|一下|这个|那个|该|当前|可见|准确|桌面上的|桌面上|"
    r"应用里的|应用中的|应用内的|名为|叫做|标题为|标签为|"
    r"按钮|选项卡|标签页|菜单|项目|对话|聊天|设计|输入框|字段|链接|"
    r"窗口|应用|程序|文件夹|文件|的)*)"
    r"\s*",
    re.IGNORECASE,
)
_GENERIC_POSSESSIVE_PARENT_BRIDGE_RE = re.compile(
    r"\s*(?:(?:the|this|that)\s+)?[^，。；,;.!！？?\n]{1,128}?(?:'s|的)\s*",
    re.IGNORECASE,
)
_POSITIVE_PRESENT_STATE_RE = re.compile(
    r"(?:显示|出现|展示|看到|可见|打开|进入|"
    r"\b(?:show|display|appear(?:s|ed)?|visible|reveal|open|view|see|present)\b)",
    re.IGNORECASE,
)
_NEGATIVE_PRESENT_STATE_RE = re.compile(
    r"(?:未显示|不显示|没有显示|隐藏|不存在|消失|不可见|看不到|"
    r"\b(?:absent|hidden|missing|disappear(?:ed)?|invisible|not\s+(?:shown|visible|present)|"
    r"does\s+not\s+appear|remains?\s+hidden)\b)",
    re.IGNORECASE,
)
_POSITIVE_ABSENT_STATE_RE = re.compile(
    r"(?:隐藏|关闭|消失|移除|不再显示|不存在|不可见|"
    r"\b(?:hide|hidden|close|closed|disappear(?:ed)?|remove|removed|absent|missing|"
    r"no\s+longer\s+(?:shown|visible|present))\b)",
    re.IGNORECASE,
)
_NEGATIVE_SELECTED_STATE_RE = re.compile(
    r"(?:未选中|不选中|取消选中|\b(?:unselected|not\s+selected|deselected)\b)",
    re.IGNORECASE,
)
_POSITIVE_SELECTED_STATE_RE = re.compile(
    r"(?:选择|选中|切换到|切换至|\b(?:select|selected|choose|chosen|switch)\b)",
    re.IGNORECASE,
)
_NEGATIVE_FOCUSED_STATE_RE = re.compile(
    r"(?:未聚焦|不聚焦|失去焦点|\b(?:unfocused|not\s+focused|lose\s+focus)\b)",
    re.IGNORECASE,
)
_POSITIVE_FOCUSED_STATE_RE = re.compile(
    r"(?:聚焦|焦点|\b(?:focus|focused)\b)",
    re.IGNORECASE,
)
_STATE_TARGET_BRIDGE_RE = re.compile(
    r"\s*(?:(?:the|this|that)\s+)?(?:(?:text|label|screen|page|dialog)\s+)?",
    re.IGNORECASE,
)
_STATE_AFTER_TARGET_BRIDGE_RE = re.compile(
    r"\s*(?:(?:is|becomes?|remains?|will\s+be|should\s+be)\s+|"
    r"(?:会|将|已经|已|变得|保持)\s*)?",
    re.IGNORECASE,
)
_PREPOSED_TEXT_TARGET_PREFIX_RE = re.compile(
    r"(?:在|于|\bin\b)[^，。；,;.!！？?\n]{0,120}(?:的|['’]s)?\s*$",
    re.IGNORECASE,
)
_PREPOSED_TEXT_TARGET_SUFFIX_RE = re.compile(
    r"\s*(?:(?:里|中|内|里面|中间|框|输入框)|(?:field|box))?\s*",
    re.IGNORECASE,
)
_POLITE_IMPERATIVE_PREFIX_RE = re.compile(
    r"\s*(?:(?:please|kindly)\s+|(?:请|麻烦|帮我)\s*)*"
    r"(?:(?:only)\s+|(?:只|仅|仅仅)\s*)?",
    re.IGNORECASE,
)
_ENGLISH_APP_LOCATION_PREFIX_RE = re.compile(
    r"\s*(?:(?:please|kindly)\s+)*(?:in|inside|within|on)\s+(?:the\s+)?"
    r"[a-z0-9_.+ -]{1,128}(?:\s+(?:app|application))?\s*",
    re.IGNORECASE,
)
_CHINESE_APP_LOCATION_PREFIX_RE = re.compile(
    r"\s*(?:(?:请|麻烦|帮我)\s*)*(?:在|于)\s*"
    r"(?!(?:我|如果|若|当|因为|由于|之后|之前))"
    r"(?:[a-z0-9_.+-]+(?:\s+[a-z0-9_.+-]+){0,5}|[\u3400-\u9fff]{2,32})"
    r"(?:\s*(?:app|应用|程序|窗口))?(?:\s*(?:里|中|内|里面|上))?\s*",
    re.IGNORECASE,
)
_APP_TRAILING_OUTCOME_RE = re.compile(
    r"\s*(?:"
    r"(?:to|and)\s+(?:show|display|reveal|see|verify|check)\b[^，。；,;.!！？?\n]{0,160}"
    r"|(?:以便|来|并|然后)?\s*(?:显示|出现|展示|看到|验证|检查)"
    r"[^，。；,;.!！？?\n]{0,160}"
    r")\s*",
    re.IGNORECASE,
)
_DISALLOWED_EXPECTATION_BRIDGE_RE = re.compile(
    r"(?:\b(?:if|when|whenever|because|before|after|while|unless|until|once|since|for|"
    r"provided(?:\s+that)?|providing(?:\s+that)?|assuming(?:\s+that)?|"
    r"in\s+case|as\s+soon\s+as|by\s+the\s+time)\b|"
    r"如果|若|假如|倘若|因为|由于|之前|以前|之后|随后|当|一旦|只要|除非|"
    r"直到|直至|同时|期间)",
    re.IGNORECASE,
)
_UNSUPPORTED_CONTROL_CONDITION_RE = re.compile(
    r"(?:\b(?:only\s+if|if|when|whenever|unless|before|after|while|because|should|where|"
    r"once|since|as\s+long\s+as|so\s+long\s+as|insofar\s+as|lest|in\s+case|"
    r"as\s+soon\s+as|at\s+such\s+time\s+as|by\s+the\s+time|"
    r"in\s+the\s+event\s+that|(?:on|under)\s+(?:the\s+)?condition\s+that|"
    r"depending\s+(?:on|upon)(?:\s+whether)?|"
    r"on\s+the\s+proviso\s+that|given(?:\s+that)?|supposing(?:\s+that)?|"
    r"suppose(?:d)?|assume(?:d)?|presuming(?:\s+that)?|contingent\s+on|"
    r"subject\s+to|whether|provided(?:\s+that)?|providing(?:\s+that)?|"
    r"assuming(?:\s+that)?)\b|"
    r"如果|若|假如|倘若|要是|假设|当|一旦|除非|只要|因为|由于|之前|之后|"
    r"待|的话|假使|如若|倘使|若是|万一|假定|条件是|前提是|取决于|"
    r"情况下|前提下|为条件|为前提|视[^，。；,;.!！？?\n]{1,120}是否|"
    r"在[^，。；,;.!！？?\n]{1,120}时(?:[,，])?)",
    re.IGNORECASE,
)
_ACTION_OUTCOME_START_RE = re.compile(
    r"(?:\b(?:so(?:\s+that)?|until|"
    r"to\s+(?:show|display|reveal|see|verify|check|make|ensure|cause|get|have)|"
    r"to\s+(?:[a-z]+ly\s+)*(?:send|submit|delete|remove|open|select|choose|switch|"
    r"upload|share|install|uninstall|close|dismiss|save|publish|unpublish)|"
    r"and\s+(?:show|display|reveal|see|verify|check|make|ensure|cause|get|have)|"
    r"with\s+(?:the\s+)?result\s+that|as\s+a\s+result|thereby)\b|"
    r"[，,;；。.!]\s*(?:expect(?:ed|ing)?(?:\s+(?:to|that))?|"
    r"(?:the\s+)?(?:outcome|result)(?:\s+(?:is|will|should|must)\b)?)|"
    r"以便|以(?:显示|看到|展示|验证|检查)|直到|直至|"
    r"并(?:显示|看到|展示|验证|检查)|让|使|"
    r"发送|提交|删除|移除|打开|选择|切换|上传|分享|安装|卸载|关闭|保存|发布)",
    re.IGNORECASE,
)
_PAGE_COUNT_RE = re.compile(
    r"(?:\b(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+pages?\b|[一二三四五六七八九十\d]+\s*页)",
    re.IGNORECASE,
)
_OUTCOME_STATE_VERB_RE = re.compile(
    r"(?:显示|出现|展示|看到|可见|"
    r"\b(?:show|display|appear|visible|reveal|see|present)\b)",
    re.IGNORECASE,
)
_SELECTION_ACTION_RE = re.compile(
    r"(?:选择|选中|切换到|切换至|"
    r"\b(?:choose|select|switch(?:\s+to)?)\b)",
    re.IGNORECASE,
)
_TARGET_TRAILING_DECORATION_RE = re.compile(
    r"\s*(?:"
    r"(?:button|tab|menu|item|option|field|box|link|dialog|window|app|application|"
    r"project|conversation|chat|design|file|folder)\b|"
    r"按钮|选项卡|标签页|菜单|项目|对话|聊天|设计|输入框|字段|链接|窗口|应用|"
    r"程序|文件夹|文件"
    r")?"
    r"(?:\s*(?:\b(?:in|inside|within|on)\b|(?:在|于))\s+(?:the\s+)?"
    r"[^，。；,;.!！？?\n]{1,128}?"
    r"(?:\s+(?:app|application|window)|\s*(?:应用|程序|窗口))?)?\s*",
    re.IGNORECASE,
)
_NEXT_ACTION_SEPARATOR_RE = re.compile(
    r"\s*(?:(?:[，。；,;.!！？?])|\b(?:and|then)\b|然后|并且|而且|以及|但|但是|不过)*\s*",
    re.IGNORECASE,
)
_PAIRED_QUOTES = (('"', '"'), ("`", "`"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u02bc": "'",
                "\uff07": "'",
            }
        )
    )
    return normalized.casefold()


def _searchable(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    # Accessibility frameworks commonly expose identifiers as API_KEY, api-key,
    # apiKey, or PasswordField.  Treat those separators and camelCase boundaries
    # as words so surface scanning cannot be bypassed by identifier style.
    normalized = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", normalized)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", normalized)
    normalized = normalized.casefold()
    normalized = re.sub(
        r'(?<![a-z0-9_])(?:(?:"?is[_-]?)?password"?)\s*[:=]\s*(?:false|0)'
        r"(?![a-z0-9_])",
        " ",
        normalized,
    )
    return " ".join(re.sub(r"[_\W]+", " ", normalized).split())


def _contains_term(value: str, terms: tuple[str, ...]) -> bool:
    normalized = _searchable(value)
    tokens = normalized.split()
    for term in terms:
        candidate = _searchable(term)
        if all(ord(character) < 128 for character in candidate):
            if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", normalized):
                return True
            if (
                " " not in candidate
                and len(candidate) >= 5
                and any(
                    "".join(tokens[start:end]) == candidate
                    for start in range(len(tokens))
                    for end in range(start + 2, min(len(tokens), start + 4) + 1)
                )
            ):
                return True
            compact_candidate = candidate.replace(" ", "")
            if (
                " " in candidate
                and len(compact_candidate) >= 5
                and any(
                    "".join(tokens[start:end]) == compact_candidate
                    for start in range(len(tokens))
                    for end in range(start + 1, min(len(tokens), start + 4) + 1)
                )
            ):
                return True
        elif candidate in normalized:
            return True
    return False


def _looks_like_auth_surface(value: str) -> bool:
    return _contains_term(value, _AUTH_SURFACE_TERMS) or (
        _contains_term(value, _AUTH_WELCOME_TERMS)
        and _contains_term(value, _AUTH_IDENTITY_FIELD_TERMS)
    )


def _looks_like_payment_surface(value: str) -> bool:
    if _contains_term(value, _PAYMENT_TERMS):
        return True
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return bool(
        re.search(
            r"\b(?:total|amount\s+due|tip)\b\s*[:：]?\s*(?:[$€£¥￥]\s*\d|"
            r"\d[^\r\n]{0,8}\b(?:usd|eur|gbp|cny|rmb)\b)",
            normalized,
        )
    )


def _looks_like_privacy_surface(value: str) -> bool:
    if _contains_term(value, _PRIVACY_SETTING_TERMS):
        return True
    searchable = _searchable(value)
    normalized = _normalized(value)
    return bool(
        (
            any(marker in searchable for marker in ("visibility", "access level", "sharing scope"))
            and "public" in searchable
            and "private" in searchable
        )
        or ("visibility" in searchable and "everyone" in searchable and "private" in searchable)
        or ("link" in searchable and "anyone" in searchable and "restricted" in searchable)
        or ("可见性" in normalized and "公开" in normalized and "私有" in normalized)
        or ("链接可见性" in normalized and "任何人" in normalized and "受限" in normalized)
    )


def _same_app(left: str, right: str) -> bool:
    return _normalized(left.strip()) == _normalized(right.strip())


def _authorized_span(candidate: str, user_text: str) -> bool:
    value = _normalized(candidate.strip())
    source = _normalized(user_text)
    if not value:
        return False
    if value.isascii():
        return bool(re.search(rf"(?<![a-z0-9_-]){re.escape(value)}(?![a-z0-9_-])", source))
    return len(value) >= 2 and value in source


def _matching_spans(candidate: str, user_text: str) -> tuple[tuple[int, int], ...]:
    value = _normalized(candidate.strip())
    source = _normalized(user_text)
    if not value:
        return ()
    expression = (
        rf"(?<![a-z0-9_-]){re.escape(value)}(?![a-z0-9_-])" if value.isascii() else re.escape(value)
    )
    return tuple((match.start(), match.end()) for match in re.finditer(expression, source))


def _quoted_ranges(source: str) -> tuple[tuple[int, int], ...]:
    """Return simple, non-nested quote ranges in normalized user-authored text."""

    ranges: list[tuple[int, int]] = []
    for opening, closing in _PAIRED_QUOTES:
        cursor = 0
        while cursor < len(source):
            start = source.find(opening, cursor)
            if start < 0:
                break
            end = source.find(closing, start + len(opening))
            if end < 0:
                break
            ranges.append((start, end + len(closing)))
            cursor = end + len(closing)
    # Single ASCII apostrophes are quotes only when they are not word-internal,
    # so contractions such as ``don't`` cannot mask a negation.
    ranges.extend(
        (match.start(), match.end())
        for match in re.finditer(r"(?<![a-z0-9])'[^'\r\n]+'(?![a-z0-9])", source)
    )
    return tuple(sorted(set(ranges)))


def _mask_quoted_text(source: str, start: int, end: int) -> str:
    clause = list(source[start:end])
    for quote_start, quote_end in _quoted_ranges(source):
        overlap_start = max(start, quote_start)
        overlap_end = min(end, quote_end)
        for index in range(overlap_start, overlap_end):
            clause[index - start] = " "
    return "".join(clause)


def _mask_text_payloads(source: str, start: int, end: int) -> str:
    """Mask quoted and unquoted dictated data while preserving character offsets."""

    clause = list(_mask_quoted_text(source, start, end))
    for payload_start, payload_end in _text_payload_ranges(source):
        overlap_start = max(start, payload_start)
        overlap_end = min(end, payload_end)
        for index in range(overlap_start, overlap_end):
            clause[index - start] = " "
    return "".join(clause)


def _imperative_prefix(prefix: str) -> bool:
    return any(
        pattern.fullmatch(prefix) is not None
        for pattern in (
            _POLITE_IMPERATIVE_PREFIX_RE,
            _ENGLISH_APP_LOCATION_PREFIX_RE,
            _CHINESE_APP_LOCATION_PREFIX_RE,
        )
    )


def _verb_requires_imperative(verb: re.Match[str]) -> bool:
    return _OUTCOME_STATE_VERB_RE.fullmatch(verb.group()) is None


def _suffix_is_reference_anchor(
    intent_clause: str,
    *,
    local_end: int,
    verb: re.Match[str],
    candidate: str,
    competing_labels: tuple[str, ...],
) -> bool:
    if not _verb_requires_imperative(verb):
        return False
    suffix = intent_clause[local_end:]
    next_action = _AFFIRMATIVE_ACTION_RE.search(suffix)
    if next_action is not None:
        suffix = suffix[: next_action.start()]
    spatial_reference = bool(
        _REFERENCE_BRIDGE_RE.search(suffix) or _EXCLUDED_REFERENCE_BRIDGE_RE.search(suffix)
    )
    possessive_reference = re.search(r"(?:['’]s|的)\s*", suffix, re.IGNORECASE) is not None
    if not spatial_reference and not possessive_reference:
        return False
    if not competing_labels:
        return spatial_reference
    return any(
        _normalized(other) != _normalized(candidate) and _authorized_span(other, suffix)
        for other in competing_labels
        if other.strip()
    )


def _possessive_parent_bridge(
    bridge: str,
    candidate: str,
    competing_labels: tuple[str, ...],
) -> bool:
    del candidate
    return any(
        re.fullmatch(
            rf"\s*(?:(?:the|this|that)\s+)?{re.escape(_normalized(other.strip()))}"
            r"\s*(?:'s|的)\s*",
            bridge,
            re.IGNORECASE,
        )
        is not None
        for other in competing_labels
        if other.strip()
    )


def _span_is_quoted_text_payload(source: str, start: int, end: int, clause_start: int) -> bool:
    """Reject a UI label occurrence that is data for a surrounding text-entry command."""

    for quote_start, quote_end in _quoted_ranges(source):
        if quote_start < start and end < quote_end:
            prefix = _mask_quoted_text(source, clause_start, quote_start)
            return _TEXT_PAYLOAD_ACTION_RE.search(prefix) is not None
    return False


def _quoted_container(source: str, start: int, end: int) -> tuple[int, int] | None:
    return next(
        (
            (quote_start, quote_end)
            for quote_start, quote_end in _quoted_ranges(source)
            if quote_start < start and end < quote_end
        ),
        None,
    )


def _quoted_span_is_whole_label(source: str, start: int, end: int, candidate: str) -> bool:
    container = _quoted_container(source, start, end)
    if container is None:
        return True
    quote_start, quote_end = container
    quoted = source[quote_start:quote_end].strip("'\"`“”‘’「」『』 ")
    return _normalized(quoted) == _normalized(candidate.strip())


def _span_is_shadowed_by_longer_label(
    candidate: str,
    user_text: str,
    start: int,
    end: int,
    competing_labels: tuple[str, ...],
) -> bool:
    candidate_value = _normalized(candidate.strip())
    return any(
        _normalized(other.strip()) != candidate_value
        and len(_normalized(other.strip())) > len(candidate_value)
        and any(
            other_start <= start and end <= other_end
            for other_start, other_end in _matching_spans(other, user_text)
        )
        for other in competing_labels
        if other.strip()
    )


def _target_suffix_is_complete(intent_clause: str, local_end: int) -> bool:
    """Require a UI label to end at the user-authored target boundary.

    Visible labels are not a trustworthy dictionary of all possible longer labels.
    This check therefore rejects ``Open`` in ``click Open settings`` even when the
    current accessibility snapshot exposes only the shorter element.  A control
    noun (``button``) or an explicit app locator may follow the exact label.
    """

    suffix = intent_clause[local_end:]
    outcome = _ACTION_OUTCOME_START_RE.search(suffix)
    if outcome is not None:
        suffix = suffix[: outcome.start()]
    return _TARGET_TRAILING_DECORATION_RE.fullmatch(suffix) is not None


def _text_payload_ranges(source: str) -> tuple[tuple[int, int], ...]:
    """Locate unquoted payloads in commands such as ``type X into Prompt``.

    Speech transcripts often omit quotation marks.  Text that is being dictated
    must remain data even when it contains words such as ``click`` or a visible
    button label.  The target after ``into/in/to`` is deliberately outside the
    returned range so it can still authorize the editable control.
    """

    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    ranges: list[tuple[int, int]] = []
    for action_match in _TEXT_PAYLOAD_ACTION_RE.finditer(source):
        payload_start = action_match.end()
        filler = _TEXT_PAYLOAD_FILLER_RE.match(source, payload_start)
        if filler is not None:
            payload_start = filler.end()
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= payload_start),
            default=len(source),
        )
        # The payload itself may contain words such as "go to" or Chinese characters
        # such as “进度”.  The target locator is the final separator in the clause;
        # choosing the first one can expose later payload text as click authority.
        separators = tuple(_TEXT_TARGET_SEPARATOR_RE.finditer(source, payload_start, clause_end))
        separator = separators[-1] if separators else None
        payload_end = separator.start() if separator is not None else clause_end
        if payload_start < payload_end:
            ranges.append((payload_start, payload_end))
    return tuple(ranges)


def _has_negated_candidate_occurrence(candidate: str, user_text: str) -> bool:
    """Treat mixed positive/negative authority for the same label as ambiguous."""

    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    for start, end in _matching_spans(candidate, user_text):
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= end),
            default=len(source),
        )
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in payload_ranges
        ) or _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        if _NEGATED_ACTION_RE.search(_mask_quoted_text(source, clause_start, clause_end)):
            return True
    return False


def affirmatively_authorized_reference(
    candidate: str,
    user_text: str,
    *,
    allow_scoped_following_clause: bool = False,
) -> bool:
    """Require an exact label occurrence in an affirmative action clause.

    UI text is untrusted planner input.  Merely mentioning a label, especially in
    a negation such as ``do not click Enable sync``, cannot authorize a click.
    """

    source = _normalized(user_text)
    text_payload_ranges = _text_payload_ranges(source)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    for start, end in _matching_spans(candidate, user_text):
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= end),
            default=len(source),
        )
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in text_payload_ranges
        ):
            continue
        if _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        # Commands shown inside quotes are data, not user authority.  Mask all
        # quoted content when looking for action/negation verbs; ``click "Open"``
        # still works because the real ``click`` verb remains outside the quote.
        clause = _mask_quoted_text(source, clause_start, clause_end)
        if _NEGATED_ACTION_RE.search(clause):
            continue
        if _AFFIRMATIVE_ACTION_RE.search(clause):
            return True
        if not allow_scoped_following_clause or _NEGATED_ACTION_RE.search(clause):
            continue
        local_start = start - clause_start
        local_end = end - clause_start
        prefix = clause[:local_start]
        suffix = clause[local_end:]
        if not re.search(r"(?:在|于|\bin\b)\s*(?:the\s+)?$", prefix):
            continue
        if not re.fullmatch(r"\s*(?:app|应用)?(?:里|中|里面|上)?\s*", suffix):
            continue
        delimiter = next(
            (match for match in boundaries if match.start() == clause_end),
            None,
        )
        if delimiter is None:
            continue
        following_start = delimiter.end()
        following_end = min(
            (match.start() for match in boundaries if match.start() >= following_start),
            default=len(source),
        )
        following = _mask_quoted_text(source, following_start, following_end)
        if not _NEGATED_ACTION_RE.search(following) and _AFFIRMATIVE_ACTION_RE.search(following):
            return True
    return False


def _directly_authorized_span(
    candidate: str,
    user_text: str,
    *,
    target_verbs: re.Pattern[str],
    allow_preposed_text_target: bool = False,
    reject_outcome_bridge: bool = False,
    competing_labels: tuple[str, ...] = (),
    reject_quoted_occurrences: bool = False,
    require_complete_target: bool = False,
) -> bool:
    if _has_negated_candidate_occurrence(candidate, user_text):
        return False
    if not affirmatively_authorized_reference(candidate, user_text):
        return False
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    text_payload_ranges = _text_payload_ranges(source)
    quoted_ranges = _quoted_ranges(source)
    for start, end in _matching_spans(candidate, user_text):
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= end),
            default=len(source),
        )
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in text_payload_ranges
        ) or _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        if not _quoted_span_is_whole_label(source, start, end, candidate):
            continue
        if _span_is_shadowed_by_longer_label(
            candidate,
            user_text,
            start,
            end,
            competing_labels,
        ):
            continue
        if reject_quoted_occurrences and any(
            quote_start <= start and end <= quote_end for quote_start, quote_end in quoted_ranges
        ):
            continue
        clause = _mask_quoted_text(source, clause_start, clause_end)
        if _NEGATED_ACTION_RE.search(clause):
            continue
        intent_characters = list(clause)
        for payload_start, payload_end in text_payload_ranges:
            overlap_start = max(clause_start, payload_start)
            overlap_end = min(clause_end, payload_end)
            for index in range(overlap_start, overlap_end):
                intent_characters[index - clause_start] = " "
        intent_clause = "".join(intent_characters)
        local_start = start - clause_start
        prior_verbs = [
            match for match in target_verbs.finditer(intent_clause) if match.end() <= local_start
        ]
        if prior_verbs:
            nearest = prior_verbs[-1]
            bridge = intent_clause[nearest.end() : local_start]
            intervening_intent = _AFFIRMATIVE_ACTION_RE.search(
                intent_clause,
                nearest.end(),
                local_start,
            )
            # A label used only as a spatial/reference anchor is not an action
            # target.  This prevents "click A to show B next to C" from granting
            # authority to click C merely because an earlier verb exists.
            outcome_bridge = reject_outcome_bridge and _OUTCOME_BRIDGE_RE.search(bridge)
            possessive_bridge = _possessive_parent_bridge(
                bridge,
                candidate,
                competing_labels,
            )
            earlier_visible_target = reject_outcome_bridge and any(
                _normalized(other) != _normalized(candidate) and _authorized_span(other, bridge)
                for other in competing_labels
                if other.strip()
            )
            imperative_position = not _verb_requires_imperative(nearest) or _imperative_prefix(
                intent_clause[: nearest.start()]
            )
            suffix_reference_anchor = _suffix_is_reference_anchor(
                intent_clause,
                local_end=end - clause_start,
                verb=nearest,
                candidate=candidate,
                competing_labels=competing_labels,
            )
            if (
                intervening_intent is None
                and imperative_position
                and (_DIRECT_TARGET_BRIDGE_RE.fullmatch(bridge) or possessive_bridge)
                and not _REFERENCE_BRIDGE_RE.search(bridge)
                and not _EXCLUDED_REFERENCE_BRIDGE_RE.search(bridge)
                and not outcome_bridge
                and (not earlier_visible_target or possessive_bridge)
                and not suffix_reference_anchor
                and (
                    not require_complete_target
                    or not _verb_requires_imperative(nearest)
                    or _target_suffix_is_complete(intent_clause, end - clause_start)
                )
            ):
                return True
        # Speech often puts the editable target first: "在 Claude 的 Message
        # 输入你好". Accept only a location-bound label followed immediately
        # by a text-entry verb; an app/container earlier in the locator does
        # not pass because another label remains between it and the verb.
        prefix = intent_clause[:local_start]
        suffix = intent_clause[end - clause_start :]
        following_text_verb = target_verbs.search(suffix)
        if (
            allow_preposed_text_target
            and _PREPOSED_TEXT_TARGET_PREFIX_RE.search(prefix)
            and following_text_verb is not None
            and _PREPOSED_TEXT_TARGET_SUFFIX_RE.fullmatch(suffix[: following_text_verb.start()])
        ):
            return True
    return False


def _positive_user_action_verbs(user_text: str) -> tuple[re.Match[str], ...]:
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    quote_ranges = _quoted_ranges(source)
    verbs: list[re.Match[str]] = []
    for verb in _USER_ACTION_VERB_RE.finditer(source):
        if any(start <= verb.start() and verb.end() <= end for start, end in payload_ranges):
            continue
        if any(start <= verb.start() and verb.end() <= end for start, end in quote_ranges):
            continue
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= verb.start()),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= verb.end()),
            default=len(source),
        )
        intent_clause = _mask_text_payloads(source, clause_start, clause_end)
        if _NEGATED_ACTION_RE.search(intent_clause):
            continue
        if not _imperative_prefix(intent_clause[: verb.start() - clause_start]):
            continue
        verbs.append(verb)
    return tuple(verbs)


def _verb_matches_action_type(verb: re.Match[str], action_type: DesktopActionType) -> bool:
    if action_type == DesktopActionType.TYPE_TEXT:
        return _TYPE_TEXT_ACTION_RE.fullmatch(verb.group()) is not None
    if action_type == DesktopActionType.SET_VALUE:
        return _SET_VALUE_ACTION_RE.fullmatch(verb.group()) is not None
    if action_type == DesktopActionType.SCROLL:
        return _SCROLL_TARGET_RE.fullmatch(verb.group()) is not None
    is_key_press = (
        re.fullmatch(
            r"(?:按下(?:tab|制表|回车|空格|退出|方向键)?|"
            r"press(?:\s+(?:tab|shift\+tab|enter|return|escape|space|pageup|pagedown|"
            r"home|end|left|up|right|down))?)",
            verb.group(),
            re.IGNORECASE,
        )
        is not None
    )
    if action_type == DesktopActionType.PRESS_KEY:
        return is_key_press
    if action_type in {DesktopActionType.CLICK, DesktopActionType.PERFORM_SECONDARY_ACTION}:
        return _ACTIVATION_TARGET_RE.fullmatch(verb.group()) is not None and not is_key_press
    return False


def _target_spans_for_verb(
    target: str,
    user_text: str,
    verb: re.Match[str],
    action_type: DesktopActionType,
) -> tuple[tuple[int, int, int, int], ...]:
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    clause_start = max(
        (match.end() for match in boundaries if match.end() <= verb.start()),
        default=0,
    )
    clause_end = min(
        (match.start() for match in boundaries if match.start() >= verb.end()),
        default=len(source),
    )
    intent_clause = _mask_text_payloads(source, clause_start, clause_end)
    results: list[tuple[int, int, int, int]] = []
    for start, end in _matching_spans(target, user_text):
        if start < clause_start or end > clause_end:
            continue
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in payload_ranges
        ) or _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        local_start = start - clause_start
        local_end = end - clause_start
        if start >= verb.end():
            bridge = intent_clause[verb.end() - clause_start : local_start]
            if _AFFIRMATIVE_ACTION_RE.search(bridge):
                continue
            if not (
                _DIRECT_TARGET_BRIDGE_RE.fullmatch(bridge)
                or _GENERIC_POSSESSIVE_PARENT_BRIDGE_RE.fullmatch(bridge)
            ):
                continue
            if (
                _OUTCOME_BRIDGE_RE.search(bridge)
                or _REFERENCE_BRIDGE_RE.search(bridge)
                or _EXCLUDED_REFERENCE_BRIDGE_RE.search(bridge)
                or _suffix_is_reference_anchor(
                    intent_clause,
                    local_end=local_end,
                    verb=verb,
                    candidate=target,
                    competing_labels=(),
                )
                or not _target_suffix_is_complete(intent_clause, local_end)
            ):
                continue
            results.append((start, end, clause_start, clause_end))
            continue
        if action_type not in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            continue
        prefix = intent_clause[:local_start]
        suffix = intent_clause[local_end : verb.start() - clause_start]
        if _PREPOSED_TEXT_TARGET_PREFIX_RE.search(
            prefix
        ) and _PREPOSED_TEXT_TARGET_SUFFIX_RE.fullmatch(suffix):
            results.append((start, end, clause_start, clause_end))
    return tuple(results)


def _payload_belongs_to_verb(
    payload: str,
    user_text: str,
    verb: re.Match[str],
) -> bool:
    if not payload:
        return False
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    clause_end = min(
        (match.start() for match in boundaries if match.start() >= verb.end()),
        default=len(source),
    )
    payload_value = _normalized(payload)
    expected_payload_start = verb.end()
    filler = _TEXT_PAYLOAD_FILLER_RE.match(source, expected_payload_start)
    if filler is not None:
        expected_payload_start = filler.end()
    for payload_start, payload_end in _text_payload_ranges(source):
        if payload_start != expected_payload_start or payload_end > clause_end:
            continue
        spoken_payload = source[payload_start:payload_end].strip()
        for opening, closing in _PAIRED_QUOTES:
            if (
                spoken_payload.startswith(opening)
                and spoken_payload.endswith(closing)
                and len(spoken_payload) >= len(opening) + len(closing)
            ):
                spoken_payload = spoken_payload[len(opening) : -len(closing)].strip()
                break
        if payload_value == spoken_payload:
            return True
    return False


def _step_has_explicit_outcome(
    user_text: str,
    verb: re.Match[str],
    step_end: int,
) -> bool:
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    for outcome in _ACTION_OUTCOME_START_RE.finditer(source, verb.end(), step_end):
        if any(start <= outcome.start() and outcome.end() <= end for start, end in payload_ranges):
            continue
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= outcome.start()),
            default=verb.end(),
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= outcome.end()),
            default=step_end,
        )
        if _PURE_NEGATED_TEXT_SIDE_CLAUSE_RE.fullmatch(source[clause_start:clause_end]):
            continue
        return True
    independent_boundaries = [
        boundary
        for boundary in boundaries
        if verb.end() <= boundary.start() < step_end
        and not any(
            payload_start <= boundary.start() and boundary.end() <= payload_end
            for payload_start, payload_end in payload_ranges
        )
    ]
    for index, boundary in enumerate(independent_boundaries):
        clause_end = (
            independent_boundaries[index + 1].start()
            if index + 1 < len(independent_boundaries)
            else step_end
        )
        clause = source[boundary.end() : clause_end]
        if not clause.strip() or _POLITE_TAIL_RE.fullmatch(clause):
            continue
        if _PURE_NEGATED_TEXT_SIDE_CLAUSE_RE.fullmatch(clause):
            continue
        # The local grammar cannot prove that an additional affirmative clause
        # is merely commentary. It therefore cannot let payload presence stand
        # in for completion when that clause may state a separate result.
        return True
    return False


def _expectation_suffix_is_complete(
    kind: DesktopExpectationKind,
    source: str,
    end: int,
    step_end: int,
    *,
    has_next_action: bool,
) -> bool:
    """Reject partial result labels and unverified qualifiers/conjuncts."""

    suffix = source[end:step_end]
    if has_next_action and _NEXT_ACTION_SEPARATOR_RE.fullmatch(suffix):
        suffix = ""
    patterns = {
        DesktopExpectationKind.TEXT_PRESENT: re.compile(
            r"\s*(?:(?:(?:is|becomes?|remains?)\s+)?"
            r"(?:visible|present|shown|displayed)|appears?|可见|显示|出现|展示)?\s*",
            re.IGNORECASE,
        ),
        DesktopExpectationKind.TEXT_ABSENT: re.compile(
            r"\s*(?:(?:(?:is|becomes?|remains?)\s+)?"
            r"(?:absent|hidden|missing|invisible)|disappears?|消失|隐藏|不存在|不可见)?\s*",
            re.IGNORECASE,
        ),
        DesktopExpectationKind.FOCUSED_CONTAINS: re.compile(
            r"\s*(?:(?:(?:is|becomes?|remains?)\s+)?focused|聚焦|获得焦点)?\s*",
            re.IGNORECASE,
        ),
        DesktopExpectationKind.ELEMENT_SELECTED: re.compile(
            r"\s*(?:(?:(?:is|becomes?|remains?)\s+)?selected|选中)?\s*",
            re.IGNORECASE,
        ),
    }
    pattern = patterns.get(kind)
    return pattern is not None and pattern.fullmatch(suffix) is not None


def action_matches_next_user_step(
    action: DesktopAction,
    target_label: str,
    user_text: str,
    *,
    completed_steps: int,
) -> bool:
    """Bind each proposed mutation to the next direct positive action in the task."""

    verbs = _positive_user_action_verbs(user_text)
    if completed_steps < 0 or completed_steps >= len(verbs):
        return False
    verb = verbs[completed_steps]
    if not _verb_matches_action_type(verb, action.type):
        return False
    target_spans = _target_spans_for_verb(
        target_label,
        user_text,
        verb,
        action.type,
    )
    if not target_label or not target_spans:
        return False
    source = _normalized(user_text)
    step_end = (
        verbs[completed_steps + 1].start() if completed_steps + 1 < len(verbs) else len(source)
    )
    outcome = _ACTION_OUTCOME_START_RE.search(source, verb.end(), step_end)
    command_end = outcome.start() if outcome is not None else step_end
    command_characters = list(source[verb.start() : command_end])
    for target_start, target_end, _clause_start, _clause_end in target_spans:
        overlap_start = max(verb.start(), target_start)
        overlap_end = min(command_end, target_end)
        for index in range(overlap_start, overlap_end):
            command_characters[index - verb.start()] = " "
    command_text = "".join(command_characters)
    if action.type == DesktopActionType.CLICK:
        if action.click_count not in {None, 1} or action.mouse_button not in {None, "left"}:
            return False
    elif action.type == DesktopActionType.PERFORM_SECONDARY_ACTION:
        if _normalized(action.action_name or "") != "invoke":
            return False
    elif action.type == DesktopActionType.PRESS_KEY:
        requested_key = _normalized(action.key or "").replace(" ", "")
        key_aliases = {
            "enter": ("enter", "return", "回车"),
            "return": ("enter", "return", "回车"),
            "escape": ("escape", "退出"),
            "space": ("space", "空格"),
            "tab": ("tab", "制表"),
            "shift+tab": ("shift+tab", "shift tab"),
            "pageup": ("pageup", "page up"),
            "pagedown": ("pagedown", "page down"),
            "home": ("home",),
            "end": ("end",),
            "left": ("left", "左"),
            "right": ("right", "右"),
            "up": ("up", "上"),
            "down": ("down", "下"),
        }
        if requested_key not in key_aliases or not any(
            _authorized_span(alias, verb.group()) for alias in key_aliases[requested_key]
        ):
            return False
    elif action.type == DesktopActionType.SCROLL:
        direction_aliases = {
            "up": ("up", "向上", "往上", "上滚"),
            "down": ("down", "向下", "往下", "下滚"),
            "left": ("left", "向左", "往左", "左滚"),
            "right": ("right", "向右", "往右", "右滚"),
        }
        direction = _normalized(action.direction or "")
        mentioned_directions = {
            candidate_direction
            for candidate_direction, aliases in direction_aliases.items()
            if any(_authorized_span(alias, command_text) for alias in aliases)
        }
        if direction not in direction_aliases or mentioned_directions != {direction}:
            return False
        explicit_page_count = _PAGE_COUNT_RE.search(command_text) is not None
        if action.pages is None and explicit_page_count:
            return False
        if action.pages is not None:
            page_value = float(action.pages)
            page_tokens = {str(page_value), str(int(page_value)) if page_value.is_integer() else ""}
            if page_value.is_integer() and 1 <= page_value <= 10:
                english = (
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                    "ten",
                )[int(page_value) - 1]
                chinese = "一二三四五六七八九十"[int(page_value) - 1]
                page_tokens.update({english, f"{chinese}页"})
            if not explicit_page_count or not any(
                token and _authorized_span(token, command_text) for token in page_tokens
            ):
                return False
    if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
        payload = action.text if action.type == DesktopActionType.TYPE_TEXT else action.value
        return _payload_belongs_to_verb(payload or "", user_text, verb)
    return True


def expectation_matches_user_step(
    action: DesktopAction,
    target_label: str,
    expectation: DesktopExpectation,
    user_text: str,
    *,
    completed_steps: int,
) -> bool:
    verbs = _positive_user_action_verbs(user_text)
    if completed_steps < 0 or completed_steps >= len(verbs):
        return False
    verb = verbs[completed_steps]
    target_spans = _target_spans_for_verb(target_label, user_text, verb, action.type)
    if not target_spans:
        return False
    expected = _normalized(expectation.text or "")
    source = _normalized(user_text)
    step_end = (
        verbs[completed_steps + 1].start() if completed_steps + 1 < len(verbs) else len(source)
    )
    if (
        action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}
        and expectation.kind
        in {
            DesktopExpectationKind.TEXT_PRESENT,
            DesktopExpectationKind.FOCUSED_CONTAINS,
        }
        and expected
        in {_normalized(value) for value in (action.text or "", action.value or "") if value}
        and _payload_belongs_to_verb(expectation.text or "", user_text, verb)
        and not _step_has_explicit_outcome(user_text, verb, step_end)
    ):
        return True
    if not expectation.text:
        return False
    payload_ranges = _text_payload_ranges(source)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    has_next_action = completed_steps + 1 < len(verbs)
    for start, end in _matching_spans(expectation.text, user_text):
        expectation_clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        for target_start, target_end, _clause_start, _clause_end in target_spans:
            if start < verb.end() or end > step_end:
                continue
            if any(
                payload_start <= start and end <= payload_end
                for payload_start, payload_end in payload_ranges
            ) or _span_is_quoted_text_payload(source, start, end, expectation_clause_start):
                continue
            if not _expectation_suffix_is_complete(
                expectation.kind,
                source,
                end,
                step_end,
                has_next_action=has_next_action,
            ):
                continue
            if start == target_start and end == target_end:
                return (
                    expectation.kind == DesktopExpectationKind.ELEMENT_SELECTED
                    and _SELECTION_ACTION_RE.fullmatch(verb.group()) is not None
                )
            if start < target_end:
                continue
            bridge = source[target_end:start]
            if (
                _DISALLOWED_EXPECTATION_BRIDGE_RE.search(bridge)
                or _REFERENCE_BRIDGE_RE.search(bridge)
                or _EXCLUDED_REFERENCE_BRIDGE_RE.search(bridge)
            ):
                continue
            return True
    return False


def user_action_step_count(user_text: str) -> int:
    return len(_positive_user_action_verbs(user_text))


def affirmatively_authorized_action_reference(candidate: str, user_text: str) -> bool:
    """Authorize a directly requested navigation target, not an outcome or alternative."""

    return _directly_authorized_span(
        candidate,
        user_text,
        target_verbs=_ACTIVATION_TARGET_RE,
        reject_outcome_bridge=True,
        require_complete_target=True,
    )


def affirmatively_authorized_app_scope(candidate: str, user_text: str) -> bool:
    """Require an app to be a direct control target or an explicit action location."""

    if _has_negated_candidate_occurrence(candidate, user_text):
        # Explicitly excluding the same app anywhere in the task vetoes an
        # earlier/later positive mention. Ambiguous mixed authority fails closed.
        return False

    if _directly_authorized_span(
        candidate,
        user_text,
        target_verbs=_APP_CONTROL_TARGET_RE,
        reject_outcome_bridge=True,
        reject_quoted_occurrences=True,
    ):
        return True
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    quoted_ranges = _quoted_ranges(source)
    for start, end in _matching_spans(candidate, user_text):
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= end),
            default=len(source),
        )
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in payload_ranges
        ) or _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        if any(
            quote_start <= start and end <= quote_end for quote_start, quote_end in quoted_ranges
        ):
            continue
        clause = _mask_quoted_text(source, clause_start, clause_end)
        if _NEGATED_ACTION_RE.search(clause):
            continue
        local_start = start - clause_start
        local_end = end - clause_start
        prefix = clause[:local_start]
        suffix = clause[local_end:]
        english_scope = re.search(
            r"\b(?:in|inside|within|on)\s+(?:the\s+)?$",
            prefix,
            re.IGNORECASE,
        )
        chinese_scope = re.search(r"(?:在|于)\s*$", prefix)
        if english_scope is None and chinese_scope is None:
            continue
        if english_scope is not None:
            scope_marker = re.match(
                r"^\s*(?:(?:app|application)\b\s*)?(?:['’]s\s*)?",
                suffix,
                re.IGNORECASE,
            )
        else:
            scope_marker = re.match(
                r"^\s*(?:(?:app|应用)\s*)?(?:里面|里|中|内|上|的)\s*",
                suffix,
                re.IGNORECASE,
            )
        scoped_suffix = suffix[scope_marker.end() :] if scope_marker is not None else suffix
        scope_anchor = english_scope if english_scope is not None else chinese_scope
        assert scope_anchor is not None
        preposed_prefix = prefix[: scope_anchor.start()]
        if _POLITE_IMPERATIVE_PREFIX_RE.fullmatch(
            preposed_prefix
        ) is not None and _AFFIRMATIVE_ACTION_RE.match(scoped_suffix.lstrip()):
            return True
        trailing_outcome = _APP_TRAILING_OUTCOME_RE.fullmatch(scoped_suffix) is not None
        if (not scoped_suffix.strip() or trailing_outcome) and scope_anchor is not None:
            prior_scope = prefix[: scope_anchor.start()]
            prior_actions = list(_AFFIRMATIVE_ACTION_RE.finditer(prior_scope))
            if prior_actions:
                prior_action = prior_actions[-1]
                scope_bridge = prior_scope[prior_action.end() :]
                if not (
                    _verb_requires_imperative(prior_action)
                    and not _imperative_prefix(prior_scope[: prior_action.start()])
                ) and not (
                    _OUTCOME_BRIDGE_RE.search(scope_bridge)
                    or _REFERENCE_BRIDGE_RE.search(scope_bridge)
                    or _EXCLUDED_REFERENCE_BRIDGE_RE.search(scope_bridge)
                ):
                    return True
        delimiter = next(
            (match for match in boundaries if match.start() == clause_end),
            None,
        )
        if (
            delimiter is None
            or scoped_suffix.strip()
            or _POLITE_IMPERATIVE_PREFIX_RE.fullmatch(preposed_prefix) is None
        ):
            continue
        following_start = delimiter.end()
        following_end = min(
            (match.start() for match in boundaries if match.start() >= following_start),
            default=len(source),
        )
        following = _mask_quoted_text(source, following_start, following_end)
        if not _NEGATED_ACTION_RE.search(following) and _AFFIRMATIVE_ACTION_RE.search(following):
            return True
    return False


def _affirmatively_authorized_span(
    candidate: str,
    user_text: str,
    *,
    competing_labels: tuple[str, ...] = (),
) -> bool:
    return _directly_authorized_span(
        candidate,
        user_text,
        target_verbs=_AFFIRMATIVE_ACTION_RE,
        allow_preposed_text_target=True,
        reject_outcome_bridge=True,
        competing_labels=competing_labels,
        require_complete_target=True,
    )


def _affirmatively_authorized_action_target(
    candidate: str,
    user_text: str,
    action_type: DesktopActionType,
    *,
    competing_labels: tuple[str, ...] = (),
) -> bool:
    if action_type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
        return _directly_authorized_span(
            candidate,
            user_text,
            target_verbs=_TEXT_PAYLOAD_ACTION_RE,
            allow_preposed_text_target=True,
            require_complete_target=True,
        )
    if action_type == DesktopActionType.SCROLL:
        target_verbs = _SCROLL_TARGET_RE
    else:
        target_verbs = _ACTIVATION_TARGET_RE
    return _directly_authorized_span(
        candidate,
        user_text,
        target_verbs=target_verbs,
        reject_outcome_bridge=True,
        competing_labels=competing_labels,
        require_complete_target=True,
    )


def _expectation_semantically_authorized(
    expectation: DesktopExpectation,
    user_text: str,
) -> bool:
    """Bind a planner postcondition to the polarity the user actually requested."""

    candidate = expectation.text or ""
    source = _normalized(user_text)
    boundaries = tuple(_CLAUSE_BOUNDARY_RE.finditer(source))
    payload_ranges = _text_payload_ranges(source)
    masked_source = _mask_text_payloads(source, 0, len(source))
    occurrences: list[tuple[str, int, int]] = []
    for start, end in _matching_spans(candidate, user_text):
        clause_start = max(
            (match.end() for match in boundaries if match.end() <= start),
            default=0,
        )
        clause_end = min(
            (match.start() for match in boundaries if match.start() >= end),
            default=len(source),
        )
        if any(
            payload_start <= start and end <= payload_end
            for payload_start, payload_end in payload_ranges
        ) or _span_is_quoted_text_payload(source, start, end, clause_start):
            continue
        trailing = source[end:clause_end]
        if trailing.strip() and not re.fullmatch(
            r"\s*(?:(?:(?:is|becomes?|remains?)\s+)?"
            r"(?:appears?|visible|selected|focused|present|absent|hidden|missing)|"
            r"出现|显示|可见|选中|聚焦|消失|隐藏|不存在|不可见)\s*",
            trailing,
            re.IGNORECASE,
        ):
            continue
        occurrences.append(
            (
                _mask_text_payloads(source, clause_start, clause_end),
                start - clause_start,
                end - clause_start,
            )
        )
    if not occurrences:
        return False
    if any(_NEGATED_ACTION_RE.search(clause) for clause, _, _ in occurrences):
        return False
    affirmative_clauses = [clause for clause, _, _ in occurrences]

    if expectation.kind == DesktopExpectationKind.TEXT_PRESENT:
        if _NEGATIVE_PRESENT_STATE_RE.search(masked_source):
            return False
        for clause, local_start, local_end in occurrences:
            prior = [
                match
                for match in _POSITIVE_PRESENT_STATE_RE.finditer(clause)
                if match.end() <= local_start
            ]
            if prior:
                state_verb = prior[-1]
                if (
                    _ACTIVATION_TARGET_RE.fullmatch(state_verb.group()) is None
                    or not _imperative_prefix(clause[: state_verb.start()])
                ) and _STATE_TARGET_BRIDGE_RE.fullmatch(clause[state_verb.end() : local_start]):
                    return True
            following = _POSITIVE_PRESENT_STATE_RE.search(clause, local_end)
            if following is not None and _STATE_AFTER_TARGET_BRIDGE_RE.fullmatch(
                clause[local_end : following.start()]
            ):
                return True
        return False
    if expectation.kind == DesktopExpectationKind.TEXT_ABSENT:
        return any(_POSITIVE_ABSENT_STATE_RE.search(clause) for clause in affirmative_clauses)
    if expectation.kind == DesktopExpectationKind.ELEMENT_SELECTED:
        if _NEGATIVE_SELECTED_STATE_RE.search(masked_source):
            return False
        return any(_POSITIVE_SELECTED_STATE_RE.search(clause) for clause in affirmative_clauses)
    if expectation.kind == DesktopExpectationKind.FOCUSED_CONTAINS:
        if _NEGATIVE_FOCUSED_STATE_RE.search(masked_source):
            return False
        return any(_POSITIVE_FOCUSED_STATE_RE.search(clause) for clause in affirmative_clauses)
    return False


def expectation_is_terminal_user_condition(
    expectation: DesktopExpectation | None,
    user_text: str,
    *,
    last_action: DesktopAction | None,
    last_action_target: str | None,
) -> bool:
    """Require DONE evidence to belong to the user's final positive desktop action."""

    if expectation is None or not expectation.text or last_action is None or not last_action_target:
        return False
    final_step = user_action_step_count(user_text) - 1
    if final_step < 0:
        return False
    return action_matches_next_user_step(
        last_action,
        last_action_target,
        user_text,
        completed_steps=final_step,
    ) and expectation_matches_user_step(
        last_action,
        last_action_target,
        expectation,
        user_text,
        completed_steps=final_step,
    )


def _surface_identity(observation: DesktopObservation) -> str:
    return "\n".join((observation.app, observation.window_title or ""))


def _full_observation_context(observation: DesktopObservation) -> str:
    structured = json.dumps(
        [element.fingerprint_payload() for element in observation.elements],
        ensure_ascii=False,
        sort_keys=True,
    )
    return "\n".join(
        (
            observation.app,
            observation.window_title or "",
            observation.accessibility_text,
            structured,
        )
    )


def _element_line(observation: DesktopObservation, element_index: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(element_index)}(?:\s|$)")
    return next(
        (line for line in observation.accessibility_text.splitlines() if pattern.match(line)),
        None,
    )


def _focused_context(observation: DesktopObservation) -> str:
    markers = (
        "the focused ui element is ",
        "focused=true",
        "haskeyboardfocus=true",
        "[focused]",
        "focused:",
    )
    return "\n".join(
        line
        for line in observation.accessibility_text.splitlines()
        if any(marker in _normalized(line) for marker in markers)
    )


def _action_digest(
    action: DesktopAction,
    observation: DesktopObservation,
    expectation: DesktopExpectation | None,
) -> str:
    payload = {
        "type": action.type.value,
        "app": action.app,
        "arguments": action.tool_arguments(),
        "observation_fingerprint": observation.fingerprint,
        "expectation": (
            {"kind": expectation.kind.value, "text": expectation.text}
            if expectation is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _allow(reason: str) -> DesktopSafetyResult:
    return DesktopSafetyResult(DesktopSafetyDisposition.ALLOW, reason)


def _block(reason: str) -> DesktopSafetyResult:
    return DesktopSafetyResult(DesktopSafetyDisposition.BLOCK, reason)


def _confirm(
    reason: str,
    action: DesktopAction,
    observation: DesktopObservation,
    expectation: DesktopExpectation | None = None,
    *,
    trusted_target_label: str | None = None,
) -> DesktopSafetyResult:
    digest = _action_digest(action, observation, expectation)
    target = next(
        (element for element in observation.elements if element.index == action.element_index),
        None,
    )
    target_type = target.control_type[:32] if target is not None else "unknown"
    target_label_digest = hashlib.sha256(
        (target.name if target is not None else "").encode("utf-8")
    ).hexdigest()[:8]
    window_label_digest = hashlib.sha256(
        (observation.window_title or "").encode("utf-8")
    ).hexdigest()[:8]
    provenance = (
        "; payload="
        + json.dumps(
            action.text if action.type == DesktopActionType.TYPE_TEXT else action.value,
            ensure_ascii=False,
        )[:72]
        + " (exact user-spoken span)"
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}
        else ""
    )
    trusted_target = (
        "; user-spoken-target=" + json.dumps(trusted_target_label[:48], ensure_ascii=False)
        if trusted_target_label
        else ""
    )
    parameter_values = {
        "key": action.key,
        "direction": action.direction,
        "pages": action.pages,
        "secondary-action": action.action_name,
        "click-count": action.click_count,
        "mouse-button": action.mouse_button,
    }
    trusted_parameters = "; ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in parameter_values.items()
        if value is not None
    )
    parameter_summary = f"; {trusted_parameters}" if trusted_parameters else ""
    confirmation = DesktopConfirmation(
        confirmation_id=f"desktop-{digest[:16]}",
        action_digest=digest,
        summary=(
            f"{reason}; action={action.type.value}; app={action.app}; "
            f"{trusted_target.lstrip('; ')}{provenance}{parameter_summary}; "
            f"target-control={target_type}; untrusted-window-label-digest={window_label_digest}; "
            f"untrusted-target-label-digest={target_label_digest}; binding={digest[:10]}"
        ),
    )
    return DesktopSafetyResult(
        DesktopSafetyDisposition.CONFIRM,
        reason,
        confirmation=confirmation,
    )


class DesktopSafetyPolicy:
    """Local, monotonic safety checks for one atomic desktop action."""

    def planner_observation(
        self,
        observation: DesktopObservation,
        *,
        user_text: str,
    ) -> DesktopObservation:
        """Return the minimal task-authorized UI subset that may cross a planner boundary."""

        name_counts: dict[str, int] = {}
        for element in observation.elements:
            normalized_name = _normalized(element.name.strip())
            if normalized_name:
                name_counts[normalized_name] = name_counts.get(normalized_name, 0) + 1

        authorized: list[DesktopElement] = []
        competing_labels = tuple(
            element.name for element in observation.elements if element.name.strip()
        )
        for element in observation.elements:
            normalized_name = _normalized(element.name.strip())
            if (
                element.password
                or not normalized_name
                or name_counts.get(normalized_name) != 1
                or contains_credential_like_value(element.name)
                or not _affirmatively_authorized_span(
                    element.name,
                    user_text,
                    competing_labels=competing_labels,
                )
            ):
                continue
            authorized.append(
                DesktopElement(
                    index=element.index,
                    name=element.name,
                    control_type=element.control_type,
                    selected=element.selected,
                    focused=element.focused,
                    password=False,
                    enabled=element.enabled,
                )
            )
        lines = [json.dumps({"app": observation.app, "task_authorized_subset": True})]
        for element in authorized:
            fields = element.planner_payload()
            lines.append(
                f"{element.index} "
                + " ".join(
                    f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
                    for key, value in fields.items()
                    if key != "index" and value is not None
                )
            )
        return DesktopObservation(
            app=observation.app,
            generation=observation.generation,
            accessibility_text="\n".join(lines),
            screenshot_png=None,
            window_title=f"{observation.app} task-authorized UI subset",
            elements=tuple(authorized),
            captured_at=observation.captured_at,
        )

    def inspect_observation(
        self,
        observation: DesktopObservation,
        *,
        user_text: str = "",
    ) -> DesktopSafetyResult:
        """Block sensitive surfaces before their UI tree is sent to a cloud planner."""

        if "\ufffd" in observation.accessibility_text:
            return _block("accessibility state contains damaged Unicode")
        identity = _surface_identity(observation)
        if _contains_term(identity, _TERMINAL_TERMS):
            return _block("terminal and shell windows are outside the desktop agent boundary")
        if _contains_term(identity, _RUN_IDENTITY_TERMS):
            return _block("the Windows Run dialog is outside the desktop agent boundary")
        if _contains_term(identity, _UAC_TERMS):
            return _block("UAC and operating-system security prompts cannot be automated")
        if _looks_like_auth_surface(identity):
            return _block("authentication windows cannot be sent to the desktop planner")
        if any(element.password for element in observation.elements):
            return _block("password-entry surfaces cannot be sent to the desktop planner")
        # The raw UIA snapshot never leaves this process, but it must still be
        # classified locally before a harmless-looking subset is sent or acted
        # on.  Otherwise a generic "Next" button could advance a payment or
        # credential surface hidden in an unmentioned sibling element.
        local_context = _full_observation_context(observation)
        if contains_credential_like_value(local_context):
            return _block("credential-like value is present on the local desktop surface")
        if _contains_term(local_context, _TERMINAL_TERMS):
            return _block("terminal and shell surfaces are outside the desktop agent boundary")
        if _contains_term(local_context, _UAC_TERMS):
            return _block("UAC and operating-system security prompts cannot be automated")
        if _looks_like_auth_surface(local_context):
            return _block("authentication surfaces cannot be sent to the desktop planner")
        if _contains_term(local_context, _SECRET_TERMS):
            return _block("password and credential surfaces cannot be sent to the desktop planner")
        if _looks_like_payment_surface(local_context):
            return _block("payments and purchase surfaces are outside the desktop agent boundary")
        if _looks_like_privacy_surface(local_context):
            return _block("privacy, account, telemetry, and link-sharing surfaces are prohibited")
        planner_view = (
            self.planner_observation(observation, user_text=user_text) if user_text else observation
        )
        full_context = _full_observation_context(planner_view)
        if contains_credential_like_value(full_context):
            return _block("credential-like value cannot be sent to the desktop planner")
        if _contains_term(full_context, _TERMINAL_TERMS):
            return _block("terminal and shell surfaces are outside the desktop agent boundary")
        if _contains_term(full_context, _UAC_TERMS):
            return _block("UAC and operating-system security prompts cannot be automated")
        if _looks_like_auth_surface(full_context):
            return _block("authentication surfaces cannot be sent to the desktop planner")
        if _contains_term(full_context, _SECRET_TERMS):
            return _block("password and credential surfaces cannot be sent to the desktop planner")
        if _looks_like_payment_surface(full_context):
            return _block("payments and purchase surfaces are outside the desktop agent boundary")
        if _looks_like_privacy_surface(full_context):
            return _block("privacy, account, telemetry, and link-sharing surfaces are prohibited")
        return _allow("observation is within the locally allow-listed desktop boundary")

    def evaluate(
        self,
        action: DesktopAction,
        observation: DesktopObservation,
        *,
        user_text: str = "",
        expectation: DesktopExpectation | None = None,
    ) -> DesktopSafetyResult:
        action_is_bound = _same_app(action.app, observation.app) and (
            action.generation == observation.generation
        )
        if not action_is_bound:
            return _block("action is not bound to the current application observation")
        if "\ufffd" in observation.accessibility_text:
            return _block("accessibility state contains damaged Unicode")
        normalized_task = _normalized(user_text)
        if _UNSUPPORTED_CONTROL_CONDITION_RE.search(
            _mask_text_payloads(normalized_task, 0, len(normalized_task))
        ):
            return _block(
                "conditional desktop actions require unsupported local condition evaluation"
            )

        inspection = self.inspect_observation(observation, user_text=user_text)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return inspection

        identity = _surface_identity(observation)
        if _contains_term(identity, _TERMINAL_TERMS):
            return _block("terminal and shell surfaces are outside the desktop agent boundary")
        if _contains_term(identity, _RUN_IDENTITY_TERMS):
            return _block("the Windows Run dialog is outside the desktop agent boundary")
        if _contains_term(identity, _UAC_TERMS):
            return _block("UAC and operating-system security prompts cannot be automated")
        if _looks_like_auth_surface(identity):
            return _block("authentication windows cannot be automated")

        if action.type in {DesktopActionType.CLICK, DesktopActionType.DRAG} and (
            action.element_index is None
        ):
            return _block("coordinate-only actions have no locally verifiable semantic target")

        target_context = ""
        if action.element_index is not None:
            target_line = _element_line(observation, action.element_index)
            if target_line is None:
                return _block("element target is absent from the current accessibility state")
            target_context = target_line
        elif action.type == DesktopActionType.TYPE_TEXT:
            target_context = _focused_context(observation)
            if not target_context:
                return _block("text input has no locally identified focused accessibility element")
        elif action.type == DesktopActionType.PRESS_KEY:
            target_context = _focused_context(observation)

        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.PRESS_KEY}:
            focused_targets = [
                element
                for element in observation.elements
                if element.index == action.element_index and element.focused is True
            ]
            if len(focused_targets) != 1:
                return _block("text and key input must target the unique observed focused element")
        if action.type == DesktopActionType.PRESS_KEY and not is_allowed_desktop_key(action.key):
            return _block("key input is outside the navigation-only allow-list")

        action_payload = (
            action.text
            if action.type == DesktopActionType.TYPE_TEXT
            else action.value
            if action.type == DesktopActionType.SET_VALUE
            else ""
        )
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            payload_is_exact = bool(action_payload) and any(
                _verb_matches_action_type(verb, action.type)
                and _payload_belongs_to_verb(action_payload, user_text, verb)
                for verb in _positive_user_action_verbs(user_text)
            )
            if not payload_is_exact:
                return _block("text payload is not the complete user-authored payload")

        risk_context = "\n".join(
            (target_context, action.action_name or "", action_payload or "", user_text)
        )
        if _contains_term(risk_context, _TERMINAL_TERMS):
            return _block("the selected target is a terminal or shell surface")
        if _contains_term(risk_context, _UAC_TERMS):
            return _block("the selected target is an operating-system security prompt")
        if _looks_like_auth_surface(risk_context):
            return _block("authentication and account-verification targets cannot be automated")
        if _contains_term(risk_context, _SECRET_TERMS):
            return _block("passwords, credentials, and secret-entry surfaces cannot be automated")
        if _looks_like_payment_surface(risk_context):
            return _block("payments, purchases, and transfers cannot be automated")
        if _looks_like_privacy_surface(risk_context):
            return _block("privacy, account, telemetry, and link-sharing settings are prohibited")

        key = _normalized(action.key or "").replace(" ", "")
        target = next(
            (element for element in observation.elements if element.index == action.element_index),
            None,
        )
        competing_labels = tuple(
            element.name for element in observation.elements if element.name.strip()
        )
        if target is not None and not any(
            action_matches_next_user_step(
                action,
                target.name,
                user_text,
                completed_steps=step,
            )
            for step in range(user_action_step_count(user_text))
        ):
            return _block("action parameters are not bound to an explicit user step")
        if target is not None:
            target_name = _normalized(target.name.strip())
            target_control_type = _normalized(target.control_type)
            if (
                action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}
                and target_control_type not in _EDITABLE_CONTROL_TYPES
            ):
                return _block("text input target is not an editable accessibility control")
            same_name_targets = [
                element
                for element in observation.elements
                if _normalized(element.name.strip()) == target_name
                and (
                    action.type not in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}
                    or _normalized(element.control_type) in _EDITABLE_CONTROL_TYPES
                )
            ]
            if not target_name or len(same_name_targets) != 1:
                return _block(
                    "semantic target label is absent or ambiguous in the current observation"
                )

        text_confirmation_target = ""
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            # Exact-span provenance prevents invented payloads.  A one-time user
            # challenge is still required because an exact span may be quoted or
            # explicitly negated (for example, "不要输入 X").
            if (
                target is None
                or not target.name.strip()
                or not _affirmatively_authorized_action_target(
                    target.name,
                    user_text,
                    action.type,
                    competing_labels=competing_labels,
                )
            ):
                return _block("text target label is not affirmatively named in the user task")
            text_confirmation_target = target.name

        if action.type in {
            DesktopActionType.CLICK,
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            DesktopActionType.SCROLL,
        } and (
            target is None
            or not target.name.strip()
            or not _affirmatively_authorized_action_target(
                target.name,
                user_text,
                action.type,
                competing_labels=competing_labels,
            )
        ):
            return _block("UI target label is not affirmatively named in the user task")
        if (
            action.type == DesktopActionType.PRESS_KEY
            and key in {"enter", "return", "space"}
            and (
                target is None
                or not target.name.strip()
                or not _affirmatively_authorized_action_target(
                    target.name,
                    user_text,
                    action.type,
                    competing_labels=competing_labels,
                )
            )
        ):
            return _block("key activation target is not affirmatively named in the user task")
        if (
            action.type == DesktopActionType.PRESS_KEY
            and key == "escape"
            and (
                target is None
                or not target.name.strip()
                or not _affirmatively_authorized_action_target(
                    target.name,
                    user_text,
                    action.type,
                    competing_labels=competing_labels,
                )
            )
        ):
            return _block("Escape target is not affirmatively named in the user task")

        mutating_navigation_keys = {
            "pageup",
            "pagedown",
            "home",
            "end",
            "left",
            "up",
            "right",
            "down",
        }
        if (
            action.type == DesktopActionType.PRESS_KEY
            and key in mutating_navigation_keys
            and (
                target is None
                or not target.name.strip()
                or not _affirmatively_authorized_action_target(
                    target.name,
                    user_text,
                    action.type,
                    competing_labels=competing_labels,
                )
            )
        ):
            return _block("state-changing navigation target is not named in the user task")

        if expectation is not None and expectation.text:
            expected = _normalized(expectation.text)
            payload_exact = {
                _normalized(value)
                for value in (
                    action.text or "",
                    action.value or "",
                )
                if value
            }
            expected_is_authorized = _expectation_semantically_authorized(
                expectation,
                user_text,
            )
            if expected in payload_exact and expectation.kind in {
                DesktopExpectationKind.TEXT_PRESENT,
                DesktopExpectationKind.FOCUSED_CONTAINS,
            }:
                expected_is_authorized = True
            if target is None or not any(
                action_matches_next_user_step(
                    action,
                    target.name,
                    user_text,
                    completed_steps=step,
                )
                and expectation_matches_user_step(
                    action,
                    target.name,
                    expectation,
                    user_text,
                    completed_steps=step,
                )
                for step in range(user_action_step_count(user_text))
            ):
                expected_is_authorized = False
            if not expected_is_authorized:
                return _block("postcondition text is not bound to the user task or target")
            target_name = _normalized(target.name) if target is not None else ""
            if action.type in {
                DesktopActionType.CLICK,
                DesktopActionType.PERFORM_SECONDARY_ACTION,
            }:
                if expectation.kind == DesktopExpectationKind.TEXT_ABSENT:
                    return _block("target disappearance alone cannot prove click success")
                if (
                    expectation.kind
                    in {
                        DesktopExpectationKind.ELEMENT_SELECTED,
                        DesktopExpectationKind.FOCUSED_CONTAINS,
                    }
                    and expected != target_name
                ):
                    return _block("click selection/focus postcondition must name its exact target")
                if expectation.kind == DesktopExpectationKind.FOCUSED_CONTAINS:
                    return _block("click focus alone cannot prove the requested navigation result")
                if expectation.kind == DesktopExpectationKind.TEXT_PRESENT and (
                    not expected_is_authorized or expected == target_name
                ):
                    return _block(
                        "click navigation needs a distinct user-authored destination state"
                    )
            elif action.type == DesktopActionType.SCROLL:
                if (
                    expectation.kind != DesktopExpectationKind.TEXT_PRESENT
                    or not expected_is_authorized
                ):
                    return _block("scroll needs a user-authored newly visible text condition")
            elif action.type in {
                DesktopActionType.TYPE_TEXT,
                DesktopActionType.SET_VALUE,
            } and expectation.kind not in {
                DesktopExpectationKind.FOCUSED_CONTAINS,
                DesktopExpectationKind.TEXT_PRESENT,
            }:
                return _block("text input needs an exact local text-presence postcondition")
            elif action.type == DesktopActionType.PRESS_KEY:
                if key in {"enter", "return", "space"} and expectation.kind in {
                    DesktopExpectationKind.TEXT_ABSENT,
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                }:
                    return _block("key activation needs selection or positive destination evidence")
                if key in {"tab", "shift+tab"} and (
                    expectation.kind != DesktopExpectationKind.FOCUSED_CONTAINS
                    or not expected_is_authorized
                ):
                    return _block("focus navigation needs a user-authored focused target")
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            if expectation is None or not expectation.text:
                return _block("text input needs an exact local text-presence postcondition")
            return _confirm(
                "enter exact user-spoken text",
                action,
                observation,
                expectation,
                trusted_target_label=text_confirmation_target,
            )
        if action.type == DesktopActionType.PRESS_KEY and key in {
            "ctrl+v",
            "control+v",
            "shift+insert",
        }:
            return _block("clipboard paste is not allowed because its contents are unverified")
        if action.type == DesktopActionType.PRESS_KEY and key in {
            "alt+f4",
            "ctrl+w",
            "control+w",
            "ctrl+q",
            "control+q",
        }:
            return _confirm("close a window or application", action, observation, expectation)
        if action.type == DesktopActionType.PRESS_KEY and key in {
            "ctrl+enter",
            "control+enter",
            "alt+s",
        }:
            return _confirm("send or submit external content", action, observation, expectation)
        if action.type == DesktopActionType.PRESS_KEY and key == "shift+delete":
            return _confirm("permanently delete data", action, observation, expectation)

        target_control_type = _normalized(target.control_type) if target is not None else ""
        if (
            action.type == DesktopActionType.PRESS_KEY
            and key == "space"
            and target_control_type in {"edit", "document"}
        ):
            return _block("Space on an editable control must use exact verified text input")
        if (
            action.type == DesktopActionType.PRESS_KEY
            and key in {"enter", "return"}
            and target_control_type in {"edit", "document"}
        ):
            assert target is not None
            return _confirm(
                "submit or change editable content with Enter",
                action,
                observation,
                expectation,
                trusted_target_label=target.name,
            )
        if action.type == DesktopActionType.PRESS_KEY and key == "escape":
            assert target is not None
            return _confirm(
                "dismiss or close the focused application state",
                action,
                observation,
                expectation,
                trusted_target_label=target.name,
            )

        if action.type == DesktopActionType.PRESS_KEY and key in mutating_navigation_keys:
            assert target is not None
            return _confirm(
                "change a focused control with a navigation key",
                action,
                observation,
                expectation,
                trusted_target_label=target.name,
            )

        side_effect_context = risk_context
        whole_surface_activation = action.type in {
            DesktopActionType.CLICK,
            DesktopActionType.PERFORM_SECONDARY_ACTION,
        } or (
            action.type == DesktopActionType.PRESS_KEY
            and key in {"enter", "return", "space", "escape"}
        )
        if whole_surface_activation:
            # An innocuous-looking Next/Proceed control can commit a side effect described
            # by a sibling label. Inspect the whole current surface for every activation,
            # not only buttons named OK/Yes/Confirm.
            side_effect_context = "\n".join(
                (
                    side_effect_context,
                    _full_observation_context(observation),
                    user_text,
                )
            )

        if action.type in {
            DesktopActionType.CLICK,
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            DesktopActionType.PRESS_KEY,
        }:
            for reason, terms in _CONFIRM_GROUPS:
                if _contains_term(side_effect_context, terms):
                    trusted_target_label = (
                        target.name
                        if target is not None
                        and _affirmatively_authorized_action_target(
                            target.name,
                            user_text,
                            action.type,
                            competing_labels=competing_labels,
                        )
                        else ""
                    )
                    return _confirm(
                        reason,
                        action,
                        observation,
                        expectation,
                        trusted_target_label=trusted_target_label,
                    )

        return _allow("action stays within the locally allow-listed desktop boundary")
