"""Chrome MV3 扩展的静态权限、协议与隐私边界测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = PROJECT_ROOT / "extension"


def test_manifest_uses_only_required_http_https_content_access() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "0.3.0"
    assert int(manifest["minimum_chrome_version"]) >= 116
    assert manifest.get("permissions", []) == []
    assert manifest.get("host_permissions", []) == []
    assert manifest["action"] == {"default_title": "连接本地采集助手"}
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
    assert "const PROTOCOL_VERSION = 3" in source


def test_background_stops_after_bounded_retry_burst_and_supports_manual_retry() -> None:
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    assert "const MAX_RECONNECT_ATTEMPTS = 3" in source
    assert "RECONNECT_COOLDOWN_MS" not in source
    assert "桌面端未连接；点击图标重新连接" in source
    assert "chrome.action.onClicked.addListener" in source
    assert "if (socket !== candidate)" in source
    assert "candidate.close();" not in source
    assert source.rstrip().endswith("connect();")


def test_background_bounds_and_normalizes_v3_observation_context() -> None:
    source = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")

    assert 'typeof value.sensitive_input !== "boolean"' in source
    assert "normalizePageContext(response, activeTab.id)" in source
    assert "tab_id: tabId" in source
    assert "OBSERVATION_KINDS" in source
    assert 'new Set(["none", "selection", "caption"])' in source
    assert "OBSERVATION_JSON_BYTES = 20 * 1_024" in source
    assert "VIDEO_KEY_JSON_BYTES = 256" in source
    assert "observation_text: observationText" in source
    assert "observation_kind: observationKind" in source
    assert "video_key: videoKey" in source
    assert "selection: sensitiveInput" in source
    assert "let observationText = sensitiveInput" in source
    assert "value.tab_id" not in source


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
    assert 'localName(activeElement) === "iframe"' in source
    assert "element.localName" in source
    assert "instanceof HTMLInputElement" not in source
    assert "instanceof HTMLTextAreaElement" not in source
    assert "instanceof HTMLIFrameElement" not in source
    assert 'const selection = sensitiveInput ? "" : selectedText(activeElement)' in source
    assert 'sensitiveInput\n    ? { text: "", kind: "none" }' in source
    assert "sensitive_input: sensitiveInput" in source


def test_content_observation_prefers_enabled_html5_captions_then_selection() -> None:
    source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")

    assert "video.textTracks" in source
    assert "track.activeCues" in source
    assert 'document.querySelectorAll("video")' in source
    assert "videoVisibilityScore" in source
    assert "videos.length > MAX_VIDEO_ELEMENTS" in source
    assert "visible.length !== 1" in source
    assert 'track.mode !== "showing"' in source
    assert 'kind !== "captions" && kind !== "subtitles"' in source
    assert "cue.text" in source
    caption_position = source.index("const captionText = activeCaptionText(video)")
    selection_position = source.index("const selectionText = normalizedObservationText(selection)")
    assert caption_position < selection_position
    assert 'return { text: captionText, kind: "caption" }' in source
    assert 'return { text: selectionText, kind: "selection" }' in source


def test_content_observation_excludes_editable_controls_and_is_bounded() -> None:
    source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")

    assert "MAX_SELECTION_CHARACTERS = 16_384" in source
    assert "MAX_OBSERVATION_CHARACTERS = 16_384" in source
    assert "MAX_OBSERVATION_SCAN_CHARACTERS = 65_536" in source
    assert "MAX_VIDEO_ELEMENTS = 32" in source
    assert "MAX_TEXT_TRACKS = 16" in source
    assert "MAX_ACTIVE_CUES_PER_TRACK = 32" in source
    assert "value.slice(0, maximumCharacters * 2)" in source
    assert "Math.min(video.textTracks.length, MAX_TEXT_TRACKS)" in source
    assert "Math.min(" in source and "MAX_ACTIVE_CUES_PER_TRACK" in source
    assert 'elementName === "input"' in source
    assert 'elementName === "textarea"' in source
    assert 'elementName === "select"' in source
    assert "element.isContentEditable === true" in source
    assert 'role === "textbox"' in source
    assert 'if (isEditableControl(activeElement))' in source
    assert 'return { text: "", kind: "none" }' in source
    assert 'replace(/\\u0000/g, "")' in source
    assert 'replace(/\\s+/gu, " ")' in source


def test_video_key_uses_page_local_identity_and_never_exports_current_src() -> None:
    source = (EXTENSION_DIR / "content.js").read_text(encoding="utf-8")

    assert "const VIDEO_STATES = new WeakMap()" in source
    assert "video.currentSrc" in source
    assert "state.epoch += 1" in source
    assert "VIDEO_STATES.set(video, state)" in source
    assert 'video.addEventListener("emptied", advanceEpoch)' in source
    assert 'video.addEventListener("loadstart", advanceEpoch)' in source
    assert "`video-${state.identity}:${state.epoch}`" in source
    response_source = source.split("sendResponse({", maxsplit=1)[1]
    assert "video_key: videoKey(video)" in response_source
    assert "currentSrc" not in response_source
