"""捕获前可确定的本地敏感场景拦截规则。

这里只使用前台进程名和浏览器扩展明确返回的敏感输入标记，不分析或记录用户
输入内容。规则无法覆盖所有敏感应用，因此调用方必须把它视为保守保护而非保证。
"""

from __future__ import annotations

from pathlib import PureWindowsPath


_SENSITIVE_PROCESSES = frozenset(
    {
        "1password",
        "bitwarden",
        "credentialui",
        "credentialuibroker",
        "dashlane",
        "enpass",
        "keeperpasswordmanager",
        "keepass",
        "keepassxc",
        "lastpass",
        "lockapp",
        "logonui",
        "nordpass",
        "passwordsafe",
        "protonpass",
        "roboform",
    }
)

_CHROMIUM_PROCESSES = frozenset(
    {
        "brave",
        "brave-browser",
        "chrome",
        "chromium",
        "google-chrome",
        "msedge",
        "opera",
        "opera_gx",
        "vivaldi",
    }
)


def normalize_process_name(app_name: str | None) -> str | None:
    """返回不含路径和 ``.exe`` 的小写 Windows 进程名。"""

    if not app_name or not app_name.strip():
        return None
    normalized = PureWindowsPath(app_name.strip()).name.casefold()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized or None


def is_sensitive_application(app_name: str | None) -> bool:
    """已知密码管理器或 Windows 凭据/锁屏进程在前台时返回 ``True``。"""

    normalized = normalize_process_name(app_name)
    return normalized in _SENSITIVE_PROCESSES if normalized is not None else False


def is_chromium_application(app_name: str | None) -> bool:
    """当前浏览器扩展支持的 Chromium 进程在前台时返回 ``True``。"""

    normalized = normalize_process_name(app_name)
    return normalized in _CHROMIUM_PROCESSES if normalized is not None else False


def capture_block_reason(
    app_name: str | None,
    *,
    browser_sensitive_input: bool = False,
) -> str | None:
    """返回本次捕获应暂停的原因文本；可以捕获时返回 ``None``。"""

    if is_sensitive_application(app_name):
        return "检测到密码管理器、系统凭据或锁屏窗口，已暂停本次捕获。"
    if browser_sensitive_input:
        return "检测到浏览器密码、验证码或支付输入框，已暂停本次捕获。"
    return None
