// Clicking the toolbar icon opens (or focuses) Kite. The content script
// injected on that page handles user id / password / TOTP automatically.
const KITE_URL = "https://kite.zerodha.com/";

chrome.action.onClicked.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: "https://kite.zerodha.com/*" });
  if (tabs.length) {
    const tab = tabs[0];
    await chrome.tabs.update(tab.id, { active: true, url: KITE_URL });
    chrome.windows.update(tab.windowId, { focused: true });
  } else {
    chrome.tabs.create({ url: KITE_URL });
  }
});
