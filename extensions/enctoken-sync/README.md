# Oracle Kite enctoken Sync — Chrome extension

A one-click extension that reads the Zerodha **enctoken** and **user_id** from
your `kite.zerodha.com` browser cookies and pushes them to your Project Oracle
server via the enctoken ingest API. It only activates on `kite.zerodha.com`.

## Configure (once)
Edit the top of [popup.js](popup.js) to match your server's `.env`:

```js
const API_BASE   = "https://YOUR_VPS_HOST"; // your Oracle server, no trailing slash
const BASIC_USER = "oracle";                 // == ENCTOKEN_API_USER
const BASIC_PASS = "change-me";              // == ENCTOKEN_API_PASS
```

Then edit [manifest.json](manifest.json) `host_permissions` — replace
`https://YOUR_VPS_HOST/*` with the same host (the extension can only call hosts
listed here). Keep the `https://kite.zerodha.com/*` entry.

## Install in Chrome (local)
1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `enctoken-sync/` folder.
4. (After any edit to the files, click the **↻ reload** icon on the card.)

## Use
1. Log in to `https://kite.zerodha.com`.
2. The toolbar icon becomes clickable only on that site — click it.
3. The popup shows the detected User ID + a masked enctoken. Click **Sync to
   Oracle**. On success it shows `✓ Synced`.

## Notes
- The extension uses the `chrome.cookies` API (not `document.cookie`) so it can
  read the **HttpOnly** `enctoken` cookie.
- If `user_id` isn't present as a cookie, the server falls back to
  `ZERODHA_USER_ID` from its `.env`.
- Basic auth + the enctoken travel in the request body/headers — use an
  **HTTPS** `API_BASE` so they aren't sent in cleartext.
