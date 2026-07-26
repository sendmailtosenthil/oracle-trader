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

## Configure

### 1. Your accounts (in `config.js`)
Copy `config.js.example` to `config.js` and list every account once:
```js
self.KITE_ACCOUNTS = [
  { user: "PC8006", pass: "...", secret: "..." },
  { user: "ZD2461", pass: "...", secret: "..." },
  { user: "LEB470", pass: "...", secret: "..." },
];
```
`config.js` is **gitignored** (this repo is public). This is the only place
accounts are defined — the extension has no add-an-account form.

### 2. The Oracle server (in `config.js`)
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

## One account per browser
Each browser profile is **attached** to one of the configured accounts, and the
choice lives in `chrome.storage.local` — per profile — so three browsers each log
into a different account off the same `config.js`.

- **Attached** (the normal state): clicking the **Z** icon opens Kite, logs in
  and syncs the token. No popup, nothing to confirm.
- **Not attached yet**: clicking **Z** opens a short list of the accounts in
  `config.js`. Pick one — that attaches this browser and starts the login
  immediately. You only ever see this once per browser.
- **A single configured account** counts as attached automatically; there is
  nothing to choose.
- **To switch a browser to another account**: right-click the **Z** icon →
  *Pick a different account for this browser…*, then click the icon again to get
  the list.

When the Kite login page loads by any other route, the content script still
resolves the account by, in order: this browser's attached account; the User ID
already prefilled on the form; an account id shown on the page (e.g. the 2FA
screen); or the only configured account. If it genuinely can't tell, it shows a
small picker on the Kite page itself.

## Install in Chrome
1. `chrome://extensions` → **Developer mode** on → **Load unpacked** → select
   this `zerodha-login/` folder.
2. After any edit, click the **↻ reload** icon on the card.

> `config.js` must exist for the content script to load — copy
> `config.js.example` to `config.js` before loading the extension.

## Use
Click the **Z** icon. That's the whole flow: Kite opens, this browser's account
logs in (User ID → password → TOTP), and the fresh enctoken is pushed to Oracle.
Opening Kite directly works too — the auto-login runs on the page regardless of
how you got there.

## Syncing the enctoken to Oracle
No clicking needed. The service worker watches for Kite issuing a new
`enctoken` cookie — after an auto-login, after a login you typed yourself, after
any re-login — and `POST`s it with the user id to `/api/enctoken`. The same
token is never sent twice.

If a sync fails, a red **!** badge sits on the toolbar icon until the next one
succeeds. The account-picker popup also carries an **Oracle server sync** panel
with the last result (`✓ Synced PC8006`, or the error) and a **Sync enctoken
now** button to force a resend.

Which account gets updated on the server is decided by Kite's own `user_id`
cookie, falling back to this browser's selected account — so a browser logged
into ZD2461 updates ZD2461's row, not the master's.

> The `enctoken` cookie is HttpOnly, so it is read via the `chrome.cookies` API
> in the service worker; a content script cannot see it.

## Security note
Kite passwords **and** TOTP secrets are as sensitive as your password — anyone
with them can log in as you and generate your OTPs. They stay local, in the
gitignored `config.js`; only the *choice* of account is kept in
`chrome.storage.local`.

The `ORACLE_API` Basic-auth password and the enctoken are sent to your server on
every sync. Over a plain `http://` base they travel in cleartext — put the API
behind HTTPS if it's reachable from anywhere untrusted.

## If auto-fill breaks
Kite occasionally changes its login markup. The selectors to adjust are in
`content.js`: `#userid`, `#password`, `button[type="submit"]`, and the OTP field
(`#pin` / `.twofa-form input` / a numeric `maxlength=6` field).
