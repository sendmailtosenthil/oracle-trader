// Auto-login for the Project Oracle Streamlit app.
//
// Credentials come from config.js (self.ORACLE_CREDS) — copy config.js.example
// to config.js and set your password there. That file is gitignored.
//
// Streamlit login is React-controlled and synced over a websocket. The key
// pitfall: filling both fields back-to-back triggers a rerun that re-renders
// the inputs before the first value is committed server-side, so the server
// ends up with an empty/partial value ("Invalid username or password") even
// though the DOM looks filled. The fix is to commit each field, WAIT for its
// rerun to settle, re-query the (possibly replaced) element, then continue.
const CREDS = self.ORACLE_CREDS || {};
const USERNAME = CREDS.user || "";
const PASSWORD = CREDS.pass || "";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setNativeValue(el, value) {
  const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
  desc.set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

// Set + commit a Streamlit text_input (it commits on Enter or blur).
function commit(el, value) {
  el.focus();
  setNativeValue(el, value);
  for (const type of ["keydown", "keyup"]) {
    el.dispatchEvent(
      new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true })
    );
  }
  el.dispatchEvent(new Event("blur", { bubbles: true }));
  el.blur();
}

const userField = () =>
  document.querySelector('input[aria-label="Username"]') || document.querySelector('input[type="text"]');
const passField = () =>
  document.querySelector('input[aria-label="Password"]') || document.querySelector('input[type="password"]');
const loginBtn = () =>
  [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Login");

async function attempt() {
  const u = userField();
  if (!u) return false;
  commit(u, USERNAME);
  await sleep(700); // let the username rerun settle

  const p = passField(); // re-query: the rerun may have replaced the element
  if (!p) return false;
  commit(p, PASSWORD);
  await sleep(700); // let the password rerun settle

  const b = loginBtn();
  if (b) b.click();
  await sleep(1500);
  return !passField(); // success once the login form is gone
}

async function run() {
  if (!USERNAME || !PASSWORD) {
    console.warn("[app-login] Set your credentials in config.js");
    return;
  }
  // The form renders after the websocket connects — wait up to ~10s for it.
  for (let i = 0; i < 40 && !passField(); i++) await sleep(250);
  if (!passField()) return; // already logged in, or no login form here

  for (let tries = 0; tries < 3; tries++) {
    if (await attempt()) return; // logged in
  }
}

run();
