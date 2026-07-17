# Zerodha Kite Auto-Login — Chrome extension

One click on the **Z** toolbar icon opens `kite.zerodha.com` and completes the
full login: User ID → Password → time-based OTP (TOTP). Only runs on
`kite.zerodha.com`.

It's smart about prefilled fields — if the browser already populated the User ID
and/or Password, it leaves them and only fills what's empty, then submits. After
login it generates the 6-digit OTP from your TOTP secret and submits the 2FA
form (retrying with a fresh code if a 30-second window rolls over).

## Configure (once)
Copy `config.js.example` to `config.js` and fill it in:
```js
self.KITE_CREDS = {
  user: "PC8006",
  pass: "YOUR_KITE_PASSWORD",
  secret: "YOUR_TOTP_SECRET_BASE32", // the "external TOTP" secret you set in Kite
};
```
`config.js` is **gitignored** — your real secrets are never committed (this repo
is public). `content.js` holds only the login logic.

> The `secret` is the base32 key shown when you enable **External TOTP** in Kite
> (the same key your authenticator app uses). The extension needs the secret,
> not a one-time code, so it can generate codes itself.

## Install in Chrome
1. `chrome://extensions` → **Developer mode** on → **Load unpacked** → select
   this `zerodha-login/` folder.
2. After any edit, click the **↻ reload** icon on the card.

## Use
Click the **Z** icon → a Kite tab opens and logs you in end-to-end.

## Security note
Your Kite password **and** TOTP secret live in `config.js` on your machine. Anyone
with that file can log in as you and generate your OTPs — treat it like your
password. It stays local (gitignored) and is only used by this extension.

## If auto-fill breaks
Kite occasionally changes its login markup. The selectors to adjust are in
`content.js`: `#userid`, `#password`, `button[type="submit"]`, and the OTP field
(`#pin` / `.twofa-form input`).
