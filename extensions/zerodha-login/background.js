// Service worker: pushes this browser's Kite session (enctoken + user id) to
// the Project Oracle server.
//
// This was the standalone "enctoken-sync" extension; logging in and reporting
// the resulting token are the same job, so the extension that performs the
// login now also does the push. It happens **automatically whenever Kite hands
// out a new enctoken** — after an auto-login, after a login you typed yourself,
// after a re-login when the old session expired — so the server's token stays
// fresh without anyone clicking anything.
//
// Server details come from config.js (`self.ORACLE_API`), which is gitignored.
// Without it, syncing simply stays off and the login half still works.
//
// The enctoken cookie is HttpOnly, so it can only be read through the
// chrome.cookies API from here — a content script cannot see it.

try {
  importScripts("config.js");
} catch (e) {
  // No config.js in the folder yet — syncing stays off until there is one.
}

const KITE_URL = "https://kite.zerodha.com";

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

// --- Triggers ---------------------------------------------------------------

// The real "a login just happened" signal: Kite setting a fresh enctoken.
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || cookie.name !== "enctoken") return;
  if (!cookie.domain.includes("kite.zerodha.com")) return;
  syncEnctoken("cookie");
});

// Content script announcing an already-live session (covers a token that was
// set while the worker was asleep, or an earlier push that failed).
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "oracle-sync") return undefined;
  syncEnctoken(msg.reason || "message", !!msg.force).then(sendResponse);
  return true; // async response
});
