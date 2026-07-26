// Account picker for the Zerodha Kite auto-login extension.
//
// This popup is shown only when the browser has no account attached yet (the
// service worker clears the popup once one is chosen, so from then on clicking
// the icon logs straight in). Picking an account here attaches this browser to
// it and immediately starts the login.
//
// Accounts are read from config.js — there is no add/edit form. Any accounts
// stored by an older version of the extension are still listed so an existing
// browser keeps working.
const CONFIG_ACCOUNTS = self.KITE_ACCOUNTS || (self.KITE_CREDS ? [self.KITE_CREDS] : []);
const $ = (id) => document.getElementById(id);

async function getState() {
  const s = await chrome.storage.local.get(["accounts", "selectedUser"]);
  return {
    stored: Array.isArray(s.accounts) ? s.accounts : [],
    selectedUser: s.selectedUser || "",
  };
}

function userIds(stored) {
  const seen = new Set();
  const out = [];
  for (const a of [...CONFIG_ACCOUNTS, ...stored]) {
    const u = (a.user || "").toUpperCase();
    if (a.user && !seen.has(u)) {
      seen.add(u);
      out.push(a.user);
    }
  }
  return out;
}

async function attachAndLogIn(user) {
  await chrome.storage.local.set({ selectedUser: user });
  chrome.runtime.sendMessage({ type: "kite-login" }, () => {
    void chrome.runtime.lastError;
    window.close();
  });
}

async function render() {
  const { stored, selectedUser } = await getState();
  const users = userIds(stored);
  const box = $("list");
  box.innerHTML = "";

  if (!users.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No accounts in config.js — add them there, then reload the extension.";
    box.appendChild(p);
    return;
  }

  for (const user of users) {
    const active = user.toUpperCase() === selectedUser.toUpperCase();
    const btn = document.createElement("button");
    btn.className = "use" + (active ? " active" : "");
    btn.textContent = (active ? "✓ " : "") + user + (active ? "  (this browser)" : "");
    btn.addEventListener("click", () => attachAndLogIn(user));
    box.appendChild(btn);
  }
}

// ----- Oracle server sync ---------------------------------------------------
// The push happens on its own after every login (see background.js); this panel
// just reports the last result and offers a manual retry.
function showSync(status) {
  const el = $("syncStatus");
  if (!status) {
    el.className = "muted";
    el.textContent = "No sync yet — log in to Kite.";
    return;
  }
  const when = new Date(status.at).toLocaleTimeString();
  el.className = status.ok ? "ok" : "err";
  el.textContent = `${status.ok ? "✓" : "✗"} ${status.message} (${when})`;
}

async function renderSync() {
  const { oracleSync } = await chrome.storage.local.get("oracleSync");
  showSync(oracleSync);
}

$("sync").addEventListener("click", () => {
  $("syncStatus").className = "muted";
  $("syncStatus").textContent = "Syncing…";
  chrome.runtime.sendMessage({ type: "oracle-sync", reason: "popup", force: true }, (status) => {
    if (chrome.runtime.lastError) {
      showSync({ ok: false, message: chrome.runtime.lastError.message, at: new Date().toISOString() });
      return;
    }
    showSync(status);
  });
});

render();
renderSync();
