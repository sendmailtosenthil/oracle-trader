// Service worker: the toolbar button, and the push of this browser's Kite
// session (enctoken + user id) to the Project Oracle server.
//
// Clicking the icon logs straight in — no popup, nothing to fill in. Each
// browser is attached to one account, so there is only ever one thing to do.
// The popup appears *only* when this browser has no account attached yet, as a
// list of the configured accounts to pick from.
//
// The sync half was the standalone "enctoken-sync" extension; logging in and
// reporting the resulting token are the same job, so the extension that
// performs the login now also does the push. It happens automatically whenever
// Kite hands out a new enctoken — after an auto-login, after a login typed by
// hand, after a re-login when the old session expired — so the server's token
// stays fresh without anyone clicking anything.
//
// Accounts come from config.js (`self.KITE_ACCOUNTS`) and server details from
// `self.ORACLE_API` in the same file; it is gitignored. Without ORACLE_API,
// syncing stays off and the login half still works.
//
// The enctoken cookie is HttpOnly, so it can only be read through the
// chrome.cookies API from here — a content script cannot see it.

try {
  importScripts("config.js");
} catch (e) {
  // No config.js in the folder yet — syncing stays off until there is one.
}

const KITE_URL = "https://kite.zerodha.com/";
const REATTACH_MENU_ID = "zl-reattach";

function apiConfig() {
  const cfg = self.ORACLE_API || {};
  if (!cfg.base || !cfg.pass) return null;
  return {
    base: String(cfg.base).replace(/\/+$/, ""),
    user: cfg.user || "",
    pass: cfg.pass,
  };
}

// One snapshot of Kite's cookies, so the enctoken and the user_id it belongs to
// are always read together. Fetching them with two separate calls could pair a
// fresh token with the previous account's id if a re-login lands in between —
// which would write one account's token onto another's row on the server.
function kiteCookies() {
  return new Promise((resolve) => {
    chrome.cookies.getAll({ url: KITE_URL }, (list) => {
      const jar = {};
      for (const c of list || []) jar[c.name] = c.value;
      resolve(jar);
    });
  });
}

// The badge is a problem light: it shows "!" when the last sync failed and is
// cleared when one succeeds, so a healthy setup stays visually quiet.
async function setStatus(ok, message, extra) {
  const status = { ok, message, at: new Date().toISOString(), ...(extra || {}) };
  await chrome.storage.local.set({ oracleSync: status });
  try {
    await chrome.action.setBadgeText({ text: ok ? "" : "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
  } catch (e) {
    /* action API unavailable — status in storage is enough */
  }
  return status;
}

async function doSync(reason, force) {
  const api = apiConfig();
  if (!api) {
    return setStatus(false, "Set ORACLE_API in config.js to enable syncing.");
  }

  const jar = await kiteCookies();
  if (!jar.enctoken) {
    return setStatus(false, "No enctoken cookie — log in to Kite first.");
  }
  const enctoken = jar.enctoken;

  // Kite's own user_id cookie is the source of truth for *which* account this
  // session belongs to; fall back to the account this browser logs in as.
  const stored = await chrome.storage.local.get(["selectedUser", "lastSyncedToken"]);
  const userId = jar.user_id || stored.selectedUser || "";

  if (!force && stored.lastSyncedToken === enctoken) {
    return setStatus(true, `Already synced${userId ? " (" + userId + ")" : ""}.`, { userId });
  }

  try {
    const resp = await fetch(`${api.base}/api/enctoken`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Basic " + btoa(`${api.user}:${api.pass}`),
      },
      body: JSON.stringify({ user_id: userId || undefined, enctoken }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.status !== "success") {
      return setStatus(false, `${resp.status}: ${data.message || "sync failed"}`, { userId });
    }
    await chrome.storage.local.set({ lastSyncedToken: enctoken });
    console.log(`[zerodha-login] enctoken synced for ${data.user_id} (${reason})`);
    return setStatus(true, `Synced ${data.user_id}.`, { userId: data.user_id });
  } catch (err) {
    // Usually a wrong `base`, a host missing from manifest host_permissions, or
    // the VPS being unreachable.
    return setStatus(false, `${err.message} (check base / host_permissions)`, { userId });
  }
}

// Pushes run one at a time, in order. Queued rather than dropped: when a second
// login lands while the first push is still in flight, the newer token is the
// one that matters, so it must still be sent. Repeats are cheap — a token that
// was already delivered is skipped without an HTTP call.
let queue = Promise.resolve();
function syncEnctoken(reason, force = false) {
  queue = queue.catch(() => {}).then(() => doSync(reason, force));
  return queue;
}

// --- Which account is this browser attached to? -----------------------------

function configAccounts() {
  return self.KITE_ACCOUNTS || (self.KITE_CREDS ? [self.KITE_CREDS] : []);
}

// User ids of every account this browser could log in as: config.js, plus any
// left in storage by an older version that let you add them in the popup.
async function candidateUsers() {
  const { accounts } = await chrome.storage.local.get("accounts");
  const merged = [...configAccounts(), ...(Array.isArray(accounts) ? accounts : [])];
  const seen = new Set();
  const out = [];
  for (const a of merged) {
    const u = (a.user || "").toUpperCase();
    if (a.user && !seen.has(u)) {
      seen.add(u);
      out.push(a.user);
    }
  }
  return out;
}

// The account this browser logs in as, or "" if it isn't attached to one yet.
// A single configured account counts as attached — there is nothing to choose.
async function attachedUser() {
  const { selectedUser } = await chrome.storage.local.get("selectedUser");
  const users = await candidateUsers();
  const sel = (selectedUser || "").toUpperCase();
  const match = users.find((u) => u.toUpperCase() === sel);
  if (match) return match;
  return users.length === 1 ? users[0] : "";
}

// Attached → clicking the icon logs in (no popup). Not attached → clicking
// opens the picker so the browser can be attached to one of the accounts.
async function refreshActionMode() {
  const user = await attachedUser();
  await chrome.action.setPopup({ popup: user ? "" : "popup.html" });
  await chrome.action.setTitle({
    title: user
      ? `Log in to Kite as ${user} and sync the token`
      : "Pick this browser's Zerodha account",
  });
  return user;
}

async function openKiteAndLogIn() {
  // Navigating an existing Kite tab to the root gives the content script the
  // login form to fill; if the session is still alive Kite just redirects to the
  // dashboard and the content script re-syncs the token instead.
  const tabs = await chrome.tabs.query({ url: "https://kite.zerodha.com/*" });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { active: true, url: KITE_URL });
    try {
      await chrome.windows.update(tabs[0].windowId, { focused: true });
    } catch (e) {
      /* window gone — the tab update is enough */
    }
  } else {
    await chrome.tabs.create({ url: KITE_URL });
  }
}

// --- Triggers ---------------------------------------------------------------

// The worker re-runs whenever Chrome wakes it, so this keeps the button's mode
// correct without relying on a startup event having fired.
refreshActionMode();

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && ("selectedUser" in changes || "accounts" in changes)) {
    refreshActionMode();
  }
});

// Only fires when no popup is set, i.e. this browser has its account.
chrome.action.onClicked.addListener(() => {
  openKiteAndLogIn();
});

// Re-attaching a browser: right-click the icon → the next click shows the
// picker again. Attached browsers otherwise never see the popup.
function createMenu() {
  chrome.contextMenus.create(
    {
      id: REATTACH_MENU_ID,
      title: "Pick a different account for this browser…",
      contexts: ["action"],
    },
    () => void chrome.runtime.lastError, // already exists on a worker restart
  );
}
chrome.runtime.onInstalled.addListener(createMenu);
chrome.runtime.onStartup.addListener(createMenu);

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== REATTACH_MENU_ID) return;
  await chrome.storage.local.remove("selectedUser");
  await refreshActionMode();
});

// The real "a login just happened" signal: Kite setting a fresh enctoken.
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || cookie.name !== "enctoken") return;
  if (!cookie.domain.includes("kite.zerodha.com")) return;
  syncEnctoken("cookie");
});

// Messages from the content script (an already-live session — covers a token
// set while the worker was asleep, or an earlier push that failed) and from the
// picker popup.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg) return undefined;
  if (msg.type === "oracle-sync") {
    syncEnctoken(msg.reason || "message", !!msg.force).then(sendResponse);
    return true; // async response
  }
  if (msg.type === "kite-login") {
    openKiteAndLogIn().then(() => sendResponse({ ok: true }));
    return true;
  }
  return undefined;
});
