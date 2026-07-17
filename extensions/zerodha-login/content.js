// Auto-login for Zerodha Kite (https://kite.zerodha.com).
//
// Flow:
//   1. Login form: fill User ID + Password, then click Login.
//   2. 2FA form: generate a time-based OTP from the TOTP secret and submit it.
//
// Accounts can be configured two ways (the popup wins over config.js):
//   • Extension popup — click the Z icon, add your account(s) and pick which one
//     this browser uses. Stored in chrome.storage.local, which is PER browser
//     profile, so 3 browsers each keep their own account with no file edits.
//   • config.js — self.KITE_ACCOUNTS (a list) as an optional shared fallback.
//
// If the extension can't tell which account this browser is for (no saved
// choice, nothing prefilled, more than one candidate), it shows a small picker
// on the Kite page listing the available user ids to choose from.
const CONFIG_ACCOUNTS = self.KITE_ACCOUNTS || (self.KITE_CREDS ? [self.KITE_CREDS] : []);
let USERID = "";
let PASSWORD = "";
let SECRET = "";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isVisible = (el) => !!(el && el.offsetParent !== null);

function setNativeValue(el, value) {
  const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
  desc.set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

// ----- TOTP (RFC 6238, HMAC-SHA1, 6 digits, 30s) via Web Crypto -------------
function base32Decode(b32) {
  const alph = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of b32.replace(/=+$/, "").toUpperCase().replace(/\s/g, "")) {
    const v = alph.indexOf(c);
    if (v >= 0) bits += v.toString(2).padStart(5, "0");
  }
  const out = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.substr(i, 8), 2));
  return new Uint8Array(out);
}

async function totp(secret, digits = 6, period = 30) {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 1000 / period);
  const buf = new ArrayBuffer(8);
  const view = new DataView(buf);
  view.setUint32(0, Math.floor(counter / 2 ** 32));
  view.setUint32(4, counter >>> 0);
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", k, buf));
  const off = sig[sig.length - 1] & 0x0f;
  const bin =
    ((sig[off] & 0x7f) << 24) | ((sig[off + 1] & 0xff) << 16) | ((sig[off + 2] & 0xff) << 8) | (sig[off + 3] & 0xff);
  return (bin % 10 ** digits).toString().padStart(digits, "0");
}

// ----- DOM selectors --------------------------------------------------------
const userField = () => document.querySelector("#userid");
const passField = () => document.querySelector("#password");
const submitBtn = () => document.querySelector('button[type="submit"]');
const otpField = () =>
  document.querySelector("#pin") ||
  document.querySelector('.twofa-form input') ||
  document.querySelector('input[type="number"]') ||
  document.querySelector('input[type="tel"]') ||
  document.querySelector('input[maxlength="6"]');
const isLoggedIn = () =>
  !passField() && !(otpField() && isVisible(otpField())) && !/\/(connect\/)?login/.test(location.pathname);

// ----- account resolution ---------------------------------------------------
function findAccount(list, userid) {
  const u = (userid || "").trim().toUpperCase();
  return list.find((a) => (a.user || "").toUpperCase() === u) || null;
}

// Match a configured account id appearing in the visible page text — used on the
// 2FA screen, which shows the account id (e.g. "PC8006") but has no id input.
function accountFromPageText(list) {
  const text = (document.body && document.body.innerText) || "";
  return list.find((a) => a.user && new RegExp(`\\b${a.user}\\b`).test(text)) || null;
}

// Which account is this browser for? Prefer the choice saved in the popup, then
// the prefilled login user id, then the account id shown on the page, then the
// sole candidate. Returns null if it's genuinely ambiguous (→ show the picker).
function resolveAccount(list, selectedUser) {
  const uf = userField();
  return (
    findAccount(list, selectedUser) ||
    findAccount(list, uf && uf.value) ||
    accountFromPageText(list) ||
    (list.length === 1 ? list[0] : null)
  );
}

function applyAccount(acct) {
  USERID = acct.user || "";
  PASSWORD = acct.pass || "";
  SECRET = acct.secret || "";
}

// Candidate accounts = config.js + popup-added (chrome.storage), deduped by user
// id; plus this browser's saved choice (selectedUser). storage.local is per
// profile, so each browser keeps its own list and its own selection.
async function loadState() {
  let stored = { accounts: [], selectedUser: "" };
  try {
    stored = { ...stored, ...(await chrome.storage.local.get(["accounts", "selectedUser"])) };
  } catch (e) {
    /* storage unavailable → config.js only */
  }
  const merged = [...CONFIG_ACCOUNTS, ...(Array.isArray(stored.accounts) ? stored.accounts : [])];
  const seen = new Set();
  const list = [];
  for (const a of merged) {
    const u = (a.user || "").toUpperCase();
    if (a.user && !seen.has(u)) {
      seen.add(u);
      list.push(a);
    }
  }
  return { list, selectedUser: stored.selectedUser || "" };
}

// ----- Steps ----------------------------------------------------------------
async function doLogin() {
  for (let i = 0; i < 60; i++) {
    const pass = passField();
    if (pass && isVisible(pass)) {
      const user = userField();
      // Force the user id to the resolved account, so a stale prefill (or a
      // different id remembered here) can't be paired with the wrong password.
      if (user && user.value !== USERID) setNativeValue(user, USERID);
      if (!pass.value) setNativeValue(pass, PASSWORD);
      await sleep(200);
      submitBtn()?.click();
      return;
    }
    if (otpField() && isVisible(otpField())) return; // already on 2FA
    if (isLoggedIn()) return;
    await sleep(250);
  }
}

async function doOtp() {
  if (!SECRET) return;
  for (let attempt = 0; attempt < 3; attempt++) {
    let otp = null;
    for (let i = 0; i < 60; i++) {
      otp = otpField();
      if (otp && isVisible(otp)) break;
      if (isLoggedIn()) return;
      await sleep(250);
    }
    if (!otp || !isVisible(otp)) return;

    const code = await totp(SECRET); // fresh code each attempt (may cross a 30s window)
    setNativeValue(otp, code);
    await sleep(200);
    submitBtn()?.click();

    // Wait to see whether it took; if we're still on the OTP page, retry.
    for (let i = 0; i < 16; i++) {
      await sleep(250);
      if (isLoggedIn() || !(otpField() && isVisible(otpField()))) return;
    }
  }
}

// ----- on-page picker (shown only when we can't auto-resolve) ---------------
function showPicker(list) {
  if (document.getElementById("zl-picker")) return; // once
  const box = document.createElement("div");
  box.id = "zl-picker";
  box.style.cssText =
    "position:fixed;top:14px;right:14px;z-index:2147483647;background:#fff;" +
    "border:1px solid #ff5722;border-radius:8px;padding:12px 14px;" +
    "box-shadow:0 6px 20px rgba(0,0,0,.22);font:13px system-ui,-apple-system,sans-serif;color:#222;";

  const label = document.createElement("div");
  label.textContent = "Log in to Kite as:";
  label.style.cssText = "margin-bottom:8px;font-weight:600;";

  const sel = document.createElement("select");
  sel.style.cssText = "padding:6px;min-width:150px;margin-right:6px;border:1px solid #ccc;border-radius:4px;";
  for (const a of list) {
    const o = document.createElement("option");
    o.value = a.user;
    o.textContent = a.user;
    sel.appendChild(o);
  }

  const remember = document.createElement("label");
  remember.style.cssText = "display:block;margin:8px 0;color:#555;font-size:12px;";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = true;
  cb.style.marginRight = "5px";
  remember.appendChild(cb);
  remember.appendChild(document.createTextNode("Remember for this browser"));

  const btn = document.createElement("button");
  btn.textContent = "Log in";
  btn.style.cssText =
    "padding:6px 12px;background:#ff5722;color:#fff;border:0;border-radius:4px;cursor:pointer;font-weight:600;";
  btn.addEventListener("click", async () => {
    const acct = list.find((a) => a.user === sel.value);
    if (!acct) return;
    if (cb.checked) {
      try {
        await chrome.storage.local.set({ selectedUser: acct.user });
      } catch (e) {
        /* ignore */
      }
    }
    box.remove();
    applyAccount(acct);
    await doLogin();
    await doOtp();
  });

  box.appendChild(label);
  const rowTop = document.createElement("div");
  rowTop.appendChild(sel);
  rowTop.appendChild(btn);
  box.appendChild(rowTop);
  box.appendChild(remember);
  document.body.appendChild(box);
}

// Kite shows the login + 2FA forms at the site root ("/"); once authenticated it
// redirects to an app route. If we're not at the login URL, we're already logged
// in — do nothing (and never risk typing into a dashboard field).
const atLoginUrl = () => {
  const p = location.pathname;
  return p === "/" || p === "" || p.startsWith("/connect");
};

async function run() {
  const { list, selectedUser } = await loadState();
  if (!list.length) {
    console.warn("[zerodha-login] No accounts configured — click the Z icon to add one.");
    return;
  }
  if (!atLoginUrl()) return; // already logged in → no action

  const acct = resolveAccount(list, selectedUser);
  if (acct) {
    applyAccount(acct);
    await doLogin();
    await doOtp();
    return;
  }
  // Couldn't tell which account → let the user pick from the available list.
  showPicker(list);
}

run();
