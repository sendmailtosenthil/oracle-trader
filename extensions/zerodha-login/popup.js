// Popup for the Zerodha Kite auto-login extension.
//
// Lets you manage accounts and choose which one THIS browser profile uses.
// Everything is stored in chrome.storage.local, which is per profile — so the
// same extension in 3 browsers can log each into a different account with no
// file edits. config.js (if present) is merged in as an optional shared source;
// accounts added here are stored and can be deleted here.
const KITE_URL = "https://kite.zerodha.com/";
const CONFIG_ACCOUNTS = self.KITE_ACCOUNTS || (self.KITE_CREDS ? [self.KITE_CREDS] : []);
const $ = (id) => document.getElementById(id);

async function getState() {
  const s = await chrome.storage.local.get(["accounts", "selectedUser"]);
  return {
    accounts: Array.isArray(s.accounts) ? s.accounts : [],
    selectedUser: s.selectedUser || "",
  };
}

// Candidate list = config.js + popup-added, deduped (popup-added wins on clash).
function mergedList(stored) {
  const seen = new Set();
  const list = [];
  for (const a of [...stored, ...CONFIG_ACCOUNTS]) {
    const u = (a.user || "").toUpperCase();
    if (a.user && !seen.has(u)) {
      seen.add(u);
      list.push(a);
    }
  }
  return list;
}

async function render() {
  const { accounts, selectedUser } = await getState();
  const list = mergedList(accounts);
  const box = $("list");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="muted">No accounts yet — add one below.</div>';
    return;
  }
  for (const a of list) {
    const active = a.user.toUpperCase() === selectedUser.toUpperCase();
    const row = document.createElement("div");
    row.className = "row";

    const use = document.createElement("button");
    use.className = "use" + (active ? " active" : "");
    use.textContent = (active ? "✓ " : "") + a.user + (active ? "  (this browser)" : "");
    use.addEventListener("click", async () => {
      await chrome.storage.local.set({ selectedUser: a.user });
      render();
    });
    row.appendChild(use);

    // Only accounts added via the popup can be deleted here (not config.js ones).
    if (accounts.some((x) => x.user.toUpperCase() === a.user.toUpperCase())) {
      const del = document.createElement("button");
      del.className = "del";
      del.textContent = "✕";
      del.title = "Remove " + a.user;
      del.addEventListener("click", async () => {
        const next = accounts.filter((x) => x.user.toUpperCase() !== a.user.toUpperCase());
        const patch = { accounts: next };
        if (a.user.toUpperCase() === selectedUser.toUpperCase()) patch.selectedUser = "";
        await chrome.storage.local.set(patch);
        render();
      });
      row.appendChild(del);
    }
    box.appendChild(row);
  }
}

$("add").addEventListener("click", async () => {
  const user = $("user").value.trim();
  const pass = $("pass").value;
  const secret = $("secret").value.trim().replace(/\s+/g, "");
  if (!user || !pass || !secret) {
    $("status").style.color = "#c33";
    $("status").textContent = "Fill in user id, password and TOTP secret.";
    return;
  }
  const { accounts } = await getState();
  const next = accounts.filter((x) => x.user.toUpperCase() !== user.toUpperCase());
  next.push({ user, pass, secret });
  // Saving an account also selects it for this browser (the common case).
  await chrome.storage.local.set({ accounts: next, selectedUser: user });
  $("user").value = $("pass").value = $("secret").value = "";
  $("status").style.color = "#2a7";
  $("status").textContent = "Saved " + user + " for this browser.";
  render();
});

$("login").addEventListener("click", async () => {
  const tabs = await chrome.tabs.query({ url: "https://kite.zerodha.com/*" });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { active: true, url: KITE_URL });
    chrome.windows.update(tabs[0].windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: KITE_URL });
  }
  window.close();
});

render();
