"""Chrome MV3 扩展的静态权限与隐私边界测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = PROJECT_ROOT / "extension"


def test_manifest_uses_only_required_http_https_content_access() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert int(manifest["minimum_chrome_version"]) >= 116
    assert manifest.get("permissions", []) == []
    assert manifest.get("host_permissions", []) == []
    assert manifest["background"] == {"service_worker": "background.js"}
    assert manifest["content_scripts"] == [
        {
            "matches": ["http://*/*", "https://*/*"],
            "js": ["content.js"],
            "run_at": "document_idle",
        }
    ]
    assert (
        manifest["content_security_policy"]["extension_pages"]
        == "script-src 'self'; object-src 'self'; connect-src ws://127.0.0.1:8765"
    )


def test_background_connects_only_to_loopback_and_requires_focused_window() -> None:
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    websocket_urls = re.findall(r"wss?://[^\"']+", source)

    assert websocket_urls == ["ws://127.0.0.1:8765"]
    assert "chrome.windows.getLastFocused" in source
    assert "focusedWindow.focused !== true" in source
    assert "windowId: focusedWindow.id" in source
    assert "lastFocusedWindow: true" not in source
    assert "new TextEncoder()" in source
    assert "KEEPALIVE_INTERVAL_MS = 20_000" in source
    assert "const PROTOCOL_VERSION = 2" in source


def test_background_requires_boolean_sensitive_flag_and_clears_selection() -> None:
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    assert 'typeof value.sensitive_input !== "boolean"' in source
    assert 'selection: value.sensitive_input' in source
    assert 'sensitive_input: value.sensitive_input' in source


def test_content_script_never_reads_common_sensitive_input_values() -> None:
    source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")

    for marker in (
        'inputType === "password"',
        '"current-password"',
        '"new-password"',
        '"one-time-code"',
        '"cc-number"',
        '"cc-csc"',
        '"cc-exp"',
        '"cc-name"',
    ):
        assert marker in source
    assert "activeState.uninspectableFrame || isSensitiveInput(activeElement)" in source
    assert 'activeName === "iframe"' in source
    assert "element.localName" in source
    assert "instanceof HTMLInputElement" not in source
    assert "instanceof HTMLTextAreaElement" not in source
    assert "instanceof HTMLIFrameElement" not in source
    assert 'selection: sensitiveInput ? "" : selectedText(activeElement)' in source
    assert "sensitive_input: sensitiveInput" in source
