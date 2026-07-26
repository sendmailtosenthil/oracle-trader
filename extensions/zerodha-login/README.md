# Zerodha Kite Auto-Login — Chrome extension

Opens `kite.zerodha.com` and completes the full login: User ID → Password →
time-based OTP (TOTP). Only runs on `kite.zerodha.com`. Supports **multiple
accounts**, so you can run the same extension in several browsers/profiles and
have each log into a different Zerodha account.

After every login it **pushes the resulting `enctoken` + user id to your Project
Oracle server** automatically — this used to be a separate `enctoken-sync`
extension and is now built in. Since the server files the token by user id, each
browser syncing its own account keeps every account's token fresh, and PC8006
(the master) is never overwritten by another account.

## Configure — two ways

### 1. In the extension popup (recommended for multiple browsers)
Click the **Z** toolbar icon to open the popup:
- **Add / update an account** — enter User ID, Password and TOTP secret, *Save*.
- The saved account list shows which one is active for **this browser**; click
  any account to make it this browser's account.
- Everything is stored in `chrome.storage.local`, which is **per browser
  profile** — so 3 browsers each keep their own account with no file edits.

### 2. In `config.js` (optional shared source)
Copy `config.js.example` to `config.js` and list your accounts:
```js
self.KITE_ACCOUNTS = [
  { user: "PC8006", pass: "...", secret: "..." },
  { user: "ZD2461", pass: "...", secret: "..." },
];
```
`config.js` is **gitignored** (this repo is public). Accounts from `config.js`
are merged with any you add in the popup.

### 3. The Oracle server (in `config.js`)
Syncing is on by default; it just needs somewhere to send to:
```js
self.ORACLE_API = {
  base: "http://YOUR_VPS_HOST:8502", // no trailing slash
  user: "oracle",                     // == ENCTOKEN_API_USER
  pass: "YOUR_ENCTOKEN_API_PASS",     // == ENCTOKEN_API_PASS
};
```
The host must **also** be listed in `manifest.json` → `host_permissions`
(alongside the Kite entry) — Chrome blocks calls to hosts that aren't. Omit
`ORACLE_API` entirely and the extension logs you in without syncing.

> The `secret` is the base32 key shown when you enable **External TOTP** in Kite
> (the same key your authenticator app uses). The extension needs the secret,
> not a one-time code, so it can generate codes itself.

## How it picks the account per browser
When the Kite login page loads, the extension chooses the account by, in order:
1. the account you selected for this browser in the popup;
2. the User ID the browser already has prefilled on the login form;
3. an account id shown on the page (e.g. on the 2FA screen);
4. the only configured account, if there's just one.

If it still can't tell (nothing prefilled, more than one account), it shows a
small **picker** on the Kite page listing your user ids — choose one and it logs
in (and can remember your choice for this browser).

## Install in Chrome
1. `chrome://extensions` → **Developer mode** on → **Load unpacked** → select
   this `zerodha-login/` folder.
2. After any edit, click the **↻ reload** icon on the card.

> `config.js` must exist for the content script to load. If you configure only
> via the popup, copy `config.js.example` to `config.js` and leave it as an
> empty list.

## Use
Click the **Z** icon → set/confirm this browser's account → **Open Kite & log
in**. Or just open Kite directly; it auto-logs-in with this browser's saved
account.

## Syncing the enctoken to Oracle
No clicking needed. The service worker watches for Kite issuing a new
`enctoken` cookie — after an auto-login, after a login you typed yourself, after
any re-login — and `POST`s it with the user id to `/api/enctoken`. The same
token is never sent twice.

The popup's **Oracle server sync** panel shows the last result (`✓ Synced
PC8006`, or the error) and has a **Sync enctoken now** button to force a resend.
If a sync fails, a red **!** badge sits on the toolbar icon until the next one
succeeds.

Which account gets updated on the server is decided by Kite's own `user_id`
cookie, falling back to this browser's selected account — so a browser logged
into ZD2461 updates ZD2461's row, not the master's.

> The `enctoken` cookie is HttpOnly, so it is read via the `chrome.cookies` API
> in the service worker; a content script cannot see it.

## Security note
Kite passwords **and** TOTP secrets are as sensitive as your password — anyone
with them can log in as you and generate your OTPs. They stay local: in
`chrome.storage.local` (this browser) and/or the gitignored `config.js`.

The `ORACLE_API` Basic-auth password and the enctoken are sent to your server on
every sync. Over a plain `http://` base they travel in cleartext — put the API
behind HTTPS if it's reachable from anywhere untrusted.

## If auto-fill breaks
Kite occasionally changes its login markup. The selectors to adjust are in
`content.js`: `#userid`, `#password`, `button[type="submit"]`, and the OTP field
(`#pin` / `.twofa-form input` / a numeric `maxlength=6` field).
