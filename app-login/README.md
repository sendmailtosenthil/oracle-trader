# Oracle Web Login — Chrome extension

One click on the toolbar icon opens the Project Oracle web app and logs you in
automatically (Streamlit login: username + password). Only runs on the Oracle
app host.

## Configure (once)
1. Copy `config.js.example` to `config.js` and set your password:
   ```js
   self.ORACLE_CREDS = { user: "senthil", pass: "YOUR_PASSWORD" };
   ```
   `config.js` is **gitignored** — your real password never gets committed
   (this repo is public). `content.js` holds only the login logic, no secrets.
2. If your app host/port ever changes, update the `129.159.236.137:8501` URL in
   both [manifest.json](manifest.json) (`host_permissions` + `content_scripts.matches`)
   and [background.js](background.js) (`APP_URL`).

## Install in Chrome
1. Open `chrome://extensions`
2. Toggle **Developer mode** on (top-right)
3. **Load unpacked** → select this `app-login/` folder
4. After any edit, click the **↻ reload** icon on the card.

## Use
Click the toolbar icon → a tab opens to the Oracle app and logs in. If a tab is
already open it's focused and reloaded so the login re-runs.

## Notes
- Streamlit login state is per browser session, so it re-logs in on each reload —
  that's expected; this extension just automates it.
- If the auto-fill ever stops working after a Streamlit upgrade, the input/button
  selectors in `content.js` may need adjusting (it targets `input[type=password]`,
  `input[aria-label="Username"]`, and the button labelled `Login`).
