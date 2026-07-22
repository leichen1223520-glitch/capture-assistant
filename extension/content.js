"use strict";

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

function isSensitiveInput(element) {
  // 同源 iframe 中的元素属于另一个 Window；顶层 Realm 的 instanceof 会误判。
  const elementName = element && typeof element.localName === "string"
    ? element.localName.toLowerCase()
    : "";
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

function deepActiveElement(root = document) {
  let activeElement = root.activeElement;
  while (activeElement && activeElement.shadowRoot && activeElement.shadowRoot.activeElement) {
    activeElement = activeElement.shadowRoot.activeElement;
  }
  const activeName = activeElement && typeof activeElement.localName === "string"
    ? activeElement.localName.toLowerCase()
    : "";
  if (activeName === "iframe") {
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
    return visibleSelection;
  }

  if (
    activeElement
    && (activeElement.localName === "input" || activeElement.localName === "textarea")
    && typeof activeElement.selectionStart === "number"
    && typeof activeElement.selectionEnd === "number"
    && activeElement.selectionEnd > activeElement.selectionStart
  ) {
    return activeElement.value.slice(activeElement.selectionStart, activeElement.selectionEnd);
  }
  return "";
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (
    message === null
    || typeof message !== "object"
    || message.type !== "capture_assistant_get_context"
  ) {
    return false;
  }

  const video = document.querySelector("video");
  const currentTime = video && Number.isFinite(video.currentTime) && video.currentTime >= 0
    ? video.currentTime
    : null;
  const activeState = deepActiveElement();
  const activeElement = activeState.element;
  // A focused cross-origin frame cannot be inspected for password fields. Fail closed instead
  // of silently claiming that it is safe.
  const sensitiveInput = activeState.uninspectableFrame || isSensitiveInput(activeElement);
  sendResponse({
    url: window.location.href,
    title: document.title,
    selection: sensitiveInput ? "" : selectedText(activeElement),
    video_time: currentTime,
    sensitive_input: sensitiveInput,
  });
  return false;
});
