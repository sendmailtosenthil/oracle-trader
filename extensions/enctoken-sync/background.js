// Service worker: keep the toolbar button disabled everywhere EXCEPT
// kite.zerodha.com. This is the "only works on the specific website" guard at
// the browser level; popup.js enforces it a second time before doing anything.
chrome.runtime.onInstalled.addListener(() => {
  // Disabled by default; declarativeContent re-enables it on matching pages.
  chrome.action.disable();
  chrome.declarativeContent.onPageChanged.removeRules(undefined, () => {
    chrome.declarativeContent.onPageChanged.addRules([
      {
        conditions: [
          new chrome.declarativeContent.PageStateMatcher({
            pageUrl: { hostEquals: "kite.zerodha.com", schemes: ["https"] },
          }),
        ],
        actions: [new chrome.declarativeContent.ShowAction()],
      },
    ]);
  });
});
