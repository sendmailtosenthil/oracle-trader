// Auto-login for Zerodha Kite (https://kite.zerodha.com).
//
// Flow:
//   1. Login form: fill User ID + Password (only the fields that are empty —
//      Kite/browser may have prepopulated one or both), then click Login.
//   2. 2FA form: generate a time-based OTP from the TOTP secret and submit it.
//
// Credentials come from config.js (self.KITE_CREDS) — copy config.js.example
// to config.js and fill it in. config.js is gitignored.
const CREDS = self.KITE_CREDS || {};
const USERID = CREDS.user || "";
const PASSWORD = CREDS.pass || "";
const SECRET = CREDS.secret || "";

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

// ----- Steps ----------------------------------------------------------------
async function doLogin() {
  for (let i = 0; i < 60; i++) {
    const pass = passField();
    if (pass && isVisible(pass)) {
      const user = userField();
      if (user && !user.value) setNativeValue(user, USERID);
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

// Kite shows the login + 2FA forms at the site root ("/"); once authenticated it
// redirects to an app route (/dashboard, /holdings, ...). So if we're not at the
// login URL, we're already logged in — do nothing (and never risk typing into a
// dashboard field).
const atLoginUrl = () => {
  const p = location.pathname;
  return p === "/" || p === "" || p.startsWith("/connect");
};

async function run() {
  if (!USERID || !PASSWORD) {
    console.warn("[zerodha-login] Set your credentials in config.js");
    return;
  }
  if (!atLoginUrl()) return; // already logged in → no action
  await doLogin();
  await doOtp();
}

run();
