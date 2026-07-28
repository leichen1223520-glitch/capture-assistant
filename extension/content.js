"use strict";

const MAX_SELECTION_CHARACTERS = 16_384;
const MAX_OBSERVATION_CHARACTERS = 16_384;
const MAX_OBSERVATION_SCAN_CHARACTERS = 65_536;
const MAX_VIDEO_ELEMENTS = 32;
const MAX_TEXT_TRACKS = 16;
const MAX_ACTIVE_CUES_PER_TRACK = 32;
const VIDEO_STATES = new WeakMap();
let nextVideoIdentity = 1;

const SENSITIVE_AUTOCOMPLETE_TOKENS = new Set([
  "current-password",
  "new-password",
  "one-time-code",
  "cc-name",
  "cc-given-name",
  "cc-additional-name",
  "cc-family-name",
  "cc-number",
  "cc-exp",
  "cc-exp-month",
  "cc-exp-year",
  "cc-csc",
  "cc-type",
]);

function localName(element) {
  return element && typeof element.localName === "string"
    ? element.localName.toLowerCase()
    : "";
}

function boundedCharacters(value, maximumCharacters) {
  if (typeof value !== "string") {
    return "";
  }
  // 先按 UTF-16 上界截取，再做 code point 数量修正；不能对不可信整串直接
  // Array.from，否则超长页面选区会先制造一个同等规模的临时数组。
  return Array.from(value.slice(0, maximumCharacters * 2))
    .slice(0, maximumCharacters)
    .join("");
}

function normalizedObservationText(value) {
  if (typeof value !== "string") {
    return "";
  }
  const scanned = boundedCharacters(value, MAX_OBSERVATION_SCAN_CHARACTERS);
  return boundedCharacters(
    scanned.replace(/\u0000/g, "").replace(/\s+/gu, " ").trim(),
    MAX_OBSERVATION_CHARACTERS,
  );
}

function isSensitiveInput(element) {
  // 同源 iframe 中的元素属于另一个 Window；顶层 Realm 的 instanceof 会误判。
  const elementName = localName(element);
  if (elementName !== "input" && elementName !== "textarea") {
    return false;
  }
  const inputType = elementName === "input" && typeof element.type === "string"
    ? element.type.toLowerCase()
    : "";
  if (inputType === "password") {
    return true;
  }
  const autocomplete = typeof element.autocomplete === "string" ? element.autocomplete : "";
  return autocomplete
    .toLowerCase()
    .split(/\s+/)
    .some((token) => SENSITIVE_AUTOCOMPLETE_TOKENS.has(token));
}

function isEditableControl(element) {
  const elementName = localName(element);
  if (
    elementName === "input"
    || elementName === "textarea"
    || elementName === "select"
  ) {
    return true;
  }
  if (element && element.isContentEditable === true) {
    return true;
  }
  const role = element && typeof element.getAttribute === "function"
    ? (element.getAttribute("role") || "").toLowerCase()
    : "";
  return role === "textbox" || role === "searchbox" || role === "combobox";
}

function deepActiveElement(root = document) {
  let activeElement = root.activeElement;
  while (activeElement && activeElement.shadowRoot && activeElement.shadowRoot.activeElement) {
    activeElement = activeElement.shadowRoot.activeElement;
  }
  if (localName(activeElement) === "iframe") {
    try {
      if (activeElement.contentDocument) {
        return deepActiveElement(activeElement.contentDocument);
      }
    } catch (_error) {
      // Cross-origin frame contents are intentionally inaccessible.
    }
    return { element: activeElement, uninspectableFrame: true };
  }
  return { element: activeElement, uninspectableFrame: false };
}

function selectedText(activeElement) {
  const documentSelection = window.getSelection();
  const visibleSelection = documentSelection ? documentSelection.toString() : "";
  if (visibleSelection) {
    return boundedCharacters(visibleSelection, MAX_SELECTION_CHARACTERS);
  }

  if (
    activeElement
    && (localName(activeElement) === "input" || localName(activeElement) === "textarea")
    && typeof activeElement.selectionStart === "number"
    && typeof activeElement.selectionEnd === "number"
    && activeElement.selectionEnd > activeElement.selectionStart
  ) {
    return boundedCharacters(
      activeElement.value.slice(activeElement.selectionStart, activeElement.selectionEnd),
      MAX_SELECTION_CHARACTERS,
    );
  }
  return "";
}

function videoVisibilityScore(video) {
  try {
    const rect = video.getBoundingClientRect();
    const style = window.getComputedStyle(video);
    if (
      style.display === "none"
      || style.visibility === "hidden"
      || Number(style.opacity) === 0
    ) {
      return 0;
    }
    const visibleWidth = Math.max(
      0,
      Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0),
    );
    const visibleHeight = Math.max(
      0,
      Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0),
    );
    return visibleWidth * visibleHeight;
  } catch (_error) {
    return 0;
  }
}

function preferredVideo() {
  const videos = document.querySelectorAll("video");
  if (videos.length > MAX_VIDEO_ELEMENTS) {
    return null;
  }
  const visible = [];
  for (let index = 0; index < videos.length; index += 1) {
    const video = videos[index];
    const item = {
      video,
      score: videoVisibilityScore(video),
    };
    if (item.score > 0) {
      visible.push(item);
    }
  }
  // 首版无法把桌面像素选区可靠映射到某个 DOM video。多个视频同时可见时
  // 宁可不返回视频信号，也不能把 A 的字幕/时间码配到 B 的截图。
  if (visible.length !== 1) {
    return null;
  }
  return visible[0].video;
}

function activeCaptionText(video) {
  if (!video || !video.textTracks) {
    return "";
  }
  const cueTexts = [];
  let remainingCodeUnits = MAX_OBSERVATION_SCAN_CHARACTERS;
  try {
    const trackCount = Math.min(video.textTracks.length, MAX_TEXT_TRACKS);
    for (let trackIndex = 0; trackIndex < trackCount; trackIndex += 1) {
      const track = video.textTracks[trackIndex];
      const kind = track && typeof track.kind === "string"
        ? track.kind.toLowerCase()
        : "";
      if (
        !track
        || track.mode !== "showing"
        || (kind !== "captions" && kind !== "subtitles")
        || !track.activeCues
      ) {
        continue;
      }
      const cueCount = Math.min(
        track.activeCues.length,
        MAX_ACTIVE_CUES_PER_TRACK,
      );
      for (let cueIndex = 0; cueIndex < cueCount; cueIndex += 1) {
        const cue = track.activeCues[cueIndex];
        if (cue && typeof cue.text === "string") {
          const chunk = cue.text.slice(0, remainingCodeUnits);
          if (chunk) {
            cueTexts.push(chunk);
            remainingCodeUnits -= chunk.length + 1;
          }
          if (remainingCodeUnits <= 0) {
            return normalizedObservationText(cueTexts.join(" "));
          }
        }
      }
    }
  } catch (_error) {
    // 跨源或自定义播放器可能拒绝读取 cue；此时安全降级到普通页面选区。
    return "";
  }
  return normalizedObservationText(cueTexts.join(" "));
}

function videoKey(video) {
  if (!video) {
    return "";
  }
  const currentSource = typeof video.currentSrc === "string" ? video.currentSrc : "";
  let state = VIDEO_STATES.get(video);
  if (!state) {
    state = {
      identity: nextVideoIdentity,
      source: currentSource,
      epoch: 0,
    };
    nextVideoIdentity += 1;
    VIDEO_STATES.set(video, state);
    const advanceEpoch = () => {
      const currentState = VIDEO_STATES.get(video);
      if (currentState) {
        currentState.source = typeof video.currentSrc === "string"
          ? video.currentSrc
          : "";
        currentState.epoch += 1;
      }
    };
    // 有些 MediaSource/blob 播放器换片时 currentSrc 字符串保持不变；
    // load 生命周期仍应推进来源 epoch，避免相同字幕被跨视频误去重。
    video.addEventListener("emptied", advanceEpoch);
    video.addEventListener("loadstart", advanceEpoch);
  } else if (state.source !== currentSource) {
    state.source = currentSource;
    state.epoch += 1;
  }
  // currentSrc 可能包含签名参数或临时令牌，只在本页内比较，绝不外发。
  return `video-${state.identity}:${state.epoch}`;
}

function observation(activeElement, video, selection) {
  if (isEditableControl(activeElement)) {
    return { text: "", kind: "none" };
  }
  const captionText = activeCaptionText(video);
  if (captionText) {
    return { text: captionText, kind: "caption" };
  }
  const selectionText = normalizedObservationText(selection);
  if (selectionText) {
    return { text: selectionText, kind: "selection" };
  }
  return { text: "", kind: "none" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (
    message === null
    || typeof message !== "object"
    || message.type !== "capture_assistant_get_context"
  ) {
    return false;
  }

  const video = preferredVideo();
  const currentTime = video && Number.isFinite(video.currentTime) && video.currentTime >= 0
    ? video.currentTime
    : null;
  const activeState = deepActiveElement();
  const activeElement = activeState.element;
  // A focused cross-origin frame cannot be inspected for password fields. Fail closed instead
  // of silently claiming that it is safe.
  const sensitiveInput = activeState.uninspectableFrame || isSensitiveInput(activeElement);
  const selection = sensitiveInput ? "" : selectedText(activeElement);
  const observed = sensitiveInput
    ? { text: "", kind: "none" }
    : observation(activeElement, video, selection);
  sendResponse({
    url: window.location.href,
    title: document.title,
    selection,
    video_time: currentTime,
    sensitive_input: sensitiveInput,
    observation_text: observed.text,
    observation_kind: observed.kind,
    video_key: videoKey(video),
  });
  return false;
});
