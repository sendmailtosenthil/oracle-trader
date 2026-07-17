// Clicking the toolbar icon opens (or focuses) the Oracle web app. The content
// script injected on that page handles the actual login.
const APP_URL = "http://129.159.236.137:8501/";

chrome.action.onClicked.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: "http://129.159.236.137:8501/*" });
  if (tabs.length) {
    const tab = tabs[0];
    // Reload so the content script runs and re-logs in if the session dropped.
    await chrome.tabs.update(tab.id, { active: true, url: APP_URL });
    chrome.windows.update(tab.windowId, { focused: true });
  } else {
    chrome.tabs.create({ url: APP_URL });
  }
});
