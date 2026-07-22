"use strict";

const BRIDGE_URL = "ws://127.0.0.1:8765";
const PROTOCOL_VERSION = 1;
const KEEPALIVE_INTERVAL_MS = 20_000;
const STABLE_CONNECTION_MS = 60_000;
const MAX_RECONNECT_ATTEMPTS = 8;
const MAX_RECONNECT_DELAY_MS = 30_000;
const RECONNECT_COOLDOWN_MS = 5 * 60_000;
const MAX_PROTOCOL_MESSAGE_BYTES = 60 * 1_024;
const URL_JSON_BYTES = 12 * 1_024;
const TITLE_JSON_BYTES = 8 * 1_024;
const SELECTION_JSON_BYTES = 36 * 1_024;
const REQUEST_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UTF8_ENCODER = new TextEncoder();

let socket = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let keepaliveTimer = null;
let stableConnectionTimer = null;

function clearConnectionTimers() {
  if (keepaliveTimer !== null) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
  if (stableConnectionTimer !== null) {
    clearTimeout(stableConnectionTimer);
    stableConnectionTimer = null;
  }
}

function sendJson(target, payload) {
  if (target.readyState === WebSocket.OPEN) {
    const serialized = JSON.stringify(payload);
    if (UTF8_ENCODER.encode(serialized).byteLength > MAX_PROTOCOL_MESSAGE_BYTES) {
      target.close(1009, "message too large");
      return false;
    }
    target.send(serialized);
    return true;
  }
  return false;
}

function scheduleReconnect() {
  if (reconnectTimer !== null) {
    return;
  }
  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    reconnectTimer = setTimeout(() => {
      reconnectAttempts = 0;
      reconnectTimer = null;
      connect();
    }, RECONNECT_COOLDOWN_MS);
    return;
  }
  const delay = Math.min(
    1_000 * (2 ** reconnectAttempts),
    MAX_RECONNECT_DELAY_MS,
  );
  reconnectAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function boundedString(value, maximumCharacters, maximumJsonBytes) {
  if (typeof value !== "string") {
    return "";
  }

  const characters = Array.from(value.slice(0, maximumCharacters));
  const encodedLength = (count) => UTF8_ENCODER.encode(
    JSON.stringify(characters.slice(0, count).join("")),
  ).byteLength;
  if (encodedLength(characters.length) <= maximumJsonBytes) {
    return characters.join("");
  }

  let lower = 0;
  let upper = characters.length;
  while (lower < upper) {
    const middle = Math.ceil((lower + upper) / 2);
    if (encodedLength(middle) <= maximumJsonBytes) {
      lower = middle;
    } else {
      upper = middle - 1;
    }
  }
  return characters.slice(0, lower).join("");
}

function normalizePageContext(value) {
  if (value === null || typeof value !== "object") {
    return null;
  }
  const rawVideoTime = value.video_time;
  const videoTime = typeof rawVideoTime === "number"
    && Number.isFinite(rawVideoTime)
    && rawVideoTime >= 0
    ? rawVideoTime
    : null;
  return {
    url: boundedString(value.url, 8_192, URL_JSON_BYTES),
    title: boundedString(value.title, 2_048, TITLE_JSON_BYTES),
    selection: boundedString(value.selection, 32_768, SELECTION_JSON_BYTES),
    video_time: videoTime,
  };
}

async function replyWithContext(target, requestId) {
  try {
    // Service Worker 中的“当前窗口”可能只是该浏览器配置最后活动的窗口。
    // 只有它此刻确实拥有系统焦点时才返回上下文，避免多个 Chrome 配置或
    // Chrome/Edge 并存时把后台标签的文字关联到前台截图。
    const focusedWindow = await chrome.windows.getLastFocused({ populate: false });
    if (
      !focusedWindow
      || focusedWindow.focused !== true
      || !Number.isInteger(focusedWindow.id)
      || focusedWindow.id === chrome.windows.WINDOW_ID_NONE
    ) {
      sendJson(target, {
        type: "context_error",
        request_id: requestId,
        error: "no_active_tab",
      });
      return;
    }

    const tabs = await chrome.tabs.query({
      active: true,
      windowId: focusedWindow.id,
    });
    const activeTab = tabs.find((tab) => Number.isInteger(tab.id));
    if (!activeTab) {
      sendJson(target, {
        type: "context_error",
        request_id: requestId,
        error: "no_active_tab",
      });
      return;
    }

    const response = await chrome.tabs.sendMessage(activeTab.id, {
      type: "capture_assistant_get_context",
    });
    const context = normalizePageContext(response);
    if (context === null) {
      sendJson(target, {
        type: "context_error",
        request_id: requestId,
        error: "internal_error",
      });
      return;
    }
    sendJson(target, {
      type: "context",
      request_id: requestId,
      ...context,
    });
  } catch (_error) {
    sendJson(target, {
      type: "context_error",
      request_id: requestId,
      error: "content_script_unavailable",
    });
  }
}

function handleBridgeMessage(target, event) {
  if (
    typeof event.data !== "string"
    || UTF8_ENCODER.encode(event.data).byteLength > MAX_PROTOCOL_MESSAGE_BYTES
  ) {
    target.close(1008, "invalid message");
    return;
  }

  let message;
  try {
    message = JSON.parse(event.data);
  } catch (_error) {
    target.close(1008, "invalid json");
    return;
  }
  if (message === null || typeof message !== "object" || Array.isArray(message)) {
    target.close(1008, "invalid message");
    return;
  }

  const keys = Object.keys(message).sort();
  if (message.type === "hello_ack") {
    if (
      keys.length !== 2
      || keys[0] !== "protocol"
      || keys[1] !== "type"
      || message.protocol !== PROTOCOL_VERSION
    ) {
      target.close(1008, "invalid hello acknowledgement");
    }
    return;
  }
  if (message.type === "pong") {
    if (keys.length !== 1 || keys[0] !== "type") {
      target.close(1008, "invalid pong");
    }
    return;
  }
  if (
    keys.length !== 2
    || keys[0] !== "request_id"
    || keys[1] !== "type"
    || message.type !== "get_context"
    || typeof message.request_id !== "string"
    || !REQUEST_ID_PATTERN.test(message.request_id)
  ) {
    target.close(1008, "invalid protocol message");
    return;
  }
  void replyWithContext(target, message.request_id);
}

function connect() {
  if (
    socket !== null
    && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  const candidate = new WebSocket(BRIDGE_URL);
  socket = candidate;

  candidate.addEventListener("open", () => {
    sendJson(candidate, { type: "hello", protocol: PROTOCOL_VERSION });
    keepaliveTimer = setInterval(() => {
      sendJson(candidate, { type: "ping" });
    }, KEEPALIVE_INTERVAL_MS);
    stableConnectionTimer = setTimeout(() => {
      reconnectAttempts = 0;
      stableConnectionTimer = null;
    }, STABLE_CONNECTION_MS);
  });

  candidate.addEventListener("message", (event) => {
    handleBridgeMessage(candidate, event);
  });

  candidate.addEventListener("error", () => {
    candidate.close();
  });

  candidate.addEventListener("close", () => {
    if (socket === candidate) {
      socket = null;
    }
    clearConnectionTimers();
    scheduleReconnect();
  });
}

connect();
