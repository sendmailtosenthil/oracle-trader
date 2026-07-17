// Config comes from config.js (self.ORACLE_API) — copy config.js.example to
// config.js and fill it in. config.js is gitignored so secrets never commit.
const CFG = self.ORACLE_API || {};
const API_BASE = CFG.base || "";
const BASIC_USER = CFG.user || "";
const BASIC_PASS = CFG.pass || "";

const KITE_HOST = "kite.zerodha.com";
const KITE_URL = "https://kite.zerodha.com";

const $ = (id) => document.getElementById(id);
const setStatus = (msg, cls) => {
  const el = $("status");
  el.textContent = msg;
  el.className = cls || "";
};
const mask = (v) => (v && v.length > 12 ? v.slice(0, 6) + "…" + v.slice(-4) : v || "—");

// Read a cookie value from the Kite domain (works for HttpOnly cookies too,
// which document.cookie cannot see — that's why this uses the cookies API).
function getCookie(name) {
  return new Promise((resolve) => {
    chrome.cookies.get({ url: KITE_URL, name }, (c) => resolve(c ? c.value : null));
  });
}

async function activeTabIsKite() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    return tab && new URL(tab.url).hostname === KITE_HOST;
  } catch {
    return false;
  }
}

let current = { userId: null, enctoken: null };

async function init() {
  if (!API_BASE || !BASIC_PASS) {
    setStatus("Set your API details in config.js, then reload.", "err");
    return;
  }
  // Guard: only operate when the active tab is Kite.
  if (!(await activeTabIsKite())) {
    setStatus("Open kite.zerodha.com (logged in) to use this.", "err");
    return;
  }

  const [enctoken, userId] = await Promise.all([
    getCookie("enctoken"),
    getCookie("user_id"),
  ]);
  current = { enctoken, userId };

  $("userId").textContent = userId || "(not in cookie — server default will be used)";
  $("enctoken").textContent = mask(enctoken);

  if (!enctoken) {
    setStatus("No enctoken cookie found — are you logged in to Kite?", "err");
    return;
  }
  $("syncBtn").disabled = false;
}

async function sync() {
  if (!current.enctoken) return;
  $("syncBtn").disabled = true;
  setStatus("Sending…");
  try {
    const resp = await fetch(`${API_BASE}/api/enctoken`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Basic " + btoa(`${BASIC_USER}:${BASIC_PASS}`),
      },
      body: JSON.stringify({
        user_id: current.userId || undefined,
        enctoken: current.enctoken,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.status === "success") {
      setStatus(`✓ Synced (user ${data.user_id})`, "ok");
    } else {
      setStatus(`✗ ${resp.status}: ${data.message || "failed"}`, "err");
      $("syncBtn").disabled = false;
    }
  } catch (e) {
    setStatus(`✗ ${e.message} (check API_BASE / host_permissions)`, "err");
    $("syncBtn").disabled = false;
  }
}

$("syncBtn").addEventListener("click", sync);
init();
