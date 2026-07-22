"""捕获前敏感应用与浏览器输入信号的纯逻辑测试。"""

from __future__ import annotations

import pytest

from app.safety import (
    capture_block_reason,
    is_chromium_application,
    is_sensitive_application,
    normalize_process_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\Program Files\KeePassXC\KeePassXC.exe", "keepassxc"),
        ("BITWARDEN.EXE", "bitwarden"),
        (" chrome.exe ", "chrome"),
        (None, None),
        ("  ", None),
    ],
)
def test_normalize_process_name(raw: str | None, expected: str | None) -> None:
    assert normalize_process_name(raw) == expected


@pytest.mark.parametrize(
    "app_name",
    [
        "KeePass.exe",
        "KeePassXC.exe",
        "1Password.exe",
        "Bitwarden.exe",
        "CredentialUI.exe",
        "CredentialUIBroker.exe",
        "LockApp.exe",
        "LogonUI.exe",
    ],
)
def test_known_sensitive_applications_are_blocked(app_name: str) -> None:
    assert is_sensitive_application(app_name)
    assert capture_block_reason(app_name) is not None


def test_normal_application_without_browser_signal_is_allowed() -> None:
    assert not is_sensitive_application("chrome.exe")
    assert is_chromium_application(r"C:\Program Files\Google\Chrome\chrome.exe")
    assert not is_chromium_application("notepad.exe")
    assert capture_block_reason("chrome.exe") is None


def test_browser_sensitive_input_blocks_even_for_unknown_process() -> None:
    reason = capture_block_reason(None, browser_sensitive_input=True)

    assert reason is not None
    assert "浏览器" in reason
