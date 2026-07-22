"use strict";

function isSensitiveInput(element) {
  if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) {
    return false;
  }
  if (element instanceof HTMLInputElement && element.type.toLowerCase() === "password") {
    return true;
  }
  const sensitiveAutocompleteTokens = new Set([
    "current-password",
    "new-password",
    "one-time-code",
    "cc-number",
    "cc-csc",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
  ]);
  return element.autocomplete
    .toLowerCase()
    .split(/\s+/)
    .some((token) => sensitiveAutocompleteTokens.has(token));
}

function selectedText() {
  const activeElement = document.activeElement;
  if (isSensitiveInput(activeElement)) {
    return "";
  }

  const documentSelection = window.getSelection();
  const visibleSelection = documentSelection ? documentSelection.toString() : "";
  if (visibleSelection) {
    return visibleSelection;
  }

  if (
    (activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement)
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
  sendResponse({
    url: window.location.href,
    title: document.title,
    selection: selectedText(),
    video_time: currentTime,
  });
  return false;
});
