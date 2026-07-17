# Zerodha Kite Auto-Login — Chrome extension

Opens `kite.zerodha.com` and completes the full login: User ID → Password →
time-based OTP (TOTP). Only runs on `kite.zerodha.com`. Supports **multiple
accounts**, so you can run the same extension in several browsers/profiles and
have each log into a different Zerodha account.

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

## Security note
Kite passwords **and** TOTP secrets are as sensitive as your password — anyone
with them can log in as you and generate your OTPs. They stay local: in
`chrome.storage.local` (this browser) and/or the gitignored `config.js`.

## If auto-fill breaks
Kite occasionally changes its login markup. The selectors to adjust are in
`content.js`: `#userid`, `#password`, `button[type="submit"]`, and the OTP field
(`#pin` / `.twofa-form input` / a numeric `maxlength=6` field).
