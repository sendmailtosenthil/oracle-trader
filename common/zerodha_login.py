"""Headless, unattended Zerodha Kite login → fresh ``enctoken``.

On the VPS there is no browser and no human to copy an enctoken out of a
logged-in session. This module drives a **headless Chromium** (Playwright)
through Kite's normal login form exactly the way the ``zerodha-login`` Chrome
extension does interactively — fill user id + password, submit, then answer the
TOTP 2FA challenge — and reads the ``enctoken`` cookie Kite sets on success.

It is pure browser automation: no Kite private API is called directly, so it
keeps working as long as the visible login flow does.

Credentials come from the environment (same ``.env`` the rest of the app uses):

    ZERODHA_USER_ID      Kite user id (e.g. PC8006)   [also used elsewhere]
    ZERODHA_PASSWORD     Kite login password
    ZERODHA_TOTP_SECRET  base32 TOTP secret (the "enable external 2FA app" key)

Playwright + a Chromium build must be installed on the host:

    pip install playwright && python -m playwright install chromium

The single public entry point is :func:`fetch_enctoken`, which returns the
enctoken string or raises :class:`ZerodhaLoginError`.
"""
import base64
import hashlib
import hmac
import logging
import os
import struct
import tempfile
import time

log = logging.getLogger("zerodha-login")

# Kite serves the login + 2FA forms at the site root; a successful login
# redirects to an app route (/dashboard, ...). We detect success by the
# enctoken cookie appearing rather than by URL, which is more robust.
_KITE_URL = "https://kite.zerodha.com/"


class ZerodhaLoginError(Exception):
    """Raised when the automated login could not obtain an enctoken."""


# --- TOTP (RFC 6238, HMAC-SHA1, 6 digits, 30s) -----------------------------
# Mirrors the Web-Crypto implementation in extensions/zerodha-login/content.js
# so the two stay behaviourally identical. Kept here as a few lines of stdlib
# rather than pulling in pyotp for one function.
def _totp(secret, digits=6, period=30, at=None):
    key = base64.b32decode(secret.strip().replace(" ", "").upper() + "=" * (-len(secret.strip()) % 8))
    counter = int((at if at is not None else time.time()) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def _read_credentials():
    user_id = (os.environ.get("ZERODHA_USER_ID") or "").strip()
    password = (os.environ.get("ZERODHA_PASSWORD") or "").strip()
    secret = (os.environ.get("ZERODHA_TOTP_SECRET") or "").strip()
    missing = [
        name
        for name, val in (
            ("ZERODHA_USER_ID", user_id),
            ("ZERODHA_PASSWORD", password),
            ("ZERODHA_TOTP_SECRET", secret),
        )
        if not val
    ]
    if missing:
        raise ZerodhaLoginError(f"missing credentials in environment: {', '.join(missing)}")
    return user_id, password, secret


def _enctoken_from_cookies(context):
    for c in context.cookies():
        if c.get("name") == "enctoken" and c.get("value"):
            return c["value"]
    return None


def fetch_enctoken(user_id=None, password=None, totp_secret=None, headless=True, timeout=45):
    """Log in to Kite headlessly and return a fresh ``enctoken`` string.

    Credentials default to the ``ZERODHA_*`` environment variables. ``timeout``
    bounds each individual wait (seconds). Raises :class:`ZerodhaLoginError` on
    any failure (missing creds, Playwright not installed, form not found,
    wrong password / TOTP, or no enctoken after 2FA).
    """
    if user_id is None or password is None or totp_secret is None:
        env_user, env_pass, env_secret = _read_credentials()
        user_id = user_id or env_user
        password = password or env_pass
        totp_secret = totp_secret or env_secret

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - install-time guard
        raise ZerodhaLoginError(
            "playwright is not installed — run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    ms = timeout * 1000
    with sync_playwright() as p:
        try:
            # --no-sandbox / --disable-dev-shm-usage are required on most headless
            # VPS + container hosts: the default /dev/shm is tiny (~64MB) and
            # Chromium HANGS trying to use it. --disable-dev-shm-usage routes that
            # to /tmp instead. --disable-gpu avoids a GPU probe with no display.
            log.info("launching headless Chromium...")
            browser = p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        except Exception as exc:  # noqa: BLE001 - surface a clean install hint
            raise ZerodhaLoginError(
                f"could not launch headless Chromium ({exc}); "
                "run: python -m playwright install --with-deps chromium"
            ) from exc
        context = browser.new_context()
        page = context.new_page()

        def _debug_shot(tag):
            """Save a screenshot for post-mortem when a headless run misbehaves."""
            try:
                path = os.path.join(tempfile.gettempdir(), f"zerodha_login_{tag}.png")
                page.screenshot(path=path)
                log.warning("saved debug screenshot: %s", path)
            except Exception:  # noqa: BLE001 - best-effort diagnostics only
                pass

        try:
            log.info("opening Kite login for user_id=%s", user_id)
            page.goto(_KITE_URL, wait_until="domcontentloaded", timeout=ms)
            log.info("login page loaded")

            # --- Step 1: user id + password -------------------------------
            # On the password screen #userid is a text field; on the 2FA screen
            # Kite reuses the same #userid id for a numeric maxlength=6 field.
            page.wait_for_selector("#userid", timeout=ms)
            page.fill("#userid", user_id)
            page.fill("#password", password)
            page.click('button[type="submit"]')
            log.info("submitted user id + password, waiting for 2FA prompt...")

            # --- Step 2: TOTP 2FA -----------------------------------------
            # The TOTP field is the numeric maxlength=6 input; waiting on that
            # (rather than a bare #userid) avoids racing the screen transition.
            # Kite AUTO-SUBMITS the moment the 6th digit lands, so the submit
            # click is best-effort — by the time we try, the page has usually
            # already navigated and the button is detached (not an error).
            otp_selector = "input[maxlength='6']"
            page.wait_for_selector(otp_selector, timeout=ms)

            # Try up to 2 codes in case we fill right on a 30s TOTP boundary.
            last_error = None
            for attempt in range(2):
                otp = page.query_selector(otp_selector)
                if otp is None:
                    break  # field gone → 2FA already accepted; check cookie below
                code = _totp(totp_secret)
                otp.fill("")
                otp.fill(code)
                try:
                    page.click('button[type="submit"]', timeout=2000)
                except Exception:  # noqa: BLE001 - auto-submit already navigated
                    pass
                log.info("submitted TOTP (attempt %d), waiting for enctoken...", attempt + 1)
                # Success = enctoken cookie set. Poll briefly, with a heartbeat.
                for i in range(int(timeout * 2)):
                    token = _enctoken_from_cookies(context)
                    if token:
                        log.info("login succeeded (enctoken len=%d)", len(token))
                        return token
                    if i and i % 20 == 0:
                        log.info("...still waiting for enctoken (%ds)", i // 2)
                    page.wait_for_timeout(500)
                # Still no token → if the field is gone, login failed for good;
                # if it's back (rejected code), loop for a fresh code.
                if page.query_selector(otp_selector) is None:
                    break
                last_error = "2FA code not accepted"
                page.wait_for_timeout(1000)

            token = _enctoken_from_cookies(context)
            if token:
                return token
            _debug_shot("no_token")
            raise ZerodhaLoginError(f"no enctoken after login ({last_error or 'unknown reason'})")
        except ZerodhaLoginError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise Playwright errors
            _debug_shot("error")
            raise ZerodhaLoginError(f"headless login failed: {exc}") from exc
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":  # manual smoke test: python -m common.zerodha_login
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tok = fetch_enctoken()
    print(f"enctoken ({len(tok)} chars): {tok[:12]}...")
