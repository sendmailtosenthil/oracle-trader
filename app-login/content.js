// ============================================================================
// EDIT your password below, then reload the extension (chrome://extensions).
// Leave this file OUT of any public commit if you put your real password here.
// ============================================================================
const USERNAME = "senthil";
const PASSWORD = "YOUR_PASSWORD"; // <-- set your Oracle login password
// ============================================================================

// Streamlit renders login as React-controlled inputs synced over a websocket,
// so we must set values via the native setter + fire input/blur (which is how
// Streamlit commits a text_input to the server), then click the Login button.
function setNativeValue(el, value) {
  const proto = Object.getPrototypeOf(el);
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur", { bubbles: true }));
}

let attempts = 0;
let busy = false;

function tryLogin() {
  if (busy || attempts >= 3) return;

  const pass =
    document.querySelector('input[aria-label="Password"]') ||
    document.querySelector('input[type="password"]');
  if (!pass) return; // no login form => already logged in, or not rendered yet

  const user =
    document.querySelector('input[aria-label="Username"]') ||
    document.querySelector('input[type="text"]');
  if (!user) return;

  busy = true;
  attempts++;

  setNativeValue(user, USERNAME);
  setNativeValue(pass, PASSWORD);

  // Give the blur-triggered rerun time to commit both values server-side,
  // then click the freshly-rendered Login button.
  setTimeout(() => {
    const btn = [...document.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "Login"
    );
    if (btn) btn.click();
    // Allow a retry if we're still on the login form a moment later.
    setTimeout(() => {
      busy = false;
    }, 1500);
  }, 600);
}

// The login form appears asynchronously after the websocket connects, so watch
// the DOM and attempt as soon as the fields exist.
const observer = new MutationObserver(() => tryLogin());
observer.observe(document.documentElement, { childList: true, subtree: true });
tryLogin();

// Stop watching after 20s regardless (login done or genuinely absent).
setTimeout(() => observer.disconnect(), 20000);
