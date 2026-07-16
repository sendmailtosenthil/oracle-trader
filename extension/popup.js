// ============================================================================
// EDIT THESE THREE VALUES, then reload the extension (chrome://extensions).
// They must match the ENCTOKEN_API_* settings on your Oracle server.
// ============================================================================
const API_BASE = "https://YOUR_VPS_HOST"; // e.g. https://oracle.example.com  (no trailing slash)
const BASIC_USER = "oracle";              // == ENCTOKEN_API_USER on the server
const BASIC_PASS = "change-me";           // == ENCTOKEN_API_PASS on the server
// ============================================================================

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
